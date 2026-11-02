"""HTTP helpers of the MCP role: the JSON error envelope, the probe bodies and the per-request id middleware."""

from __future__ import annotations

from typing import override

from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from logger.correlation import new_request_context


class McpInvariantError(Exception):
    """An internal invariant of the MCP role does not hold (a bug or a missing role setting)."""


def required[V](value: V | None, what: str) -> V:
    """``value`` unless it is ``None``, which is a ``McpInvariantError`` naming ``what``."""

    if value is None:
        raise McpInvariantError(what)
    return value


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, str] = Field(default_factory=dict)


class ErrorBody(BaseModel):
    """The envelope of ``api.errors``: ``{"error": {code, message, details}}``."""

    error: ErrorDetail


class CheckBody(BaseModel):
    ok: bool
    detail: str
    duration_ms: int


class ReadyzBody(BaseModel):
    status: str
    checks: dict[str, CheckBody]


def error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    body = ErrorBody(error=ErrorDetail(code=code, message=message))
    return JSONResponse(body.model_dump(), status_code=status, headers=headers)


class RequestContextMiddleware:
    """Bind a fresh ``request_id`` for each HTTP request (FR-MCP-OPS-07).

    Without it every event carries the process id that ``cli.main`` binds at startup, and the audit trail of
    unrelated callers shares one id. Unlike the REST role it never adopts a caller's ``X-Request-ID``: the id
    is the server's own correlation key. Lifespan events pass through, so the startup lines keep the process id.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with new_request_context():
            await self.app(scope, receive, send)


class McpStarlette(Starlette):
    """``Starlette`` with ``RequestContextMiddleware`` outside the whole stack (``McpGuard`` and the 500 handler)."""

    @override
    def build_middleware_stack(self) -> ASGIApp:
        return RequestContextMiddleware(super().build_middleware_stack())
