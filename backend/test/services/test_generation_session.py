"""
Tests for GenerationSessionService

Covers state management scenarios from docs/deployment/state-management.md:
- State transitions (pending → initializing → running → completed/failed)
- Heartbeat mechanism
- Lease expiration
- Progress tracking
- Crash detection
"""

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.database.memory import InMemoryDatabase
from app.schemas.generation_workflow_enums import (
    GenerationCheckpoint,
    GenerationStatus,
    WorkspaceStatus,
)
from app.schemas.model_token_usage import ModelTokenUsage
from app.schemas.workflow_usage_metrics import WORKFLOW_USAGE_METRICS_FIELD
from app.schemas.workspace_model_usage_store import WORKSPACE_MODEL_USAGE_SUBCOLLECTION
from app.services.artifact_store import ARTIFACTS_BASE
from app.services.generation_session import (
    GenerationSessionService,
    InvalidGenerationSessionStateError,
)
from app.services.workspace_pool import WorkspacePoolService
from app.state.db_adapter import COL_GENERATION_SESSIONS, StateMachineDBAdapter
from app.state.generation_session_state_machine import InvalidGenerationSessionStateError as SMInvalidGenerationSessionStateError
from app.state.workflow_orchestrator import WorkflowOrchestrator


def _seed_outputs_archived_for_completion(db, generation_id: str) -> None:
    """Fields written by GenerationSessionStateMachine.advance_checkpoint(OUTPUTS_ARCHIVED)."""
    db.update(
        COL_GENERATION_SESSIONS,
        generation_id,
        {
            "outputs_archived": True,
            "artifact_path": str(ARTIFACTS_BASE / generation_id),
        },
    )


@pytest.fixture
def db():
    """Create a fresh in-memory database for each test."""
    database = InMemoryDatabase()
    yield database
    database.clear()


