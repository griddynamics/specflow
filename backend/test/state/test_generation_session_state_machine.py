"""
Tests for GenerationSessionStateMachine.
Covers all test cases from implementation-plan.md section 1.10.
"""
import pytest
from unittest.mock import AsyncMock

from app.state.generation_session_state_machine import GenerationSessionStateMachine
from app.state.transitions import TriggeredBy
from app.state.workspace_models import set_workspace_model_override
from app.state.exceptions import (
    InvalidGenerationSessionStateError,
    ArchivePreconditionError,
    MaxRetriesExceededError,
)
from app.schemas.generation_workflow_enums import (
    GenerationCheckpoint,
    GenerationStatus,
    parse_checkpoint,
    parse_status,
)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def make_esm(db, workspace_sm=None, notification_service=None):
    return GenerationSessionStateMachine(
        db=db,
        workspace_state_machine=workspace_sm,
        notification_service=notification_service,
    )


def make_workspace_sm_mock(all_confirmed=True):
    """Returns a mock WorkspaceStateMachine."""
    mock = AsyncMock()
    mock.archive_and_release = AsyncMock(return_value={})
    mock.allocation_rollback = AsyncMock(return_value={})
    return mock


def make_archive_service_mock(confirmed=True):
    """Returns a mock workspace_archive_service."""
    mock = AsyncMock()
    mock.verify_archive_branch = AsyncMock(return_value=confirmed)
    return mock


# ------------------------------------------------------------------ #
# Tests
# ------------------------------------------------------------------ #

class TestCreate:
    @pytest.mark.asyncio
    async def test_create_sets_pending_and_history(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {})
        await esm.create("est-1", triggered_by="api:create_generation_session")
        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.PENDING
        assert len(data["state_history"]) == 1
        entry = data["state_history"][0]
        assert entry["status"] == GenerationStatus.PENDING
        assert entry["triggered_by"] == "api:create_generation_session"
        assert "at" in entry

    @pytest.mark.asyncio
    async def test_create_history_entry_has_required_fields(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {})
        await esm.create("est-1", triggered_by="test", metadata={"key": "val"})
        entry = db.get_generation_session_data("est-1")["state_history"][0]
        assert "status" in entry
        assert "at" in entry
        assert "triggered_by" in entry
        assert "metadata" in entry
        assert entry["metadata"] == {"key": "val"}


class TestBeginAllocation:
    @pytest.mark.asyncio
    async def test_pending_to_initializing(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {"status": GenerationStatus.PENDING, "state_history": []})
        await esm.begin_allocation("est-1", triggered_by="api:start_generation_session")
        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.INITIALIZING
        assert len(data["state_history"]) == 1

    @pytest.mark.asyncio
    async def test_begin_allocation_from_running_raises(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {"status": GenerationStatus.RUNNING, "state_history": []})
        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.begin_allocation("est-1", triggered_by="test")


class TestAllocationSucceeded:
    @pytest.mark.asyncio
    async def test_initializing_to_running(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {"status": GenerationStatus.INITIALIZING, "state_history": []})
        await esm.allocation_succeeded("est-1", triggered_by="api:start_generation_session")
        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.RUNNING

    @pytest.mark.asyncio
    async def test_resets_last_activity_at(self, db):
        """allocation_succeeded must refresh last_activity_at so stuck_running_detector
        does not immediately kill a retried generation with a stale timestamp."""
        from datetime import datetime, timezone, timedelta
        esm = make_esm(db)
        stale = datetime.now(timezone.utc) - timedelta(days=3)
        db.seed_generation_session("est-retry", {
            "status": GenerationStatus.INITIALIZING,
            "last_activity_at": stale,
            "state_history": [],
        })
        before = datetime.now(timezone.utc)
        await esm.allocation_succeeded("est-retry", triggered_by="api:start_generation_session")
        data = db.get_generation_session_data("est-retry")
        assert data.get("last_activity_at") is not None
        assert data["last_activity_at"] >= before


class TestAllocationFailed:
    @pytest.mark.asyncio
    async def test_initializing_to_pending(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {"status": GenerationStatus.INITIALIZING, "state_history": []})
        await esm.allocation_failed("est-1", triggered_by="test")
        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.PENDING


