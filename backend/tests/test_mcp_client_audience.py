"""FR-MCP-AUTH-07/-09/-10/-12: client-audience access tokens at the ``/mcp`` boundary (``AUTH_TOKEN_TYPE=claim``).

Sequence: the client sends a Bearer token whose ``aud`` and ``azp`` are the MCP client id; the guard verifies the
signature against the loopback JWKS stub, then the claim marker; the tool runs for the email taken from ``sub``.

    Client -> /mcp guard : Bearer (aud = azp = client id, x_token_kind = access)
    guard -> JWKS stub   : key lookup
    guard -> tool        : Principal (email from ``sub``, no ``email`` claim)
"""

from __future__ import annotations

import httpx
import pytest

from mcp_server.registry import install
from mcp_support import (
    MCP_CLIENT,
    MCP_URL,
    TOOLS_LIST,
    OidcRig,
    build_app,
    call,
    log_text,
    oidc_env,
    rpc,
    sdk_session,
    serving,
    structured,
    text,
)

EMAIL = "alice@example.com"
WEB_CLIENT = "specflow-web"  # the REST surface's client (``AUTH_API_CLIENT_IDS`` default)
# Provider-specific extras the guard must ignore and never log (synthetic marker strings).
EXTRAS = {
    "extra_addr": "192.0.2.10",
    "extra_list": ["list-marker-7f3a"],
    "extra_kind": "kind-marker-91c2",
    "extra_note": "note-marker-4d8e",
    "extra_path": ["path-marker-b6a1"],
    "jti": "jti-marker-2e57",
}
LEAKS = ["192.0.2.10", "list-marker-7f3a", "kind-marker-91c2", "note-marker-4d8e", "path-marker-b6a1", EMAIL]


def settings_over(rig: OidcRig) -> dict[str, str]:
    return {
        "MCP_AUDIENCE": MCP_CLIENT,
        "MCP_CLIENT_IDS": MCP_CLIENT,
        "AUTH_TOKEN_TYPE": "claim",
        "AUTH_TOKEN_TYPE_CLAIM": "x_token_kind",
        "AUTH_TOKEN_TYPE_VALUE": "access",
        "AUTH_EMAIL_CLAIM": "sub",
        "AUTH_EMAIL_VERIFIED_CLAIM": "",
    }


def access_token(rig: OidcRig, **over: object) -> str:
    """The provider's access-token shape: no ``email``, ``email_verified``, ``typ`` or ``name`` claim."""

    shaped = {
        "aud": MCP_CLIENT,
        "azp": MCP_CLIENT,
        "sub": EMAIL,
        "email": None,
        "email_verified": None,
        "typ": None,
        "name": None,
        "x_token_kind": "access",
        "scp": "mcp",
        "nbf": 0,
        **EXTRAS,
    }
    return rig.token(**{**shaped, **over})


def app_over(env: dict[str, str], transport: httpx.MockTransport | None = None):
    return build_app(env, transport=transport, tools=lambda server: install(server, resource_url=MCP_URL))


async def test_a_client_audience_access_token_lists_projects_and_leaks_nothing(valid_env, oidc_rig, mcp_log):
    """FR-MCP-AUTH-07/-09/-10: accepted with the marker; no extra claim value or email in logs or the result; the
    same token is what the API receives."""

    mine = "00000000-0000-4000-8000-000000000001"
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        page = {
            "items": [{"project_id": mine, "name": "Alpha", "created_at": "2026-03-03T09:30:00Z"}],
            "next_cursor": None,
        }
        return httpx.Response(200, json=page, headers={"X-SpecFlow-User-Id": mine})

    env = oidc_env(valid_env, oidc_rig.stub, **settings_over(oidc_rig))
    token = access_token(oidc_rig)
    async with sdk_session(app_over(env, httpx.MockTransport(api)), token) as session:
        result = await call(session, "list_projects")

    assert [i["project_id"] for i in structured(result)["items"]] == [mine]
    assert [r.headers["authorization"] for r in seen] == [f"Bearer {token}"]
    haystack = log_text(mcp_log, "mcp") + text(result)
    assert [leak for leak in LEAKS if leak in haystack and leak != EMAIL] == []
    assert EMAIL not in log_text(mcp_log, "mcp")


@pytest.mark.parametrize("marker", [{"x_token_kind": None}, {"x_token_kind": "id"}, {"x_token_kind": ["access"]}])
async def test_the_id_token_shape_is_refused_and_leaks_nothing(valid_env, oidc_rig, mcp_log, marker):
    """FR-MCP-AUTH-07/-09/-10: no marker, another value or a non-string value is a 401 without any claim echoed."""

    env = oidc_env(valid_env, oidc_rig.stub, **settings_over(oidc_rig))
    app = app_over(env)
    async with serving(app) as client:
        response = await rpc(client, access_token(oidc_rig, **marker), TOOLS_LIST)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith('Bearer error="invalid_token"')
    haystack = log_text(mcp_log, "mcp") + response.text + str(response.headers)
    assert [leak for leak in LEAKS if leak in haystack] == []
    rejected = [r for r in mcp_log.records if getattr(r, "event", None) == "mcp.auth.rejected"]
    assert [getattr(r, "reason", None) for r in rejected] == ["typ"]


async def test_a_web_client_token_with_the_marker_is_refused_on_mcp(valid_env, oidc_rig, mcp_log):
    """FR-MCP-AUTH-09 (CA-1): the REST surface's client audience is not accepted on ``/mcp``, marker or not."""

    env = oidc_env(valid_env, oidc_rig.stub, **settings_over(oidc_rig))
    app = app_over(env)
    web = access_token(oidc_rig, aud=WEB_CLIENT, azp=WEB_CLIENT)
    async with serving(app) as client:
        response = await rpc(client, web, TOOLS_LIST)

    assert response.status_code == 401
    rejected = [r for r in mcp_log.records if getattr(r, "event", None) == "mcp.auth.rejected"]
    assert [getattr(r, "reason", None) for r in rejected] == ["audience"]


async def test_the_rest_resource_audience_is_still_refused_under_claim(valid_env, oidc_rig):
    """FR-MCP-AUTH-09: the marker does not relax the audience rule itself."""

    env = oidc_env(valid_env, oidc_rig.stub, **settings_over(oidc_rig))
    app = app_over(env)
    async with serving(app) as client:
        response = await rpc(client, access_token(oidc_rig, aud="specflow-api"), TOOLS_LIST)

    assert response.status_code == 401
