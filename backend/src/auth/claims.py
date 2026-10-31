"""The single claim-validation path shared by every token verifier.

A verifier only checks the signature; ``validate`` then applies the spec's
claim rules (``auth-architecture.md`` "Required claims", SPECS §10). Token
validity failures are 401 ``invalid_token``; a valid token whose email is not
verified or not in an allowed domain is 403.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, TypedDict, Unpack, get_args

from auth.errors import (
    DOMAIN_NOT_ALLOWED,
    EMAIL_INVALID,
    EMAIL_NOT_VERIFIED,
    AuthError,
    forbidden,
    invalid_token,
)
from settings import (
    DEFAULT_CLIENT_ID_CLAIMS,
    DEFAULT_EMAIL_CLAIM,
    DEFAULT_EMAIL_VERIFIED_CLAIM,
    DEFAULT_IGNORED_AUDIENCES,
    DEFAULT_SUBJECT_CLAIM,
    AuthSettings,
    ConfigurationError,
    TokenType,
    reserved_marker_claims,
)

LEEWAY_SECONDS: Final = 60
BEARER_TYP: Final = "Bearer"
# RFC 9068 JOSE header ``typ`` values (``AUTH_TOKEN_TYPE=rfc9068``).
TOKEN_TYPES: Final = frozenset(get_args(TokenType))
AT_JWT_TYPES: Final = frozenset({"at+jwt", "application/at+jwt"})


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, built only from validated claims."""

    subject: str
    email: str
    email_verified: bool
    name: str | None
    issuer: str
    client_id: str


class ClaimProfile(TypedDict, total=False):
    """The optional claim-profile arguments of ``ClaimPolicy.build``."""

    subject_claim: str
    client_id_claims: Iterable[str]
    email_claim: str
    email_verified_claim: str
    typ_rule: TokenType
    typ_claim: str
    typ_value: str
    ignored_audiences: Iterable[str]


