"""FR-MCP-AUTH-07/-09/-10/-12: client-audience access tokens at the ``/mcp`` boundary (``AUTH_TOKEN_TYPE=claim``).

Sequence: the client sends a Bearer token whose ``aud`` and ``azp`` are the MCP client id; the guard verifies the
signature against the loopback JWKS stub, then the claim marker; the tool runs for the email taken from ``sub``.

    Client -> /mcp guard : Bearer (aud = azp = client id, x_token_kind = access)
    guard -> JWKS stub   : key lookup
    guard -> tool        : Principal (email from ``sub``, no ``email`` claim)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_support import (
    MCP_CLIENT,
    TOOLS_LIST,
    OidcRig,
    call,
    log_text,
    oidc_env,
    rpc,
    sdk_session,
    serving,
    structured,
    text,
    tool_app,
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


async def test_a_client_audience_access_token_lists_projects_and_leaks_nothing(
    monkeypatch, valid_env, oidc_rig, mcp_log
):
    """FR-MCP-AUTH-07/-09/-10: accepted with the marker; no extra claim value or email in logs or the result."""

    env = oidc_env(valid_env, oidc_rig.stub, **settings_over(oidc_rig))
    app, repo = tool_app(monkeypatch, valid_env, None, env=env)
    user = await repo.upsert_user(None, SimpleNamespace(subject=EMAIL, email=EMAIL, name=None))  # subject = sub
    mine = await repo.create_project(None, user.id, "Alpha")
    async with sdk_session(app, access_token(oidc_rig)) as session:
        result = await call(session, "list_projects")

    assert [i["project_id"] for i in structured(result)["items"]] == [str(mine.id)]
    haystack = log_text(mcp_log, "mcp") + text(result)
    assert [leak for leak in LEAKS if leak in haystack and leak != EMAIL] == []
    assert EMAIL not in log_text(mcp_log, "mcp")


@pytest.mark.parametrize("marker", [{"x_token_kind": None}, {"x_token_kind": "id"}, {"x_token_kind": ["access"]}])
async def test_the_id_token_shape_is_refused_and_leaks_nothing(monkeypatch, valid_env, oidc_rig, mcp_log, marker):
    """FR-MCP-AUTH-07/-09/-10: no marker, another value or a non-string value is a 401 without any claim echoed."""

    env = oidc_env(valid_env, oidc_rig.stub, **settings_over(oidc_rig))
    app, _ = tool_app(monkeypatch, valid_env, None, env=env)
    async with serving(app) as client:
        response = await rpc(client, access_token(oidc_rig, **marker), TOOLS_LIST)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith('Bearer error="invalid_token"')
    haystack = log_text(mcp_log, "mcp") + response.text + str(response.headers)
    assert [leak for leak in LEAKS if leak in haystack] == []
    rejected = [r for r in mcp_log.records if getattr(r, "event", None) == "mcp.auth.rejected"]
    assert [getattr(r, "reason", None) for r in rejected] == ["typ"]


async def test_a_web_client_token_with_the_marker_is_refused_on_mcp(monkeypatch, valid_env, oidc_rig, mcp_log):
    """FR-MCP-AUTH-09 (CA-1): the REST surface's client audience is not accepted on ``/mcp``, marker or not."""

    env = oidc_env(valid_env, oidc_rig.stub, **settings_over(oidc_rig))
    app, _ = tool_app(monkeypatch, valid_env, None, env=env)
    web = access_token(oidc_rig, aud=WEB_CLIENT, azp=WEB_CLIENT)
    async with serving(app) as client:
        response = await rpc(client, web, TOOLS_LIST)

    assert response.status_code == 401
    rejected = [r for r in mcp_log.records if getattr(r, "event", None) == "mcp.auth.rejected"]
    assert [getattr(r, "reason", None) for r in rejected] == ["audience"]


async def test_the_rest_resource_audience_is_still_refused_under_claim(monkeypatch, valid_env, oidc_rig):
    """FR-MCP-AUTH-09: the marker does not relax the audience rule itself."""

    env = oidc_env(valid_env, oidc_rig.stub, **settings_over(oidc_rig))
    app, _ = tool_app(monkeypatch, valid_env, None, env=env)
    async with serving(app) as client:
        response = await rpc(client, access_token(oidc_rig, aud="specflow-api"), TOOLS_LIST)

    assert response.status_code == 401
