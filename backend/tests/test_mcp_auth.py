"""The ``/mcp`` bearer guard: a test's own loopback JWKS stub and the run's IdP (FR-MCP-AUTH-01 to -06, -10; B-05)."""

from __future__ import annotations

import json
import time

import pytest

from logger.logger import INFO
from mcp_support import (
    MCP_URL,
    PRM_URL,
    TOOLS_LIST,
    build_app,
    log_text,
    mcp_token,
    oidc_env,
    rpc,
    sdk_session,
    serving,
)
from specflow_api_support import token as web_token
from specflow_auth_support import claims, retired_dev_token, rsa_key, sign

CHALLENGE = f'Bearer resource_metadata="{PRM_URL}"'
REJECTED = f'Bearer error="invalid_token", resource_metadata="{PRM_URL}"'


def error_code(response) -> str:
    return response.json()["error"]["code"]


# --- a test's own JWKS stub ---------------------------------------------------


async def test_no_token_gets_401_with_the_prm_challenge(oidc_app):
    """FR-MCP-AUTH-01: 401, the exact challenge naming the PRM URL, code invalid_token."""

    async with serving(oidc_app) as client:
        response = await rpc(client)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == CHALLENGE
    assert error_code(response) == "invalid_token"


async def test_a_valid_token_is_accepted(oidc_app, oidc_rig):
    async with serving(oidc_app) as client:
        response = await rpc(client, oidc_rig.token())

    assert response.status_code == 200


async def test_a_non_bearer_scheme_and_a_malformed_value_are_401(oidc_app):
    async with serving(oidc_app) as client:
        basic = await rpc(client, headers={"Authorization": "Basic abc"})
        garbage = await rpc(client, headers={"Authorization": "Bearer a b c"})

    assert [r.status_code for r in (basic, garbage)] == [401, 401]
    assert basic.headers["www-authenticate"] == REJECTED


async def test_an_hs256_token_and_an_unknown_kid_are_401(oidc_app, oidc_rig):
    """FR-MCP-AUTH-03, B-05: only asymmetric algorithms and a published kid verify; dev-login tokens never do."""

    shaped = {"aud": MCP_URL, "azp": "specflow-mcp"}
    hs256 = [retired_dev_token(**shaped), retired_dev_token(**shaped, iss=oidc_rig.stub.issuer)]
    unknown = sign(claims(iss=oidc_rig.stub.issuer, **shaped), oidc_rig.key, kid="nope")
    async with serving(oidc_app) as client:
        responses = [await rpc(client, token) for token in (*hs256, unknown)]

    assert [r.status_code for r in responses] == [401, 401, 401]
    assert {r.headers["www-authenticate"] for r in responses} == {REJECTED}


async def test_a_token_signed_by_another_key_is_401(oidc_app, oidc_rig):
    forged = sign(claims(iss=oidc_rig.stub.issuer, aud=MCP_URL, azp="specflow-mcp"), rsa_key(), kid="rsa")
    async with serving(oidc_app) as client:
        response = await rpc(client, forged)

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("audience", "status"),
    [
        (MCP_URL, 200),
        ([MCP_URL, "account"], 200),
        ("specflow-api", 401),
        ([MCP_URL, "specflow-api"], 401),
        ("https://other.example/mcp", 401),
        ([], 401),
    ],
)
async def test_the_audience_must_be_the_mcp_resource_and_never_the_rest_api(oidc_app, oidc_rig, audience, status):
    """FR-MCP-AUTH-04."""

    async with serving(oidc_app) as client:
        response = await rpc(client, oidc_rig.token(aud=audience))

    assert response.status_code == status


