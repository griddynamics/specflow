"""``GET /readyz`` of the MCP role: the API's ``database`` and ``graph`` checks, passed through (FR-MCP-OPS-02).

The probe sends no token. The role has no connection of its own, so it is ready when the API is reachable and both
checks of the API are ok; an unreachable API (or an answer that is not the probe body) makes both checks not ok
with the detail ``api_unreachable``.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Final

from pydantic import ValidationError
from starlette.responses import JSONResponse

from mcp_server.api_client import ApiClient, ApiResult
from mcp_server.http import CheckBody, ReadyzBody

CHECKS: Final = ("database", "graph")  # the API's checks the role passes through
UNREACHABLE_DETAIL: Final = "api_unreachable"
_OK_STATUSES: Final = (HTTPStatus.OK, HTTPStatus.SERVICE_UNAVAILABLE)  # the API's ready and not_ready answers


def _passed_through(reply: ApiResult) -> dict[str, CheckBody] | None:
    """The API's ``database``, ``graph`` checks of a probe answer, else ``None`` (no answer, or not a probe body)."""

    if reply.status not in _OK_STATUSES:
        return None
    try:
        api = ReadyzBody.model_validate(reply.body)
    except ValidationError:
        return None
    return {name: api.checks[name] for name in CHECKS} if all(name in api.checks for name in CHECKS) else None


async def check(client: ApiClient) -> ReadyzBody:
    """The probe body: the API's ``database`` and ``graph`` checks, else both not ok (``api_unreachable``)."""

    checks = _passed_through(await client.readyz())
    if checks is None:
        unreachable = CheckBody(ok=False, detail=UNREACHABLE_DETAIL, duration_ms=0)
        return ReadyzBody(status="not_ready", checks=dict.fromkeys(CHECKS, unreachable))
    return ReadyzBody(status="ready" if all(c.ok for c in checks.values()) else "not_ready", checks=checks)


async def readiness_response(client: ApiClient) -> JSONResponse:
    """``200`` when both checks are ok, else ``503``."""

    body = await check(client)
    return JSONResponse(body.model_dump(), status_code=200 if body.status == "ready" else 503)
