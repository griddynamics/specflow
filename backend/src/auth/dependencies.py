"""FastAPI wiring: the ``current_principal`` dependency and the dev-mode warning.

The app (S7) stores one ``TokenVerifier`` at ``app.state.token_verifier``
(``VERIFIER_STATE_ATTR``) in its lifespan and maps ``AuthError`` onto the API
error envelope, copying ``AuthError.headers`` (``WWW-Authenticate: Bearer``).
"""

from __future__ import annotations

from typing import Final

from fastapi import Request

from auth.bearer import bearer_token
from auth.claims import Principal
from auth.errors import AuthError
from auth.verifiers import TokenVerifier
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("auth.dependencies")

VERIFIER_STATE_ATTR: Final = "token_verifier"


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