@pytest.mark.parametrize(("azp", "status"), [("specflow-mcp", 200), ("specflow-web", 401), ("other", 401)])
async def test_only_the_mcp_client_is_accepted(oidc_app, oidc_rig, azp, status):
    """FR-MCP-AUTH-05: the web client's tokens never work on /mcp."""

    async with serving(oidc_app) as client:
        response = await rpc(client, oidc_rig.token(azp=azp))

    assert response.status_code == status


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"email": "carol@other.example"}, "domain_not_allowed"),
        ({"email_verified": False}, "email_not_verified"),
        ({"email": "a@b@example.com"}, "email_invalid"),
    ],
)
async def test_the_email_policy_answers_403_without_a_challenge(oidc_app, oidc_rig, override, code):
    """FR-MCP-AUTH-06."""

    async with serving(oidc_app) as client:
        response = await rpc(client, oidc_rig.token(**override))

    assert response.status_code == 403
    assert error_code(response) == code
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize(
    "over",
    [
        {"iss": "https://other.example/realms/t"},
        {"exp": int(time.time()) - 3600},
        {"nbf": int(time.time()) + 3600},
        {"sub": None},
    ],
    ids=["wrong issuer", "expired", "not yet valid", "no subject"],
)
async def test_the_issuer_the_clock_and_the_subject_are_checked_at_the_guard(oidc_app, oidc_rig, over):
    """FR-MCP-AUTH-04: issuer equals OIDC_ISSUER; expiry, not-before and the subject claim are enforced."""

    async with serving(oidc_app) as client:
        response = await rpc(client, oidc_rig.token(**over))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == REJECTED


async def test_an_unreachable_jwks_fails_closed_with_401(oidc_app, oidc_rig):
    """Fail closed: when the IdP cannot supply its keys the answer is 401, never a 500 and never a pass."""

    oidc_rig.stub.mode = "redirect"  # the JWKS fetch refuses the redirect: no key set
    async with serving(oidc_app) as client:
        response = await rpc(client, oidc_rig.token())

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == REJECTED
    assert oidc_rig.stub.jwks_fetches >= 1


@pytest.mark.parametrize(
    "forged",
    [{"Host": "evil.example"}, {"X-Forwarded-Host": "evil.example"}, {"Forwarded": "host=evil.example"}],
    ids=["host", "x-forwarded-host", "forwarded"],
)
async def test_the_challenge_is_built_from_settings_not_from_forged_host_headers(valid_env, oidc_rig, forged):
    """S4: the PRM URL in the 401 challenge never follows the request's Host or forwarding headers."""

    app = build_app(oidc_env(valid_env, oidc_rig.stub, MCP_ALLOWED_HOSTS="evil.example"))
    async with serving(app) as client:
        response = await rpc(client, headers=forged)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == CHALLENGE


@pytest.mark.parametrize("order", ["bad-first", "good-first"])
async def test_two_authorization_headers_are_401_in_either_order(oidc_app, oidc_rig, order):
    """SEC3: more than one Authorization header is malformed, whichever comes first."""

    pair = [("authorization", "Bearer bad"), ("authorization", f"Bearer {oidc_rig.token()}")]
    if order == "good-first":
        pair.reverse()
    async with serving(oidc_app) as client:
        response = await client.post("/mcp", json=TOOLS_LIST, headers=[("content-type", "application/json"), *pair])

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == REJECTED


# --- FR-MCP-AUTH-10: nothing secret reaches logs or bodies --------------------


