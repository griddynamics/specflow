"""The account the Linear gap channel acts as: the member the project's Linear is connected by (R1), with that member's
gateway, Linear user id and workspace id. Never a token or an id in an error; ``code`` is a closed API code.

The gateway comes from the member connections (``runtime.linear``): ``async with runtime.linear.gateway(user_id) as
gateway`` is the ``LinearGateway`` of that member; the context closes its client and refreshes the token once when
Linear refuses it, so nothing here retries a 401."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

from db import integrations as integrations_db
from db.integrations import LINEAR
from linear.errors import LinearCode, LinearError
from linear.gateway import LinearGateway

UNAVAILABLE: Final = "linear_unavailable"  # no Linear OAuth app on this server
NOT_CONNECTED: Final = "linear_not_connected"  # the project has no member to act as, or they have no connection
RECONNECT: Final = "linear_reconnect_required"  # the member's connection expired or was revoked


class ActingError(Exception):
    """The project has no member to act as; ``code`` is one of the closed codes above."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Acting:
    user_id: uuid.UUID
    linear_user_id: str
    organization_id: str
    gateway: LinearGateway


def code_of(error: LinearError) -> str:
    """The closed code a refused Linear call is kept as on the target."""

    return RECONNECT if error.code is LinearCode.UNAUTHORIZED else error.code.value


@asynccontextmanager
async def acting_gateway(runtime: Any, project_id: uuid.UUID) -> AsyncIterator[Acting]:
    """The bound member's gateway, Linear user id and workspace id, for the length of the ``async with``;
    ``ActingError`` without Linear, a binding or an active connection, ``LinearError`` when the gateway is refused."""

    linear = runtime.linear
    if linear is None:
        raise ActingError(UNAVAILABLE)
    async with runtime.transaction() as session:
        bound = await integrations_db.get_integration(session, project_id, LINEAR)
    if bound is None or bound.user_id is None:
        raise ActingError(NOT_CONNECTED)
    view = await linear.get(bound.user_id)
    if view is None:
        raise ActingError(NOT_CONNECTED)
    if view.status != "active":
        raise ActingError(RECONNECT)
    async with linear.gateway(bound.user_id) as gateway:
        yield Acting(bound.user_id, view.linear_user_id, view.organization_id, gateway)
