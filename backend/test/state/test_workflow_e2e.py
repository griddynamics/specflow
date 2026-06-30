"""
E2E integration tests for the state machine + workflow orchestrator.

These tests use REAL GenerationSessionStateMachine and WorkspaceStateMachine objects
(no mocks on them) together with the FakeDB from conftest.py.
AsyncMock is used only for archive_svc (an external service).

Commandments tested here:
  II   — fail() NEVER releases workspaces
  III  — only archive_and_release() exits ALLOCATED
  IV   — archive is a hard precondition for complete()
  VIII — checkpoints never go backward
  X    — invalid transitions raise immediately
"""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock

from app.state.generation_session_state_machine import GenerationSessionStateMachine
from app.state.workspace_state_machine import WorkspaceStateMachine
from app.state.workflow_orchestrator import WorkflowOrchestrator
from app.state.exceptions import (
    InvalidGenerationSessionStateError,
    ArchivePreconditionError,
    WorkspaceOwnershipError,
    InvalidWorkspaceStateError,
)
from app.schemas.generation_workflow_enums import GenerationStatus, GenerationCheckpoint, WorkspaceStatus


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def make_esm(db, workspace_sm=None, archive_svc=None):
    return GenerationSessionStateMachine(
        db=db,
        workspace_state_machine=workspace_sm,
        notification_service=None,
    )


def make_wsm(db):
    return WorkspaceStateMachine(db)


def make_archive_svc(confirmed=True):
    mock = AsyncMock()
    mock.verify_archive_branch = AsyncMock(return_value=confirmed)
    return mock


def make_orchestrator(db, esm, archive_svc=None):
    return WorkflowOrchestrator(db, esm, workspace_archive_service=archive_svc)


def noop_step():
    async def _step():
        pass
    return _step


def make_recording_steps():
    """Returns (steps_dict, executed_list) where executed_list tracks which steps ran."""
    executed = []

    def make_step(name):
        async def _step():
            executed.append(name)
        return _step

    steps = {
        "validate_contract": make_step("validate_contract"),
        "kb_init": make_step("kb_init"),
        "generation": make_step("generation"),
        "deploy_and_e2e": make_step("deploy_and_e2e"),
        "archive_outputs": make_step("archive_outputs"),
        "estimation": make_step("estimation"),
    }
    return steps, executed


def seed_running_generation(db, est_id, workspace_ids=None, extra=None):
    """Seed a minimal RUNNING generation."""
    doc = {
        "status": GenerationStatus.RUNNING,
        "checkpoint": None,
        "state_history": [],
        "workspace_ids": workspace_ids or [],
        "outputs_archived": False,
    }
    if extra:
        doc.update(extra)
    db.seed_generation_session(est_id, doc)


def seed_allocated_workspace(db, ws_id, locked_by="est-1"):
    db.seed_workspace(ws_id, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": locked_by,
    })


# ------------------------------------------------------------------ #
# TestFullCheckpointSequence
# ------------------------------------------------------------------ #

