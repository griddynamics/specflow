"""Runtime hook of stream S3: register the Linear plan destination. With ``runtime.linear`` set (stream S1's member
connections) it builds the runner, assigns it to ``runtime.linear_plan``, starts its recovery, closes it with the
runtime and registers it, so a plan run starts the export like the Jira sync and GitHub Projects do.

Contract with stream S1: ``async with runtime.linear.gateway(user_id) as gateway`` yields a ``LinearGateway`` acting
as the member (``api.imports.linear_access.LinearGateways``; ``LinearUnauthorizedError`` when the connection
cannot act)."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from api.linear_plan.runner import LinearPlanRunner
from api.plan_destinations import PlanDestinations


def add_linear_plan(runtime: Any, destinations: PlanDestinations, stack: AsyncExitStack) -> None:
    """When ``runtime.linear`` is set, build the runner, assign it to ``runtime.linear_plan``, start its recovery, push
    its ``aclose`` on ``stack`` and register it as a plan destination."""

    connections = runtime.linear
    if connections is None:
        return
    mcp = runtime.settings.mcp
    runner = LinearPlanRunner(
        connections.gateway,
        transaction=runtime.transaction,
        graph_store=runtime.graph_store,
        public_url=mcp.public_url if mcp else None,
    )
    runtime.linear_plan = runner
    stack.push_async_callback(runner.aclose)
    runner.start_recovery()
    destinations.register(runner)