class TestAdvanceCheckpoint:
    @pytest.mark.asyncio
    async def test_advance_valid_forward_checkpoint(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.FILES_UPLOADED,
            "state_history": [],
        })
        await esm.advance_checkpoint(
            "est-1",
            checkpoint=GenerationCheckpoint.CONTRACT_VALIDATED,
            triggered_by="orchestrator:validate_contract",
        )
        data = db.get_generation_session_data("est-1")
        assert data["checkpoint"] == GenerationCheckpoint.CONTRACT_VALIDATED
        assert "last_activity_at" in data

    @pytest.mark.asyncio
    async def test_advance_checkpoint_backward_raises(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.CONTRACT_VALIDATED,
            "state_history": [],
        })
        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.advance_checkpoint(
                "est-1",
                checkpoint=GenerationCheckpoint.FILES_UPLOADED,
                triggered_by="test",
            )

    @pytest.mark.asyncio
    async def test_advance_checkpoint_appends_history(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.FILES_UPLOADED,
            "state_history": [],
        })
        await esm.advance_checkpoint(
            "est-1",
            checkpoint=GenerationCheckpoint.CONTRACT_VALIDATED,
            triggered_by="orchestrator:validate_contract",
        )
        history = db.get_generation_session_data("est-1")["state_history"]
        assert len(history) == 1
        assert history[0]["checkpoint"] == GenerationCheckpoint.CONTRACT_VALIDATED

    @pytest.mark.asyncio
    async def test_advance_checkpoint_outputs_archived_sets_outputs_archived_and_path(self, db):
        """outputs_archived + artifact_path are written only when checkpoint is OUTPUTS_ARCHIVED."""
        from app.services.artifact_store import ARTIFACTS_BASE

        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.GENERATION_DONE,
            "state_history": [],
            "outputs_archived": False,
        })
        await esm.advance_checkpoint(
            "est-1",
            checkpoint=GenerationCheckpoint.OUTPUTS_ARCHIVED,
            triggered_by="generate_app:archive_outputs",
        )
        doc = db.get_generation_session_data("est-1")
        assert doc["checkpoint"] == GenerationCheckpoint.OUTPUTS_ARCHIVED
        assert doc.get("outputs_archived") is True
        assert doc.get("artifact_path") == str(ARTIFACTS_BASE / "est-1")

    @pytest.mark.asyncio
    async def test_advance_checkpoint_from_non_running_raises(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.FAILED,
            "checkpoint": GenerationCheckpoint.FILES_UPLOADED,
            "state_history": [],
        })
        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.advance_checkpoint(
                "est-1",
                checkpoint=GenerationCheckpoint.FILES_UPLOADED,
                triggered_by="test",
            )


class TestFail:
    @pytest.mark.asyncio
    async def test_fail_from_running(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "state_history": [],
        })
        await esm.fail("est-1", reason="something broke", triggered_by="test")
        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.FAILED
        assert "failed_at" in data
        assert data["error"] == "something broke"

    @pytest.mark.asyncio
    async def test_fail_from_initializing(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.INITIALIZING,
            "state_history": [],
        })
        await esm.fail("est-1", reason="alloc failed", triggered_by="test")
        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.FAILED

    @pytest.mark.asyncio
    async def test_fail_from_pending(self, db):
        """
        PENDING → FAILED must be allowed.
        A background task can crash before allocation starts, leaving the generation
        in PENDING. The error handler calls fail() to mark it FAILED so the user
        can retry. Without this, the generation is permanently stuck in PENDING.
        """
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.PENDING,
            "state_history": [],
        })
        await esm.fail("est-1", reason="background task crashed", triggered_by="test")
        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.FAILED
        assert data["error"] == "background task crashed"
        assert "failed_at" in data

    @pytest.mark.asyncio
    async def test_fail_from_completed_raises(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.COMPLETED,
            "state_history": [],
        })
        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.fail("est-1", reason="test", triggered_by="test")

    @pytest.mark.asyncio
    async def test_fail_workspaces_not_touched(self, db):
        """Invariant 2: workspaces stay ALLOCATED on fail."""
        workspace_sm = make_workspace_sm_mock()
        esm = make_esm(db, workspace_sm=workspace_sm)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_ids": ["ws-1", "ws-2"],
            "state_history": [],
        })
        await esm.fail("est-1", reason="test", triggered_by="test")
        # archive_and_release must NOT be called
        workspace_sm.archive_and_release.assert_not_called()


class TestStuckDetected:
    @pytest.mark.asyncio
    async def test_stuck_detected_from_running(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "state_history": [],
        })
        await esm.stuck_detected("est-1", triggered_by="job:stuck_running_detector")
        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.FAILED
        assert "failed_at" in data


class TestInterruptedByShutdown:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("start_status", [
        GenerationStatus.RUNNING,
        GenerationStatus.INITIALIZING,
        GenerationStatus.PENDING,
    ])
    async def test_marks_failed_with_marker(self, db, start_status):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {"status": start_status, "state_history": []})
        await esm.interrupted_by_shutdown("est-1", triggered_by="system:server_shutdown")
        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.FAILED
        assert data["shutdown_interrupted"] is True
        assert "failed_at" in data
        assert data["error"]
        entry = data["state_history"][-1]
        assert entry["status"] == GenerationStatus.FAILED
        assert entry["triggered_by"] == "system:server_shutdown"

    @pytest.mark.asyncio
    async def test_does_not_touch_workspaces(self, db):
        workspace_sm = make_workspace_sm_mock()
        esm = make_esm(db, workspace_sm=workspace_sm)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_ids": ["ws-1", "ws-2"],
            "state_history": [],
        })
        await esm.interrupted_by_shutdown("est-1", triggered_by="system:server_shutdown")
        # Steel Commandment II — workspaces stay ALLOCATED, code preserved
        workspace_sm.archive_and_release.assert_not_called()
        workspace_sm.allocation_rollback.assert_not_called()
        assert db.get_generation_session_data("est-1")["workspace_ids"] == ["ws-1", "ws-2"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal", [GenerationStatus.FAILED, GenerationStatus.COMPLETED])
    async def test_rejects_terminal_states(self, db, terminal):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {"status": terminal, "state_history": []})
        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.interrupted_by_shutdown("est-1", triggered_by="system:server_shutdown")