class TestFullCheckpointSequence:

    @pytest.mark.asyncio
    async def test_all_7_steps_advance_checkpoints_in_order(self, db):
        """
        Run orchestrator with all 7 noop steps on a fresh generation.
        Verify db state has checkpoint=ESTIMATION_DONE at end.
        """
        seed_running_generation(
            db, "est-1",
            workspace_ids=["ws-1"],
            extra={"outputs_archived": True},
        )
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")

        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)
        archive_svc = make_archive_svc(confirmed=True)
        orch = make_orchestrator(db, esm, archive_svc=archive_svc)

        steps = {name: noop_step() for name in [
            "validate_contract", "kb_init",
            "generation", "archive_outputs", "estimation",
        ]}

        await orch.run("est-1", steps, triggered_by="test")

        data = db.get_generation_session_data("est-1")
        assert data["checkpoint"] == GenerationCheckpoint.ESTIMATION_DONE

    @pytest.mark.asyncio
    async def test_state_history_entry_per_checkpoint_advance(self, db):
        """
        After a full 7-step run, state_history must have at least 7 checkpoint entries.
        Each entry must have status, at, triggered_by, metadata.
        """
        seed_running_generation(
            db, "est-1",
            workspace_ids=["ws-1"],
            extra={"outputs_archived": True},
        )
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")

        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)
        archive_svc = make_archive_svc(confirmed=True)
        orch = make_orchestrator(db, esm, archive_svc=archive_svc)

        steps = {name: noop_step() for name in [
            "validate_contract", "kb_init",
            "generation", "archive_outputs", "estimation",
        ]}

        await orch.run("est-1", steps, triggered_by="test")

        data = db.get_generation_session_data("est-1")
        history = data.get("state_history", [])

        # Find checkpoint-advance entries (they have a "checkpoint" key)
        checkpoint_entries = [e for e in history if "checkpoint" in e]
        assert len(checkpoint_entries) == 5, (
            f"Expected 5 checkpoint entries, got {len(checkpoint_entries)}. "
            f"Full history: {history}"
        )

        for entry in checkpoint_entries:
            assert "status" in entry, f"Missing 'status' in {entry}"
            assert "at" in entry, f"Missing 'at' in {entry}"
            assert "triggered_by" in entry, f"Missing 'triggered_by' in {entry}"
            assert "metadata" in entry, f"Missing 'metadata' in {entry}"

    @pytest.mark.asyncio
    async def test_complete_transitions_generation_to_completed(self, db):
        """
        After all 7 steps + archive_svc confirms, status must be COMPLETED.
        """
        seed_running_generation(
            db, "est-1",
            workspace_ids=["ws-1"],
            extra={"outputs_archived": True},
        )
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")

        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)
        archive_svc = make_archive_svc(confirmed=True)
        orch = make_orchestrator(db, esm, archive_svc=archive_svc)

        steps = {name: noop_step() for name in [
            "validate_contract", "kb_init",
            "generation", "archive_outputs", "estimation",
        ]}

        await orch.run("est-1", steps, triggered_by="test")

        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_complete_releases_workspaces_to_cleaning(self, db):
        """
        After complete(), both workspaces must transition to CLEANING.
        """
        seed_running_generation(
            db, "est-1",
            workspace_ids=["ws-1", "ws-2"],
            extra={"outputs_archived": True},
        )
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")
        seed_allocated_workspace(db, "ws-2", locked_by="est-1")

        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)
        archive_svc = make_archive_svc(confirmed=True)
        orch = make_orchestrator(db, esm, archive_svc=archive_svc)

        steps = {name: noop_step() for name in [
            "validate_contract", "kb_init",
            "generation", "archive_outputs", "estimation",
        ]}

        await orch.run("est-1", steps, triggered_by="test")

        for ws_id in ["ws-1", "ws-2"]:
            ws_data = db.get_workspace_data(ws_id)
            assert ws_data["status"] == WorkspaceStatus.CLEANING, (
                f"Expected {ws_id} to be CLEANING after complete(), "
                f"got {ws_data['status']}"
            )


# ------------------------------------------------------------------ #
# TestCommandmentII_FailNeverReleasesWorkspaces
# ------------------------------------------------------------------ #