async def test_no_token_authorization_value_or_email_reaches_logs_or_bodies(oidc_app, oidc_rig, whoami_app, mcp_log):
    """The own-stub matrix and the run-IdP matrix: no token, header value or email in logs, bodies or headers."""

    tokens = {
        "good": oidc_rig.token(email="alice@example.com"),
        "other-domain": oidc_rig.token(email="carol@other.example"),
        "wrong-aud": oidc_rig.token(aud="specflow-api", email="bob@example.com"),
        "web-client": oidc_rig.token(azp="specflow-web", email="dave@example.com"),
        "hs256": retired_dev_token("erin@example.com"),
        "idp-good": mcp_token("frank@example.com"),
        "idp-web": web_token("grace@example.com"),
        "idp-wrong-aud": mcp_token("heidi@example.com", aud="specflow-api"),
    }
    emails = (
        "alice@example.com",
        "carol@other.example",
        "bob@example.com",
        "dave@example.com",
        "erin@example.com",
        "frank@example.com",
        "grace@example.com",
        "heidi@example.com",
    )
    seen: list[str] = []

    async def record(response) -> None:
        seen.append(response.text + json.dumps(dict(response.headers)))

    async with serving(oidc_app) as client:
        for name in ("good", "other-domain", "wrong-aud", "web-client", "hs256"):
            await record(await rpc(client, tokens[name]))
        await record(await rpc(client))
        pair = [("authorization", f"Bearer {tokens['good']}"), ("authorization", f"Bearer {tokens['hs256']}")]
        await record(await client.post("/mcp", json=TOOLS_LIST, headers=[("content-type", "application/json"), *pair]))
    async with serving(whoami_app) as client:
        for name in ("idp-good", "idp-web", "idp-wrong-aud"):
            await record(await rpc(client, tokens[name]))
        await record(await rpc(client))
    secrets_ = {
        **{f"token-{name}": token for name, token in tokens.items()},
        **{f"bearer-{name}": f"Bearer {token}" for name, token in tokens.items()},
        **{f"email-{email}": email for email in emails},
    }
    haystack = log_text(mcp_log) + "\n".join(seen)

    leaked = [name for name, value in secrets_.items() if value in haystack]
    assert leaked == []
    rejected = [r for r in mcp_log.records if getattr(r, "event", None) == "mcp.auth.rejected"]
    assert rejected
    assert {r.levelno for r in rejected} == {INFO}
    assert all(set(vars(r)) & {"token", "authorization", "email"} == set() for r in rejected)


# --- the run's IdP (valid_env): the default of every other test -------------------


async def test_an_mcp_token_is_accepted_and_the_subject_is_the_token_sub(whoami_app):
    """The subject is the ``sub`` claim, readable by a tool through the request (Principal seam)."""

    async with sdk_session(whoami_app, mcp_token("alice@example.com")) as session:
        result = await session.call_tool("whoami", {})

    assert result.content[0].text == "user:alice@example.com"


async def test_a_web_token_and_no_token_are_401_with_the_prm_challenge(whoami_app):
    """FR-MCP-AUTH-01/-05: the PRM is served and named in every 401 challenge."""

    async with serving(whoami_app) as client:
        web = await rpc(client, web_token("alice@example.com"))
        none = await rpc(client)

    assert (web.status_code, web.headers["www-authenticate"]) == (401, REJECTED)
    assert (none.status_code, none.headers["www-authenticate"]) == (401, CHALLENGE)


async def test_a_token_with_another_client_or_audience_is_401(whoami_app):
    async with serving(whoami_app) as client:
        wrong_client = await rpc(client, mcp_token(azp="specflow-web"))
        wrong_aud = await rpc(client, mcp_token(aud="specflow-api"))

    assert (wrong_client.status_code, wrong_aud.status_code) == (401, 401)


async def test_a_domain_refusal_is_403_without_a_challenge(whoami_app):
    """FR-MCP-AUTH-06: carol (other.example) is refused by policy, not asked to sign in again."""

    async with serving(whoami_app) as client:
        response = await rpc(client, mcp_token("carol@other.example"))

    assert response.status_code == 403
    assert error_code(response) == "domain_not_allowed"
    assert "www-authenticate" not in response.headers


async def test_a_token_of_the_removed_dev_login_is_401_with_the_challenge(whoami_app):
    """B-05: HS256 and the removed dev login issuer never pass, even with the MCP audience and client."""

    async with serving(whoami_app) as client:
        response = await rpc(client, retired_dev_token(aud=MCP_URL, azp="specflow-mcp"))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == REJECTED
    assert error_code(response) == "invalid_token"