class TestResetForRetry:
    @pytest.mark.asyncio
    async def test_reset_from_failed_to_pending(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.FAILED,
            "retry_count": 0,
            "max_retries": 3,
            "workspace_ids": ["ws-1"],
            "checkpoint": GenerationCheckpoint.CONTRACT_VALIDATED,
            "state_history": [],
        })
        await esm.reset_for_retry("est-1", triggered_by="api:manual_retry")
        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.PENDING
        assert data["retry_count"] == 1
        # workspace_ids and checkpoint must be preserved
        assert data["workspace_ids"] == ["ws-1"]
        assert data["checkpoint"] == GenerationCheckpoint.CONTRACT_VALIDATED

    @pytest.mark.asyncio
    async def test_reset_from_running_raises(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "retry_count": 0,
            "max_retries": 3,
            "state_history": [],
        })
        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.reset_for_retry("est-1", triggered_by="test")

    @pytest.mark.asyncio
    async def test_max_retries_exceeded_raises(self, db):
        """Gap 3 fix: MaxRetriesExceededError raised when retry_count >= max_retries."""
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.FAILED,
            "retry_count": 3,
            "max_retries": 3,
            "state_history": [],
        })
        with pytest.raises(MaxRetriesExceededError) as exc_info:
            await esm.reset_for_retry("est-1", triggered_by="test")
        assert exc_info.value.retry_count == 3
        assert exc_info.value.max_retries == 3

    @pytest.mark.asyncio
    async def test_reset_clears_error_field(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.FAILED,
            "retry_count": 1,
            "max_retries": 3,
            "error": "previous error",
            "state_history": [],
        })
        await esm.reset_for_retry("est-1", triggered_by="test")
        data = db.get_generation_session_data("est-1")
        assert data.get("error") is None

    @pytest.mark.asyncio
    async def test_reset_clears_shutdown_interrupted_marker(self, db):
        """A retried session is no longer an unhandled eviction casualty — boot
        recovery must not pick it up again."""
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.FAILED,
            "retry_count": 0,
            "max_retries": 3,
            "shutdown_interrupted": True,
            "state_history": [],
        })
        await esm.reset_for_retry("est-1", triggered_by="system:server_boot_recovery")
        assert db.get_generation_session_data("est-1")["shutdown_interrupted"] is False


class TestComplete:
    @pytest.mark.asyncio
    async def test_complete_without_outputs_archived_raises(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "outputs_archived": False,
            "workspace_ids": [],
            "state_history": [],
        })
        archive_svc = make_archive_service_mock(confirmed=True)
        with pytest.raises(ArchivePreconditionError):
            await esm.complete("est-1", triggered_by="test", workspace_archive_service=archive_svc)

    @pytest.mark.asyncio
    async def test_complete_with_failing_workspace_archive_raises(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "outputs_archived": True,
            "workspace_ids": ["ws-1"],
            "state_history": [],
        })
        archive_svc = make_archive_service_mock(confirmed=False)
        with pytest.raises(ArchivePreconditionError):
            await esm.complete("est-1", triggered_by="test", workspace_archive_service=archive_svc)

    @pytest.mark.asyncio
    async def test_complete_success_calls_archive_and_release(self, db):
        workspace_sm = make_workspace_sm_mock()
        esm = make_esm(db, workspace_sm=workspace_sm)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "outputs_archived": True,
            "workspace_ids": ["ws-1", "ws-2"],
            "state_history": [],
        })
        archive_svc = make_archive_service_mock(confirmed=True)
        await esm.complete("est-1", triggered_by="test", workspace_archive_service=archive_svc)
        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.COMPLETED
        assert workspace_sm.archive_and_release.call_count == 2

    @pytest.mark.asyncio
    async def test_complete_sets_code_archived_and_completed_at(self, db):
        workspace_sm = make_workspace_sm_mock()
        esm = make_esm(db, workspace_sm=workspace_sm)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "outputs_archived": True,
            "workspace_ids": [],
            "state_history": [],
        })
        archive_svc = make_archive_service_mock(confirmed=True)
        await esm.complete("est-1", triggered_by="test", workspace_archive_service=archive_svc)
        data = db.get_generation_session_data("est-1")
        assert data.get("code_archived") is True
        assert "completed_at" in data


