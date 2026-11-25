"""Runtime hook of stream S3: register the Linear plan destination. FOUNDATION STUB: a no-op until S3 fills it."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from api.plan_destinations import PlanDestinations


def add_linear_plan(runtime: Any, destinations: PlanDestinations, stack: AsyncExitStack) -> None:
    """S3: when ``runtime.linear`` is set, build the runner, assign it to ``runtime.linear_plan``, start its recovery,
    push its ``aclose`` on ``stack`` and ``destinations.register(...)`` it."""

    del runtime, destinations, stack
