"""Fixture for security.yml's request-id mint and read rules, the `ok` half.

Nested as `src/main.py`, the one module allowed to mint and read the request id, so the
rules' `paths.exclude` shields it."""

from __future__ import annotations

import uuid


async def add_request_id_header(request: object, call_next: object) -> object:
    # ok: kit.security.request-id-read-outside-middleware
    incoming = request.headers.get("X-Request-Id")
    # ok: kit.security.request-id-outside-middleware
    request_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-Id"] = incoming or request_id
    return response
