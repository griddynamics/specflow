"""Workflows and activities used by the tier-2 Temporal core tests.

A separate module so the workflow sandbox re-imports only this small file.
It contains no tests.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from agents import Agent, Runner, trace

STATE: dict[str, Any] = {"slow_started": None, "slow_cancelled": False, "slow_finished": False}


@activity.defn
async def echo_activity(text: str) -> str:
    return f"echo:{text}"


@activity.defn
async def slow_activity(seconds: float) -> str:
    STATE["slow_started"].set()
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        STATE["slow_cancelled"] = True
        raise
    STATE["slow_finished"] = True
    return "slow:done"


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, text: str) -> str:
        return await workflow.execute_activity(echo_activity, text, start_to_close_timeout=timedelta(seconds=10))


@workflow.defn
class SlowWorkflow:
    @workflow.run
    async def run(self, seconds: float) -> str:
        return await workflow.execute_activity(slow_activity, seconds, start_to_close_timeout=timedelta(seconds=30))


@workflow.defn
class ModelAgentWorkflow:
    """One agent run inside a ``trace``; the model is the worker's TestModel."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        agent = Agent(name="core-test-agent", instructions="Answer briefly.")
        with trace("core-test-trace"):
            result = await Runner.run(agent, prompt)
        return str(result.final_output)