class TestOrphanWorkspaceCleanup:
    """
    Tests for orphaned workspace cleanup in complete() when workspace_count < 3.

    Design: pool allocates all 3 workspaces in a set but only N are stored in
    workspace_ids. The remaining (orphaned) workspaces stay ALLOCATED until
    complete() releases them via allocation_rollback().
    """

    @pytest.mark.asyncio
    async def test_complete_releases_orphaned_workspaces(self, db):
        """
        Scenario: generation with workspace_count=2 completes.
        Setup: est-1 has workspace_ids=["ws-1", "ws-2"] but ws-3 is also
               ALLOCATED with locked_by="est-1" (orphaned).
        Expected: after complete(), ws-1 and ws-2 transition via archive_and_release,
                  ws-3 transitions via allocation_rollback (also CLEANING).
        """
        workspace_sm = make_workspace_sm_mock()
        esm = make_esm(db, workspace_sm=workspace_sm)

        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "outputs_archived": True,
            "workspace_ids": ["ws-1", "ws-2"],
            "state_history": [],
        })
        db.seed_workspace("ws-1", {"status": "allocated", "locked_by": "est-1"})
        db.seed_workspace("ws-2", {"status": "allocated", "locked_by": "est-1"})
        db.seed_workspace("ws-3", {"status": "allocated", "locked_by": "est-1"})  # orphaned

        archive_svc = make_archive_service_mock(confirmed=True)
        await esm.complete("est-1", triggered_by="test", workspace_archive_service=archive_svc)

        # Active workspaces released via archive_and_release
        assert workspace_sm.archive_and_release.call_count == 2
        # Orphaned workspace released via allocation_rollback
        assert workspace_sm.allocation_rollback.call_count == 1
        orphan_call_args = workspace_sm.allocation_rollback.call_args
        assert orphan_call_args[0][0] == "ws-3"

    @pytest.mark.asyncio
    async def test_complete_no_orphans_when_full_set(self, db):
        """
        Scenario: generation with workspace_count=3 (default) completes.
        Setup: all 3 workspace_ids used — no orphans.
        Expected: archive_and_release called 3 times, allocation_rollback never called.
        """
        workspace_sm = make_workspace_sm_mock()
        esm = make_esm(db, workspace_sm=workspace_sm)

        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "outputs_archived": True,
            "workspace_ids": ["ws-1", "ws-2", "ws-3"],
            "state_history": [],
        })
        for ws in ["ws-1", "ws-2", "ws-3"]:
            db.seed_workspace(ws, {"status": "allocated", "locked_by": "est-1"})

        archive_svc = make_archive_service_mock(confirmed=True)
        await esm.complete("est-1", triggered_by="test", workspace_archive_service=archive_svc)

        assert workspace_sm.archive_and_release.call_count == 3
        # No orphans: query returns empty list (all ws have been released already)
        assert workspace_sm.allocation_rollback.call_count == 0

    @pytest.mark.asyncio
    async def test_fail_does_not_release_orphaned_workspaces(self, db):
        """
        Scenario: STEEL COMMANDMENT II — fail() keeps ALL workspaces ALLOCATED.
        Setup: est-1 has workspace_ids=["ws-1", "ws-2"] and ws-3 is orphaned.
        Expected: all 3 stay ALLOCATED after fail().
        """
        workspace_sm = make_workspace_sm_mock()
        esm = make_esm(db, workspace_sm=workspace_sm)

        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_ids": ["ws-1", "ws-2"],
            "state_history": [],
        })
        for ws in ["ws-1", "ws-2", "ws-3"]:
            db.seed_workspace(ws, {"status": "allocated", "locked_by": "est-1"})

        await esm.fail("est-1", reason="test failure", triggered_by="test")

        # STEEL COMMANDMENT II: fail() never releases workspaces
        workspace_sm.archive_and_release.assert_not_called()
        workspace_sm.allocation_rollback.assert_not_called()
        # All workspaces remain ALLOCATED
        for ws in ["ws-1", "ws-2", "ws-3"]:
            data = db.get_workspace_data(ws)
            assert data["status"] == "allocated"


class TestStateHistoryInvariants:
    """Every transition must append exactly one entry to state_history."""

    @pytest.mark.asyncio
    async def test_every_transition_appends_one_history_entry(self, db):
        esm = make_esm(db)

        db.seed_generation_session("est-1", {"status": GenerationStatus.PENDING, "state_history": []})
        await esm.begin_allocation("est-1", triggered_by="test")
        assert len(db.get_generation_session_data("est-1")["state_history"]) == 1

        await esm.allocation_succeeded("est-1", triggered_by="test")
        assert len(db.get_generation_session_data("est-1")["state_history"]) == 2

        await esm.fail("est-1", reason="test", triggered_by="test")
        assert len(db.get_generation_session_data("est-1")["state_history"]) == 3

    @pytest.mark.asyncio
    async def test_history_entry_has_required_fields(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.PENDING,
            "state_history": [],
        })
        await esm.begin_allocation("est-1", triggered_by="api:start_generation_session")
        entry = db.get_generation_session_data("est-1")["state_history"][0]
        assert "status" in entry
        assert "at" in entry
        assert "triggered_by" in entry
        assert "metadata" in entry


# ---------------------------------------------------------------------------
# Phase 5: GenerationCheckpoint.is_valid_transition same-checkpoint bug
# ---------------------------------------------------------------------------

