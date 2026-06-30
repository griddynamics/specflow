"""
Tests for scheduled_wipe.py (Job 4).
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from app.jobs.scheduled_wipe import schedule_expired_workspace_wipes, execute_confirmed_wipes
from app.schemas.generation_workflow_enums import WorkspaceStatus, GenerationStatus


# ---------------------------------------------------------------------------
# Tests for schedule_expired_workspace_wipes (Pass 1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allocated_workspace_failed_generation_old_gets_scheduled(db):
    """ALLOCATED workspace + FAILED generation + failed_at > 7 days → scheduled_for_wipe=True."""
    now = datetime.now(timezone.utc)
    old_failed_at = now - timedelta(days=10)

    db.seed_generation_session("est-old-fail", {
        "status": GenerationStatus.FAILED,
        "failed_at": old_failed_at,
    })
    db.seed_workspace("ws-001", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-old-fail",
        "scheduled_for_wipe": False,
    })

    await schedule_expired_workspace_wipes(db)

    ws = db.get_workspace_data("ws-001")
    assert ws["scheduled_for_wipe"] is True
    assert ws["scheduled_for_wipe_at"] is not None
    # Wipe time should be ~24h in the future
    assert ws["scheduled_for_wipe_at"] > now


@pytest.mark.asyncio
async def test_allocated_workspace_failed_generation_recent_not_scheduled(db):
    """ALLOCATED workspace + FAILED generation + failed_at < 7 days → not scheduled."""
    now = datetime.now(timezone.utc)
    recent_failed_at = now - timedelta(days=2)

    db.seed_generation_session("est-recent-fail", {
        "status": GenerationStatus.FAILED,
        "failed_at": recent_failed_at,
    })
    db.seed_workspace("ws-001", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-recent-fail",
        "scheduled_for_wipe": False,
    })

    await schedule_expired_workspace_wipes(db)

    ws = db.get_workspace_data("ws-001")
    assert ws.get("scheduled_for_wipe") is False


@pytest.mark.asyncio
async def test_allocated_workspace_running_generation_not_scheduled(db):
    """ALLOCATED workspace + RUNNING generation → not scheduled."""
    db.seed_generation_session("est-running", {
        "status": GenerationStatus.RUNNING,
    })
    db.seed_workspace("ws-001", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-running",
        "scheduled_for_wipe": False,
    })

    await schedule_expired_workspace_wipes(db)

    ws = db.get_workspace_data("ws-001")
    assert ws.get("scheduled_for_wipe") is False


@pytest.mark.asyncio
async def test_already_scheduled_workspace_not_scheduled_again(db):
    """Already scheduled workspace (scheduled_for_wipe=True) → not re-scheduled."""
    now = datetime.now(timezone.utc)
    old_failed_at = now - timedelta(days=10)
    existing_wipe_at = now + timedelta(hours=20)

    db.seed_generation_session("est-fail", {
        "status": GenerationStatus.FAILED,
        "failed_at": old_failed_at,
    })
    db.seed_workspace("ws-001", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-fail",
        "scheduled_for_wipe": True,  # Already scheduled
        "scheduled_for_wipe_at": existing_wipe_at,
    })

    await schedule_expired_workspace_wipes(db)

    ws = db.get_workspace_data("ws-001")
    # Should NOT be re-scheduled — query filters out already-scheduled
    assert ws["scheduled_for_wipe_at"] == existing_wipe_at


@pytest.mark.asyncio
async def test_no_generation_for_workspace_skipped(db):
    """Workspace with locked_by pointing to non-existent generation → skipped."""
    db.seed_workspace("ws-orphan", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-nonexistent",
        "scheduled_for_wipe": False,
    })

    # Should not raise
    await schedule_expired_workspace_wipes(db)

    ws = db.get_workspace_data("ws-orphan")
    assert ws.get("scheduled_for_wipe") is False


@pytest.mark.asyncio
async def test_workspace_without_locked_by_skipped(db):
    """Workspace with no locked_by → skipped."""
    db.seed_workspace("ws-no-lock", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": None,
        "scheduled_for_wipe": False,
    })

    await schedule_expired_workspace_wipes(db)

    ws = db.get_workspace_data("ws-no-lock")
    assert ws.get("scheduled_for_wipe") is False


@pytest.mark.asyncio
async def test_notification_sent_on_schedule(db):
    """Notification service is called when wipe is scheduled."""
    now = datetime.now(timezone.utc)
    old_failed_at = now - timedelta(days=10)

    db.seed_generation_session("est-fail", {
        "status": GenerationStatus.FAILED,
        "failed_at": old_failed_at,
    })
    db.seed_workspace("ws-001", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-fail",
        "scheduled_for_wipe": False,
    })

    mock_notif = AsyncMock()
    mock_notif.send_wipe_warning = AsyncMock()

    await schedule_expired_workspace_wipes(db, notification_service=mock_notif)

    mock_notif.send_wipe_warning.assert_called_once()
    call_kwargs = mock_notif.send_wipe_warning.call_args.kwargs
    assert call_kwargs["generation_id"] == "est-fail"
    assert call_kwargs["workspace_id"] == "ws-001"


# ---------------------------------------------------------------------------
# Tests for execute_confirmed_wipes (Pass 2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_confirmed_wipe_past_window(db):
    """Workspace with past wipe_at → execute_scheduled_wipe called → CLEANING."""
    now = datetime.now(timezone.utc)
    past_wipe_at = now - timedelta(hours=1)

    db.seed_workspace("ws-wipe", {
        "status": WorkspaceStatus.ALLOCATED,
        "scheduled_for_wipe": True,
        "scheduled_for_wipe_at": past_wipe_at,
    })

    await execute_confirmed_wipes(db)

    ws = db.get_workspace_data("ws-wipe")
    assert ws["status"] == WorkspaceStatus.CLEANING
    assert ws["scheduled_for_wipe"] is False


@pytest.mark.asyncio
async def test_execute_confirmed_wipe_future_window_not_touched(db):
    """Workspace with future wipe_at → not touched."""
    now = datetime.now(timezone.utc)
    future_wipe_at = now + timedelta(hours=12)

    db.seed_workspace("ws-future", {
        "status": WorkspaceStatus.ALLOCATED,
        "scheduled_for_wipe": True,
        "scheduled_for_wipe_at": future_wipe_at,
    })

    await execute_confirmed_wipes(db)

    ws = db.get_workspace_data("ws-future")
    assert ws["status"] == WorkspaceStatus.ALLOCATED
    assert ws["scheduled_for_wipe"] is True


@pytest.mark.asyncio
async def test_execute_confirmed_wipe_not_scheduled_not_touched(db):
    """Workspace with scheduled_for_wipe=False → not in query results, not touched."""
    now = datetime.now(timezone.utc)
    past_wipe_at = now - timedelta(hours=1)

    db.seed_workspace("ws-not-scheduled", {
        "status": WorkspaceStatus.ALLOCATED,
        "scheduled_for_wipe": False,
        "scheduled_for_wipe_at": past_wipe_at,
    })

    await execute_confirmed_wipes(db)

    ws = db.get_workspace_data("ws-not-scheduled")
    assert ws["status"] == WorkspaceStatus.ALLOCATED
