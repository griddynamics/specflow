"""Signature checks: OIDC (JWKS, RS256/ES256) and local dev (HS256).

Each verifier pins its algorithms explicitly, verifies only the signature
with PyJWT, and hands the claims to the shared ``claims.validate``. Every
failure is an ``AuthError``; PyJWT exceptions are never chained or exposed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final, Protocol, runtime_checkable

import jwt
from jwt.types import Options
from pydantic import SecretStr

from auth.claims import ClaimPolicy, Principal, validate
from auth.errors import invalid_token
from auth.jwks import JwksCache
from settings import DEV_SIGNING_KEY_MIN_BYTES, AuthSettings

OIDC_ALGORITHMS: Final = ("RS256", "ES256")
DEV_ALGORITHMS: Final = ("HS256",)
DEV_ISSUER: Final = "specflow-dev-login"
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
    """A verifier cannot be built from the settings (a missing key or an unknown mode)."""


class OidcJwtVerifier:
    """``AUTH_MODE=oidc``: Keycloak-issued RS256/ES256 tokens via JWKS."""

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


class DevTokenVerifier:
    """``AUTH_MODE=dev`` (local only): HS256 tokens from the dev login."""

    def __init__(self, policy: ClaimPolicy, signing_key: SecretStr | None) -> None:
        secret = signing_key.get_secret_value() if signing_key is not None else ""
        if len(secret.encode()) < DEV_SIGNING_KEY_MIN_BYTES:
            raise VerifierConfigError(f"AUTH_DEV_SIGNING_KEY must be at least {DEV_SIGNING_KEY_MIN_BYTES} bytes")
        self._policy = policy
        self._key = secret.encode()

    async def verify(self, token: str) -> Principal:
        header = _header(token, DEV_ALGORITHMS)
        return validate(_decode(token, self._key, DEV_ALGORITHMS), self._policy, header=header)


def build_verifier(
    auth: AuthSettings,
    *,
    jwks: JwksCache | None = None,
    audience: str | None = None,
    client_ids: Iterable[str] | None = None,
) -> TokenVerifier:
    """The verifier for ``AUTH_MODE``; the dev verifier exists only in dev mode.

    ``audience`` and ``client_ids`` override the API's (the MCP surface passes its own).

    ``load_settings`` guards already refuse dev mode outside local; the dev
    verifier re-checks the key length itself (AC-1.4).
    """

    if auth.mode == "dev":
        policy = ClaimPolicy.from_settings(auth, issuer=DEV_ISSUER, audience=audience, client_ids=client_ids)
        return DevTokenVerifier(policy, auth.dev_signing_key)
    if auth.mode != "oidc":
        raise VerifierConfigError("AUTH_MODE must be oidc or dev")
    if not auth.issuer:
        raise VerifierConfigError("OIDC_ISSUER is required when AUTH_MODE is oidc")
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
