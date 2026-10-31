"""Response bodies of the MCP role: the JSON error envelope and the probe bodies."""

from __future__ import annotations

from pydantic import BaseModel, Field
from starlette.responses import JSONResponse


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