class TestCheckpointIsValidTransition:
    """
    Phase 5: is_valid_transition(X, X) must return False.
    The enum classmethod used >= (allows same checkpoint) but the state machine's
    _validate_checkpoint_order uses <= (strict forward-only).  Aligning both
    prevents future code from being misled into thinking same-checkpoint
    transitions are valid.
    """

    def test_same_checkpoint_transition_is_invalid(self):
        """is_valid_transition(X, X) must return False for every checkpoint."""
        from app.schemas.generation_workflow_enums import GenerationCheckpoint, CHECKPOINT_ORDER
        for cp in CHECKPOINT_ORDER:
            result = GenerationCheckpoint.is_valid_transition(cp, cp)
            assert result is False, (
                f"is_valid_transition({cp}, {cp}) returned True — "
                "same-checkpoint transitions must be invalid"
            )

    def test_is_at_or_after_planning_done(self):
        """is_at_or_after correctly identifies checkpoints >= PLANNING_DONE."""
        from app.schemas.generation_workflow_enums import GenerationCheckpoint
        planning = GenerationCheckpoint.CONTRACT_VALIDATED

        # PLANNING_DONE itself must pass (the regression case)
        assert GenerationCheckpoint.is_at_or_after(planning, planning) is True
        # Something after PLANNING_DONE also passes
        assert GenerationCheckpoint.is_at_or_after(GenerationCheckpoint.GENERATION_DONE, planning) is True
        # Something before PLANNING_DONE fails
        assert GenerationCheckpoint.is_at_or_after(GenerationCheckpoint.FILES_UPLOADED, planning) is False
        # None always fails
        assert GenerationCheckpoint.is_at_or_after(None, planning) is False

    def test_is_valid_transition_is_not_an_ordering_predicate(self):
        """
        Regression: is_valid_transition(PLANNING_DONE, PLANNING_DONE) is False.
        Code that needs "current_cp >= PLANNING_DONE" must use CHECKPOINT_ORDER
        index comparison, NOT is_valid_transition.

        The /run API guard was previously mis-using is_valid_transition for this
        purpose, causing a 400 when checkpoint was exactly PLANNING_DONE.
        """
        from app.schemas.generation_workflow_enums import GenerationCheckpoint, CHECKPOINT_ORDER
        planning = GenerationCheckpoint.CONTRACT_VALIDATED
        # is_valid_transition is a TRANSITION check (strictly forward), not an ordering check
        assert GenerationCheckpoint.is_valid_transition(planning, planning) is False

        # The correct way to check "at or after PLANNING_DONE":
        planning_idx = CHECKPOINT_ORDER.index(planning)
        assert CHECKPOINT_ORDER.index(planning) >= planning_idx  # PLANNING_DONE itself passes
        assert CHECKPOINT_ORDER.index(GenerationCheckpoint.GENERATION_DONE) >= planning_idx
        assert CHECKPOINT_ORDER.index(GenerationCheckpoint.FILES_UPLOADED) < planning_idx

    def test_forward_transition_is_valid(self):
        """is_valid_transition(A, B) where B is strictly after A must return True."""
        from app.schemas.generation_workflow_enums import GenerationCheckpoint, CHECKPOINT_ORDER
        for i in range(len(CHECKPOINT_ORDER) - 1):
            from_cp = CHECKPOINT_ORDER[i]
            to_cp = CHECKPOINT_ORDER[i + 1]
            assert GenerationCheckpoint.is_valid_transition(from_cp, to_cp) is True

    def test_backward_transition_is_invalid(self):
        """is_valid_transition(B, A) where A is before B must return False."""
        from app.schemas.generation_workflow_enums import GenerationCheckpoint, CHECKPOINT_ORDER
        for i in range(1, len(CHECKPOINT_ORDER)):
            from_cp = CHECKPOINT_ORDER[i]
            to_cp = CHECKPOINT_ORDER[0]
            assert GenerationCheckpoint.is_valid_transition(from_cp, to_cp) is False

    @pytest.mark.asyncio
    async def test_advance_checkpoint_to_same_is_idempotent(self, db):
        """advance_checkpoint at the current checkpoint is a no-op (idempotent re-advance)."""
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.GENERATION_DONE,
            "state_history": [],
        })
        # Should not raise; checkpoint stays the same
        await esm.advance_checkpoint(
            "est-1",
            checkpoint=GenerationCheckpoint.GENERATION_DONE,
            triggered_by="test",
        )
        data = db.get_generation_session_data("est-1")
        assert data["checkpoint"] == GenerationCheckpoint.GENERATION_DONE


# ---------------------------------------------------------------------------
# Phase 6: advance_checkpoint GENERATION_DONE → DEPLOY_AND_E2E_DONE
# ---------------------------------------------------------------------------