class TestCommandmentII_FailNeverReleasesWorkspaces:

    @pytest.mark.asyncio
    async def test_fail_from_running_leaves_workspaces_allocated(self, db):
        """
        esm.fail() must NOT transition workspaces out of ALLOCATED.
        Commandment II: workspaces are sacred when generation fails.
        """
        seed_running_generation(db, "est-1", workspace_ids=["ws-1", "ws-2", "ws-3"])
        for ws_id in ["ws-1", "ws-2", "ws-3"]:
            seed_allocated_workspace(db, ws_id, locked_by="est-1")

        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)

        await esm.fail("est-1", reason="network error", triggered_by="test")

        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.FAILED
        assert "failed_at" in data
        assert data.get("error") == "network error"

        for ws_id in ["ws-1", "ws-2", "ws-3"]:
            ws_data = db.get_workspace_data(ws_id)
            assert ws_data["status"] == WorkspaceStatus.ALLOCATED, (
                f"Commandment II violated: {ws_id} transitioned to "
                f"{ws_data['status']} after fail()"
            )

    @pytest.mark.asyncio
    async def test_stuck_detected_leaves_workspaces_allocated(self, db):
        """
        esm.stuck_detected() must NOT release workspaces.
        """
        seed_running_generation(db, "est-1", workspace_ids=["ws-1", "ws-2", "ws-3"])
        for ws_id in ["ws-1", "ws-2", "ws-3"]:
            seed_allocated_workspace(db, ws_id, locked_by="est-1")

        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)

        await esm.stuck_detected("est-1", triggered_by="job:stuck_running_detector")

        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.FAILED

        for ws_id in ["ws-1", "ws-2", "ws-3"]:
            ws_data = db.get_workspace_data(ws_id)
            assert ws_data["status"] == WorkspaceStatus.ALLOCATED, (
                f"Commandment II violated: {ws_id} should stay ALLOCATED after stuck_detected(), "
                f"got {ws_data['status']}"
            )

    @pytest.mark.asyncio
    async def test_fail_sets_failed_at_and_error(self, db):
        """
        fail() must write failed_at timestamp and error field.
        """
        seed_running_generation(db, "est-1")

        esm = make_esm(db)
        reason = "disk quota exceeded"

        await esm.fail("est-1", reason=reason, triggered_by="test")

        data = db.get_generation_session_data("est-1")
        assert "failed_at" in data, "failed_at must be set by fail()"
        assert data["failed_at"] is not None
        assert data.get("error") == reason, (
            f"error field must match reason. Got '{data.get('error')}', expected '{reason}'"
        )


# ------------------------------------------------------------------ #
# TestCommandmentIII_OnlyArchiveAndReleaseExitsAllocated
# ------------------------------------------------------------------ #

class TestCommandmentIII_OnlyArchiveAndReleaseExitsAllocated:

    @pytest.mark.asyncio
    async def test_mark_clean_from_allocated_raises(self, db):
        """mark_clean is only valid from CLEANING, not ALLOCATED."""
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")
        wsm = make_wsm(db)

        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.mark_clean("ws-1", triggered_by="test")

    @pytest.mark.asyncio
    async def test_begin_recovery_from_allocated_raises(self, db):
        """begin_recovery requires STUCK status; ALLOCATED raises InvalidWorkspaceStateError."""
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")
        wsm = make_wsm(db)

        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.begin_recovery("ws-1", triggered_by="test")

    @pytest.mark.asyncio
    async def test_allocation_rollback_goes_to_cleaning_not_available(self, db):
        """
        HR-4 Option A: allocation_rollback transitions ALLOCATED → CLEANING,
        NOT back to AVAILABLE.
        """
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")
        wsm = make_wsm(db)

        await wsm.allocation_rollback("ws-1", "est-1", triggered_by="test")

        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.CLEANING, (
            f"HR-4: allocation_rollback must go to CLEANING, not AVAILABLE. "
            f"Got {data['status']}"
        )
        assert data.get("locked_by") is None
        assert "cleaning_started_at" in data

    @pytest.mark.asyncio
    async def test_only_archive_and_release_can_move_allocated_workspace(self, db):
        """
        Commandment III: archive_and_release is the legal exit from ALLOCATED.
        """
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")
        wsm = make_wsm(db)

        await wsm.archive_and_release("ws-1", "est-1", triggered_by="test")

        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.CLEANING
        assert data.get("last_used_by") == "est-1"


# ------------------------------------------------------------------ #
# TestCommandmentIV_ArchivePreconditions
# ------------------------------------------------------------------ #