@pytest.fixture
def temp_workspace_dir():
    """Create a temporary directory for workspace operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def workspace_pool(db, temp_workspace_dir):
    """Create workspace pool service with temporary workspace directory."""
    service = WorkspacePoolService(db, workspace_base_path=temp_workspace_dir)
    
    # Mock git clone operations to avoid actual network calls in tests
    async def mock_ensure_repo_cloned(workspace_id: str, ws_doc: dict, generation_id: str):
        """Mock repository cloning - just create the workspace directory."""
        workspace_path = service._get_workspace_path(workspace_id)
        workspace_path.mkdir(parents=True, exist_ok=True)
        # Create a mock .git directory to simulate cloned repo
        (workspace_path / ".git").mkdir(exist_ok=True)
    
    # Replace the method with our mock
    service._ensure_repo_cloned = AsyncMock(side_effect=mock_ensure_repo_cloned)
    
    return service


@pytest.fixture
def generation_session_service(db, workspace_pool):
    """Create generation service."""
    return GenerationSessionService(db, workspace_pool)


@pytest.fixture
def sample_workspaces(db):
    """Create sample workspaces."""
    now = datetime.now(timezone.utc)
    
    for set_num in [1, 2]:
        for i in range(1, 4):
            ws_id = f"ws-{set_num:02d}-{i}"
            db.set("workspaces", ws_id, {
                "repo_url": f"https://github.com/org/workspace-{set_num}-{i}",
                "p10y_repository_id": 74900 + (set_num * 10) + i,
                "workspace_pool": "default",
                "set_number": set_num,
                "status": "available",
                "locked_by": None,
                "locked_at": None,
                "lease_expires_at": None,
                "clean_verified": True,
                "last_used_by": None,
                "last_cleaned_at": now,
                "allocation_history": [],
                "error": None,
            })


class TestGenerationCreation:
    """Test generation creation."""
    
    @pytest.mark.asyncio
    async def test_create_generation_session(self, generation_session_service):
        """Create generation in pending state."""
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md", "model": "claude-sonnet-4"}
        )
        
        assert est_id.startswith("est-")
        
        # Verify generation document
        est = generation_session_service._db.get(COL_GENERATION_SESSIONS, est_id)
        assert est["user_email"] == "user-123@example.com"
        assert est["status"] == "pending"
        assert est["workspace_ids"] == []
        assert est["retry_count"] == 0
        assert est["max_retries"] == 3


class TestGenerationStartup:
    """Test generation startup and workspace allocation."""
    
    @pytest.mark.asyncio
    async def test_start_generation_session_happy_path(self, generation_session_service, sample_workspaces):
        """Start generation: pending → initializing → running."""
        # Create generation
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        
        # Start generation
        workspace_ids = await generation_session_service.start_generation_session(est_id)
        
        assert len(workspace_ids) == 3
        
        # Verify generation state
        est = generation_session_service._db.get(COL_GENERATION_SESSIONS, est_id)
        assert est["status"] == "running"
        assert est["workspace_ids"] == workspace_ids
    
    @pytest.mark.asyncio
    async def test_start_generation_session_wrong_state(self, generation_session_service, sample_workspaces):
        """Cannot start generation not in pending state."""
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        
        # Start generation
        await generation_session_service.start_generation_session(est_id)
        
        # Try to start again
        with pytest.raises(InvalidGenerationSessionStateError):
            await generation_session_service.start_generation_session(est_id)
    
    @pytest.mark.asyncio
    async def test_start_generation_session_no_workspaces(self, generation_session_service):
        """Starting with no workspaces transitions back to pending."""
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        
        # Try to start (no workspaces available)
        with pytest.raises(Exception):
            await generation_session_service.start_generation_session(est_id)
        
        # Verify generation went back to pending
        est = generation_session_service._db.get(COL_GENERATION_SESSIONS, est_id)
        assert est["status"] == "pending"


class TestWorkspaceReuseOnRestart:
    """
    Bug regression (2026-02-24): start_generation_session() unconditionally called
    allocate_workspace_set() even when the generation already had workspace_ids
    that were ALLOCATED and locked by it. On a stuck-PENDING retry this allocated
    a fresh set (ws-02), overwrote workspace_ids in the doc, and orphaned the
    original workspaces (ws-01 stayed ALLOCATED with no owner reference).
    Violates HR-2 / Commandment V.
    """

    @pytest.mark.asyncio
    async def test_reuses_existing_allocated_workspaces_skips_allocation(
        self, generation_session_service, sample_workspaces, db
    ):
        """
        workspace_ids already set + all ALLOCATED locked by this generation
        → allocate_workspace_set() must NOT be called, existing IDs returned.
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com", parameters={}
        )
        existing_ws = ["ws-01-1", "ws-01-2", "ws-01-3"]
        db.update(COL_GENERATION_SESSIONS, est_id, {"workspace_ids": existing_ws})
        for ws_id in existing_ws:
            db.update("workspaces", ws_id, {
                "status": WorkspaceStatus.ALLOCATED.value,
                "locked_by": est_id,
            })

        # Make allocate_workspace_set() blow up — it must not be reached
        generation_session_service.workspace_pool.allocate_workspace_set = AsyncMock(
            side_effect=AssertionError(
                "allocate_workspace_set() must NOT be called when workspaces are reusable"
            )
        )

        result = await generation_session_service.start_generation_session(est_id)

        assert result == existing_ws

    @pytest.mark.asyncio
    async def test_workspace_ids_not_overwritten_when_reusing(
        self, generation_session_service, sample_workspaces, db
    ):
        """
        workspace_ids in the generation doc must be identical after start_generation_session()
        when reusing existing allocated workspaces.
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com", parameters={}
        )
        existing_ws = ["ws-01-1", "ws-01-2", "ws-01-3"]
        db.update(COL_GENERATION_SESSIONS, est_id, {"workspace_ids": existing_ws})
        for ws_id in existing_ws:
            db.update("workspaces", ws_id, {
                "status": WorkspaceStatus.ALLOCATED.value,
                "locked_by": est_id,
            })
        generation_session_service.workspace_pool.allocate_workspace_set = AsyncMock(
            side_effect=AssertionError("must not be called")
        )

        await generation_session_service.start_generation_session(est_id)

        est = db.get(COL_GENERATION_SESSIONS, est_id)
        assert est["workspace_ids"] == existing_ws, (
            "workspace_ids must not be overwritten when workspaces are reused"
        )

    @pytest.mark.asyncio
    async def test_allocates_fresh_when_existing_workspaces_not_allocated(
        self, generation_session_service, sample_workspaces, db
    ):
        """
        If existing workspace_ids are CLEANING (not ALLOCATED), fresh allocation
        must happen. ws-01 in CLEANING → start_generation_session() allocates ws-02.
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com", parameters={}
        )
        stale_ws = ["ws-01-1", "ws-01-2", "ws-01-3"]
        db.update(COL_GENERATION_SESSIONS, est_id, {"workspace_ids": stale_ws})
        for ws_id in stale_ws:
            db.update("workspaces", ws_id, {
                "status": WorkspaceStatus.CLEANING.value,
                "locked_by": None,
            })

        result = await generation_session_service.start_generation_session(est_id)

        # ws-01 was CLEANING — fresh allocation must have picked ws-02
        assert result != stale_ws
        assert len(result) == 3
        est = db.get(COL_GENERATION_SESSIONS, est_id)
        assert est["workspace_ids"] == result

    @pytest.mark.asyncio
    async def test_allocates_fresh_when_no_existing_workspace_ids(
        self, generation_session_service, sample_workspaces, db
    ):
        """
        Normal initial start: workspace_ids is empty → fresh allocation proceeds
        as before (regression guard on the normal path).
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com", parameters={}
        )
        # workspace_ids not set (default for new generations)

        result = await generation_session_service.start_generation_session(est_id)

        assert len(result) == 3
        est = db.get(COL_GENERATION_SESSIONS, est_id)
        assert est["workspace_ids"] == result


class TestGenerationCompletion:
    """Test generation completion scenarios."""
    
    @pytest.mark.asyncio
    async def test_complete_generation_session(self, generation_session_service, sample_workspaces):
        """Complete generation releases workspaces to CLEANING."""
        # Create and start
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)

        _seed_outputs_archived_for_completion(generation_session_service._db, est_id)

        # Mock archive verification so tests don't need real git repos
        generation_session_service._archive_svc.verify_archive_branch = AsyncMock(return_value=True)

        # Complete
        result = {"p10y_scores": [1, 2, 3], "commit_count": 5}
        await generation_session_service.complete_generation_session(est_id, result)

        # Verify generation state
        est = generation_session_service._db.get(COL_GENERATION_SESSIONS, est_id)
        assert est["status"] == "completed"
        assert est["result"] == result
        assert est["completed_at"] is not None

        # Verify workspaces transitioned to CLEANING via archive_and_release
        for ws_id in workspace_ids:
            ws = generation_session_service._db.get("workspaces", ws_id)
            assert ws["status"] == "cleaning"

    @pytest.mark.asyncio
    async def test_update_completed_generation_session_result_patches_result_only(
        self, generation_session_service, sample_workspaces
    ):
        """After completion, update_completed_generation_session_result may replace stored result (e.g. P10Y refresh)."""
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"},
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)
        _seed_outputs_archived_for_completion(generation_session_service._db, est_id)
        generation_session_service._archive_svc.verify_archive_branch = AsyncMock(return_value=True)

        await generation_session_service.complete_generation_session(est_id, {"version": 1})

        new_result = {
            "version": 2,
            "aggregate_p10y_commit_coverage_pct": 95.0,
            "skipped_workspaces": [],
        }
        await generation_session_service.update_completed_generation_session_result(est_id, new_result)

        est = generation_session_service._db.get(COL_GENERATION_SESSIONS, est_id)
        assert est["status"] == "completed"
        assert est["result"] == new_result
        assert est["result"]["version"] == 2
        # Workspaces must stay released (unchanged by result patch)
        for ws_id in workspace_ids:
            assert generation_session_service._db.get("workspaces", ws_id)["status"] == "cleaning"

    @pytest.mark.asyncio
    async def test_update_completed_generation_session_result_rejects_non_completed(
        self, generation_session_service, sample_workspaces
    ):
        """Cannot patch result on a non-completed generation."""
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"},
        )
        await generation_session_service.start_generation_session(est_id)

        with pytest.raises(InvalidGenerationSessionStateError, match="not completed"):
            await generation_session_service.update_completed_generation_session_result(
                est_id, {"version": 99}
            )
    
    @pytest.mark.asyncio
    async def test_fail_generation_session(self, generation_session_service, sample_workspaces):
        """Fail generation preserves workspaces ALLOCATED (Commandment II)."""
        # Create and start
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)

        # Fail
        await generation_session_service.fail_generation_session(est_id, "Test error")

        # Verify generation state
        est = generation_session_service._db.get(COL_GENERATION_SESSIONS, est_id)
        assert est["status"] == "failed"
        assert est["error"] == "Test error"
        assert est.get("failed_at") is not None

        # Commandment II: workspaces stay ALLOCATED — generated code is preserved
        for ws_id in workspace_ids:
            ws = generation_session_service._db.get("workspaces", ws_id)
            assert ws["status"] == "allocated"

    @pytest.mark.asyncio
    async def test_cancel_generation_session_is_terminal_and_silent(
        self, generation_session_service, sample_workspaces
    ):
        """cancel_generation_session → CANCELLED with markers, workspaces retained, no notify."""
        from unittest.mock import patch

        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)

        with patch("app.services.generation_session.notifications") as mock_notifications:
            await generation_session_service.cancel_generation_session(est_id)
            mock_notifications.notify.assert_not_called()

        est = generation_session_service._db.get(COL_GENERATION_SESSIONS, est_id)
        assert est["status"] == "cancelled"
        assert est["cancelled_by_user"] is True
        assert est.get("cancelled_at") is not None
        assert est.get("failed_at") is None

        # Commandment II: workspaces stay ALLOCATED — generated code preserved.
        for ws_id in workspace_ids:
            ws = generation_session_service._db.get("workspaces", ws_id)
            assert ws["status"] == "allocated"

    @pytest.mark.asyncio
    async def test_cancel_generation_session_not_found_raises(
        self, generation_session_service
    ):
        from app.services.generation_session import GenerationSessionNotFoundError

        with pytest.raises(GenerationSessionNotFoundError):
            await generation_session_service.cancel_generation_session("est-missing")


class TestProgressTracking:
    """Test progress tracking for retry continuity."""
    
    @pytest.mark.asyncio
    async def test_update_progress(self, generation_session_service, sample_workspaces):
        """Update progress checkpoints."""
        # Create generation
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        
        # Update progress
        await generation_session_service.update_progress(
            est_id,
            phase="implementation",
            metadata={"last_commit_sha": "abc123"}
        )
        
        # Verify progress
        est = generation_session_service._db.get(COL_GENERATION_SESSIONS, est_id)
        assert est["progress"]["phase"] == "implementation"
        assert est["progress"]["last_commit_sha"] == "abc123"
    
    @pytest.mark.asyncio
    async def test_progress_tracks_completed_phases(self, generation_session_service, sample_workspaces):
        """Progress tracks completed phases."""
        # Create generation
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        
        # Update progress multiple times
        await generation_session_service.update_progress(est_id, phase="analysis")
        await generation_session_service.update_progress(est_id, phase="implementation")
        await generation_session_service.update_progress(est_id, phase="testing")
        
        # Verify completed phases
        est = generation_session_service._db.get(COL_GENERATION_SESSIONS, est_id)
        assert "analysis" in est["progress"]["completed_phases"]
        assert "implementation" in est["progress"]["completed_phases"]
        assert est["progress"]["phase"] == "testing"


class TestStateHistory:
    """Test state history audit trail."""
    
    @pytest.mark.asyncio
    async def test_state_history_tracks_transitions(self, generation_session_service, sample_workspaces):
        """State history tracks all state transitions."""
        # Create generation
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )

        # Start generation
        await generation_session_service.start_generation_session(est_id)

        _seed_outputs_archived_for_completion(generation_session_service._db, est_id)

        # Mock archive verification for completion
        generation_session_service._archive_svc.verify_archive_branch = AsyncMock(return_value=True)

        # Complete generation
        await generation_session_service.complete_generation_session(est_id, {})

        # Verify state history
        est = generation_session_service._db.get(COL_GENERATION_SESSIONS, est_id)
        state_history = est["state_history"]

        assert len(state_history) >= 4  # pending, initializing, running, completed
        assert state_history[0]["status"] == "pending"
        assert state_history[-1]["status"] == "completed"


class TestStaleWorkspaceIds:
    """Test edge cases with stale workspace_ids after maintenance."""
    
    @pytest.mark.asyncio
    async def test_validate_and_clean_workspace_ids_removes_stale(self, generation_session_service, sample_workspaces):
        """validate_and_clean_workspace_ids removes stale workspace_ids."""
        # Create generation and allocate workspaces
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)
        
        # Simulate maintenance: manually release one workspace (clearing locked_by)
        db = generation_session_service._db
        stale_ws_id = workspace_ids[0]
        db.update("workspaces", stale_ws_id, {
            "status": "available",
            "locked_by": None,
            "locked_at": None,
            "lease_expires_at": None,
        })
        
        # Generation document still has stale workspace_ids
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert stale_ws_id in est_doc["workspace_ids"]
        
        # Validate and clean - should remove stale workspace_id
        valid_workspace_ids = await generation_session_service.validate_and_clean_workspace_ids(
            generation_id=est_id,
            workspace_ids=workspace_ids,
            raise_if_empty=False
        )
        
        # Should only return valid workspaces
        assert stale_ws_id not in valid_workspace_ids
        assert len(valid_workspace_ids) == len(workspace_ids) - 1
        assert all(ws_id in valid_workspace_ids for ws_id in workspace_ids[1:])
        
        # Generation document should be updated
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert stale_ws_id not in est_doc["workspace_ids"]
        assert len(est_doc["workspace_ids"]) == len(valid_workspace_ids)
    
    @pytest.mark.asyncio
    async def test_validate_and_clean_workspace_ids_all_stale_raises(self, generation_session_service, sample_workspaces):
        """validate_and_clean_workspace_ids raises when all workspace_ids are stale."""
        # Create generation and allocate workspaces
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)
        
        # Simulate maintenance: manually release all workspaces
        db = generation_session_service._db
        for ws_id in workspace_ids:
            db.update("workspaces", ws_id, {
                "status": "available",
                "locked_by": None,
                "locked_at": None,
                "lease_expires_at": None,
            })
        
        # Should raise exception when raise_if_empty=True
        with pytest.raises(InvalidGenerationSessionStateError) as exc_info:
            await generation_session_service.validate_and_clean_workspace_ids(
                generation_id=est_id,
                workspace_ids=workspace_ids,
                raise_if_empty=True
            )
        
        assert "no valid allocated workspaces" in str(exc_info.value).lower()
        
        # Generation document should be cleared
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert est_doc["workspace_ids"] == []
    
    @pytest.mark.asyncio
    async def test_validate_and_clean_workspace_ids_all_stale_no_raise(self, generation_session_service, sample_workspaces):
        """validate_and_clean_workspace_ids returns empty list when all stale and raise_if_empty=False."""
        # Create generation and allocate workspaces
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)
        
        # Simulate maintenance: manually release all workspaces
        db = generation_session_service._db
        for ws_id in workspace_ids:
            db.update("workspaces", ws_id, {
                "status": "available",
                "locked_by": None,
                "locked_at": None,
                "lease_expires_at": None,
            })
        
        # Should return empty list when raise_if_empty=False
        valid_workspace_ids = await generation_session_service.validate_and_clean_workspace_ids(
            generation_id=est_id,
            workspace_ids=workspace_ids,
            raise_if_empty=False
        )
        
        assert valid_workspace_ids == []
        
        # Generation document should be cleared
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert est_doc["workspace_ids"] == []

    @pytest.mark.asyncio
    async def test_ensure_workspaces_for_sync_reallocates_after_release(
        self, generation_session_service, sample_workspaces
    ):
        """After contract reject clears ids, sync path can allocate fresh workspaces."""
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md", "workspace_count": 1},
        )
        first_ids = await generation_session_service.start_generation_session(est_id)
        assert len(first_ids) == 1

        db = generation_session_service._db
        for ws_id in first_ids:
            db.update("workspaces", ws_id, {
                "status": "cleaning",
                "locked_by": None,
            })
        db.update(COL_GENERATION_SESSIONS, est_id, {
            "status": GenerationStatus.PENDING.value,
            "workspace_ids": [],
        })

        new_ids = await generation_session_service.ensure_workspaces_for_sync(est_id)
        assert len(new_ids) == 1
        assert new_ids != first_ids
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert est_doc["workspace_ids"] == new_ids
        ws_doc = db.get("workspaces", new_ids[0])
        assert ws_doc["status"] == WorkspaceStatus.ALLOCATED.value
        assert ws_doc["locked_by"] == est_id

    @pytest.mark.asyncio
    async def test_fail_generation_session_preserves_workspace_ids(self, generation_session_service, sample_workspaces):
        """fail_generation_session keeps workspace_ids on generation doc (Commandment II)."""
        # Create generation and allocate workspaces
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)

        # Fail generation
        await generation_session_service.fail_generation_session(est_id, "Test error")

        # Generation should be marked as failed
        db = generation_session_service._db
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert est_doc["status"] == "failed"
        assert est_doc["error"] == "Test error"

        # Commandment II: workspace_ids NOT cleared — workspaces stay ALLOCATED
        assert set(est_doc["workspace_ids"]) == set(workspace_ids)
        for ws_id in workspace_ids:
            ws = db.get("workspaces", ws_id)
            assert ws["status"] == "allocated"
    
    @pytest.mark.asyncio
    async def test_complete_generation_session_with_stale_workspace_raises(self, generation_session_service, sample_workspaces):
        """complete_generation_session raises if a workspace in workspace_ids is not ALLOCATED.

        Stale workspace IDs in workspace_ids are an invariant violation in the new model.
        The state machine enforces ownership and ALLOCATED status before releasing.
        """
        # Create generation and allocate workspaces
        est_id = await generation_session_service.create_generation_session(
            user_email="user-123@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)

        # Simulate maintenance: manually change one workspace to available (stale)
        db = generation_session_service._db
        stale_ws_id = workspace_ids[0]
        db.update("workspaces", stale_ws_id, {
            "status": "available",
            "locked_by": None,
        })

        _seed_outputs_archived_for_completion(db, est_id)

        # Mock archive verification
        generation_session_service._archive_svc.verify_archive_branch = AsyncMock(return_value=True)

        # complete_generation_session raises because the stale workspace is not ALLOCATED
        with pytest.raises(Exception):
            await generation_session_service.complete_generation_session(est_id, {"result": "success"})


class TestCheckpointTransitions:
    """Test checkpoint transitions and get_current_phase_name."""
    
    @pytest.mark.asyncio
    async def test_update_checkpoint_transitions(self, generation_session_service, sample_workspaces):
        """Test checkpoint transitions require RUNNING status."""
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com",
            parameters={"spec_file": "spec.md"}
        )

        # Must be RUNNING to advance checkpoints
        await generation_session_service.start_generation_session(est_id)

        # Initial checkpoint should be UPLOADED_SPECS
        checkpoint = generation_session_service.get_checkpoint(est_id)
        assert checkpoint == GenerationCheckpoint.FILES_UPLOADED

        # Advance through checkpoints (forward-only)
        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.CONTRACT_VALIDATED)
        checkpoint = generation_session_service.get_checkpoint(est_id)
        assert checkpoint == GenerationCheckpoint.CONTRACT_VALIDATED

        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.CONTRACT_VALIDATED)
        checkpoint = generation_session_service.get_checkpoint(est_id)
        assert checkpoint == GenerationCheckpoint.CONTRACT_VALIDATED

        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.GENERATION_STARTED)
        checkpoint = generation_session_service.get_checkpoint(est_id)
        assert checkpoint == GenerationCheckpoint.GENERATION_STARTED

    @pytest.mark.asyncio
    async def test_update_checkpoint_idempotent_on_retry(self, generation_session_service, sample_workspaces):
        """
        update_checkpoint(ESTIMATION_DONE) must be a no-op when the checkpoint is
        already ESTIMATION_DONE.

        Scenario: complete_generation_session() fails after update_checkpoint(ESTIMATION_DONE)
        succeeds (e.g. archive branch not yet confirmed).  The generation becomes FAILED
        with checkpoint=ESTIMATION_DONE.  On retry, the outer handler calls
        update_checkpoint(ESTIMATION_DONE) again before complete_generation_session().
        Without the guard this raises InvalidGenerationSessionStateError and the retry loop
        fails permanently.
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com",
            parameters={"spec_file": "spec.md"},
        )
        await generation_session_service.start_generation_session(est_id)

        # Advance through to ESTIMATION_DONE (normal first run)
        for cp in [
            GenerationCheckpoint.FILES_UPLOADED,
            GenerationCheckpoint.CONTRACT_VALIDATED,
            GenerationCheckpoint.KB_INIT_DONE,
            GenerationCheckpoint.GENERATION_DONE,
            GenerationCheckpoint.OUTPUTS_ARCHIVED,
            GenerationCheckpoint.ESTIMATION_DONE,
        ]:
            await generation_session_service.update_checkpoint(est_id, cp)

        assert generation_session_service.get_checkpoint(est_id) == GenerationCheckpoint.ESTIMATION_DONE

        # Retry path: update_checkpoint(ESTIMATION_DONE) called again — must be a no-op
        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.ESTIMATION_DONE)  # noqa: must not raise
        assert generation_session_service.get_checkpoint(est_id) == GenerationCheckpoint.ESTIMATION_DONE

    @pytest.mark.asyncio
    async def test_get_current_phase_name_checkpoint_based(self, generation_session_service, sample_workspaces):
        """Test get_current_phase_name returns checkpoint value for non-generation checkpoints."""
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com",
            parameters={"spec_file": "spec.md"}
        )

        # Must be RUNNING to advance checkpoints
        await generation_session_service.start_generation_session(est_id)

        # Test forward-advancing checkpoints (new minimal order)
        test_cases = [
            GenerationCheckpoint.CONTRACT_VALIDATED,
            GenerationCheckpoint.KB_INIT_DONE,
            GenerationCheckpoint.GENERATION_DONE,
            GenerationCheckpoint.ESTIMATION_DONE,
        ]

        for checkpoint in test_cases:
            await generation_session_service.update_checkpoint(est_id, checkpoint)
            phase_name = generation_session_service.get_current_phase_name(est_id)
            assert phase_name == checkpoint.value
    
    @pytest.mark.asyncio
    async def test_get_current_phase_name_with_workspace_phases(self, generation_session_service, sample_workspaces):
        """Test get_current_phase_name returns phase name when generation is active."""
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)
        
        # Set up planning data with phases
        planning_data = {
            "phase_count": 3,
            "phases": [
                {"number": 1, "name": "Project Setup", "description": "Setup", "estimated_commits": 3},
                {"number": 2, "name": "Core Backend API", "description": "Backend", "estimated_commits": 4},
                {"number": 3, "name": "Frontend Implementation", "description": "Frontend", "estimated_commits": 4},
            ],
            "plan_file_path": "/path/to/plan.md"
        }
        
        await generation_session_service.update_planning_data(est_id, planning_data, total_phases=3)
        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.CONTRACT_VALIDATED)
        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.KB_INIT_DONE)

        # No phases completed yet → returns checkpoint value
        phase_name = generation_session_service.get_current_phase_name(est_id)
        assert phase_name == GenerationCheckpoint.KB_INIT_DONE.value

        # Update workspace phase to phase 1 completed
        await generation_session_service.update_workspace_phase(est_id, workspace_ids[0], completed_phase=1)

        # Now should return phase 2 name
        phase_name = generation_session_service.get_current_phase_name(est_id)
        assert phase_name == "Core Backend API"

        # Update to GENERATION_STARTED checkpoint — still returns phase name
        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.GENERATION_STARTED)
        phase_name = generation_session_service.get_current_phase_name(est_id)
        assert phase_name == "Core Backend API"
        
        # Complete phase 2
        await generation_session_service.update_workspace_phase(est_id, workspace_ids[0], completed_phase=2)
        
        # Should return phase 3 name
        phase_name = generation_session_service.get_current_phase_name(est_id)
        assert phase_name == "Frontend Implementation"
        
        # Complete all phases
        await generation_session_service.update_workspace_phase(est_id, workspace_ids[0], completed_phase=3)
        
        # Should return checkpoint value when all phases done
        phase_name = generation_session_service.get_current_phase_name(est_id)
        assert phase_name == GenerationCheckpoint.GENERATION_STARTED.value
    
    @pytest.mark.asyncio
    async def test_get_current_phase_name_no_planning_data(self, generation_session_service, sample_workspaces):
        """Test get_current_phase_name handles missing planning data gracefully."""
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com",
            parameters={"spec_file": "spec.md"}
        )
        await generation_session_service.start_generation_session(est_id)
        
        # Set checkpoint to PLAN_SYNCED but no planning data
        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.CONTRACT_VALIDATED)
        
        # Should return checkpoint value
        phase_name = generation_session_service.get_current_phase_name(est_id)
        assert phase_name == GenerationCheckpoint.CONTRACT_VALIDATED.value


