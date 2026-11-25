"""Runtime hook of stream S1: build the member connections of the Linear integration. With ``LinearSettings`` the
connections are assigned to ``runtime.linear`` (``api.linear.port.LinearConnectionsPort``) and closed with the runtime;
without them ``runtime.linear`` stays ``None`` (every Linear route answers 503)."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from db.linear import connection_store
from linear.connections import LinearConnections
from logger.logger import INFO, get_logger, log_event
from settings import LinearSettings

logger = get_logger("api.linear.wiring")


def add_linear(runtime: Any, settings: LinearSettings | None, stack: AsyncExitStack) -> None:
    """With ``settings`` set, build the connections, assign them to ``runtime.linear`` and push their ``aclose``."""

    if settings is None:
        return
    log_event(logger, INFO, "api.linear.mode", mode="on")
    runtime.linear = LinearConnections(settings, store=connection_store(runtime.transaction))
    stack.push_async_callback(runtime.linear.aclose)
