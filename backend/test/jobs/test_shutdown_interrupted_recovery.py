"""Tests for the boot-time shutdown-interrupted recovery job.

rerun_generation_session and asyncio.create_task are mocked: this job's job is
*selection* and *atomic reset*, not running the workflow (covered elsewhere).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.jobs import shutdown_interrupted_recovery as job_mod
from app.jobs.shutdown_interrupted_recovery import recover_interrupted_sessions
from app.schemas.generation_workflow_enums import GenerationStatus, WorkspaceStatus
from app.state.transitions import TriggeredBy


def _seed_interrupted(db, gid, *, failed_minutes_ago=5, workspace_ids=None, code_archived=False):
    failed_at = datetime.now(timezone.utc) - timedelta(minutes=failed_minutes_ago)
    db.seed_generation_session(gid, {
        "status": GenerationStatus.FAILED,
        "shutdown_interrupted": True,
        "failed_at": failed_at,
        "retry_count": 0,
        "max_retries": 3,
        "code_archived": code_archived,
        "workspace_ids": workspace_ids or [],
        "state_history": [],
    })


def _seed_allocated_ws(db, gid, ws_ids):
    for ws in ws_ids:
        db.seed_workspace(ws, {
            "status": WorkspaceStatus.ALLOCATED.value,
            "locked_by": gid,
        })


@pytest.fixture
def gss():
    return MagicMock()


@pytest.fixture(autouse=True)
def _patch_spawn():
    """Mock the runner + create_task so nothing actually executes a workflow."""
    with patch.object(job_mod, "rerun_generation_session", MagicMock(name="rerun")) as rerun, \
         patch.object(job_mod.asyncio, "create_task", MagicMock(name="create_task")) as create_task:
        yield rerun, create_task


@pytest.fixture(autouse=True)
def notify_mock():
    """Capture the reboot Slack notification (always patched so to_thread is a no-op)."""
    with patch.object(job_mod.notifications, "notify", MagicMock(name="notify")) as notify:
        yield notify


@pytest.mark.asyncio
async def test_recovers_marked_session_within_window(db, gss, _patch_spawn, notify_mock):
    rerun, create_task = _patch_spawn
    _seed_interrupted(db, "gen-1", workspace_ids=["ws-1"])
    _seed_allocated_ws(db, "gen-1", ["ws-1"])

    recovered = await recover_interrupted_sessions(db, gss)

    assert recovered == ["gen-1"]
    # atomic reset happened: FAILED → PENDING, marker cleared, retry_count incremented
    data = db.get_generation_session_data("gen-1")
    assert data["status"] == GenerationStatus.PENDING
    assert data["shutdown_interrupted"] is False
    assert data["retry_count"] == 1
    rerun.assert_called_once()
    assert rerun.call_args.args[0] == "gen-1"
    create_task.assert_called_once()
    # one reboot Slack notification naming the recovered session
    notify_mock.assert_called_once()
    assert "gen-1" in notify_mock.call_args.args[0]


@pytest.mark.asyncio
async def test_no_notification_when_nothing_recovered(db, gss, notify_mock):
    """A genuine FAILED session (no marker) → nothing recovered → no Slack."""
    db.seed_generation_session("gen-real", {
        "status": GenerationStatus.FAILED,
        "failed_at": datetime.now(timezone.utc),
        "state_history": [],
    })
    recovered = await recover_interrupted_sessions(db, gss)
    assert recovered == []
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ignores_genuine_failure_without_marker(db, gss, _patch_spawn):
    rerun, _ = _patch_spawn
    db.seed_generation_session("gen-real", {
        "status": GenerationStatus.FAILED,
        "failed_at": datetime.now(timezone.utc),
        "state_history": [],
    })
    recovered = await recover_interrupted_sessions(db, gss)
    assert recovered == []
    rerun.assert_not_called()
    # untouched — still FAILED
    assert db.get_generation_session_data("gen-real")["status"] == GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_ignores_marked_session_outside_window(db, gss, _patch_spawn):
    rerun, _ = _patch_spawn
    _seed_interrupted(db, "gen-old", failed_minutes_ago=120, workspace_ids=["ws-1"])
    _seed_allocated_ws(db, "gen-old", ["ws-1"])

    recovered = await recover_interrupted_sessions(db, gss, window_minutes=30)

    assert recovered == []
    rerun.assert_not_called()
    assert db.get_generation_session_data("gen-old")["status"] == GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_ignores_cancelled_session(db, gss, _patch_spawn):
    """A user-cancelled session must never be resumed on boot recovery."""
    rerun, _ = _patch_spawn
    db.seed_generation_session("gen-cancelled", {
        "status": GenerationStatus.CANCELLED,
        "cancelled_by_user": True,
        "cancelled_at": datetime.now(timezone.utc),
        "state_history": [],
    })
    recovered = await recover_interrupted_sessions(db, gss)
    assert recovered == []
    rerun.assert_not_called()
    assert db.get_generation_session_data("gen-cancelled")["status"] == GenerationStatus.CANCELLED


@pytest.mark.asyncio
async def test_non_failed_marked_session_ignored(db, gss, _patch_spawn):
    """A PENDING session carrying a stale marker must not be re-fired (only FAILED)."""
    rerun, _ = _patch_spawn
    db.seed_generation_session("gen-pending", {
        "status": GenerationStatus.PENDING,
        "shutdown_interrupted": True,
        "failed_at": datetime.now(timezone.utc),
        "state_history": [],
    })
    recovered = await recover_interrupted_sessions(db, gss)
    assert recovered == []
    rerun.assert_not_called()


@pytest.mark.asyncio
async def test_max_retries_exhausted_left_failed(db, gss, _patch_spawn):
    rerun, _ = _patch_spawn
    _seed_interrupted(db, "gen-max", workspace_ids=["ws-1"])
    _seed_allocated_ws(db, "gen-max", ["ws-1"])
    # already at the limit
    db.update("generation_sessions", "gen-max", {"retry_count": 3, "max_retries": 3})

    recovered = await recover_interrupted_sessions(db, gss)

    assert recovered == []
    rerun.assert_not_called()
    assert db.get_generation_session_data("gen-max")["status"] == GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_non_reusable_workspaces_skipped(db, gss, _patch_spawn):
    """HR-2 / Commandment V: never fresh-allocate over un-archived code."""
    rerun, _ = _patch_spawn
    _seed_interrupted(db, "gen-ws", workspace_ids=["ws-1"])
    # workspace is CLEANING, not ALLOCATED → not reusable
    db.seed_workspace("ws-1", {"status": WorkspaceStatus.CLEANING.value, "locked_by": "gen-ws"})

    recovered = await recover_interrupted_sessions(db, gss)

    assert recovered == []
    rerun.assert_not_called()
    # left FAILED for manual handling
    assert db.get_generation_session_data("gen-ws")["status"] == GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_disabled_by_config(db, gss, _patch_spawn):
    rerun, _ = _patch_spawn
    _seed_interrupted(db, "gen-1", workspace_ids=["ws-1"])
    _seed_allocated_ws(db, "gen-1", ["ws-1"])

    with patch.object(job_mod.settings, "AUTO_RECOVER_INTERRUPTED_SESSIONS", False):
        recovered = await recover_interrupted_sessions(db, gss)

    assert recovered == []
    rerun.assert_not_called()
    assert db.get_generation_session_data("gen-1")["status"] == GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_uses_shutdown_recovery_triggered_by(db, gss, _patch_spawn):
    _seed_interrupted(db, "gen-1", workspace_ids=["ws-1"])
    _seed_allocated_ws(db, "gen-1", ["ws-1"])

    await recover_interrupted_sessions(db, gss)

    history = db.get_generation_session_data("gen-1")["state_history"]
    assert history[-1]["triggered_by"] == TriggeredBy.SHUTDOWN_RECOVERY