class TestCommandmentIV_ArchivePreconditions:

    @pytest.mark.asyncio
    async def test_complete_raises_without_outputs_archived(self, db):
        """
        complete() must raise ArchivePreconditionError if outputs_archived is False.
        """
        seed_running_generation(
            db, "est-1",
            workspace_ids=["ws-1"],
            extra={"outputs_archived": False},
        )
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")

        archive_svc = make_archive_svc(confirmed=True)
        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)

        with pytest.raises(ArchivePreconditionError):
            await esm.complete(
                "est-1",
                triggered_by="test",
                workspace_archive_service=archive_svc,
            )

    @pytest.mark.asyncio
    async def test_complete_raises_when_archive_branch_not_found(self, db):
        """
        complete() must raise ArchivePreconditionError when archive branch not confirmed.
        """
        seed_running_generation(
            db, "est-1",
            workspace_ids=["ws-1"],
            extra={"outputs_archived": True},
        )
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")

        archive_svc = make_archive_svc(confirmed=False)
        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)

        with pytest.raises(ArchivePreconditionError):
            await esm.complete(
                "est-1",
                triggered_by="test",
                workspace_archive_service=archive_svc,
            )

    @pytest.mark.asyncio
    async def test_complete_raises_if_outputs_archived_missing(self, db):
        """
        complete() must raise ArchivePreconditionError if outputs_archived field is absent.
        """
        seed_running_generation(
            db, "est-1",
            workspace_ids=["ws-1"],
        )
        # No outputs_archived field at all (defaults to falsy)
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")

        archive_svc = make_archive_svc(confirmed=True)
        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)

        with pytest.raises(ArchivePreconditionError):
            await esm.complete(
                "est-1",
                triggered_by="test",
                workspace_archive_service=archive_svc,
            )

    @pytest.mark.asyncio
    async def test_complete_succeeds_with_all_preconditions_met(self, db):
        """
        complete() succeeds when outputs_archived=True and archive branch confirmed.
        """
        seed_running_generation(
            db, "est-1",
            workspace_ids=["ws-1"],
            extra={"outputs_archived": True},
        )
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")

        archive_svc = make_archive_svc(confirmed=True)
        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)

        await esm.complete(
            "est-1",
            triggered_by="test",
            workspace_archive_service=archive_svc,
        )

        data = db.get_generation_session_data("est-1")
        assert data["status"] == GenerationStatus.COMPLETED
        assert data.get("code_archived") is True


# ------------------------------------------------------------------ #
# TestCommandmentVIII_CheckpointsNeverGoBackward
# ------------------------------------------------------------------ #

