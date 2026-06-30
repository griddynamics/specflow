"""
Tests for stuck_cleaning_recovery.py (Job 3).

Critical tests:
- ALLOCATED workspace is never touched (Invariants 3, 4)
- Only CLEANING workspaces with old cleaning_started_at are transitioned
- When workspace_pool_service is provided, cleanup_workspace() is called
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from app.jobs.stuck_cleaning_recovery import recover_stuck_cleaning
from app.schemas.generation_workflow_enums import WorkspaceStatus


@pytest.mark.asyncio
async def test_stale_cleaning_workspace_transitioned_to_stuck_then_cleaning(db):
    """Stale CLEANING workspace → mark_stuck → begin_recovery → back to CLEANING."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(hours=5)

    db.seed_workspace("ws-stale", {
        "status": WorkspaceStatus.CLEANING,
        "cleaning_started_at": stale_time,
    })

    await recover_stuck_cleaning(db, threshold_hours=2)

    ws = db.get_workspace_data("ws-stale")
    # After mark_stuck then begin_recovery, ends up back in CLEANING
    assert ws["status"] == WorkspaceStatus.CLEANING


@pytest.mark.asyncio
async def test_fresh_cleaning_workspace_not_touched(db):
    """Fresh CLEANING workspace → not touched."""
    now = datetime.now(timezone.utc)
    fresh_time = now - timedelta(minutes=30)

    db.seed_workspace("ws-fresh", {
        "status": WorkspaceStatus.CLEANING,
        "cleaning_started_at": fresh_time,
    })

    await recover_stuck_cleaning(db, threshold_hours=2)

    ws = db.get_workspace_data("ws-fresh")
    assert ws["status"] == WorkspaceStatus.CLEANING


@pytest.mark.asyncio
async def test_allocated_workspace_never_touched(db):
    """Critical Invariants 3 & 4: ALLOCATED workspace is never touched."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(hours=5)

    db.seed_workspace("ws-allocated", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-001",
        "cleaning_started_at": stale_time,  # Even with old timestamp
    })

    await recover_stuck_cleaning(db, threshold_hours=2)

    ws = db.get_workspace_data("ws-allocated")
    # ALLOCATED workspace must remain untouched
    assert ws["status"] == WorkspaceStatus.ALLOCATED
    assert ws["locked_by"] == "est-001"


@pytest.mark.asyncio
async def test_stuck_workspace_not_touched(db):
    """Already STUCK workspace → not matched by query (status != CLEANING), skipped."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(hours=5)

    db.seed_workspace("ws-stuck", {
        "status": WorkspaceStatus.STUCK,
        "cleaning_started_at": stale_time,
    })

    await recover_stuck_cleaning(db, threshold_hours=2)

    ws = db.get_workspace_data("ws-stuck")
    assert ws["status"] == WorkspaceStatus.STUCK  # Unchanged


@pytest.mark.asyncio
async def test_available_workspace_not_touched(db):
    """AVAILABLE workspace → not matched by query, not touched."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(hours=5)

    db.seed_workspace("ws-avail", {
        "status": WorkspaceStatus.AVAILABLE,
        "cleaning_started_at": stale_time,
    })

    await recover_stuck_cleaning(db, threshold_hours=2)

    ws = db.get_workspace_data("ws-avail")
    assert ws["status"] == WorkspaceStatus.AVAILABLE


@pytest.mark.asyncio
async def test_multiple_stale_cleaning_workspaces(db):
    """Multiple stale CLEANING workspaces → all recovered."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(hours=5)

    for i in range(3):
        db.seed_workspace(f"ws-{i}", {
            "status": WorkspaceStatus.CLEANING,
            "cleaning_started_at": stale_time,
        })

    await recover_stuck_cleaning(db, threshold_hours=2)

    for i in range(3):
        ws = db.get_workspace_data(f"ws-{i}")
        # After mark_stuck + begin_recovery, each ends up in CLEANING again
        assert ws["status"] == WorkspaceStatus.CLEANING


@pytest.mark.asyncio
async def test_cleaning_without_timestamp_immediately_recovered(db):
    """
    CLEANING workspace with None cleaning_started_at → treated as legacy stuck,
    recovered immediately.
    
    This handles workspaces that transitioned to CLEANING before cleaning_started_at
    was properly set in all transition paths.
    """
    db.seed_workspace("ws-no-ts", {
        "status": WorkspaceStatus.CLEANING,
        "cleaning_started_at": None,
    })

    await recover_stuck_cleaning(db, threshold_hours=2)

    ws = db.get_workspace_data("ws-no-ts")
    # Should be recovered: mark_stuck → begin_recovery → back to CLEANING
    # (but now with cleaning_started_at set by begin_recovery)
    assert ws["status"] == WorkspaceStatus.CLEANING
    assert ws["cleaning_started_at"] is not None  # Now set by begin_recovery


class TestWithWorkspacePoolService:
    """Tests for when workspace_pool_service is provided."""

    @pytest.mark.asyncio
    async def test_cleanup_called_after_begin_recovery(self, db):
        """When service provided, cleanup_workspace() is called after begin_recovery."""
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(hours=5)

        db.seed_workspace("ws-stale", {
            "status": WorkspaceStatus.CLEANING,
            "cleaning_started_at": stale_time,
        })

        pool_service = MagicMock()
        pool_service.cleanup_workspace = AsyncMock()

        await recover_stuck_cleaning(db, workspace_pool_service=pool_service, threshold_hours=2)

        pool_service.cleanup_workspace.assert_called_once_with("ws-stale")

    @pytest.mark.asyncio
    async def test_cleanup_failure_does_not_abort_other_workspaces(self, db):
        """cleanup_workspace() failure for one workspace does not prevent others from running."""
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(hours=5)

        for i in range(3):
            db.seed_workspace(f"ws-{i}", {
                "status": WorkspaceStatus.CLEANING,
                "cleaning_started_at": stale_time,
            })

        pool_service = MagicMock()
        # First call raises, remaining calls succeed
        pool_service.cleanup_workspace = AsyncMock(
            side_effect=[Exception("git error"), None, None]
        )

        # Should not raise
        await recover_stuck_cleaning(db, workspace_pool_service=pool_service, threshold_hours=2)

        assert pool_service.cleanup_workspace.call_count == 3

    @pytest.mark.asyncio
    async def test_no_service_no_cleanup_called(self, db):
        """Without workspace_pool_service, state is reset but cleanup is not attempted."""
        now = datetime.now(timezone.utc)
        stale_time = now - timedelta(hours=5)

        db.seed_workspace("ws-stale", {
            "status": WorkspaceStatus.CLEANING,
            "cleaning_started_at": stale_time,
        })

        # No service provided — should not raise, workspace ends up back in CLEANING
        await recover_stuck_cleaning(db, threshold_hours=2)

        ws = db.get_workspace_data("ws-stale")
        assert ws["status"] == WorkspaceStatus.CLEANING
