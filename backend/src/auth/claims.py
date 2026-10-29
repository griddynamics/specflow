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
from typing import Any, Final

from auth.errors import (
    DOMAIN_NOT_ALLOWED,
    EMAIL_INVALID,
    EMAIL_NOT_VERIFIED,
    AuthError,
    forbidden,
    invalid_token,
)
from settings import AuthSettings, ConfigurationError

LEEWAY_SECONDS: Final = 60
BEARER_TYP: Final = "Bearer"
# Keycloak adds its built-in account-management client to every access token's
# ``aud``. It names no API surface, so it is the only extra audience tolerated.
IGNORED_AUDIENCES: Final = frozenset({"account"})


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, built only from validated claims."""

    subject: str
    email: str
    email_verified: bool
    name: str | None
    issuer: str
    client_id: str


@dataclass(frozen=True, slots=True)
class ClaimPolicy:
    """What a token must claim to be accepted on the API surface."""

    issuer: str
    audience: str
    client_ids: frozenset[str]
    allowed_email_domains: frozenset[str]
    leeway_seconds: int = LEEWAY_SECONDS

    @classmethod
    def build(
        cls,
        *,
        issuer: str,
        audience: str,
        client_ids: Iterable[str],
        allowed_email_domains: Iterable[str],
    ) -> ClaimPolicy:
        """Normalise the allowlists (stripped; domains lower-cased)."""

        return cls(
            issuer=issuer,
            audience=audience,
            client_ids=frozenset(c.strip() for c in client_ids if c.strip()),
            allowed_email_domains=frozenset(d.strip().lower() for d in allowed_email_domains if d.strip()),
        )

    @classmethod
    def from_settings(cls, auth: AuthSettings, *, issuer: str | None = None) -> ClaimPolicy:
        """The API policy from ``AuthSettings``; ``issuer`` overrides ``OIDC_ISSUER``."""

        resolved = auth.issuer if issuer is None else issuer
        if not resolved:
            raise ConfigurationError(("OIDC_ISSUER",))
        return cls.build(
            issuer=resolved,
            audience=auth.api_audience,
            client_ids=auth.api_client_ids,
            allowed_email_domains=auth.allowed_email_domains,
        )


def validate(claims: Mapping[str, Any], policy: ClaimPolicy, *, now: float | None = None) -> Principal:
    """Return the ``Principal`` or raise ``AuthError`` (401 token, 403 email)."""

    current = time.time() if now is None else now
    leeway = policy.leeway_seconds
    if claims.get("iss") != policy.issuer:
        raise invalid_token("issuer")
    audiences = _audiences(claims.get("aud"))
    if audiences is None or [a for a in audiences if a not in IGNORED_AUDIENCES] != [policy.audience]:
        raise invalid_token("audience")
    exp = _number(claims.get("exp"), "exp")
    if current > exp + leeway:
        raise invalid_token("expired")
    iat = _number(claims.get("iat"), "iat")
    if iat > current + leeway:
        raise invalid_token("issued_in_future")
    if "nbf" in claims and _number(claims["nbf"], "nbf") > current + leeway:
        raise invalid_token("not_yet_valid")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise invalid_token("subject")
    client_id = claims.get("azp")
    if not isinstance(client_id, str) or client_id not in policy.client_ids:
        raise invalid_token("client")
    if claims.get("typ") != BEARER_TYP:
        raise invalid_token("typ")
    return Principal(
        subject=subject,
        email=_checked_email(claims, policy),
        email_verified=True,
        name=_optional_text(claims.get("name")),
        issuer=policy.issuer,
        client_id=client_id,
    )


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
    if claims.get("email_verified") is not True:
        raise forbidden(EMAIL_NOT_VERIFIED, "The account email is not verified.")
    email = claims.get("email")
    if not isinstance(email, str) or email.count("@") != 1 or email.startswith("@"):
        raise forbidden(EMAIL_INVALID, "The account email is missing or invalid.")
    if email.split("@")[1].lower() not in policy.allowed_email_domains:
        raise forbidden(DOMAIN_NOT_ALLOWED, "The account email domain is not allowed.")
    return email


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


__all__ = ["LEEWAY_SECONDS", "AuthError", "ClaimPolicy", "Principal", "validate"]