class TestStateMachineDbIntegration:
    """
    Regression tests for GenerationSessionService._db_adapter — the StateMachineDBAdapter.

    These tests use REAL objects (no mocks) to catch the class of errors that
    mock-heavy unit tests cannot detect:

    Error 1: 'GenerationSessionService' object has no attribute '_db_adapter'
    Error 2: 'EmulatorDatabase' object has no attribute 'get_generation_session'

    WorkflowOrchestrator.run() calls self._db.get_generation_session(). If _db_adapter is the
    raw IDatabase instead of StateMachineDBAdapter, this fails at runtime with
    AttributeError — invisible to tests that mock the whole service.
    """

    @pytest.mark.asyncio
    async def test_db_is_adapter_not_raw_db(self, generation_session_service):
        """
        _db_adapter must be StateMachineDBAdapter, not the raw IDatabase.

        Regression: generate_poc.py was passing generation_session_service._db (IDatabase)
        to WorkflowOrchestrator instead of generation_session_service._db_adapter (adapter).
        """
        assert isinstance(generation_session_service._db_adapter, StateMachineDBAdapter), (
            f"_db_adapter must be StateMachineDBAdapter, got {type(generation_session_service._db_adapter).__name__}. "
            f"WorkflowOrchestrator requires get_generation_session() which only the adapter provides."
        )

    @pytest.mark.asyncio
    async def test_db_has_get_generation_session(self, generation_session_service):
        """_db_adapter must expose get_generation_session() for WorkflowOrchestrator.run()."""
        assert hasattr(generation_session_service._db_adapter, 'get_generation_session'), (
            f"_db_adapter {type(generation_session_service._db_adapter).__name__} is missing get_generation_session(). "
            f"WorkflowOrchestrator.run() calls this at line 133."
        )

    @pytest.mark.asyncio
    async def test_db_has_update_generation_session(self, generation_session_service):
        """_db_adapter must expose update_generation_session() for state transitions."""
        assert hasattr(generation_session_service._db_adapter, 'update_generation_session')

    @pytest.mark.asyncio
    async def test_db_get_generation_session_returns_created_record(self, generation_session_service):
        """
        _db_adapter.get_generation_session() must return real data from the underlying database.

        Verifies the adapter correctly wraps IDatabase and can read records.
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="test@example.com",
            parameters={"spec_file": "spec.md"},
        )

        doc = await generation_session_service._db_adapter.get_generation_session(est_id)

        assert doc is not None, "_db_adapter.get_generation_session() returned None for a created generation"
        assert doc.get("user_email") == "test@example.com"
        assert doc.get("status") == "pending"

    @pytest.mark.asyncio
    async def test_workflow_orchestrator_can_read_generation_via_db(self, generation_session_service):
        """
        WorkflowOrchestrator can read generation state through _db_adapter.

        Replicates exactly what generate_poc.py does:
            orchestrator = WorkflowOrchestrator(
                db=generation_session_service._db_adapter,
                generation_session_state_machine=generation_session_service._esm,
            )

        If _db_adapter is a raw IDatabase, WorkflowOrchestrator raises:
            AttributeError: object has no attribute 'get_generation_session'
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="workflow-test@example.com",
            parameters={"spec_file": "spec.md"},
        )

        orchestrator = WorkflowOrchestrator(
            db=generation_session_service._db_adapter,
            generation_session_state_machine=generation_session_service._esm,
        )

        doc = await orchestrator._db.get_generation_session(est_id)
        assert doc is not None
        assert doc.get("user_email") == "workflow-test@example.com"