class TestDeployCheckpointTransition:
    @pytest.mark.asyncio
    async def test_generation_done_to_deploy_and_e2e_done_is_valid(self, db):
        """
        Phase 6: GENERATION_DONE → DEPLOY_AND_E2E_DONE must be a valid transition.
        This covers the deploy path for INTEGRATION_TESTS_READY generations.
        """
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.GENERATION_DONE,
            "state_history": [],
        })
        await esm.advance_checkpoint(
            "est-1",
            checkpoint=GenerationCheckpoint.DEPLOY_AND_E2E_DONE,
            triggered_by="test",
        )
        data = db.get_generation_session_data("est-1")
        assert data["checkpoint"] == GenerationCheckpoint.DEPLOY_AND_E2E_DONE


# ---------------------------------------------------------------------------
# Phase 6: fail() blocks subsequent complete()
# ---------------------------------------------------------------------------

class TestFailBlocksComplete:
    @pytest.mark.asyncio
    async def test_fail_then_complete_raises(self, db):
        """
        Phase 6: After fail(), complete() must raise InvalidGenerationSessionStateError.
        FAILED → COMPLETED is not a valid transition.
        """
        workspace_sm = make_workspace_sm_mock()
        esm = make_esm(db, workspace_sm=workspace_sm)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "outputs_archived": True,
            "workspace_ids": ["ws-1"],
            "state_history": [],
        })
        db.seed_workspace("ws-1", {"status": "allocated", "locked_by": "est-1"})

        await esm.fail("est-1", reason="deploy failed", triggered_by="test")

        archive_svc = make_archive_service_mock(confirmed=True)
        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.complete(
                "est-1", triggered_by="test", workspace_archive_service=archive_svc
            )


# ------------------------------------------------------------------ #
# Deploy phase tracking
# ------------------------------------------------------------------ #

_SAMPLE_PLANNING_DICT = {
    "phase_count": 3,
    "phases": [
        {"number": 1, "name": "Deploy", "description": "deploy it", "estimated_commits": 2},
        {"number": 2, "name": "Smoke", "description": "smoke test", "estimated_commits": 1},
        {"number": 3, "name": "Fix", "description": "fix issues", "estimated_commits": 3},
    ],
    "plan_file_path": "./specflow/e2e-test-plan.md",
}


class TestInitDeploymentPhases:
    @pytest.mark.asyncio
    async def test_initializes_all_workspaces(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_ids": ["ws-1", "ws-2"],
            "state_history": [],
        })
        await esm.init_deployment_phases(
            "est-1",
            planning_data_dict=_SAMPLE_PLANNING_DICT,
            total_phases=3,
            triggered_by="test",
        )
        data = db.get_generation_session_data("est-1")
        phases = data["workspace_phases_deployment"]
        assert set(phases.keys()) == {"ws-1", "ws-2"}
        for ws_data in phases.values():
            assert ws_data["last_completed_phase"] == 0
            assert ws_data["total_phases"] == 3
            assert ws_data["planning_data"] == _SAMPLE_PLANNING_DICT

    @pytest.mark.asyncio
    async def test_updates_last_activity_at(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_ids": ["ws-1"],
            "state_history": [],
        })
        await esm.init_deployment_phases(
            "est-1",
            planning_data_dict=_SAMPLE_PLANNING_DICT,
            total_phases=3,
            triggered_by="test",
        )
        data = db.get_generation_session_data("est-1")
        assert "last_activity_at" in data

    @pytest.mark.asyncio
    async def test_noop_if_already_initialized(self, db):
        """On retry, init must not overwrite existing progress."""
        esm = make_esm(db)
        existing_phases = {
            "ws-1": {"last_completed_phase": 2, "total_phases": 3, "planning_data": _SAMPLE_PLANNING_DICT},
        }
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_ids": ["ws-1"],
            "workspace_phases_deployment": existing_phases,
            "state_history": [],
        })
        await esm.init_deployment_phases(
            "est-1",
            planning_data_dict=_SAMPLE_PLANNING_DICT,
            total_phases=3,
            triggered_by="test",
        )
        data = db.get_generation_session_data("est-1")
        assert data["workspace_phases_deployment"]["ws-1"]["last_completed_phase"] == 2

    @pytest.mark.asyncio
    async def test_requires_running_state(self, db):
        """init_deployment_phases requires RUNNING — workspace tracking starts at the deploy loop."""
        esm = make_esm(db)
        for non_running in (GenerationStatus.PENDING, GenerationStatus.FAILED):
            db.seed_generation_session("est-1", {
                "status": non_running,
                "workspace_ids": ["ws-1"],
                "state_history": [],
            })
            with pytest.raises(InvalidGenerationSessionStateError):
                await esm.init_deployment_phases(
                    "est-1",
                    planning_data_dict=_SAMPLE_PLANNING_DICT,
                    total_phases=3,
                    triggered_by="test",
                )


