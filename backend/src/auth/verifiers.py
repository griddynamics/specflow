"""Signature check: OIDC (JWKS, RS256/ES256).

The verifier pins its algorithms explicitly, verifies only the signature
with PyJWT, and hands the claims to the shared ``claims.validate``. Every
failure is an ``AuthError``; PyJWT exceptions are never chained or exposed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final, Protocol, runtime_checkable

import jwt
from jwt.types import Options

from auth.claims import ClaimPolicy, Principal, validate
from auth.errors import invalid_token
from auth.jwks import JwksCache
from settings import AuthSettings

OIDC_ALGORITHMS: Final = ("RS256", "ES256")
MAX_TOKEN_BYTES: Final = 16 * 1024

# Signature only: every claim rule lives in ``claims.validate``.
_SIGNATURE_ONLY: Final[Options] = {
    "verify_signature": True,
    "verify_exp": False,
    "verify_nbf": False,
    "verify_iat": False,
    "verify_aud": False,
    "verify_iss": False,
    "verify_sub": False,
    "verify_jti": False,
}

# What PyJWT and the key objects raise for a token that cannot be parsed or checked.
_TOKEN_REJECTIONS: Final = (jwt.PyJWTError, ValueError, TypeError)


@runtime_checkable
class TokenVerifier(Protocol):
    """Turns a raw bearer token into a ``Principal`` or raises ``AuthError``."""

    async def verify(self, token: str) -> Principal: ...


class VerifierConfigError(ValueError):
    """A verifier cannot be built from the settings (no issuer)."""


class OidcJwtVerifier:
    """IdP-issued RS256/ES256 tokens, checked against the issuer JWKS."""

    def __init__(self, policy: ClaimPolicy, jwks: JwksCache) -> None:
        self._policy = policy
        self._jwks = jwks

    async def verify(self, token: str) -> Principal:
        header = _header(token, OIDC_ALGORITHMS)
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise invalid_token("kid_missing")
        key = await self._jwks.signing_key(kid)
        return validate(_decode(token, key, OIDC_ALGORITHMS), self._policy, header=header)


def build_verifier(
    auth: AuthSettings,
    *,
    jwks: JwksCache | None = None,
    audience: str | None = None,
    client_ids: Iterable[str] | None = None,
) -> TokenVerifier:
    """The OIDC verifier; ``jwks`` replaces the cache built from the settings.

    ``audience`` and ``client_ids`` override the API's (the MCP surface passes its own).
    """

    if not auth.issuer:
        raise VerifierConfigError("OIDC_ISSUER is required")
    cache = jwks or JwksCache(auth.jwks_url, issuer=auth.issuer)
    return OidcJwtVerifier(ClaimPolicy.from_settings(auth, audience=audience, client_ids=client_ids), cache)


def _header(token: object, algorithms: tuple[str, ...]) -> dict[str, Any]:
    """The unverified header, after shape and algorithm-allowlist checks."""

    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_BYTES:
        raise invalid_token("malformed")
    try:
        header: dict[str, Any] | None = jwt.get_unverified_header(token)
    except _TOKEN_REJECTIONS:
        header = None  # raised below, outside the handler: no chained PyJWT error
    if header is None:
        raise invalid_token("malformed")
    if header.get("alg") not in algorithms:
        raise invalid_token("algorithm")
    return header


def _decode(token: str, key: Any, algorithms: tuple[str, ...]) -> dict[str, Any]:
    claims: object = None
    reason = "malformed"
    try:
        claims = jwt.decode(token, key=key, algorithms=list(algorithms), options=_SIGNATURE_ONLY)
    except jwt.InvalidSignatureError:
        reason = "signature"
    except _TOKEN_REJECTIONS:
        claims = None  # raised below, outside the handler: no chained PyJWT error
    if not isinstance(claims, dict):
        raise invalid_token(reason)
    return claims