class TestUpdateWorkspacePhaseRegressionNoDoubleCheckpoint:
    """
    Regression tests for the double-checkpoint bug.

    Before the fix: update_workspace_phase() would call update_checkpoint(GENERATION_DONE)
    internally when all phases completed. The orchestrator would then try to advance to
    GENERATION_DONE again, causing InvalidGenerationSessionStateError.

    After the fix: update_workspace_phase() only updates workspace_phases.
    The orchestrator owns all checkpoint advancement.
    """

    @pytest.mark.asyncio
    async def test_update_workspace_phase_does_not_advance_main_checkpoint(
        self, generation_session_service, sample_workspaces
    ):
        """
        Calling update_workspace_phase for all phases of all workspaces
        must NOT advance the main checkpoint to GENERATION_DONE.
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)

        # Set up planning data (3 phases, 1 workspace for simplicity)
        planning_data = {
            "phase_count": 3,
            "phases": [
                {"number": i, "name": f"Phase {i}", "description": "", "estimated_commits": 2}
                for i in range(1, 4)
            ],
            "plan_file_path": "/path/plan.md"
        }
        await generation_session_service.update_planning_data(est_id, planning_data, total_phases=3)
        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.CONTRACT_VALIDATED)

        # Complete all 3 phases for first workspace
        ws_id = workspace_ids[0]
        for phase in range(1, 4):
            await generation_session_service.update_workspace_phase(est_id, ws_id, completed_phase=phase)

        # CRITICAL: checkpoint must still be PLAN_SYNCED — NOT GENERATION_DONE
        checkpoint = generation_session_service.get_checkpoint(est_id)
        assert checkpoint == GenerationCheckpoint.CONTRACT_VALIDATED, (
            f"update_workspace_phase must NOT advance main checkpoint. "
            f"Got {checkpoint.value}, expected {GenerationCheckpoint.CONTRACT_VALIDATED.value}. "
            f"This is the double-checkpoint regression."
        )

    @pytest.mark.asyncio
    async def test_orchestrator_can_advance_generation_done_after_all_phases_complete(
        self, generation_session_service, sample_workspaces
    ):
        """
        After update_workspace_phase completes all phases,
        the orchestrator must still be able to advance to GENERATION_DONE
        without raising InvalidGenerationSessionStateError.

        This is the exact sequence that was failing before the fix.
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)

        planning_data = {
            "phase_count": 2,
            "phases": [
                {"number": i, "name": f"Phase {i}", "description": "", "estimated_commits": 2}
                for i in range(1, 3)
            ],
            "plan_file_path": "/path/plan.md"
        }
        await generation_session_service.update_planning_data(est_id, planning_data, total_phases=2)
        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.CONTRACT_VALIDATED)

        # Simulate the generation step: mark all phases done for all workspaces
        for ws_id in workspace_ids:
            for phase in range(1, 3):
                await generation_session_service.update_workspace_phase(
                    est_id, ws_id, completed_phase=phase
                )

        # Now the orchestrator tries to advance to GENERATION_DONE
        # This must NOT raise InvalidGenerationSessionStateError
        await generation_session_service.update_checkpoint(
            est_id,
            GenerationCheckpoint.GENERATION_DONE
        )

        checkpoint = generation_session_service.get_checkpoint(est_id)
        assert checkpoint == GenerationCheckpoint.GENERATION_DONE

    @pytest.mark.asyncio
    async def test_update_workspace_phase_tracks_per_workspace_progress(
        self, generation_session_service, sample_workspaces
    ):
        """
        update_workspace_phase correctly tracks per-workspace phase progress
        in workspace_phases field.
        """

        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)

        planning_data = {
            "phase_count": 4,
            "phases": [
                {"number": i, "name": f"Phase {i}", "description": "", "estimated_commits": 2}
                for i in range(1, 5)
            ],
            "plan_file_path": "/path/plan.md"
        }
        await generation_session_service.update_planning_data(est_id, planning_data, total_phases=4)

        ws_id = workspace_ids[0]

        # Complete phases 1 and 2
        await generation_session_service.update_workspace_phase(est_id, ws_id, completed_phase=1)
        await generation_session_service.update_workspace_phase(est_id, ws_id, completed_phase=2)

        # Verify workspace_phases updated correctly
        db = generation_session_service._db
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        ws_phases = est_doc.get("workspace_phases", {})

        assert ws_id in ws_phases
        assert ws_phases[ws_id]["last_completed_phase"] == 2

    @pytest.mark.asyncio
    async def test_checkpoint_can_advance_independently_of_workspace_phases(
        self, generation_session_service, sample_workspaces
    ):
        """
        Main checkpoint advancement is completely independent of workspace_phases.
        The orchestrator drives checkpoints; workspace_phases is just progress metadata.
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="user@example.com",
            parameters={"spec_file": "spec.md"}
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)

        # Advance checkpoint without any workspace phase updates (new minimal order)
        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.CONTRACT_VALIDATED)
        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.KB_INIT_DONE)
        await generation_session_service.update_checkpoint(est_id, GenerationCheckpoint.GENERATION_DONE)

        checkpoint = generation_session_service.get_checkpoint(est_id)
        assert checkpoint == GenerationCheckpoint.GENERATION_DONE

        # workspace_phases should still be empty (no calls to update_workspace_phase)
        db = generation_session_service._db
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        ws_phases = est_doc.get("workspace_phases", {})

        # No phases updated — workspace_phases may have empty workspace entries from init
        for ws_id in workspace_ids:
            if ws_id in ws_phases:
                assert ws_phases[ws_id].get("last_completed_phase", 0) == 0


class TestWorkspaceCountConfig:
    """
    Tests for configurable workspace_count in start_generation_session().

    Coverage:
    - Explicit workspace_count=2 allocates only 2 active workspaces
    - When workspace_count not passed, reads from stored parameters (SSOT)
    - Default (no workspace_count) allocates 3
    """

    @pytest.mark.asyncio
    async def test_start_generation_session_with_workspace_count(
        self, generation_session_service, db, sample_workspaces
    ):
        """
        Scenario: user calls run_generation with workspace_count=2.
        Setup: create generation with workspace_count=2 in parameters.
        Steps:
          1. create_generation_session stores workspace_count=2 in parameters.
          2. start_generation_session(workspace_count=2) allocates only 2 active workspaces.
          3. generation doc workspace_ids has 2 entries.
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="test@test.com",
            parameters={"spec_path": "specs", "workspace_count": 2},
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id, workspace_count=2)

        assert len(workspace_ids) == 2
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert len(est_doc["workspace_ids"]) == 2
        assert est_doc["parameters"]["workspace_count"] == 2

    @pytest.mark.asyncio
    async def test_start_generation_session_reads_workspace_count_from_parameters(
        self, generation_session_service, db, sample_workspaces
    ):
        """
        Scenario: retry endpoint calls start_generation_session() without workspace_count.
        Setup: generation was created with workspace_count=2 in parameters (SSOT).
        Steps:
          1. create_generation_session stores workspace_count=2.
          2. start_generation_session() with no workspace_count arg reads from parameters.
          3. Only 2 workspaces allocated (SSOT preserved on retry).
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="test@test.com",
            parameters={"spec_path": "specs", "workspace_count": 2},
        )
        # Call without explicit workspace_count — simulates retry endpoint
        workspace_ids = await generation_session_service.start_generation_session(est_id)

        assert len(workspace_ids) == 2
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert len(est_doc["workspace_ids"]) == 2

    @pytest.mark.asyncio
    async def test_start_generation_session_default_allocates_three(
        self, generation_session_service, db, sample_workspaces
    ):
        """workspace_count not in parameters defaults to 3 — backward compat."""
        est_id = await generation_session_service.create_generation_session(
            user_email="test@test.com",
            parameters={"spec_path": "specs"},
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)

        assert len(workspace_ids) == 3
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert len(est_doc["workspace_ids"]) == 3

    @pytest.mark.asyncio
    async def test_workspace_count_frozen_after_first_allocation(
        self, generation_session_service, db, sample_workspaces
    ):
        """
        Scenario: workspace_count is frozen at first allocation — subsequent calls
        cannot change it even when a different value is passed explicitly.
        Setup: generation created with workspace_count=2 and started (2 active workspaces).
        Steps:
          1. Reset generation to PENDING to simulate retry.
          2. Call start_generation_session with workspace_count=3 (attempt to override).
          3. Assert only 2 workspaces are allocated (stored value wins).
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="test@test.com",
            parameters={"spec_path": "specs", "workspace_count": 2},
        )
        # First allocation: 2 workspaces stored
        await generation_session_service.start_generation_session(est_id, workspace_count=2)

        # Simulate reset to PENDING (as retry would do)
        db.update(COL_GENERATION_SESSIONS, est_id, {"status": "pending", "workspace_ids": []})

        # Second call with a different count — should be silently ignored
        workspace_ids = await generation_session_service.start_generation_session(est_id, workspace_count=3)

        assert len(workspace_ids) == 2  # frozen at original value
        est_doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert est_doc["parameters"]["workspace_count"] == 2


