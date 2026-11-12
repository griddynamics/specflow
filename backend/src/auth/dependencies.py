"""FastAPI wiring: the ``current_principal`` and ``current_agent_principal`` dependencies.

The app (S7) stores one ``TokenVerifier`` at ``app.state.token_verifier``
(``VERIFIER_STATE_ATTR``) in its lifespan and maps ``AuthError`` onto the API
error envelope, copying ``AuthError.headers`` (``WWW-Authenticate: Bearer``).
The agent route group has its own verifier, with the MCP policy, at
``app.state.agent_token_verifier`` (``AGENT_VERIFIER_STATE_ATTR``): each route group
accepts the tokens of its own audience only.
"""

from __future__ import annotations

from typing import Final

from fastapi import Request

from auth.bearer import bearer_token
from auth.claims import Principal
from auth.errors import AuthError
from auth.verifiers import TokenVerifier
from logger.logger import INFO, get_logger, log_event

logger = get_logger("auth.dependencies")

VERIFIER_STATE_ATTR: Final = "token_verifier"
AGENT_VERIFIER_STATE_ATTR: Final = "agent_token_verifier"


async def _authenticate(request: Request, attr: str) -> Principal:
    verifier: TokenVerifier = getattr(request.app.state, attr)
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


async def current_principal(request: Request) -> Principal:
    """Authenticate the request with the API policy; logs ``auth.token.rejected`` on failure."""

    return await _authenticate(request, VERIFIER_STATE_ATTR)


async def current_agent_principal(request: Request) -> Principal:
    """Authenticate the request with the MCP policy (the agent routes only); logs like ``current_principal``."""

    return await _authenticate(request, AGENT_VERIFIER_STATE_ATTR)
