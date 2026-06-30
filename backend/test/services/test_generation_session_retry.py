"""
Tests for GenerationSessionRetryService (Phase 3 version).

Uses GenerationSessionStateMachine.reset_for_retry() for state transitions.
Enforces HR-2: workspace reuse when code_archived=False.
"""
import pytest

from app.database.memory import InMemoryDatabase
from app.services.workspace_pool import WorkspacePoolService
from app.services.generation_session_retry import (
    GenerationSessionRetryService,
    InvalidRetryStateError,
    GenerationSessionRetryError,
)
from app.schemas.generation_workflow_enums import GenerationStatus, WorkspaceStatus
from app.state.exceptions import MaxRetriesExceededError
from app.state.db_adapter import COL_GENERATION_SESSIONS


@pytest.fixture
def db():
    database = InMemoryDatabase()
    yield database
    database.clear()


@pytest.fixture
def temp_workspace_dir():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def workspace_pool(db, temp_workspace_dir):
    from unittest.mock import AsyncMock
    service = WorkspacePoolService(db, workspace_base_path=temp_workspace_dir)
    async def mock_ensure_repo_cloned(workspace_id: str, ws_doc: dict, generation_id: str):
        workspace_path = service._get_workspace_path(workspace_id)
        workspace_path.mkdir(parents=True, exist_ok=True)
        (workspace_path / ".git").mkdir(exist_ok=True)
    service._ensure_repo_cloned = AsyncMock(side_effect=mock_ensure_repo_cloned)
    return service


@pytest.fixture
def retry_service(db, workspace_pool):
    return GenerationSessionRetryService(db, workspace_pool)


@pytest.fixture
def sample_workspaces(db):
    for set_num in [1, 2]:
        for i in range(1, 4):
            ws_id = f"ws-{set_num:02d}-{i}"
            db.set("workspaces", ws_id, {
                "repo_url": f"https://github.com/org/workspace-{set_num}-{i}",
                "workspace_pool": "default",
                "set_number": set_num,
                "status": WorkspaceStatus.AVAILABLE.value,
                "locked_by": None,
                "clean_verified": True,
                "last_used_by": None,
                "allocation_history": [],
            })


class TestRetryFailedGeneration:
    """Test retry of a FAILED generation."""

    @pytest.mark.asyncio
    async def test_retry_transitions_failed_to_pending(self, retry_service, sample_workspaces):
        """FAILED generation → reset_for_retry → PENDING."""
        db = retry_service._db

        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]
        db.set(COL_GENERATION_SESSIONS, "est-123", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": workspace_ids,
            "code_archived": True,  # Archived — no workspace check needed
            "retry_count": 0,
            "max_retries": 3,
            "error": "Previous error",
            "state_history": [],
        })
        for ws_id in workspace_ids:
            db.update("workspaces", ws_id, {
                "status": WorkspaceStatus.ALLOCATED.value,
                "locked_by": "est-123",
            })

        result = await retry_service.retry_generation_session("est-123")

        assert result == "est-123"
        est = db.get(COL_GENERATION_SESSIONS, "est-123")
        assert est["status"] == GenerationStatus.PENDING.value
        assert est["retry_count"] == 1
        assert est["error"] is None

    @pytest.mark.asyncio
    async def test_retry_increments_retry_count(self, retry_service, sample_workspaces):
        """retry_count is incremented by reset_for_retry."""
        db = retry_service._db

        db.set(COL_GENERATION_SESSIONS, "est-123", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": [],
            "code_archived": True,
            "retry_count": 1,
            "max_retries": 3,
            "state_history": [],
        })

        await retry_service.retry_generation_session("est-123")

        est = db.get(COL_GENERATION_SESSIONS, "est-123")
        assert est["retry_count"] == 2

    @pytest.mark.asyncio
    async def test_retry_appends_state_history_entry(self, retry_service, sample_workspaces):
        """reset_for_retry appends a state_history entry."""
        db = retry_service._db

        db.set(COL_GENERATION_SESSIONS, "est-123", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": [],
            "code_archived": True,
            "retry_count": 0,
            "max_retries": 3,
            "state_history": [],
        })

        await retry_service.retry_generation_session("est-123")

        est = db.get(COL_GENERATION_SESSIONS, "est-123")
        history = est.get("state_history", [])
        assert len(history) >= 1
        last = history[-1]
        assert last["status"] == GenerationStatus.PENDING.value
        assert "triggered_by" in last

    @pytest.mark.asyncio
    async def test_retry_preserves_checkpoint(self, retry_service, sample_workspaces):
        """reset_for_retry does NOT clear checkpoint (orchestrator resumes from it)."""
        db = retry_service._db

        db.set(COL_GENERATION_SESSIONS, "est-123", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": [],
            "code_archived": True,
            "retry_count": 0,
            "max_retries": 3,
            "checkpoint": "planning_done",
            "state_history": [],
        })

        await retry_service.retry_generation_session("est-123")

        est = db.get(COL_GENERATION_SESSIONS, "est-123")
        assert est["checkpoint"] == "planning_done"

    @pytest.mark.asyncio
    async def test_retry_preserves_workspace_ids(self, retry_service, sample_workspaces):
        """reset_for_retry does NOT clear workspace_ids (HR-2 invariant)."""
        db = retry_service._db

        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]
        db.set(COL_GENERATION_SESSIONS, "est-123", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": workspace_ids,
            "code_archived": True,
            "retry_count": 0,
            "max_retries": 3,
            "state_history": [],
        })
        for ws_id in workspace_ids:
            db.update("workspaces", ws_id, {
                "status": WorkspaceStatus.ALLOCATED.value,
                "locked_by": "est-123",
            })

        await retry_service.retry_generation_session("est-123")

        est = db.get(COL_GENERATION_SESSIONS, "est-123")
        assert est["workspace_ids"] == workspace_ids


