"""FR-MCP-AUTH-07/-08/-12: a web-client-audience access token on ``GET /api/v1/me`` (``AUTH_TOKEN_TYPE=claim``)."""

from __future__ import annotations

import logging
import time

import jwt
import pytest

from mcp_support import MCP_CLIENT, log_text
from specflow_api_support import DEV_KEY

ME = "/api/v1/me"
WEB = "specflow-web"
EMAIL = "alice@example.com"
EXTRA = "192.0.2.10"
CLAIM_ENV = {
    "AUTH_API_AUDIENCE": WEB,
    "AUTH_API_CLIENT_IDS": WEB,
    "AUTH_TOKEN_TYPE": "claim",
    "AUTH_TOKEN_TYPE_CLAIM": "x_token_kind",
    "AUTH_TOKEN_TYPE_VALUE": "access",
    "AUTH_EMAIL_CLAIM": "sub",
    "AUTH_EMAIL_VERIFIED_CLAIM": "",
}
NO_MARKER = [{"x_token_kind": None}, {"x_token_kind": "id"}, {"x_token_kind": ["access"]}]
NO_MARKER += [{"x_token_kind": 1}, {"x_token_kind": True}]


@pytest.fixture
def claim_env(valid_env):
    """Applied before ``harness`` builds its settings (fixture order follows the argument order)."""

    valid_env.update(CLAIM_ENV)
    return valid_env


@pytest.fixture
def oidc_claim_env(valid_env, oidc_rig):
    """The same settings in oidc mode against the loopback JWKS stub."""

    valid_env.update(
        CLAIM_ENV, AUTH_MODE="oidc", OIDC_ISSUER=oidc_rig.stub.issuer, AUTH_JWKS_URL=oidc_rig.stub.jwks_url
    )
    valid_env.pop("AUTH_DEV_SIGNING_KEY")
    return valid_env


def shaped(client: str = WEB, **over):
    """The access-token shape: aud = azp = client id, ``sub`` is the email, no email/verified/typ/name claim."""

    now = int(time.time())
    base = {
        "iss": "specflow-dev-login",
        "aud": client,
        "azp": client,
        "sub": EMAIL,
        "x_token_kind": "access",
        "extra_addr": EXTRA,
        "iat": now,
        "exp": now + 300,
    }
    return {**base, **over}


def access(client: str = WEB, **over):
    claims = shaped(client, **over)
    signed = jwt.encode({k: v for k, v in claims.items() if v is not None}, DEV_KEY, "HS256")
    return {"Authorization": f"Bearer {signed}"}


async def test_a_client_audience_access_token_is_accepted(claim_env, harness):
    """FR-MCP-AUTH-07/-12."""

    response = await harness.client.get(ME, headers=access())

    assert response.status_code == 200
    assert response.json()["email"] == EMAIL
    assert EXTRA not in response.text


@pytest.mark.parametrize("marker", NO_MARKER)
async def test_the_token_without_the_marker_is_refused_and_leaks_nothing(claim_env, harness, caplog, marker):
    """FR-MCP-AUTH-07/-12: missing, other, array and non-string marker values are a 401 that echoes no claim."""

    caplog.set_level(logging.DEBUG)

    response = await harness.client.get(ME, headers=access(**marker))

    assert response.status_code == 401
    haystack = response.text + str(response.headers) + log_text(caplog, "auth") + log_text(caplog, "api")
    assert [leak for leak in (EXTRA, EMAIL) if leak in haystack] == []


async def test_an_mcp_client_token_with_the_marker_is_refused_on_the_rest_api(claim_env, harness):
    """FR-MCP-AUTH-08 (CA-1): the MCP surface's client audience is not accepted on REST."""

    response = await harness.client.get(ME, headers=access(MCP_CLIENT))

    assert response.status_code == 401


async def test_the_oidc_mode_accepts_the_marker_and_refuses_its_absence(oidc_claim_env, oidc_rig, harness):
    """FR-MCP-AUTH-07/-12: the same over a JWKS-verified RS256 token (AUTH_MODE=oidc)."""

    def bearer(client: str = WEB, **over):
        absent = {"email": None, "email_verified": None, "typ": None, "name": None}  # ``OidcRig`` adds them otherwise
        claims = shaped(client, iss=oidc_rig.stub.issuer, **absent, **over)
        return {"Authorization": f"Bearer {oidc_rig.token(**claims)}"}

    ok = await harness.client.get(ME, headers=bearer())
    bare = await harness.client.get(ME, headers=bearer(x_token_kind=None))
    other = await harness.client.get(ME, headers=bearer(MCP_CLIENT))

    assert (ok.status_code, bare.status_code, other.status_code) == (200, 401, 401)
    assert ok.json()["email"] == EMAIL
