"""
execute_all_phases: a lost API connection (surfaced after the SDK's own retries are
exhausted) must abort the workspace BEFORE the checkpoint write, so the phase is never
falsely recorded as completed — a later retry_generation must re-run it, not skip it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.schemas.agent import AgentResult
from app.schemas.planning import PhaseInfo, PlanningResult
from app.schemas.specification import GenerateAppRequest
from app.services.claude_code import WorkspaceAbortedError, execute_all_phases


def _mock_workspace(tmp_path: Path) -> Mock:
    ws = Mock()
    ws.workspace_path = tmp_path
    ws.get_isolated_root = Mock(return_value=str(tmp_path))
    ws.model = "m"
    ws.provider = "openrouter"
    return ws


def _mock_manager() -> Mock:
    mgr = Mock()
    mgr.settings = Mock()
    mgr.settings.ROSETTA_MCP_ENABLED = False
    mgr.commit_and_push_outstanding = AsyncMock(return_value=True)
    return mgr


def _planning_one_phase() -> PlanningResult:
    return PlanningResult(
        phase_count=1,
        phases=[PhaseInfo(number=1, name="Backend", description="API", estimated_commits=1)],
        plan_file_path="specflow/plan.md",
    )


@pytest.mark.asyncio
async def test_connection_error_aborts_without_checkpoint(tmp_path: Path) -> None:
    """A connection-lost phase raises WorkspaceAbortedError and never writes the checkpoint."""
    planning = _planning_one_phase()
    mock_ws = _mock_workspace(tmp_path)
    mock_mgr = _mock_manager()
    request = GenerateAppRequest(spec_path="specs", outputs_dir="specflow", generation_id="e1")

    svc = Mock()
    svc.update_workspace_phase = AsyncMock()
    svc.update_deployment_workspace_phase = AsyncMock()
    # No live DB in this unit test: execute_all_phases derives db_adapter from the
    # service for the per-phase cancellation check; None makes raise_if_cancelled a
    # no-op (its documented DB-less path) so the connection-error abort is reached.
    svc.db_adapter = None

    async def fake_phase_fn(**kwargs):
        return AgentResult(
            result="API Error: Unable to connect to API (ConnectionRefused)",
            session_id=None,
            is_error=True,
        )

    with patch("app.services.claude_code.phase_agent_fn", side_effect=fake_phase_fn), \
         patch("app.services.claude_code.is_skip_mode_enabled", return_value=False):
        with pytest.raises(WorkspaceAbortedError) as exc_info:
            await execute_all_phases(
                planning_data=planning,
                workspace=mock_ws,
                manager=mock_mgr,
                request=request,
                logger=Mock(),
                generation_session_service=svc,
                workspace_id="ws-1",
            )

    assert "lost connection" in str(exc_info.value).lower()
    # The linchpin assertion: the errored phase was NOT checkpointed as completed.
    svc.update_workspace_phase.assert_not_called()
    svc.update_deployment_workspace_phase.assert_not_called()


@pytest.mark.asyncio
async def test_connection_error_aborts_on_first_occurrence(tmp_path: Path) -> None:
    """Unlike a generic error (tolerated up to 2), a single connection loss aborts immediately."""
    planning = _planning_one_phase()
    request = GenerateAppRequest(spec_path="specs", outputs_dir="specflow", generation_id="e1")

    calls = {"n": 0}

    async def fake_phase_fn(**kwargs):
        calls["n"] += 1
        return AgentResult(
            result="API Error: The socket connection was closed unexpectedly.",
            session_id=None,
            is_error=True,
        )

    with patch("app.services.claude_code.phase_agent_fn", side_effect=fake_phase_fn), \
         patch("app.services.claude_code.is_skip_mode_enabled", return_value=False):
        with pytest.raises(WorkspaceAbortedError):
            await execute_all_phases(
                planning_data=planning,
                workspace=_mock_workspace(tmp_path),
                manager=_mock_manager(),
                request=request,
                logger=Mock(),
            )

    # Aborted after the very first errored phase — no second attempt.
    assert calls["n"] == 1
