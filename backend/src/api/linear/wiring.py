"""Runtime hook of stream S1: build the member connections of the Linear integration. FOUNDATION STUB: a no-op until S1
fills it, so the runtime is unchanged and ``runtime.linear`` stays ``None`` (every Linear route answers 503)."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from settings import LinearSettings


def add_linear(runtime: Any, settings: LinearSettings | None, stack: AsyncExitStack) -> None:
    """S1: with ``settings`` set, build the connections (``api.linear.port.LinearConnectionsPort``), assign them to
    ``runtime.linear`` (``api.runtime.Runtime``) and push their ``aclose`` on ``stack``."""

    del runtime, settings, stack