class TestRetryValidation:
    """Test retry state validation."""

    @pytest.mark.asyncio
    async def test_cannot_retry_running(self, retry_service, sample_workspaces):
        """Cannot retry generation in RUNNING state."""
        db = retry_service._db
        db.set(COL_GENERATION_SESSIONS, "est-running", {
            "status": GenerationStatus.RUNNING.value,
            "workspace_ids": [],
            "code_archived": False,
            "retry_count": 0,
            "state_history": [],
        })

        with pytest.raises(InvalidRetryStateError):
            await retry_service.retry_generation_session("est-running")

    @pytest.mark.asyncio
    async def test_stuck_pending_retry_succeeds_without_incrementing_count(
        self, retry_service, sample_workspaces
    ):
        """
        PENDING (stuck) generation can be retried.
        A previous retry's background task crashed before allocation started,
        leaving the generation in PENDING. Retry must re-trigger the workflow
        without calling reset_for_retry() (which would double-increment retry_count).
        """
        db = retry_service._db
        db.set(COL_GENERATION_SESSIONS, "est-stuck", {
            "status": GenerationStatus.PENDING.value,
            "workspace_ids": [],
            "code_archived": False,
            "retry_count": 1,  # already incremented by the first retry call
            "max_retries": 3,
            "state_history": [],
        })

        result = await retry_service.retry_generation_session("est-stuck")
        assert result == "est-stuck"

        est = db.get(COL_GENERATION_SESSIONS, "est-stuck")
        # Status stays PENDING — no reset_for_retry called
        assert est["status"] == GenerationStatus.PENDING.value
        # retry_count must NOT be incremented again
        assert est["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_cannot_retry_initializing(self, retry_service, sample_workspaces):
        """Cannot retry generation that is actively INITIALIZING (allocation in progress)."""
        db = retry_service._db
        db.set(COL_GENERATION_SESSIONS, "est-init", {
            "status": GenerationStatus.INITIALIZING.value,
            "workspace_ids": [],
            "code_archived": False,
            "retry_count": 0,
            "state_history": [],
        })

        with pytest.raises(InvalidRetryStateError):
            await retry_service.retry_generation_session("est-init")

    @pytest.mark.asyncio
    async def test_cannot_retry_not_found(self, retry_service, sample_workspaces):
        """Raises GenerationSessionRetryError if generation not found."""
        with pytest.raises(GenerationSessionRetryError):
            await retry_service.retry_generation_session("est-nonexistent")

    @pytest.mark.asyncio
    async def test_max_retries_exceeded_raises(self, retry_service, sample_workspaces):
        """Raises MaxRetriesExceededError when retry_count >= max_retries."""
        db = retry_service._db
        db.set(COL_GENERATION_SESSIONS, "est-maxed", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": [],
            "code_archived": True,
            "retry_count": 3,
            "max_retries": 3,
            "state_history": [],
        })

        with pytest.raises(MaxRetriesExceededError):
            await retry_service.retry_generation_session("est-maxed")