class TestCommandmentVIII_CheckpointsNeverGoBackward:

    @pytest.mark.asyncio
    async def test_advance_to_same_checkpoint_is_idempotent(self, db):
        """Advancing to the current checkpoint is a no-op (idempotent re-advance, not backward)."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.CONTRACT_VALIDATED,
            "state_history": [],
        })
        esm = make_esm(db)

        # Should not raise; checkpoint stays the same
        await esm.advance_checkpoint(
            "est-1",
            checkpoint=GenerationCheckpoint.CONTRACT_VALIDATED,
            triggered_by="test",
        )
        data = db.get_generation_session_data("est-1")
        assert data["checkpoint"] == GenerationCheckpoint.CONTRACT_VALIDATED

    @pytest.mark.asyncio
    async def test_advance_to_earlier_checkpoint_raises(self, db):
        """Advancing to a checkpoint earlier than current raises InvalidGenerationSessionStateError."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.GENERATION_DONE,
            "state_history": [],
        })
        esm = make_esm(db)

        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.advance_checkpoint(
                "est-1",
                checkpoint=GenerationCheckpoint.FILES_UPLOADED,
                triggered_by="test",
            )

    @pytest.mark.asyncio
    async def test_advance_to_checkpoint_two_steps_back_raises(self, db):
        """Advancing two checkpoints backward raises InvalidGenerationSessionStateError."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.ESTIMATION_DONE,
            "state_history": [],
        })
        esm = make_esm(db)

        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.advance_checkpoint(
                "est-1",
                checkpoint=GenerationCheckpoint.GENERATION_DONE,
                triggered_by="test",
            )


# ------------------------------------------------------------------ #
# TestCommandmentX_InvalidTransitionsRaiseImmediately
# ------------------------------------------------------------------ #

class TestCommandmentX_InvalidTransitionsRaiseImmediately:

    @pytest.mark.asyncio
    async def test_advance_checkpoint_from_failed_status_raises(self, db):
        """advance_checkpoint on a FAILED generation must raise immediately."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.FAILED,
            "checkpoint": None,
            "state_history": [],
        })
        esm = make_esm(db)

        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.advance_checkpoint(
                "est-1",
                checkpoint=GenerationCheckpoint.FILES_UPLOADED,
                triggered_by="test",
            )

    @pytest.mark.asyncio
    async def test_advance_checkpoint_from_completed_raises(self, db):
        """advance_checkpoint on a COMPLETED generation must raise immediately."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.COMPLETED,
            "checkpoint": GenerationCheckpoint.OUTPUTS_ARCHIVED,
            "state_history": [],
        })
        esm = make_esm(db)

        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.advance_checkpoint(
                "est-1",
                checkpoint=GenerationCheckpoint.OUTPUTS_ARCHIVED,
                triggered_by="test",
            )

    @pytest.mark.asyncio
    async def test_begin_allocation_from_running_raises(self, db):
        """begin_allocation is only valid from PENDING; RUNNING must raise."""
        seed_running_generation(db, "est-1")
        esm = make_esm(db)

        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.begin_allocation("est-1", triggered_by="test")

    @pytest.mark.asyncio
    async def test_reset_for_retry_from_running_raises(self, db):
        """reset_for_retry is only valid from FAILED; RUNNING must raise."""
        seed_running_generation(db, "est-1")
        esm = make_esm(db)

        with pytest.raises(InvalidGenerationSessionStateError):
            await esm.reset_for_retry("est-1", triggered_by="test")

    @pytest.mark.asyncio
    async def test_allocation_rollback_wrong_owner_raises(self, db):
        """allocation_rollback with wrong generation_id raises WorkspaceOwnershipError."""
        db.seed_workspace("ws-1", {
            "status": WorkspaceStatus.ALLOCATED,
            "locked_by": "est-999",
        })
        wsm = make_wsm(db)

        with pytest.raises(WorkspaceOwnershipError):
            await wsm.allocation_rollback("ws-1", "est-1", triggered_by="test")

    @pytest.mark.asyncio
    async def test_archive_and_release_wrong_owner_raises(self, db):
        """archive_and_release with wrong generation_id raises WorkspaceOwnershipError."""
        db.seed_workspace("ws-1", {
            "status": WorkspaceStatus.ALLOCATED,
            "locked_by": "est-999",
        })
        wsm = make_wsm(db)

        with pytest.raises(WorkspaceOwnershipError):
            await wsm.archive_and_release("ws-1", "est-1", triggered_by="test")


# ------------------------------------------------------------------ #
# TestOrchestratorRetryResume
# ------------------------------------------------------------------ #

class TestOrchestratorRetryResume:

    @pytest.mark.asyncio
    async def test_resume_from_contract_validated_skips_validator(self, db):
        """Resuming with checkpoint=CONTRACT_VALIDATED skips validate_contract; kb_init onwards run."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.CONTRACT_VALIDATED,
            "state_history": [],
            "workspace_ids": ["ws-1"],
            "outputs_archived": True,
        })
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")

        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)
        archive_svc = make_archive_svc(confirmed=True)
        orch = make_orchestrator(db, esm, archive_svc=archive_svc)

        steps, executed = make_recording_steps()
        await orch.run("est-1", steps, triggered_by="test")

        assert "validate_contract" not in executed
        assert "kb_init" in executed
        assert "generation" in executed
        assert "estimation" in executed
        assert "archive_outputs" in executed

    @pytest.mark.asyncio
    async def test_resume_from_generation_done_skips_generation(self, db):
        """
        Resuming with checkpoint=GENERATION_DONE must skip everything up through generation.
        archive_outputs runs before generation (STEEL XI).
        """
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.GENERATION_DONE,
            "state_history": [],
            "workspace_ids": ["ws-1"],
            "outputs_archived": True,
        })
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")

        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)
        archive_svc = make_archive_svc(confirmed=True)
        orch = make_orchestrator(db, esm, archive_svc=archive_svc)

        steps, executed = make_recording_steps()
        await orch.run("est-1", steps, triggered_by="test")

        assert "validate_contract" not in executed
        assert "kb_init" not in executed
        assert "generation" not in executed
        assert executed.index("archive_outputs") < executed.index("estimation")
        assert "estimation" in executed
        assert "archive_outputs" in executed

    @pytest.mark.asyncio
    async def test_resume_from_generation_started_reruns_generation(self, db):
        """
        Gap 4 fix: GENERATION_STARTED maps to BASELINE_COMMITTED rank.
        baseline must be skipped (it force-pushes an orphan that wipes generated code).
        generation must re-run when checkpoint is GENERATION_STARTED.
        """
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.GENERATION_STARTED,
            "state_history": [],
            "workspace_ids": ["ws-1"],
            "outputs_archived": True,
        })
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")

        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)
        archive_svc = make_archive_svc(confirmed=True)
        orch = make_orchestrator(db, esm, archive_svc=archive_svc)

        steps, executed = make_recording_steps()
        await orch.run("est-1", steps, triggered_by="test")

        # Earlier steps are skipped; generation is re-run from GENERATION_STARTED
        assert "validate_contract" not in executed
        assert "kb_init" not in executed
        assert "generation" in executed, "generation must re-run from GENERATION_STARTED"
        assert executed.index("archive_outputs") < executed.index("estimation")
        assert "estimation" in executed
        assert "archive_outputs" in executed

    @pytest.mark.asyncio
    async def test_resume_does_not_double_advance_completed_steps(self, db):
        """
        Resuming from PLAN_SYNCED with a real ESM must not raise errors.
        advance_checkpoint is called exactly 4 times
        (baseline, generation, archive_outputs, generation).
        """
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": GenerationCheckpoint.CONTRACT_VALIDATED,
            "state_history": [],
            "workspace_ids": ["ws-1"],
            "outputs_archived": True,
        })
        seed_allocated_workspace(db, "ws-1", locked_by="est-1")

        wsm = make_wsm(db)
        esm = make_esm(db, workspace_sm=wsm)
        archive_svc = make_archive_svc(confirmed=True)
        orch = make_orchestrator(db, esm, archive_svc=archive_svc)

        steps, executed = make_recording_steps()

        # Must not raise InvalidGenerationSessionStateError
        await orch.run("est-1", steps, triggered_by="test")

        assert "generation" in executed
        assert "estimation" in executed
        assert "archive_outputs" in executed

        # From CONTRACT_VALIDATED: kb_init, generation, archive_outputs, estimation = 4 advances
        data = db.get_generation_session_data("est-1")
        history = data.get("state_history", [])
        checkpoint_entries = [e for e in history if "checkpoint" in e]
        # From CONTRACT_VALIDATED: kb_init, generation, deploy_and_e2e, archive_outputs, estimation = 5.
        # deploy_and_e2e is a no-op (LOCAL_ONLY) but still advances its checkpoint.
        assert len(checkpoint_entries) == 5, (
            f"Expected exactly 5 checkpoint advances from CONTRACT_VALIDATED, "
            f"got {len(checkpoint_entries)}: {[e.get('checkpoint') for e in checkpoint_entries]}"
        )

        advanced_to = [e["checkpoint"] for e in checkpoint_entries]
        assert GenerationCheckpoint.GENERATION_DONE in advanced_to
        assert GenerationCheckpoint.ESTIMATION_DONE in advanced_to
        assert GenerationCheckpoint.OUTPUTS_ARCHIVED in advanced_to


