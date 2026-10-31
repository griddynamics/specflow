"""The bearer guard of ``/mcp`` and the protected resource metadata (auth.md).

The guard owns every 401 and 403; the SDK is built without ``auth=`` (AA3). A verified
``Principal`` travels in ``scope["state"]`` so tool code reads it through the request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from api.mcp.http import error_response, required
from auth.bearer import bearer_token
from auth.claims import Principal
from auth.errors import UNAUTHORIZED, AuthError, invalid_token
from auth.verifiers import TokenVerifier
from logger.logger import INFO, get_logger, log_event
from settings import MCP_PATH

logger = get_logger("api.mcp.auth")

PRINCIPAL_KEY: Final = "principal"
RESOURCE_NAME: Final = "SpecFlow"


class ProtectedResourceMetadata(BaseModel):
    """RFC 9728 document, built from settings only (FR-MCP-AUTH-02)."""

    resource: str
    authorization_servers: list[str]
    bearer_methods_supported: list[str] = Field(default_factory=lambda: ["header"])
    resource_name: str = RESOURCE_NAME


def metadata_endpoint(metadata: ProtectedResourceMetadata) -> Callable[[Request], Awaitable[JSONResponse]]:
    """The GET handler for both well-known paths."""

    document = metadata.model_dump()

    async def endpoint(_: Request) -> JSONResponse:
        return JSONResponse(document)

    return endpoint


def request_principal(request: Request) -> Principal:
    """The caller verified by the guard; absent only when a tool is reached without it (a bug)."""

    principal: Principal | None = request.scope.get("state", {}).get(PRINCIPAL_KEY)
    return required(principal, "the request passed the MCP guard")


def _single_bearer(values: list[str]) -> str:
    """The token of the one ``Authorization`` header; several headers are refused (a proxy could read another)."""

    if len(values) > 1:
        raise invalid_token("malformed_header")
    return bearer_token(values[0] if values else None)


class McpGuard:
    """Checks 1 to 10 of auth.md for ``/mcp`` (any method); every other path passes through."""

    def __init__(self, app: ASGIApp, *, verifier: TokenVerifier, resource_metadata: str | None) -> None:
        self.app = app
        self.verifier = verifier
        self.resource_metadata = resource_metadata

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != MCP_PATH:
            await self.app(scope, receive, send)
            return
        values = Headers(scope=scope).getlist("authorization")
        authorization = values[0] if values else None
        try:
            principal = await self.verifier.verify(_single_bearer(values))
        except AuthError as error:
            log_event(logger, INFO, "mcp.auth.rejected", reason=error.reason, status=error.status, code=error.code)
            headers = {"WWW-Authenticate": self._challenge(authorization)} if error.status == UNAUTHORIZED else None
            await error_response(error.status, error.code, error.message, headers)(scope, receive, send)
            return
        scope.setdefault("state", {})[PRINCIPAL_KEY] = principal
        await self.app(scope, receive, send)

    def _challenge(self, authorization: str | None) -> str:
        """``Bearer`` plus ``error`` (a token was presented) and ``resource_metadata`` (not in dev mode)."""

        params = ['error="invalid_token"'] if authorization else []
        if self.resource_metadata:
            params.append(f'resource_metadata="{self.resource_metadata}"')
        return " ".join(["Bearer", ", ".join(params)]).strip()