class TestHR2WorkspaceReuseConstraint:
    """
    Tests for HR-2: workspace reuse when code_archived=False.

    If code has NOT been archived to git, the retry MUST use the same
    workspace_ids. If they are inaccessible, raise an explicit error.
    """

    @pytest.mark.asyncio
    async def test_code_archived_false_workspaces_accessible_ok(
        self, retry_service, sample_workspaces
    ):
        """code_archived=False + workspaces ALLOCATED to this generation → retry succeeds."""
        db = retry_service._db
        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]

        db.set(COL_GENERATION_SESSIONS, "est-123", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": workspace_ids,
            "code_archived": False,  # Not archived — must reuse
            "retry_count": 0,
            "max_retries": 3,
            "state_history": [],
        })
        for ws_id in workspace_ids:
            db.update("workspaces", ws_id, {
                "status": WorkspaceStatus.ALLOCATED.value,
                "locked_by": "est-123",
            })

        # Should succeed — workspaces are accessible
        result = await retry_service.retry_generation_session("est-123")
        assert result == "est-123"

        est = db.get(COL_GENERATION_SESSIONS, "est-123")
        assert est["status"] == GenerationStatus.PENDING.value

    @pytest.mark.asyncio
    async def test_code_archived_false_workspaces_not_allocated_raises(
        self, retry_service, sample_workspaces
    ):
        """code_archived=False + workspaces in CLEANING state → raises ValueError."""
        db = retry_service._db
        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]

        db.set(COL_GENERATION_SESSIONS, "est-123", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": workspace_ids,
            "code_archived": False,
            "retry_count": 0,
            "max_retries": 3,
            "state_history": [],
        })
        for ws_id in workspace_ids:
            db.update("workspaces", ws_id, {
                "status": WorkspaceStatus.CLEANING.value,
                "locked_by": None,
            })

        # Should raise — workspaces are not ALLOCATED, code may be at risk
        with pytest.raises(ValueError, match="Data loss would occur"):
            await retry_service.retry_generation_session("est-123")

    @pytest.mark.asyncio
    async def test_code_archived_false_workspace_locked_by_other_raises(
        self, retry_service, sample_workspaces
    ):
        """code_archived=False + workspace locked by different generation → raises."""
        db = retry_service._db
        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]

        db.set(COL_GENERATION_SESSIONS, "est-123", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": workspace_ids,
            "code_archived": False,
            "retry_count": 0,
            "max_retries": 3,
            "state_history": [],
        })
        db.update("workspaces", "ws-01-1", {
            "status": WorkspaceStatus.ALLOCATED.value,
            "locked_by": "est-999",  # Different generation
        })

        with pytest.raises(ValueError, match="Data loss would occur"):
            await retry_service.retry_generation_session("est-123")

    @pytest.mark.asyncio
    async def test_code_archived_false_no_workspace_ids_skips_check(
        self, retry_service, sample_workspaces
    ):
        """code_archived=False but no workspace_ids → check skipped, retry allowed."""
        db = retry_service._db

        db.set(COL_GENERATION_SESSIONS, "est-123", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": [],  # No workspaces — check skipped
            "code_archived": False,
            "retry_count": 0,
            "max_retries": 3,
            "state_history": [],
        })

        # No workspaces to check, so no ValueError raised
        result = await retry_service.retry_generation_session("est-123")
        assert result == "est-123"

    @pytest.mark.asyncio
    async def test_code_archived_true_workspaces_inaccessible_ok(
        self, retry_service, sample_workspaces
    ):
        """code_archived=True + workspaces CLEANING → retry allowed (code is safe in git)."""
        db = retry_service._db
        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]

        db.set(COL_GENERATION_SESSIONS, "est-123", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": workspace_ids,
            "code_archived": True,  # Archived — workspace state doesn't matter
            "retry_count": 0,
            "max_retries": 3,
            "state_history": [],
        })
        for ws_id in workspace_ids:
            db.update("workspaces", ws_id, {
                "status": WorkspaceStatus.CLEANING.value,
                "locked_by": None,
            })

        # code is archived, so even inaccessible workspaces are OK
        result = await retry_service.retry_generation_session("est-123")
        assert result == "est-123"