# ------------------------------------------------------------------ #
# TestForceReleaseAuditTrail
# ------------------------------------------------------------------ #

class TestForceReleaseAuditTrail:

    @pytest.mark.asyncio
    async def test_force_release_writes_all_audit_fields(self, db):
        """force_release must write all required audit trail fields."""
        db.seed_workspace("ws-1", {
            "status": WorkspaceStatus.ALLOCATED,
            "locked_by": "est-1",
        })
        wsm = make_wsm(db)

        await wsm.force_release(
            "ws-1",
            reason="manual override",
            confirmed_by="admin@example.com",
        )

        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.CLEANING
        assert data.get("force_released") is True
        assert data.get("force_release_reason") == "manual override"
        assert data.get("force_released_by") == "admin@example.com"
        assert data.get("force_released_at") is not None

    @pytest.mark.asyncio
    async def test_force_release_from_non_allocated_raises(self, db):
        """force_release from AVAILABLE must raise InvalidWorkspaceStateError."""
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.AVAILABLE})
        wsm = make_wsm(db)

        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.force_release(
                "ws-1",
                reason="test",
                confirmed_by="admin@example.com",
            )

    @pytest.mark.asyncio
    async def test_force_release_from_cleaning_raises(self, db):
        """force_release from CLEANING must raise InvalidWorkspaceStateError."""
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.CLEANING})
        wsm = make_wsm(db)

        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.force_release(
                "ws-1",
                reason="test",
                confirmed_by="admin@example.com",
            )


