"""The runtime hook of the Linear plan destination: off without Linear, otherwise a runner on the runtime that the plan
run starts and the runtime closes."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from api.linear_plan import wiring
from api.linear_plan.runner import LinearPlanRunner
from api.plan_destinations import PlanDestinations
from db import linear_plan as plan_db


@asynccontextmanager
async def transaction() -> Any:
    yield None


def runtime(linear: Any) -> Any:
    settings = SimpleNamespace(linear=SimpleNamespace(public_url="https://app.example.test"))
    return SimpleNamespace(
        linear=linear, linear_plan=None, settings=settings, transaction=transaction, graph_store=object()
    )


async def test_without_linear_nothing_is_registered() -> None:
    rt = runtime(None)
    async with AsyncExitStack() as stack:
        wiring.add_linear_plan(rt, PlanDestinations(), stack)

    assert rt.linear_plan is None


async def test_with_linear_a_plan_run_starts_the_runner_and_closing_the_runtime_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def none(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(plan_db, "running_targets", none)
    started: list[tuple[str, str]] = []
    rt = runtime(SimpleNamespace(gateway=lambda _user: None))
    destinations = PlanDestinations()
    async with AsyncExitStack() as stack:
        wiring.add_linear_plan(rt, destinations, stack)
        runner = rt.linear_plan
        assert isinstance(runner, LinearPlanRunner)

        async def start(project_id: str, trigger: str, **_: Any) -> None:
            started.append((project_id, trigger))

        monkeypatch.setattr(runner, "start", start)
        await destinations.after_plan("p1")
        await asyncio.sleep(0)

    assert started == [("p1", "plan_run")] and runner._closed
