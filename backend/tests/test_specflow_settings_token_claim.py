"""FR-MCP-AUTH-07/-09/-12: ``AUTH_TOKEN_TYPE=claim`` settings and the audience guards it relaxes."""

from __future__ import annotations

import pytest

from mcp_support import mcp_env
from settings import ConfigurationError, load_settings

CLAIM = {"AUTH_TOKEN_TYPE": "claim", "AUTH_TOKEN_TYPE_CLAIM": "x_token_kind", "AUTH_TOKEN_TYPE_VALUE": "access"}


def refused(role: str, env: dict[str, str]) -> tuple[str, ...]:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(role, env)
    return raised.value.variables


def test_claim_mode_reads_its_claim_and_value(valid_env):
    """FR-MCP-AUTH-07."""

    auth = load_settings("api", {**valid_env, **CLAIM}).auth

    assert (auth.token_type, auth.typ_claim, auth.typ_value) == ("claim", "x_token_kind", "access")


@pytest.mark.parametrize("missing", ["AUTH_TOKEN_TYPE_CLAIM", "AUTH_TOKEN_TYPE_VALUE"])
@pytest.mark.parametrize("blank", [None, "", "  "])
def test_claim_mode_needs_both_names(missing, blank, valid_env):
    """FR-MCP-AUTH-07: blank counts as unset."""

    env = {**valid_env, **CLAIM}
    if blank is None:
        del env[missing]
    else:
        env[missing] = blank

    assert refused("api", env) == (missing,)


@pytest.mark.parametrize("mode", ["none", "keycloak", "rfc9068"])
@pytest.mark.parametrize("name", ["AUTH_TOKEN_TYPE_CLAIM", "AUTH_TOKEN_TYPE_VALUE"])
def test_the_marker_is_refused_under_another_mode(mode, name, valid_env):
    """FR-MCP-AUTH-07: the refusal names the variable."""

    env = {**valid_env, "AUTH_TOKEN_TYPE": mode, name: "x"}

    assert refused("api", env) == (name,)


@pytest.mark.parametrize("mode", ["none", "keycloak", "rfc9068"])
def test_a_blank_marker_under_another_mode_is_unset(mode, valid_env):
    """FR-MCP-AUTH-07."""

    env = {**valid_env, "AUTH_TOKEN_TYPE": mode, "AUTH_TOKEN_TYPE_CLAIM": " ", "AUTH_TOKEN_TYPE_VALUE": ""}

    assert load_settings("api", env).auth.typ_claim is None


# --- MCP_AUDIENCE ------------------------------------------------------------


def _mcp(valid_env, mode: str, **over: str) -> dict[str, str]:
    marker = CLAIM if mode == "claim" else {"AUTH_TOKEN_TYPE": mode}
    return mcp_env(valid_env, **{"MCP_AUDIENCE": "specflow-mcp", **marker, **over})


def test_the_mcp_audience_equal_to_a_client_id_is_refused_under_none(valid_env):
    """FR-MCP-AUTH-09."""

    assert refused("mcp", _mcp(valid_env, "none")) == ("MCP_AUDIENCE",)


@pytest.mark.parametrize("mode", ["claim", "keycloak", "rfc9068"])
def test_the_mcp_audience_equal_to_a_client_id_starts_under_a_type_check(mode, valid_env):
    """FR-MCP-AUTH-09."""

    assert load_settings("mcp", _mcp(valid_env, mode)).mcp.audience == "specflow-mcp"


def test_the_other_mcp_refusals_hold_under_claim(valid_env):
    """FR-MCP-AUTH-09: the REST audience and ignored audiences stay refused."""

    assert refused("mcp", _mcp(valid_env, "claim", MCP_AUDIENCE="specflow-api")) == ("MCP_AUDIENCE",)
    assert refused("mcp", _mcp(valid_env, "claim", AUTH_IGNORED_AUDIENCES="specflow-mcp")) == (
        "AUTH_IGNORED_AUDIENCES",
    )
    assert refused("mcp", _mcp(valid_env, "claim", AUTH_IGNORED_AUDIENCES="specflow-api")) == (
        "AUTH_IGNORED_AUDIENCES",
    )


# --- AUTH_API_AUDIENCE -------------------------------------------------------


def _api(valid_env, mode: str, **over: str) -> dict[str, str]:
    marker = CLAIM if mode == "claim" else {"AUTH_TOKEN_TYPE": mode}
    return {**valid_env, "AUTH_API_AUDIENCE": "specflow-web", **marker, **over}


def test_the_api_audience_equal_to_a_client_id_is_refused_under_none(valid_env):
    """FR-MCP-AUTH-12."""

    assert refused("api", _api(valid_env, "none")) == ("AUTH_API_AUDIENCE",)


@pytest.mark.parametrize("mode", ["claim", "keycloak", "rfc9068"])
def test_the_api_audience_equal_to_a_client_id_starts_under_a_type_check(mode, valid_env):
    """FR-MCP-AUTH-12."""

    assert load_settings("api", _api(valid_env, mode)).auth.api_audience == "specflow-web"