# ------------------------------------------------------------------ #
# TestStateHistoryInvariants
# ------------------------------------------------------------------ #

class TestStateHistoryInvariants:

    @pytest.mark.asyncio
    async def test_every_transition_appends_exactly_one_entry(self, db):
        """
        Trace: create → begin_allocation → allocation_succeeded → fail.
        After each step, state_history len must grow by exactly 1.
        """
        db.seed_generation_session("est-1", {
            "status": None,
            "state_history": [],
        })
        esm = make_esm(db)

        # create (seeds the first entry internally via transition)
        await esm.create("est-1", triggered_by="test:create")
        assert len(db.get_generation_session_data("est-1")["state_history"]) == 1

        await esm.begin_allocation("est-1", triggered_by="test:begin")
        assert len(db.get_generation_session_data("est-1")["state_history"]) == 2

        await esm.allocation_succeeded("est-1", triggered_by="test:succeeded")
        assert len(db.get_generation_session_data("est-1")["state_history"]) == 3

        await esm.fail("est-1", reason="test failure", triggered_by="test:fail")
        assert len(db.get_generation_session_data("est-1")["state_history"]) == 4

    @pytest.mark.asyncio
    async def test_history_entry_has_all_required_fields(self, db):
        """
        An advance_checkpoint history entry must have:
        status, at, triggered_by, metadata, checkpoint.
        """
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
            "state_history": [],
        })
        esm = make_esm(db)

        await esm.advance_checkpoint(
            "est-1",
            checkpoint=GenerationCheckpoint.FILES_UPLOADED,
            triggered_by="test:advance",
            metadata={"step": "sync_specs"},
        )

        data = db.get_generation_session_data("est-1")
        history = data.get("state_history", [])
        assert len(history) == 1

        entry = history[0]
        assert "status" in entry, "Missing 'status'"
        assert "at" in entry, "Missing 'at'"
        assert "triggered_by" in entry, "Missing 'triggered_by'"
        assert "metadata" in entry, "Missing 'metadata'"
        assert "checkpoint" in entry, "Missing 'checkpoint'"
        assert entry["checkpoint"] == GenerationCheckpoint.FILES_UPLOADED

    @pytest.mark.asyncio
    async def test_advance_checkpoint_updates_last_activity_at(self, db):
        """advance_checkpoint must set last_activity_at to a datetime."""
        db.seed_generation_session("est-1", {
            "status": GenerationStatus.RUNNING,
            "checkpoint": None,
            "state_history": [],
        })
        esm = make_esm(db)

        await esm.advance_checkpoint(
            "est-1",
            checkpoint=GenerationCheckpoint.FILES_UPLOADED,
            triggered_by="test",
        )

        data = db.get_generation_session_data("est-1")
        last_activity_at = data.get("last_activity_at")
        assert last_activity_at is not None, "last_activity_at must be set"
        assert isinstance(last_activity_at, datetime), (
            f"last_activity_at must be a datetime, got {type(last_activity_at)}"
        )


# ------------------------------------------------------------------ #
# TestBeginRecovery
# ------------------------------------------------------------------ #

class TestBeginRecovery:

    @pytest.mark.asyncio
    async def test_begin_recovery_from_stuck_transitions_to_cleaning(self, db):
        """begin_recovery from STUCK must transition to CLEANING with cleaning_started_at."""
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.STUCK})
        wsm = make_wsm(db)

        await wsm.begin_recovery("ws-1", triggered_by="test")

        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.CLEANING
        assert "cleaning_started_at" in data
        assert data["cleaning_started_at"] is not None

    @pytest.mark.asyncio
    async def test_begin_recovery_from_cleaning_raises(self, db):
        """begin_recovery from CLEANING (not STUCK) must raise InvalidWorkspaceStateError."""
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.CLEANING})
        wsm = make_wsm(db)

        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.begin_recovery("ws-1", triggered_by="test")