# -------------------------------------------------------------------------
# Helpers shared by deploy-phase tests
# -------------------------------------------------------------------------

def _seed_running_generation(db, est_id: str, workspace_ids: list):
    """Seed a RUNNING generation with the given workspace IDs."""
    db.set(COL_GENERATION_SESSIONS, est_id, {
        "status": "running",
        "workspace_ids": workspace_ids,
        "workspace_phases": {},
        "workspace_phases_deployment": {},
        "state_history": [],
        "retry_count": 0,
        "max_retries": 3,
    })


_SAMPLE_PLANNING_DATA = type("PlanningResult", (), {
    "phase_count": 2,
    "phases": [
        type("PhaseInfo", (), {"number": 1, "name": "Deploy", "estimated_commits": 1})(),
        type("PhaseInfo", (), {"number": 2, "name": "Verify", "estimated_commits": 1})(),
    ],
    "plan_file_path": "./specflow/e2e-test-plan.md",
})()


_SAMPLE_PLANNING_DICT = {
    "phase_count": 2,
    "phases": [
        {"number": 1, "name": "Deploy", "estimated_commits": 1},
        {"number": 2, "name": "Verify", "estimated_commits": 1},
    ],
    "plan_file_path": "./specflow/e2e-test-plan.md",
}


class TestUpdateDeploymentPlanningData:
    @pytest.mark.asyncio
    async def test_initializes_workspace_phases_deployment(self, generation_session_service, db):
        est_id = await generation_session_service.create_generation_session(
            user_email="u@example.com", parameters={}
        )
        db.update(COL_GENERATION_SESSIONS, est_id, {
            "status": "running",
            "workspace_ids": ["ws-1", "ws-2"],
            "state_history": [],
        })

        await generation_session_service.update_deployment_planning_data(
            est_id, planning_data=_SAMPLE_PLANNING_DATA, total_phases=2
        )

        doc = db.get(COL_GENERATION_SESSIONS, est_id)
        phases = doc["workspace_phases_deployment"]
        assert set(phases.keys()) == {"ws-1", "ws-2"}
        for ws_data in phases.values():
            assert ws_data["last_completed_phase"] == 0
            assert ws_data["total_phases"] == 2

    @pytest.mark.asyncio
    async def test_noop_if_already_initialized(self, generation_session_service, db):
        """Second call must not reset progress (retry safety)."""
        est_id = await generation_session_service.create_generation_session(
            user_email="u@example.com", parameters={}
        )
        db.update(COL_GENERATION_SESSIONS, est_id, {
            "status": "running",
            "workspace_ids": ["ws-1"],
            "workspace_phases_deployment": {
                "ws-1": {"last_completed_phase": 1, "total_phases": 2, "planning_data": _SAMPLE_PLANNING_DICT}
            },
            "state_history": [],
        })

        await generation_session_service.update_deployment_planning_data(
            est_id, planning_data=_SAMPLE_PLANNING_DATA, total_phases=2
        )

        doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert doc["workspace_phases_deployment"]["ws-1"]["last_completed_phase"] == 1

    @pytest.mark.asyncio
    async def test_raises_if_generation_not_found(self, generation_session_service):
        with pytest.raises(SMInvalidGenerationSessionStateError):
            await generation_session_service.update_deployment_planning_data(
                "nonexistent", planning_data=_SAMPLE_PLANNING_DATA, total_phases=2
            )

    @pytest.mark.asyncio
    async def test_noop_if_no_workspace_ids(self, generation_session_service, db, caplog):
        est_id = await generation_session_service.create_generation_session(
            user_email="u@example.com", parameters={}
        )
        db.update(COL_GENERATION_SESSIONS, est_id, {
            "status": "running",
            "workspace_ids": [],
            "state_history": [],
        })

        with caplog.at_level(logging.WARNING):
            await generation_session_service.update_deployment_planning_data(
                est_id, planning_data=_SAMPLE_PLANNING_DATA, total_phases=2
            )

        assert any("workspace_ids" in r.message for r in caplog.records)


