"""Claim-profile policy construction and fail-closed edges (FR-MCP-AUTH-07)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from auth.claims import ClaimPolicy, validate
from auth.errors import AuthError
from settings import ConfigurationError
from specflow_auth_support import (
    AUDIENCE,
    CLIENT_ID,
    DOMAIN,
    ISSUER,
    auth_settings,
    claims,
)

NOW = 1_700_000_000.0


def _build(**overrides: Any) -> ClaimPolicy:
    base: dict[str, Any] = {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "client_ids": [CLIENT_ID],
        "allowed_email_domains": [DOMAIN],
    }
    return ClaimPolicy.build(**{**base, **overrides})


@pytest.mark.parametrize("audience", ["", "   "])
def test_a_blank_audience_is_refused(audience: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        _build(audience=audience)

    assert raised.value.variables == ("audience",)


@pytest.mark.parametrize("client_ids", [[], ["", "  "]])
def test_an_empty_client_id_set_is_refused(client_ids: list[str]) -> None:
    with pytest.raises(ConfigurationError) as raised:
        _build(client_ids=client_ids)

    assert raised.value.variables == ("client_ids",)


def test_every_bad_policy_argument_is_named_at_once() -> None:
    with pytest.raises(ConfigurationError) as raised:
        _build(audience="", client_ids=[], typ_rule=cast("Any", "rfc-9068"))

    assert raised.value.variables == ("audience", "client_ids", "typ_rule")


def test_the_audience_is_stripped() -> None:
    assert _build(audience=f"  {AUDIENCE} ").audience == AUDIENCE


def test_overrides_through_from_settings_cannot_be_blank() -> None:
    auth = auth_settings()

    with pytest.raises(ConfigurationError):
        ClaimPolicy.from_settings(auth, audience=" ")
    with pytest.raises(ConfigurationError):
        ClaimPolicy.from_settings(auth, client_ids=[])


def test_an_unknown_type_rule_fails_closed_even_when_built_directly() -> None:
    policy = _build()
    object.__setattr__(policy, "typ_rule", "rfc-9068")  # bypasses build, as a typo in a caller could
    token = claims(iat=NOW, exp=NOW + 300)

    with pytest.raises(AuthError) as raised:
        validate(token, policy, now=NOW, header={"typ": "at+jwt"})

    assert raised.value.reason == "typ"


def test_the_principal_reports_whether_the_verified_flag_was_checked() -> None:
    token = claims(iat=NOW, exp=NOW + 300)
    unchecked = validate(token, _build(email_verified_claim=""), now=NOW)
    checked = validate(token, _build(), now=NOW)

    assert (unchecked.email_verified, checked.email_verified) == (False, True)


@pytest.mark.parametrize(
    ("profile", "variables"),
    [
        ({}, ("typ_claim", "typ_value")),
        ({"typ_claim": "x_token_kind"}, ("typ_value",)),
        ({"typ_value": "access"}, ("typ_claim",)),
        ({"typ_claim": "  ", "typ_value": "access"}, ("typ_claim",)),
    ],
)
def test_the_claim_rule_needs_its_claim_and_value(profile: dict[str, str], variables: tuple[str, ...]) -> None:
    """FR-MCP-AUTH-07: ``claim`` without a claim name or value is refused by argument name."""

    with pytest.raises(ConfigurationError) as raised:
        _build(typ_rule="claim", **profile)

    assert raised.value.variables == variables


def test_the_claim_rule_is_built_from_settings() -> None:
    """FR-MCP-AUTH-07."""

    env = {"AUTH_TOKEN_TYPE": "claim", "AUTH_TOKEN_TYPE_CLAIM": "x_token_kind", "AUTH_TOKEN_TYPE_VALUE": "access"}
    auth = auth_settings(**env)

    policy = ClaimPolicy.from_settings(auth)

    assert (policy.typ_rule, policy.typ_claim, policy.typ_value) == ("claim", "x_token_kind", "access")


@pytest.mark.parametrize(
    "name", ["iss", "sub", "azp", "email", "acr", "amr", "sid", "auth_time", "nonce", "at_hash", "c_hash", "s_hash"]
)
def test_the_claim_rule_refuses_a_registered_or_identity_claim_as_its_marker(name: str) -> None:
    """FR-MCP-AUTH-07: ``typ_claim`` is named, as the settings guard names ``AUTH_TOKEN_TYPE_CLAIM``."""

    with pytest.raises(ConfigurationError) as raised:
        _build(typ_rule="claim", typ_claim=name, typ_value="access")

    assert raised.value.variables == ("typ_claim",)


def test_the_claim_rule_refuses_a_configured_profile_claim_and_keeps_typ() -> None:
    """FR-MCP-AUTH-07."""

    with pytest.raises(ConfigurationError) as raised:
        _build(typ_rule="claim", typ_claim="oid", typ_value="access", subject_claim="oid")

    assert raised.value.variables == ("typ_claim",)
    assert _build(typ_rule="claim", typ_claim="typ", typ_value="Bearer").typ_claim == "typ"
