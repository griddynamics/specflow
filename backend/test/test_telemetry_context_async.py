"""Regression: TelemetryContext must isolate per asyncio Task (PostHog dimensions)."""

import asyncio

import pytest

from app.core.telemetry_context import TelemetryContext
from app.schemas.telemetry_workflow import PhaseKind, TelemetryWorkflowLabel


@pytest.mark.asyncio
async def test_workspace_name_isolated_across_concurrent_tasks() -> None:
    """Scenario: parallel agent runs must not share a mutable context dict.

    In-place mutation of the dict stored in a ContextVar is unsafe: asyncio
    copies execution contexts shallowly, so concurrent tasks could see the same
    dict and overwrite workspace_name / workflow. Updates must copy-on-write.
    """

    async def run_label(label: str) -> str:
        TelemetryContext.set_workspace_name(label)
        TelemetryContext.set_workflow(TelemetryWorkflowLabel.phase(PhaseKind.GENERATION, 1, "coding"))
        await asyncio.sleep(0)
        assert TelemetryContext.get_workspace_name() == label
        assert TelemetryContext.get_workflow() == TelemetryWorkflowLabel.phase(
            PhaseKind.GENERATION, 1, "coding"
        )
        return TelemetryContext.get_workspace_name() or ""

    TelemetryContext.clear_context()
    a, b, c = await asyncio.gather(
        run_label("ws-a"),
        run_label("ws-b"),
        run_label("ws-c"),
    )
    assert sorted([a, b, c]) == ["ws-a", "ws-b", "ws-c"]
    TelemetryContext.clear_context()
