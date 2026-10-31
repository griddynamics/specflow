"""FR-MCP-AUTH-07: every claim-profile branch and the provider recipes of auth.md.

Unit tier, no network. Tokens are claim dicts handed to ``validate``; the JOSE
header is passed the way the verifiers pass it.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from auth.claims import ClaimPolicy, Principal, validate
from auth.errors import AuthError
from specflow_auth_support import AUDIENCE, CLIENT_ID, DOMAIN, ISSUER

NOW = int(time.time())


def policy(**profile: Any) -> ClaimPolicy:
    return ClaimPolicy.build(
        issuer=ISSUER,
        audience=profile.pop("audience", AUDIENCE),
        client_ids=profile.pop("client_ids", [CLIENT_ID]),
        allowed_email_domains=[DOMAIN],
        **profile,
    )


def token(**overrides: Any) -> dict[str, Any]:
    """Neutral-shaped access token: no ``typ``, only ``client_id``; ``None`` removes a key."""

    base: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "client_id": CLIENT_ID,
        "sub": "user-1",
        "email": f"alice@{DOMAIN}",
        "email_verified": True,
        "iat": NOW,
        "exp": NOW + 300,
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def accepted(claims: dict[str, Any], pol: ClaimPolicy, header: dict[str, Any] | None = None) -> Principal:
    return validate(claims, pol, now=NOW, header=header)


def refused(claims: dict[str, Any], pol: ClaimPolicy, header: dict[str, Any] | None = None) -> AuthError:
    with pytest.raises(AuthError) as raised:
        validate(claims, pol, now=NOW, header=header)
    return raised.value


# --- neutral defaults ------------------------------------------------------------


def test_defaults_accept_a_token_without_typ_and_with_only_client_id() -> None:
    principal = accepted(token(), policy())

    assert (principal.subject, principal.client_id) == ("user-1", CLIENT_ID)


def test_defaults_refuse_an_id_token_by_the_audience_rule() -> None:
    error = refused(token(aud=CLIENT_ID, azp=CLIENT_ID, typ="ID"), policy())

    assert (error.status, error.reason) == (401, "audience")


# --- subject and email claims ----------------------------------------------------


def test_subject_claim_oid_makes_oid_the_subject() -> None:
    pol = policy(subject_claim="oid")

    assert accepted(token(oid="oid-9"), pol).subject == "oid-9"
    assert refused(token(), pol).reason == "subject"  # `sub` alone is no longer enough


@pytest.mark.parametrize("value", ["", "  ", 5, ["oid-9"]])
def test_an_empty_or_non_string_custom_subject_is_refused(value: Any) -> None:
    """FR-MCP-AUTH-07: ``AUTH_SUBJECT_CLAIM=oid`` still needs a non-blank string."""

    assert refused(token(oid=value) | {"oid": value}, policy(subject_claim="oid")).reason == "subject"


def test_a_custom_email_claim_is_read() -> None:
    pol = policy(email_claim="mail")

    assert accepted(token(email=None, mail=f"bob@{DOMAIN}"), pol).email == f"bob@{DOMAIN}"
    error = refused(token(), pol)
    assert (error.status, error.code) == (403, "email_invalid")


def test_a_custom_verified_claim_must_be_true() -> None:
    pol = policy(email_verified_claim="xms_edov")

    assert accepted(token(xms_edov=True), pol).email_verified is True
    assert refused(token(xms_edov=False), pol).code == "email_not_verified"
    assert refused(token(), pol).code == "email_not_verified"  # `email_verified` is not consulted


def test_a_blank_verified_claim_means_no_check() -> None:
    pol = policy(email_verified_claim="")

    assert accepted(token(email_verified=False), pol).email == f"alice@{DOMAIN}"
    assert accepted(token(email_verified=None), pol).email_verified is False
    assert refused(token(email_verified=None), policy()).code == "email_not_verified"


@pytest.mark.parametrize("flag", ["true", 1, "True", None])
def test_the_verified_flag_must_be_the_boolean_true(flag: Any) -> None:
    """FR-MCP-AUTH-07: strict ``is True`` for the configured claim (a string is refused)."""

    claims = token(email_verified=flag) | {"email_verified": flag}

    assert refused(claims, policy()).code == "email_not_verified"


# --- client claims ---------------------------------------------------------------


def test_the_first_present_client_claim_wins_without_fall_through() -> None:
    pol = policy()

    assert accepted(token(azp=CLIENT_ID, client_id="other"), pol).client_id == CLIENT_ID
    assert refused(token(azp="unlisted", client_id=CLIENT_ID), pol).reason == "client"
    null_azp = token(client_id=CLIENT_ID) | {"azp": None}  # a null first claim does not fall through
    assert refused(null_azp, pol).reason == "client"


@pytest.mark.parametrize("value", [[CLIENT_ID], 5, True])
def test_a_non_string_client_claim_is_refused(value: Any) -> None:
    """FR-MCP-AUTH-07: the client claim must be a string in the allowlist."""

    assert refused(token(client_id=None) | {"azp": value}, policy()).reason == "client"


def test_a_token_without_any_client_claim_is_refused() -> None:
    assert refused(token(client_id=None), policy()).reason == "client"


def test_a_custom_client_claim_list_is_used() -> None:
    pol = policy(client_id_claims=["cid"])

    assert accepted(token(client_id=None, cid=CLIENT_ID), pol).client_id == CLIENT_ID
    assert refused(token(), pol).reason == "client"


# --- ignored audiences -----------------------------------------------------------


def test_the_default_ignored_audience_is_account() -> None:
    assert accepted(token(aud=["account", AUDIENCE]), policy()).subject == "user-1"


def test_custom_ignored_audiences_replace_account() -> None:
    pol = policy(ignored_audiences=["https://tenant.example.test/userinfo"])

    assert accepted(token(aud=[AUDIENCE, "https://tenant.example.test/userinfo"]), pol).subject == "user-1"
    assert refused(token(aud=["account", AUDIENCE]), pol).reason == "audience"


def test_the_audience_rule_stays_exact_with_ignored_audiences() -> None:
    pol = policy(ignored_audiences=["extra"])

    assert refused(token(aud=["extra"]), pol).reason == "audience"
    assert refused(token(aud=[AUDIENCE, AUDIENCE.upper()]), pol).reason == "audience"


# --- token type ------------------------------------------------------------------


@pytest.mark.parametrize("typ", ["at+jwt", "application/at+jwt", "AT+JWT"])
def test_rfc9068_accepts_the_jose_header_types(typ: str) -> None:
    assert accepted(token(), policy(typ_rule="rfc9068"), header={"typ": typ}).subject == "user-1"


@pytest.mark.parametrize("header", [None, {}, {"typ": "JWT"}, {"typ": None}, {"typ": 1}])
def test_rfc9068_refuses_a_missing_or_other_header_type(header: dict[str, Any] | None) -> None:
    assert refused(token(typ="at+jwt"), policy(typ_rule="rfc9068"), header=header).reason == "typ"


def test_keycloak_type_reads_the_claim_not_the_header() -> None:
    pol = policy(typ_rule="keycloak")

    assert accepted(token(typ="Bearer"), pol).subject == "user-1"
    assert refused(token(), pol, header={"typ": "at+jwt"}).reason == "typ"


def test_none_type_ignores_claim_and_header() -> None:
    assert accepted(token(typ="ID"), policy(), header={"typ": "JWT"}).subject == "user-1"


# --- provider recipes (auth.md#provider-recipes) ---------------------------------


def test_keycloak_recipe_passes_with_the_defaults_and_with_keycloak_typ() -> None:
    claims = token(azp=CLIENT_ID, client_id=None, typ="Bearer", aud=["account", AUDIENCE])

    assert accepted(claims, policy()).client_id == CLIENT_ID
    assert accepted(claims, policy(typ_rule="keycloak")).client_id == CLIENT_ID


def test_auth0_recipe_needs_the_userinfo_audience_ignored_and_the_at_jwt_header() -> None:
    userinfo = "https://tenant.example.test/userinfo"
    claims = token(azp=CLIENT_ID, client_id=None, aud=[AUDIENCE, userinfo])
    profile = policy(typ_rule="rfc9068", ignored_audiences=[userinfo])

    assert accepted(claims, profile, header={"typ": "at+jwt"}).client_id == CLIENT_ID
    assert refused(claims, policy(), header={"typ": "at+jwt"}).reason == "audience"  # defaults
    assert refused(claims, profile, header={"typ": "JWT"}).reason == "typ"


def test_okta_recipe_needs_cid_uid_and_an_empty_verified_claim() -> None:
    claims = token(sub=None, client_id=None, cid=CLIENT_ID, uid="00u1", email_verified=None)
    profile = policy(client_id_claims=["cid"], subject_claim="uid", email_verified_claim="")

    assert accepted(claims, profile).subject == "00u1"
    assert refused(claims, policy()).reason == "subject"  # defaults
    assert refused(claims, policy(subject_claim="uid")).reason == "client"
    assert refused(claims, policy(subject_claim="uid", client_id_claims=["cid"])).code == "email_not_verified"


def test_entra_recipe_uses_the_api_application_id_oid_and_xms_edov() -> None:
    app_id = "11111111-2222-3333-4444-555555555555"
    claims = token(aud=app_id, azp=CLIENT_ID, client_id=None, sub="pairwise", oid="oid-7", email_verified=None)
    claims["xms_edov"] = True
    profile = policy(audience=app_id, subject_claim="oid", email_verified_claim="xms_edov")

    principal = accepted(claims, profile)
    assert (principal.subject, principal.client_id) == ("oid-7", CLIENT_ID)
    assert refused(claims, policy(audience=app_id)).code == "email_not_verified"  # defaults: no email_verified
    assert refused(claims, policy()).reason == "audience"  # the API audience of the web app
    # `sub` differs per application, so the default subject would silently key users by the wrong id
    default_subject = accepted(claims, policy(audience=app_id, email_verified_claim=""))
    assert default_subject.subject == "pairwise" != principal.subject