@dataclass(frozen=True, slots=True)
class ClaimPolicy:
    """What a token must claim to be accepted on the API surface."""

    issuer: str
    audience: str
    client_ids: frozenset[str]
    allowed_email_domains: frozenset[str]
    leeway_seconds: int = LEEWAY_SECONDS
    # The claim profile (FR-MCP-AUTH-07); the defaults are provider neutral.
    subject_claim: str = DEFAULT_SUBJECT_CLAIM
    client_id_claims: tuple[str, ...] = DEFAULT_CLIENT_ID_CLAIMS
    email_claim: str = DEFAULT_EMAIL_CLAIM
    email_verified_claim: str = DEFAULT_EMAIL_VERIFIED_CLAIM  # empty: the flag is not checked
    typ_rule: TokenType = "none"  # ``AUTH_TOKEN_TYPE``
    typ_claim: str = ""  # ``AUTH_TOKEN_TYPE_CLAIM``: the marker claim, for ``typ_rule="claim"``
    typ_value: str = ""  # ``AUTH_TOKEN_TYPE_VALUE``: its exact value
    ignored_audiences: frozenset[str] = frozenset(DEFAULT_IGNORED_AUDIENCES)

    @classmethod
    def build(
        cls,
        *,
        issuer: str,
        audience: str,
        client_ids: Iterable[str],
        allowed_email_domains: Iterable[str],
        **profile: Unpack[ClaimProfile],
    ) -> ClaimPolicy:
        """Normalise the allowlists (stripped; domains lower-cased) and the claim profile.

        ``profile`` holds any of the profile fields (``typ_rule`` is ``AUTH_TOKEN_TYPE``;
        ``typ_claim`` and ``typ_value`` the claim marker of ``claim``).
        Raises ``ConfigurationError`` for a blank audience, an empty client-id set, an
        unknown ``typ_rule``, or ``claim`` without ``typ_value`` and a ``typ_claim`` that is neither blank nor a
        registered, identity or configured profile claim, so an override can never silently empty a rule. Here
        ``ConfigurationError.variables`` holds the ARGUMENT names (``audience``, ``client_ids``,
        ``typ_rule``, ``typ_claim``, ``typ_value``), not environment variable names.
        """

        audience = audience.strip()
        clients = frozenset(c.strip() for c in client_ids if c.strip())
        typ_rule = profile.get("typ_rule", "none")
        typ_claim = profile.get("typ_claim", "").strip()
        typ_value = profile.get("typ_value", "").strip()
        marker = typ_rule == "claim"
        subject_claim = profile.get("subject_claim", DEFAULT_SUBJECT_CLAIM).strip()
        client_id_claims = tuple(
            dict.fromkeys(c.strip() for c in profile.get("client_id_claims", DEFAULT_CLIENT_ID_CLAIMS) if c.strip())
        )
        email_claim = profile.get("email_claim", DEFAULT_EMAIL_CLAIM).strip()
        email_verified_claim = profile.get("email_verified_claim", DEFAULT_EMAIL_VERIFIED_CLAIM).strip()
        reserved = reserved_marker_claims(subject_claim, client_id_claims, email_claim, email_verified_claim)
        bad = [
            name
            for name, ok in (
                ("audience", bool(audience)),
                ("client_ids", bool(clients)),
                ("typ_rule", typ_rule in TOKEN_TYPES),
                ("typ_claim", (bool(typ_claim) and typ_claim not in reserved) or not marker),
                ("typ_value", bool(typ_value) or not marker),
            )
            if not ok
        ]
        if bad:
            raise ConfigurationError(tuple(bad))
        return cls(
            issuer=issuer,
            audience=audience,
            client_ids=clients,
            allowed_email_domains=frozenset(d.strip().lower() for d in allowed_email_domains if d.strip()),
            subject_claim=subject_claim,
            client_id_claims=client_id_claims,
            email_claim=email_claim,
            email_verified_claim=email_verified_claim,
            typ_rule=typ_rule,
            typ_claim=typ_claim,
            typ_value=typ_value,
            ignored_audiences=frozenset(
                a.strip() for a in profile.get("ignored_audiences", DEFAULT_IGNORED_AUDIENCES) if a.strip()
            ),
        )

    @classmethod
    def from_settings(
        cls,
        auth: AuthSettings,
        *,
        issuer: str | None = None,
        audience: str | None = None,
        client_ids: Iterable[str] | None = None,
    ) -> ClaimPolicy:
        """The policy from ``AuthSettings`` (the API's unless overridden).

        ``issuer`` overrides ``OIDC_ISSUER``; ``audience`` and ``client_ids``
        override ``AUTH_API_AUDIENCE`` and ``AUTH_API_CLIENT_IDS`` (the MCP surface).
        """

        resolved = auth.issuer if issuer is None else issuer
        if not resolved:
            raise ConfigurationError(("OIDC_ISSUER",))
        return cls.build(
            issuer=resolved,
            audience=auth.api_audience if audience is None else audience,
            client_ids=auth.api_client_ids if client_ids is None else client_ids,
            allowed_email_domains=auth.allowed_email_domains,
            subject_claim=auth.subject_claim,
            client_id_claims=auth.client_id_claims,
            email_claim=auth.email_claim,
            email_verified_claim=auth.email_verified_claim,
            typ_rule=auth.token_type,
            typ_claim=auth.typ_claim or "",
            typ_value=auth.typ_value or "",
            ignored_audiences=auth.ignored_audiences,
        )