class TestProgressPreservation:
    """Test that retry preserves progress for agent continuity."""

    @pytest.mark.asyncio
    async def test_retry_preserves_progress(self, retry_service, sample_workspaces):
        """Retry preserves the progress map (not owned by state machine)."""
        db = retry_service._db

        db.set(COL_GENERATION_SESSIONS, "est-123", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": [],
            "code_archived": True,
            "retry_count": 0,
            "max_retries": 3,
            "progress": {
                "phase": "implementation",
                "completed_phases": ["analysis"],
                "last_commit_sha": "abc123",
            },
            "state_history": [],
        })

        await retry_service.retry_generation_session("est-123")

        est = db.get(COL_GENERATION_SESSIONS, "est-123")
        assert est["progress"]["phase"] == "implementation"
        assert "analysis" in est["progress"]["completed_phases"]
        assert est["progress"]["last_commit_sha"] == "abc123"


# ---------------------------------------------------------------------------
# Phase 6: retry from DEPLOY_AND_E2E_DONE reuses workspaces
# ---------------------------------------------------------------------------

class TestRetryFromDeployCheckpoint:
    """
    Phase 6: An generation that failed during the deploy/e2e phase must resume
    from DEPLOY_AND_E2E_DONE checkpoint with the SAME workspace_ids preserved
    (Commandment V: no silent fallback to fresh workspaces when code is unarchived).
    """

    @pytest.mark.asyncio
    async def test_retry_from_deploy_phase_preserves_workspace_ids(
        self, retry_service, sample_workspaces
    ):
        """Retry from DEPLOY_AND_E2E_DONE: same workspace_ids are kept in the retry."""
        from app.schemas.generation_workflow_enums import GenerationCheckpoint
        db = retry_service._db

        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]
        db.set(COL_GENERATION_SESSIONS, "est-deploy", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": workspace_ids,
            "checkpoint": GenerationCheckpoint.DEPLOY_AND_E2E_DONE.value,
            "code_archived": False,  # code still on workspace — must reuse
            "retry_count": 0,
            "max_retries": 3,
            "error": "e2e tests failed",
            "state_history": [],
        })
        for ws_id in workspace_ids:
            db.update("workspaces", ws_id, {
                "status": WorkspaceStatus.ALLOCATED.value,
                "locked_by": "est-deploy",
            })

        result = await retry_service.retry_generation_session("est-deploy")

        assert result == "est-deploy"
        est = db.get(COL_GENERATION_SESSIONS, "est-deploy")
        assert est["status"] == GenerationStatus.PENDING.value
        # workspace_ids must be preserved — must NOT be replaced with new ones
        assert est["workspace_ids"] == workspace_ids

    @pytest.mark.asyncio
    async def test_retry_from_deploy_phase_preserves_checkpoint(
        self, retry_service, sample_workspaces
    ):
        """Checkpoint is preserved so the retry can resume from the correct step."""
        from app.schemas.generation_workflow_enums import GenerationCheckpoint
        db = retry_service._db

        workspace_ids = ["ws-01-1", "ws-01-2", "ws-01-3"]
        db.set(COL_GENERATION_SESSIONS, "est-deploy2", {
            "status": GenerationStatus.FAILED.value,
            "workspace_ids": workspace_ids,
            "checkpoint": GenerationCheckpoint.DEPLOY_AND_E2E_DONE.value,
            "code_archived": False,
            "retry_count": 0,
            "max_retries": 3,
            "state_history": [],
        })
        for ws_id in workspace_ids:
            db.update("workspaces", ws_id, {
                "status": WorkspaceStatus.ALLOCATED.value,
                "locked_by": "est-deploy2",
            })

        await retry_service.retry_generation_session("est-deploy2")

        est = db.get(COL_GENERATION_SESSIONS, "est-deploy2")
        assert est["checkpoint"] == GenerationCheckpoint.DEPLOY_AND_E2E_DONE.value