class TestSaveE2EPlan:
    @pytest.mark.asyncio
    async def test_saves_from_analysis(self, db):
        """save_e2e_plan works from ANALYSIS using the same workspace_phases_deployment schema."""
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.PENDING,
            "workspace_ids": ["ws-1"],
            "state_history": [],
        })
        await esm.save_e2e_plan(
            "est-1",
            planning_data_dict=_SAMPLE_PLANNING_DICT,
            total_phases=3,
            triggered_by="test",
        )
        data = db.get_generation_session_data("est-1")
        assert "ws-1" in data["workspace_phases_deployment"]
        ws = data["workspace_phases_deployment"]["ws-1"]
        assert ws["planning_data"] == _SAMPLE_PLANNING_DICT
        assert ws["total_phases"] == 3
        assert ws["last_completed_phase"] == 0

    @pytest.mark.asyncio
    async def test_noop_when_no_workspace_ids(self, db):
        """No-op (warning only) when workspace_ids not yet allocated."""
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.PENDING,
            "workspace_ids": [],
            "state_history": [],
        })
        await esm.save_e2e_plan(
            "est-1",
            planning_data_dict=_SAMPLE_PLANNING_DICT,
            total_phases=3,
            triggered_by="test",
        )
        data = db.get_generation_session_data("est-1")
        assert data.get("workspace_phases_deployment") is None

    @pytest.mark.asyncio
    async def test_rejects_terminal_states(self, db):
        """Terminal statuses must be rejected even for plan refresh."""
        esm = make_esm(db)
        for terminal in (GenerationStatus.FAILED, GenerationStatus.COMPLETED):
            db.seed_generation_session("est-term", {
                "status": terminal,
                "workspace_ids": ["ws-1"],
                "state_history": [],
            })
            with pytest.raises(InvalidGenerationSessionStateError):
                await esm.save_e2e_plan(
                    "est-term",
                    planning_data_dict=_SAMPLE_PLANNING_DICT,
                    total_phases=3,
                    triggered_by="test",
                )

    @pytest.mark.asyncio
    async def test_overwrites_existing_plan_when_running(self, db):
        """save_e2e_plan overwrites workspace_phases_deployment even from RUNNING (no idempotency guard)."""
        esm = make_esm(db)
        old_plan = {"phases": [{"number": 1, "name": "Old"}]}
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_ids": ["ws-1"],
            "workspace_phases_deployment": {
                "ws-1": {"last_completed_phase": 0, "total_phases": 1, "planning_data": old_plan},
            },
            "state_history": [],
        })
        new_plan = _SAMPLE_PLANNING_DICT
        await esm.save_e2e_plan(
            "est-1",
            planning_data_dict=new_plan,
            total_phases=3,
            triggered_by="test",
        )
        data = db.get_generation_session_data("est-1")
        ws = data["workspace_phases_deployment"]["ws-1"]
        assert ws["planning_data"] == new_plan
        assert ws["total_phases"] == 3


class TestRecordDeploymentPhaseProgress:
    @pytest.mark.asyncio
    async def test_records_completed_deploy_phase(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_phases_deployment": {
                "ws-1": {"last_completed_phase": 0, "total_phases": 3},
            },
            "state_history": [],
        })
        await esm.record_deployment_phase_progress(
            "est-1", workspace_id="ws-1", completed_phase=1, triggered_by="test"
        )
        data = db.get_generation_session_data("est-1")
        assert data["workspace_phases_deployment"]["ws-1"]["last_completed_phase"] == 1

    @pytest.mark.asyncio
    async def test_updates_last_activity_at(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_phases_deployment": {},
            "state_history": [],
        })
        await esm.record_deployment_phase_progress(
            "est-1", workspace_id="ws-1", completed_phase=1, triggered_by="test"
        )
        data = db.get_generation_session_data("est-1")
        assert "last_activity_at" in data

    @pytest.mark.asyncio
    async def test_does_not_touch_workspace_phases(self, db):
        """Deployment progress must not affect the generation workspace_phases field."""
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_phases": {"ws-1": {"last_completed_phase": 5, "total_phases": 5}},
            "workspace_phases_deployment": {},
            "state_history": [],
        })
        await esm.record_deployment_phase_progress(
            "est-1", workspace_id="ws-1", completed_phase=1, triggered_by="test"
        )
        data = db.get_generation_session_data("est-1")
        assert data["workspace_phases"]["ws-1"]["last_completed_phase"] == 5

    @pytest.mark.asyncio
    async def test_requires_running_state(self, db):
        esm = make_esm(db)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.FAILED,
            "state_history": [],
        })
        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.record_deployment_phase_progress(
                "est-1", workspace_id="ws-1", completed_phase=1, triggered_by="test"
            )


class TestParseCheckpointAndStatus:
    def test_parse_checkpoint_accepts_enum_values(self):
        assert parse_checkpoint("files_uploaded") == GenerationCheckpoint.FILES_UPLOADED
        assert parse_checkpoint(GenerationCheckpoint.KB_INIT_DONE) == GenerationCheckpoint.KB_INIT_DONE

    def test_parse_checkpoint_rejects_unknown_strings(self):
        assert parse_checkpoint("uploaded_specs") is None
        assert parse_checkpoint("planning_done") is None

    def test_parse_status_accepts_enum_values(self):
        assert parse_status("running") == GenerationStatus.RUNNING

    def test_parse_status_rejects_unknown_strings(self):
        assert parse_status("analysis") is None