def validate(
    claims: Mapping[str, Any],
    policy: ClaimPolicy,
    *,
    now: float | None = None,
    header: Mapping[str, Any] | None = None,
) -> Principal:
    """Return the ``Principal`` or raise ``AuthError`` (401 token, 403 email).

    ``header`` is the signature-covered JOSE header, read only for
    ``AUTH_TOKEN_TYPE=rfc9068`` (FR-MCP-AUTH-07).
    """

    current = time.time() if now is None else now
    leeway = policy.leeway_seconds
    if claims.get("iss") != policy.issuer:
        raise invalid_token("issuer")
    audiences = _audiences(claims.get("aud"))
    if audiences is None or [a for a in audiences if a not in policy.ignored_audiences] != [policy.audience]:
        raise invalid_token("audience")
    exp = _number(claims.get("exp"), "exp")
    if current > exp + leeway:
        raise invalid_token("expired")
    iat = _number(claims.get("iat"), "iat")
    if iat > current + leeway:
        raise invalid_token("issued_in_future")
    if "nbf" in claims and _number(claims["nbf"], "nbf") > current + leeway:
        raise invalid_token("not_yet_valid")
    subject = claims.get(policy.subject_claim)
    if not isinstance(subject, str) or not subject.strip():
        raise invalid_token("subject")
    client_id = _client_id(claims, policy)
    if not _token_type_ok(claims, policy, header):
        raise invalid_token("typ")
    return Principal(
        subject=subject,
        email=_checked_email(claims, policy),
        # True only when the policy checked the flag; nothing reads or stores it today.
        email_verified=bool(policy.email_verified_claim),
        name=_optional_text(claims.get("name")),
        issuer=policy.issuer,
        client_id=client_id,
    )


def _client_id(claims: Mapping[str, Any], policy: ClaimPolicy) -> str:
    """The value of the FIRST client-id claim present (no fall-through), which must be allowed."""

    name = next((n for n in policy.client_id_claims if n in claims), None)
    value = claims.get(name) if name is not None else None
    if not isinstance(value, str) or value not in policy.client_ids:
        raise invalid_token("client")
    return value


def _token_type_ok(claims: Mapping[str, Any], policy: ClaimPolicy, header: Mapping[str, Any] | None) -> bool:
    """``AUTH_TOKEN_TYPE``: ``none`` skips; ``keycloak`` the claim ``typ``; ``rfc9068`` the JOSE header ``typ``;
    ``claim`` the configured claim equal to the configured string."""

    if policy.typ_rule == "keycloak":
        return claims.get("typ") == BEARER_TYP
    if policy.typ_rule == "rfc9068":
        typ = header.get("typ") if header is not None else None
        return isinstance(typ, str) and typ.lower() in AT_JWT_TYPES  # media type: case-insensitive
    if policy.typ_rule == "claim":
        value = claims.get(policy.typ_claim) if policy.typ_claim and policy.typ_value else None
        return isinstance(value, str) and value == policy.typ_value  # exact, case-sensitive
    return policy.typ_rule == "none"  # only an explicit ``none`` skips; an unknown rule fails closed


def _audiences(value: object) -> list[str] | None:
    """``aud`` as a list of strings; ``None`` when absent or malformed."""

    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _number(value: object, reason: str) -> float:
    """A required, finite NumericDate; bools, NaN and infinities are refused."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid_token(f"{reason}_missing")
    if not math.isfinite(value):
        raise invalid_token(f"{reason}_invalid")
    return float(value)


def _checked_email(claims: Mapping[str, Any], policy: ClaimPolicy) -> str:
    if policy.email_verified_claim and claims.get(policy.email_verified_claim) is not True:
        raise forbidden(EMAIL_NOT_VERIFIED, "The account email is not verified.")
    email = claims.get(policy.email_claim)
    if not isinstance(email, str) or email.count("@") != 1 or email.startswith("@"):
        raise forbidden(EMAIL_INVALID, "The account email is missing or invalid.")
    if email.split("@")[1].lower() not in policy.allowed_email_domains:
        raise forbidden(DOMAIN_NOT_ALLOWED, "The account email domain is not allowed.")
    return email


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


__all__ = ["LEEWAY_SECONDS", "AuthError", "ClaimPolicy", "ClaimProfile", "Principal", "validate"]
