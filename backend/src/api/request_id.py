"""One ``request_id`` per HTTP request, on every log line the request writes.

Without this, every API line carries the process id that ``cli.main`` binds at
startup, so one request cannot be followed through the logs.
"""

from __future__ import annotations

import re
from typing import Final, override

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from logger.correlation import new_request_context

HEADER: Final = "x-request-id"
_SAFE_ID: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")


def inbound_request_id(scope: Scope) -> str | None:
    """The caller's ``X-Request-ID`` when it is short and plain text, else ``None``."""

    for name, value in scope.get("headers", ()):
        if name == HEADER.encode():
            text = value.decode("latin-1")
            return text if _SAFE_ID.fullmatch(text) else None
    return None


class RequestIdMiddleware:
    """Bind a safe inbound or a fresh ``request_id`` for each HTTP request.

    The id is bound for the whole request (``Runtime.drive_call`` copies it into
    its threads) and returned in the ``X-Request-ID`` response header. Lifespan
    events pass through, so the startup lines keep the process id.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with new_request_context(inbound_request_id(scope)) as request_id:

            async def send_with_id(message: Message) -> None:
                if message["type"] == "http.response.start":
                    MutableHeaders(scope=message)[HEADER] = request_id
                await send(message)

            await self.app(scope, receive, send_with_id)


class SpecflowApi(FastAPI):
    """``FastAPI`` with ``RequestIdMiddleware`` outside the whole stack.

    ``add_middleware`` would put it inside ``ServerErrorMiddleware``; the 500
    handler (``api.request.failed``) would then log without the request's id.
    """

    @override
    def build_middleware_stack(self) -> ASGIApp:
        return RequestIdMiddleware(super().build_middleware_stack())
