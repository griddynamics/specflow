"""The HTTP layer: request ids and the error envelope.

Middleware order, outermost first: request id, unhandled-error envelope. The request id is
outermost so every response carries it, a 500 included.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shop.errors import AppError

_LOG = logging.getLogger("shop.http")
_STATUS_CODES = {
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
}


def envelope(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex.encode()

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"x-request-id", request_id)]
            await send(message)

        await self.app(scope, receive, send_with_id)


class UnhandledErrorMiddleware:
    """Answer the envelope for an exception no handler took; never echo its message."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            _LOG.exception("unhandled error")
            if started:
                raise
            await envelope(500, "INTERNAL_ERROR", "Internal error.")(scope, receive, send)


async def _app_error(_request: Request, exc: Exception) -> Response:
    error = exc if isinstance(exc, AppError) else AppError()
    return envelope(error.status_code, error.code, error.public_message)


async def _http_error(_request: Request, exc: Exception) -> Response:
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    return envelope(status, _STATUS_CODES.get(status, "HTTP_ERROR"), "Request failed.")


async def _validation_error(_request: Request, _exc: Exception) -> Response:
    return envelope(422, "VALIDATION_ERROR", "Request validation failed.")


def install_http_layer(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    # `add_middleware` wraps the stack, so the middleware added last is the outermost.
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(RequestIdMiddleware)
