"""Tests for the best-effort agent error event recorder.

Contract: record via TelemetryContext handlers when a workflow registered
them; silent no-op without handler/identity; never raise into the caller.
"""

from unittest.mock import AsyncMock

import pytest

from app.core.telemetry_context import TelemetryContext
from app.schemas.agent import AgentErrorType
from app.schemas.agent_error_events import (
    AgentErrorEventKind,
    WorkspaceAgentState,
)
from app.services.agent_error_event_recorder import (
    record_agent_error_event_safe,
    set_workspace_retry_state_safe,
)


@pytest.fixture(autouse=True)
def _clean_context():
    TelemetryContext.clear_context()
    yield
    TelemetryContext.clear_context()


class TestRecordAgentErrorEventSafe:
    @pytest.mark.asyncio
    async def test_records_via_context_handler(self):
        handler = AsyncMock()
        TelemetryContext.set_agent_error_event_handler(handler)
        TelemetryContext.set_user_context(generation_id="est-1", workspace_name="ws-1")

        await record_agent_error_event_safe(
            AgentErrorEventKind.AGENT_CRASH,
            "Connection to the model API was lost.",
            phase=12,
            error_type=AgentErrorType.CONNECTION_ERROR,
            crash_backoff="1/6",
        )

        handler.assert_awaited_once()
        generation_id, event = handler.await_args.args
        assert generation_id == "est-1"
        assert event.workspace_id == "ws-1"
        assert event.kind == AgentErrorEventKind.AGENT_CRASH
        assert event.phase == 12
        assert event.error_type == AgentErrorType.CONNECTION_ERROR
        assert event.crash_backoff == "1/6"
        assert event.at  # stamped

    @pytest.mark.asyncio
    async def test_explicit_workspace_id_wins_over_context(self):
        handler = AsyncMock()
        TelemetryContext.set_agent_error_event_handler(handler)
        TelemetryContext.set_user_context(generation_id="est-1", workspace_name="ws-ctx")

        await record_agent_error_event_safe(
            AgentErrorEventKind.WORKSPACE_ABORTED, "gone", workspace_id="ws-explicit"
        )
        assert handler.await_args.args[1].workspace_id == "ws-explicit"

    @pytest.mark.asyncio
    async def test_noop_without_handler_or_identity(self):
        # No handler at all
        TelemetryContext.set_user_context(generation_id="est-1", workspace_name="ws-1")
        await record_agent_error_event_safe(AgentErrorEventKind.AGENT_CRASH, "x")

        # Handler but no generation_id
        TelemetryContext.clear_context()
        handler = AsyncMock()
        TelemetryContext.set_agent_error_event_handler(handler)
        TelemetryContext.set_user_context(workspace_name="ws-1")
        await record_agent_error_event_safe(AgentErrorEventKind.AGENT_CRASH, "x")

        # Handler + generation_id but no workspace
        TelemetryContext.set_user_context(generation_id="est-1")
        TelemetryContext.set_workspace_name("")
        await record_agent_error_event_safe(AgentErrorEventKind.AGENT_CRASH, "x")

        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swallows_handler_exception(self):
        handler = AsyncMock(side_effect=RuntimeError("firestore down"))
        TelemetryContext.set_agent_error_event_handler(handler)
        TelemetryContext.set_user_context(generation_id="est-1", workspace_name="ws-1")

        await record_agent_error_event_safe(AgentErrorEventKind.AGENT_CRASH, "x")
        handler.assert_awaited_once()  # attempted, exception swallowed

    @pytest.mark.asyncio
    async def test_truncates_long_messages(self):
        handler = AsyncMock()
        TelemetryContext.set_agent_error_event_handler(handler)
        TelemetryContext.set_user_context(generation_id="est-1", workspace_name="ws-1")

        await record_agent_error_event_safe(AgentErrorEventKind.AGENT_CRASH, "y" * 1000)
        assert len(handler.await_args.args[1].message) == 300


class TestSetWorkspaceRetryStateSafe:
    @pytest.mark.asyncio
    async def test_sets_and_clears_via_handler(self):
        handler = AsyncMock()
        TelemetryContext.set_workspace_agent_state_handler(handler)
        TelemetryContext.set_user_context(generation_id="est-1", workspace_name="ws-1")

        await set_workspace_retry_state_safe(WorkspaceAgentState.RETRYING)
        assert handler.await_args.args == ("est-1", "ws-1", WorkspaceAgentState.RETRYING)

        await set_workspace_retry_state_safe(None)
        assert handler.await_args.args == ("est-1", "ws-1", None)

    @pytest.mark.asyncio
    async def test_noop_without_handler(self):
        TelemetryContext.set_user_context(generation_id="est-1", workspace_name="ws-1")
        await set_workspace_retry_state_safe(WorkspaceAgentState.RETRYING)  # no raise

    @pytest.mark.asyncio
    async def test_swallows_handler_exception(self):
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        TelemetryContext.set_workspace_agent_state_handler(handler)
        TelemetryContext.set_user_context(generation_id="est-1", workspace_name="ws-1")

        await set_workspace_retry_state_safe(WorkspaceAgentState.ABORTED)
        handler.assert_awaited_once()
