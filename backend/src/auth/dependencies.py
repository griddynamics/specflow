"""FastAPI wiring: the ``current_principal`` dependency and the dev-mode warning.

The app (S7) stores one ``TokenVerifier`` at ``app.state.token_verifier``
(``VERIFIER_STATE_ATTR``) in its lifespan and maps ``AuthError`` onto the API
error envelope, copying ``AuthError.headers`` (``WWW-Authenticate: Bearer``).
"""

from __future__ import annotations

from typing import Final

from fastapi import Request

from auth.claims import Principal
from auth.errors import AuthError, invalid_token
from auth.verifiers import MAX_TOKEN_BYTES, TokenVerifier
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("auth.dependencies")

VERIFIER_STATE_ATTR: Final = "token_verifier"
_TOKEN_CHARS: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.=+/~")


def bearer_token(authorization: str | None) -> str:
    """The token from an ``Authorization: Bearer <token>`` value, else 401."""

    if not authorization:
        raise invalid_token("missing_header")
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > MAX_TOKEN_BYTES or not set(token) <= _TOKEN_CHARS:
        raise invalid_token("malformed_header")
    return token


async def current_principal(request: Request) -> Principal:
    """Authenticate the request; logs ``auth.token.rejected`` on failure."""

    verifier: TokenVerifier = getattr(request.app.state, VERIFIER_STATE_ATTR)
    try:
        return await verifier.verify(bearer_token(request.headers.get("authorization")))
    except AuthError as error:
        log_event(
            logger,
            INFO,
            "auth.token.rejected",
            reason=error.reason,
            status=error.status,
            code=error.code,
        )
        raise


def log_dev_mode_warning() -> None:
    """Log the startup WARNING for ``AUTH_MODE=dev`` (the API lifespan calls it)."""

    log_event(
        logger,
        WARNING,
        "auth.mode.dev_enabled",
        "AUTH_MODE=dev: locally signed dev tokens are accepted (local only)",
        mode="dev",
    )