class TestUpdateDeploymentWorkspacePhase:
    @pytest.mark.asyncio
    async def test_records_deploy_phase(self, generation_session_service, db):
        est_id = await generation_session_service.create_generation_session(
            user_email="u@example.com", parameters={}
        )
        db.update(COL_GENERATION_SESSIONS, est_id, {
            "status": "running",
            "workspace_ids": ["ws-1"],
            "workspace_phases_deployment": {
                "ws-1": {"last_completed_phase": 0, "total_phases": 2}
            },
            "state_history": [],
        })

        await generation_session_service.update_deployment_workspace_phase(
            est_id, workspace_id="ws-1", completed_phase=1
        )

        doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert doc["workspace_phases_deployment"]["ws-1"]["last_completed_phase"] == 1

    @pytest.mark.asyncio
    async def test_raises_if_generation_not_found(self, generation_session_service):
        with pytest.raises(SMInvalidGenerationSessionStateError):
            await generation_session_service.update_deployment_workspace_phase(
                "nonexistent", workspace_id="ws-1", completed_phase=1
            )


class TestAddAgentQueryTotals:
    @pytest.mark.asyncio
    async def test_accumulates_in_transaction(self, generation_session_service, db):
        est_id = await generation_session_service.create_generation_session(
            user_email="u@example.com", parameters={}
        )
        await generation_session_service.add_agent_query_token_usage(
            est_id,
            "planning",
            "ws-1",
            ModelTokenUsage(
                model_name="anthropic/claude-1",
                num_turns=3,
                input_tokens=60,
                output_tokens=30,
                cache_write_tokens=5,
                cache_read_tokens=5,
            ),
            cost_usd=0.012,
        )
        await generation_session_service.add_agent_query_token_usage(
            est_id,
            "planning",
            "ws-1",
            ModelTokenUsage(
                model_name="anthropic/claude-1",
                num_turns=2,
                input_tokens=30,
                output_tokens=15,
                cache_write_tokens=3,
                cache_read_tokens=2,
            ),
            cost_usd=0.008,
        )
        doc = db.get(COL_GENERATION_SESSIONS, est_id)
        mu = doc["model_usage"]
        assert mu["num_turns"] == 5
        assert mu["input_tokens"] == 90
        assert mu["output_tokens"] == 45
        assert mu["cache_write_tokens"] == 8
        assert mu["cache_read_tokens"] == 7
        assert mu["input_tokens"] + mu["output_tokens"] + mu["cache_write_tokens"] + mu["cache_read_tokens"] == 150
        assert abs(doc["total_usd_cost"] - 0.02) < 1e-9
        tree = doc[WORKFLOW_USAGE_METRICS_FIELD]
        mrow = tree["planning"]["workspaces"]["ws-1"]["models"]["anthropic/claude-1"]
        assert mrow["num_turns"] == 5

        sub_rows = db.list_subcollection(
            COL_GENERATION_SESSIONS, est_id, WORKSPACE_MODEL_USAGE_SUBCOLLECTION
        )
        assert len(sub_rows) == 1
        ws_row = sub_rows[0]
        assert ws_row["_id"] == "ws-1"
        assert abs(ws_row["total_usd_cost"] - 0.02) < 1e-9
        assert ws_row["input_tokens"] == 90

    @pytest.mark.asyncio
    async def test_skips_noop_deltas(self, generation_session_service: GenerationSessionService, db):
        est_id = await generation_session_service.create_generation_session(
            user_email="u@example.com", parameters={}
        )
        await generation_session_service.add_agent_query_token_usage(
            est_id, "planning", "ws-1", ModelTokenUsage(model_name="anthropic/claude-1")
        )
        doc = db.get(COL_GENERATION_SESSIONS, est_id)
        assert "model_usage" not in doc
        assert WORKFLOW_USAGE_METRICS_FIELD not in doc
