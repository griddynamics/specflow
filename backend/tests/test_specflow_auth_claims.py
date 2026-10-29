"""AC-6.1: the claim matrix of the single validation path (``claims.validate``)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from auth.claims import ClaimPolicy, Principal, validate
from auth.errors import AuthError
from specflow_auth_support import (
    AUDIENCE,
    CLIENT_ID,
    DOMAIN,
    EMAIL,
    ISSUER,
    POLICY,
    auth_settings,
    claims,
)

NOW = int(time.time())


def _reject(payload: dict[str, Any]) -> AuthError:
    with pytest.raises(AuthError) as caught:
        validate(payload, POLICY, now=NOW)
    return caught.value


def test_a_valid_claim_set_yields_the_principal() -> None:
    principal = validate(claims(iat=NOW, exp=NOW + 60), POLICY, now=NOW)

    assert principal == Principal(
        subject="user-123",
        email=EMAIL,
        email_verified=True,
        name="Alice Example",
        issuer=ISSUER,
        client_id=CLIENT_ID,
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"iss": "https://evil.example/realms/x"}, "issuer"),
        ({"iss": None}, "issuer"),
        ({"aud": "specflow-mcp"}, "audience"),
        ({"aud": [AUDIENCE, "https://api.example.com/mcp"]}, "audience"),
        ({"aud": []}, "audience"),
        ({"aud": None}, "audience"),
        ({"exp": NOW - 61}, "expired"),
        ({"exp": None}, "exp_missing"),
        ({"exp": True}, "exp_missing"),
        ({"exp": "9999999999"}, "exp_missing"),
        ({"exp": float("nan")}, "exp_invalid"),
        ({"exp": float("inf")}, "exp_invalid"),
        ({"iat": float("-inf")}, "iat_invalid"),
        ({"iat": float("nan")}, "iat_invalid"),
        ({"nbf": float("nan")}, "nbf_invalid"),
        ({"iat": None}, "iat_missing"),
        ({"iat": NOW + 61}, "issued_in_future"),
        ({"nbf": NOW + 61}, "not_yet_valid"),
        ({"sub": None}, "subject"),
        ({"sub": "  "}, "subject"),
        ({"sub": 42}, "subject"),
        ({"azp": "specflow-claude-web"}, "client"),
        ({"azp": None}, "client"),
        ({"typ": "ID"}, "typ"),
        ({"typ": "Refresh"}, "typ"),
        ({"typ": None}, "typ"),
    ],
)
def test_token_validity_failures_are_401_invalid_token(overrides: dict[str, Any], reason: str) -> None:
    error = _reject(claims(**{"iat": NOW, "exp": NOW + 300, **overrides}))

    assert (error.status, error.code, error.reason) == (401, "invalid_token", reason)
    assert error.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"email_verified": False}, "email_not_verified"),
        ({"email_verified": "true"}, "email_not_verified"),
        ({"email_verified": None}, "email_not_verified"),
        ({"email": "mallory@evil.example"}, "domain_not_allowed"),
        ({"email": f"bob@sub.{DOMAIN}"}, "domain_not_allowed"),
        ({"email": f"bob@{DOMAIN}.evil.example"}, "domain_not_allowed"),
        ({"email": f"@{DOMAIN}"}, "email_invalid"),
        ({"email": None}, "email_invalid"),
        ({"email": f"a@b@{DOMAIN}"}, "email_invalid"),
    ],
)
def test_email_policy_failures_are_403(overrides: dict[str, Any], code: str) -> None:
    error = _reject(claims(iat=NOW, exp=NOW + 300, **overrides))

    assert (error.status, error.code) == (403, code)
    assert error.headers == {}


def test_the_leeway_is_60_seconds_each_way() -> None:
    accepted = claims(exp=NOW - 59, iat=NOW + 59, nbf=NOW + 59)

    assert validate(accepted, POLICY, now=NOW).subject == "user-123"


def test_token_validity_is_checked_before_the_email_policy() -> None:
    error = _reject(claims(iss="https://other", email_verified=False, iat=NOW, exp=NOW + 9))

    assert error.status == 401


def test_the_email_domain_match_is_case_insensitive() -> None:
    upper = claims(email=f"Alice@{DOMAIN.upper()}", iat=NOW, exp=NOW + 9)

    assert validate(upper, POLICY, now=NOW).email == f"Alice@{DOMAIN.upper()}"


def test_a_single_element_audience_list_is_accepted() -> None:
    assert validate(claims(aud=[AUDIENCE], iat=NOW, exp=NOW + 9), POLICY, now=NOW)


def test_the_keycloak_account_audience_is_tolerated() -> None:
    for aud in ([AUDIENCE, "account"], ["account", AUDIENCE]):
        assert validate(claims(aud=aud, iat=NOW, exp=NOW + 9), POLICY, now=NOW)


def test_account_alone_or_with_another_surface_audience_is_rejected() -> None:
    for aud in (["account"], ["account", "https://api.example.com/mcp"]):
        assert _reject(claims(aud=aud, iat=NOW, exp=NOW + 9)).reason == "audience"


def test_a_blank_name_becomes_none() -> None:
    assert validate(claims(name=" ", iat=NOW, exp=NOW + 9), POLICY, now=NOW).name is None


def test_the_policy_comes_from_auth_settings() -> None:
    settings = auth_settings(
        AUTH_API_CLIENT_IDS="specflow-web, specflow-smoke",
        AUTH_ALLOWED_EMAIL_DOMAINS=" Example.COM ,gd.example",
    )

    policy = ClaimPolicy.from_settings(settings)
    dev = ClaimPolicy.from_settings(settings, issuer="specflow-dev-login")

    assert policy.issuer == ISSUER and dev.issuer == "specflow-dev-login"
    assert policy.audience == AUDIENCE
    assert policy.client_ids == frozenset({"specflow-web", "specflow-smoke"})
    assert policy.allowed_email_domains == frozenset({"example.com", "gd.example"})
    assert policy.leeway_seconds == 60


def test_auth_error_messages_are_fixed_and_generic() -> None:
    error = _reject(claims(sub=None, iat=NOW, exp=NOW + 9))

    assert str(error) == error.message == "The access token is invalid."
    assert "user" not in repr(error)
