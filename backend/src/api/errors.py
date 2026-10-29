"""The error envelope ``{"error": {code, message, details}}`` (SPECS section 9).

Messages are fixed sentences chosen here; validation details carry only the
field location and the error type, never the submitted value.
"""

from __future__ import annotations

from typing import Any, Final, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from auth.errors import AuthError
from logger.logger import ERROR, INFO, get_logger, log_event

logger = get_logger("api.errors")

CLIENT_CLOSED_REQUEST: Final = 499
_HTTP_CODES: Final = {404: "not_found", 405: "method_not_allowed"}


class ApiError(Exception):
    """A deliberate API failure: HTTP ``status``, public ``code`` and fixed ``message``."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}


class ApiInvariantError(RuntimeError):
    """An internal invariant of the API does not hold (a bug or a setting the role needs is missing)."""


def required[V](value: V | None, what: str) -> V:
    """``value`` unless it is ``None``, which is an ``ApiInvariantError`` naming ``what``."""

    if value is None:
        raise ApiInvariantError(what)
    return value


def project_not_found() -> ApiError:
    return ApiError(404, "project_not_found", "The project does not exist.")


def document_not_found() -> ApiError:
    return ApiError(404, "document_not_found", "The document does not exist.")


def source_not_found() -> ApiError:
    """The one code that means "this source is gone" (INT-SRC-0003, FR-SRC-0019.AC3)."""

    return ApiError(404, "source_not_found", "The source does not exist.")


def batch_ended() -> ApiError:
    return ApiError(409, "batch_ended", "The upload batch is closed.")


def removal_unavailable() -> ApiError:
    return ApiError(503, "removal_unavailable", "The source could not be removed. Try again.")


def database_unavailable() -> ApiError:
    return ApiError(503, "database_unavailable", "The database is unavailable. Try again later.")


def model_not_allowed() -> ApiError:
    return ApiError(422, "model_not_allowed", "The requested model is not allowed.")


def envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _respond(
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None,
    headers: dict[str, str],
) -> JSONResponse:
    return JSONResponse(envelope(code, message, details), status_code=status, headers=headers)


async def _api_error(_: Request, exc: Exception) -> JSONResponse:
    error = cast(ApiError, exc)
    return _respond(error.status, error.code, error.message, error.details, error.headers)


async def _auth_error(_: Request, exc: Exception) -> JSONResponse:
    error = cast(AuthError, exc)
    return _respond(error.status, error.code, error.message, None, error.headers)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    error = cast(RequestValidationError, exc)
    fields = [{"loc": [str(part) for part in item["loc"]], "type": item["type"]} for item in error.errors()]
    return _respond(422, "validation_failed", "The request is invalid.", {"errors": fields}, {})


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    error = cast(StarletteHTTPException, exc)
    code = _HTTP_CODES.get(error.status_code, "http_error")
    headers = {str(k): str(v) for k, v in (error.headers or {}).items()}
    return _respond(error.status_code, code, "The request cannot be served.", None, headers)


async def _disconnect(request: Request, _: Exception) -> Response:
    log_event(logger, INFO, "api.request.disconnected", path=request.url.path)
    return Response(status_code=CLIENT_CLOSED_REQUEST)


async def _unhandled(request: Request, error: Exception) -> JSONResponse:
    log_event(
        logger,
        ERROR,
        "api.request.failed",
        method=request.method,
        path=request.url.path,
        error_type=type(error).__name__,
        exc_info=True,
    )
    return _respond(500, "internal_error", "An internal error occurred.", None, {})


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(AuthError, _auth_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(ClientDisconnect, _disconnect)
    app.add_exception_handler(Exception, _unhandled)