def test_the_ignored_audience_refusal_holds_for_the_api_under_claim(valid_env):
    """FR-MCP-AUTH-12."""

    env = _api(valid_env, "claim", AUTH_IGNORED_AUDIENCES="specflow-web")

    assert refused("api", env) == ("AUTH_IGNORED_AUDIENCES",)


# --- the marker claim must tell the two token kinds apart (CA-2) ----------------------------


@pytest.mark.parametrize(
    "name",
    [
        *("iss", "sub", "aud", "exp", "nbf", "iat", "jti", "azp", "client_id", "email", "email_verified"),
        *("acr", "amr", "sid", "auth_time", "nonce", "at_hash", "c_hash", "s_hash"),
    ],
)
def test_a_registered_or_identity_claim_is_refused_as_the_marker(name, valid_env):
    """FR-MCP-AUTH-07: it would be present in an ID token too, so the check would be vacuous."""

    env = {**valid_env, **CLAIM, "AUTH_TOKEN_TYPE_CLAIM": name}

    assert refused("api", env) == ("AUTH_TOKEN_TYPE_CLAIM",)


@pytest.mark.parametrize(
    ("name", "override"),
    [
        ("oid", {"AUTH_SUBJECT_CLAIM": "oid"}),
        ("cid", {"AUTH_CLIENT_ID_CLAIM": "azp, cid"}),
        ("mail", {"AUTH_EMAIL_CLAIM": "mail"}),
        ("verified", {"AUTH_EMAIL_VERIFIED_CLAIM": "verified"}),
    ],
)
def test_a_configured_profile_claim_is_refused_as_the_marker(name, override, valid_env):
    """FR-MCP-AUTH-07."""

    env = {**valid_env, **CLAIM, "AUTH_TOKEN_TYPE_CLAIM": name, **override}

    assert refused("api", env) == ("AUTH_TOKEN_TYPE_CLAIM",)


def test_typ_stays_allowed_as_the_marker(valid_env):
    """FR-MCP-AUTH-07: ``typ`` = ``Bearer`` is the ``keycloak`` rule spelled as a claim."""

    env = {**valid_env, **CLAIM, "AUTH_TOKEN_TYPE_CLAIM": "typ", "AUTH_TOKEN_TYPE_VALUE": "Bearer"}

    assert load_settings("api", env).auth.typ_claim == "typ"


def test_the_marker_is_stripped_like_the_policy_sees_it(valid_env):
    """FR-MCP-AUTH-07 (CA-6): settings and the policy agree on the exact value."""

    env = {**valid_env, **CLAIM, "AUTH_TOKEN_TYPE_CLAIM": " x_token_kind ", "AUTH_TOKEN_TYPE_VALUE": " access "}

    auth = load_settings("api", env).auth

    assert (auth.typ_claim, auth.typ_value) == ("x_token_kind", "access")


# --- each surface needs its own client when audiences are client ids (CA-1) -------------------


@pytest.mark.parametrize("mode", ["none", "claim", "keycloak", "rfc9068"])
def test_the_mcp_audience_equal_to_a_rest_client_is_refused_in_every_mode(mode, valid_env):
    """FR-MCP-AUTH-09: a web-client token would pass on ``/mcp``."""

    env = _mcp(
        valid_env, mode, MCP_AUDIENCE="specflow-web", MCP_CLIENT_IDS="specflow-mcp", AUTH_API_CLIENT_IDS="specflow-web"
    )

    assert refused("mcp", env) == ("MCP_AUDIENCE",)


@pytest.mark.parametrize("mode", ["claim", "keycloak", "rfc9068"])
def test_a_client_shared_by_both_surfaces_is_refused_naming_the_mcp_client_ids(mode, valid_env):
    """FR-MCP-AUTH-09: with a client audience, ``MCP_CLIENT_IDS`` must not hold a REST client."""

    env = _mcp(valid_env, mode, MCP_CLIENT_IDS="specflow-mcp,specflow-web")

    assert refused("mcp", env) == ("MCP_CLIENT_IDS",)


def test_distinct_clients_start_under_claim(valid_env):
    """FR-MCP-AUTH-09."""

    env = _mcp(valid_env, "claim", MCP_CLIENT_IDS="specflow-mcp,specflow-mcp-2", AUTH_API_CLIENT_IDS="specflow-web")

    assert load_settings("mcp", env).mcp.client_ids == ("specflow-mcp", "specflow-mcp-2")


def test_a_resource_audience_may_share_its_client_with_the_rest_api(valid_env):
    """FR-MCP-AUTH-09: the shared-client refusal is for client audiences only (the audience is not a client id)."""

    env = mcp_env(valid_env, MCP_CLIENT_IDS="specflow-mcp,specflow-web")

    assert load_settings("mcp", env).mcp.audience == env["MCP_RESOURCE_URL"]