class TestRejectContract:
    @pytest.mark.asyncio
    async def test_running_to_pending_without_failed_at(self, db):
        workspace_sm = make_workspace_sm_mock()
        esm = make_esm(db, workspace_sm=workspace_sm)
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_ids": ["ws-1", "ws-2"],
            "state_history": [],
        })

        await esm.reject_contract(
            "est-1",
            reason="Missing plan",
            triggered_by="test:validate_contract",
        )

        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.PENDING
        assert data.get("failed_at") is None
        assert data["error"] == "Missing plan"
        assert data["workspace_ids"] == []
        assert data["workspace_phases"] == {}
        assert data["workspace_phases_deployment"] == {}
        assert workspace_sm.allocation_rollback.await_count == 2

    @pytest.mark.asyncio
    async def test_reject_resets_checkpoint_to_files_uploaded(self, db):
        """
        PR255 Bug 3: a rejected session must not retain a checkpoint at or past
        CONTRACT_VALIDATED — otherwise a later retry's _should_skip would skip the
        validator. Defensive invariant: reject always resets to FILES_UPLOADED.
        """
        workspace_sm = make_workspace_sm_mock()
        esm = make_esm(db, workspace_sm=workspace_sm)
        db.seed_generation_session("est-ckpt", {
            "status": GenerationStatus.RUNNING,
            "workspace_ids": ["ws-1"],
            "checkpoint": GenerationCheckpoint.CONTRACT_VALIDATED,
            "state_history": [],
        })

        await esm.reject_contract(
            "est-ckpt", reason="bad plan", triggered_by="test:validate_contract",
        )

        data = db.get_generation_session_data("est-ckpt")
        assert data["checkpoint"] == GenerationCheckpoint.FILES_UPLOADED


class TestSaveGenerationPlan:
    @pytest.mark.asyncio
    async def test_writes_workspace_phases_and_history(self, db):
        esm = make_esm(db)
        planning = _SAMPLE_PLANNING_DICT
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_ids": ["ws-1", "ws-2"],
            "state_history": [],
        })

        await esm.save_generation_plan(
            "est-1",
            planning_data_dict=planning,
            total_phases=3,
            triggered_by="test:contract_validator",
        )

        data = db.get_generation_session_data("est-1")
        assert data["workspace_phases"]["ws-1"]["total_phases"] == 3
        assert data["workspace_phases"]["ws-2"]["planning_data"] == planning
        assert len(data["state_history"]) == 1
        assert data["state_history"][0]["triggered_by"] == "test:contract_validator"


class TestSetWorkspaceModelOverride:
    @pytest.mark.asyncio
    async def test_writes_override_and_state_history(self, db):
        db.seed_generation_session("gen-1", {
            "status": GenerationStatus.RUNNING,
            "state_history": [],
        })

        await set_workspace_model_override(
            "gen-1", "ws-a", "claude-3-5-haiku-20251022", triggered_by=TriggeredBy.MODEL_FALLBACK, db=db
        )

        data = db.get_generation_session_data("gen-1")
        assert data["workspace_model_overrides"]["ws-a"] == "claude-3-5-haiku-20251022"
        assert len(data["state_history"]) == 1
        assert data["state_history"][0]["metadata"]["model_override"] == "claude-3-5-haiku-20251022"
        assert data["state_history"][0]["triggered_by"] == TriggeredBy.MODEL_FALLBACK

    @pytest.mark.asyncio
    async def test_overwrites_existing_override(self, db):
        db.seed_generation_session("gen-1", {
            "status": GenerationStatus.RUNNING,
            "workspace_model_overrides": {"ws-a": "old-model"},
            "state_history": [],
        })

        await set_workspace_model_override(
            "gen-1", "ws-a", "new-model", triggered_by="test", db=db
        )

        data = db.get_generation_session_data("gen-1")
        assert data["workspace_model_overrides"]["ws-a"] == "new-model"

    @pytest.mark.asyncio
    async def test_sets_multiple_workspaces_independently(self, db):
        db.seed_generation_session("gen-1", {
            "status": GenerationStatus.RUNNING,
            "state_history": [],
        })

        await set_workspace_model_override("gen-1", "ws-a", "haiku", triggered_by="test", db=db)
        await set_workspace_model_override("gen-1", "ws-b", "sonnet", triggered_by="test", db=db)

        data = db.get_generation_session_data("gen-1")
        assert data["workspace_model_overrides"]["ws-a"] == "haiku"
        assert data["workspace_model_overrides"]["ws-b"] == "sonnet"
        assert len(data["state_history"]) == 2

    @pytest.mark.asyncio
    async def test_raises_when_not_running(self, db):
        db.seed_generation_session("gen-1", {"status": GenerationStatus.FAILED})

        with pytest.raises(InvalidGenerationSessionStateError):
            await set_workspace_model_override(
                "gen-1", "ws-a", "haiku", triggered_by="test", db=db
            )
