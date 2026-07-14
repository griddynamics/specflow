"""Tests for the shared rerun_generation_session coroutine.

Focus: the happy path registers the session (so the TUI shows the resumed run) and
runs the workflow to completion under task_slot; failures route through the shared
handler (never propagate). The heavy workflow steps are mocked.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import generation_workflow_runner as runner
from app.services.generation_workflow_runner import rerun_generation_session
from app.state.api_key_session_concurrency import OperationKind, SessionBeginOutcome


@asynccontextmanager
async def _noop_slot(**_kwargs):
    """Stand-in for ApiKeySessionConcurrency.task_slot (an async CM)."""
    yield


def _make_gss(*, parameters, begin_outcome=SessionBeginOutcome.ACQUIRED):
    """Build a mocked GenerationSessionService with a wired api_key_sessions facade.

    Pass ``begin_outcome=None`` to simulate unresolvable api_keys doc (no registration).
    """
    gss = AsyncMock()
    gss.get_generation_session_status.return_value = {"parameters": parameters}
    gss.start_generation_session.return_value = ["ws-1"]

    sessions = MagicMock(name="api_key_sessions")
    begin = MagicMock(outcome=begin_outcome) if begin_outcome is not None else None
    sessions.try_begin_for_generation = AsyncMock(return_value=begin)
    sessions.refresh_lease = AsyncMock()
    sessions.task_slot = MagicMock(side_effect=lambda **kw: _noop_slot(**kw))
    gss.api_key_sessions = sessions
    return gss, sessions


@pytest.fixture(autouse=True)
def _patch_workflow_steps():
    """Stub every heavy step so only the runner's own control flow executes."""
    with patch.object(runner, "create_agent_logger", MagicMock()), \
         patch.object(runner, "generate_app_workflow", AsyncMock()), \
         patch.object(runner, "multi_workspace_estimation_p10y_workflow", AsyncMock(return_value=MagicMock())), \
         patch.object(runner, "clear_workspace_caches", AsyncMock()), \
         patch.object(runner, "_build_simplified_response", MagicMock(return_value=MagicMock())), \
         patch.object(runner, "_multi_workspace_result_to_store", MagicMock(return_value={})), \
         patch.object(runner.TelemetryContext, "set_user_context", MagicMock()), \
         patch.object(runner.TelemetryContext, "merge_generation_id", MagicMock()), \
         patch.object(runner.TelemetryContext, "set_agent_query_totals_handler", MagicMock()):
        yield


@pytest.mark.asyncio
async def test_happy_path_registers_and_completes():
    gss, sessions = _make_gss(parameters={"spec_path": "specs/", "outputs_dir": "docs"})

    await rerun_generation_session("gen-1", generation_session_service=gss, user_email="u@x.io")

    # Session re-registered in active_generation_sessions so the TUI shows the resumed run.
    sessions.try_begin_for_generation.assert_awaited_once_with(
        generation_id="gen-1", operation=OperationKind.GENERATION
    )
    sessions.task_slot.assert_called_once_with(generation_id="gen-1")
    gss.start_generation_session.assert_awaited_once_with("gen-1")
    gss.complete_generation_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_already_held_refreshes_lease():
    gss, sessions = _make_gss(
        parameters={"spec_path": "specs/", "outputs_dir": "docs"},
        begin_outcome=SessionBeginOutcome.ALREADY_HELD,
    )

    await rerun_generation_session("gen-held", generation_session_service=gss)

    sessions.refresh_lease.assert_awaited_once_with(
        generation_id="gen-held", operation=OperationKind.GENERATION
    )
    gss.complete_generation_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_lease_failure_still_completes():
    """Lease refresh is housekeeping — a Firestore blip must not fail the resumed run."""
    gss, sessions = _make_gss(
        parameters={"spec_path": "specs/", "outputs_dir": "docs"},
        begin_outcome=SessionBeginOutcome.ALREADY_HELD,
    )
    sessions.refresh_lease = AsyncMock(side_effect=RuntimeError("firestore blip"))

    await rerun_generation_session("gen-held", generation_session_service=gss)

    sessions.refresh_lease.assert_awaited_once()
    gss.complete_generation_session.assert_awaited_once()
    gss.fail_generation_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_at_capacity_still_runs_workflow():
    """AT_CAPACITY must not block resume — workflow proceeds even without TUI registration."""
    gss, sessions = _make_gss(
        parameters={"spec_path": "specs/", "outputs_dir": "docs"},
        begin_outcome=SessionBeginOutcome.AT_CAPACITY,
    )

    await rerun_generation_session("gen-cap", generation_session_service=gss)

    sessions.try_begin_for_generation.assert_awaited_once()
    sessions.task_slot.assert_called_once_with(generation_id="gen-cap")
    gss.start_generation_session.assert_awaited_once()
    gss.complete_generation_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_unresolvable_registration_still_runs_workflow():
    """Unresolvable api_keys doc returns None — resume proceeds without registration."""
    gss, sessions = _make_gss(
        parameters={"spec_path": "specs/", "outputs_dir": "docs"},
        begin_outcome=None,
    )

    await rerun_generation_session("gen-none", generation_session_service=gss)

    sessions.try_begin_for_generation.assert_awaited_once()
    sessions.task_slot.assert_called_once_with(generation_id="gen-none")
    gss.complete_generation_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_routed_not_raised():
    gss, sessions = _make_gss(parameters={"spec_path": "specs/", "outputs_dir": "docs"})
    gss.start_generation_session.side_effect = RuntimeError("boom")

    # failure routes through _handle_workflow_exception (fail_generation_session), never propagates
    await rerun_generation_session("gen-err", generation_session_service=gss)

    sessions.try_begin_for_generation.assert_awaited_once()
    sessions.task_slot.assert_called_once_with(generation_id="gen-err")
    gss.fail_generation_session.assert_awaited()


@pytest.mark.asyncio
async def test_missing_parameters_fail_routed_not_raised():
    gss, sessions = _make_gss(parameters={})  # no spec_path

    await rerun_generation_session("gen-bad", generation_session_service=gss)

    # registration + start_generation_session never reached; failure handled, not raised
    sessions.try_begin_for_generation.assert_not_awaited()
    gss.start_generation_session.assert_not_called()
    gss.fail_generation_session.assert_awaited()


@pytest.mark.asyncio
async def test_handle_workflow_exception_cancelled_is_silent_noop():
    """GenerationCancelledError must NOT fail() or reject() — session already CANCELLED."""
    from app.services.generation_workflow_runner import _handle_workflow_exception
    from app.state.exceptions import GenerationCancelledError

    gss = AsyncMock()
    await _handle_workflow_exception(GenerationCancelledError("gen-1"), "gen-1", gss)

    gss.fail_generation_session.assert_not_called()
    gss.reject_generation_session.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_during_rerun_not_failed():
    """A cooperative cancellation raised mid-workflow is a silent no-op, never fail()."""
    from app.state.exceptions import GenerationCancelledError

    gss, sessions = _make_gss(parameters={"spec_path": "specs/", "outputs_dir": "docs"})
    gss.start_generation_session.side_effect = GenerationCancelledError("gen-cancel")

    await rerun_generation_session("gen-cancel", generation_session_service=gss)

    gss.fail_generation_session.assert_not_called()
