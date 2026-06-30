"""Tests for the graceful-shutdown handler (mark in-flight sessions FAILED + Slack).

Half A now finds in-flight sessions by querying Firestore (no in-process registry), so these
run against a real StateMachineDBAdapter over an InMemoryDatabase.
"""
from unittest.mock import patch

import pytest

from app.database.memory import InMemoryDatabase
from app.services import shutdown_handler
from app.services.shutdown_handler import mark_active_sessions_interrupted
from app.schemas.generation_workflow_enums import GenerationStatus
from app.state.db_adapter import COL_GENERATION_SESSIONS, StateMachineDBAdapter


@pytest.fixture
def db():
    return StateMachineDBAdapter(InMemoryDatabase())


def _seed(db, gid, status, **extra):
    db._db.set(COL_GENERATION_SESSIONS, gid, {
        "status": status,
        "state_history": [],
        "user_email": "u@x.io",
        "parameters": {"spec_path": "specs/"},
        **extra,
    })


def _status(db, gid):
    return db._db.get(COL_GENERATION_SESSIONS, gid)["status"]


@pytest.mark.asyncio
async def test_marks_running_and_initializing_only(db):
    _seed(db, "gen-run", GenerationStatus.RUNNING)
    _seed(db, "gen-init", GenerationStatus.INITIALIZING)
    _seed(db, "gen-pending", GenerationStatus.PENDING)
    _seed(db, "gen-done", GenerationStatus.COMPLETED)
    _seed(db, "gen-failed", GenerationStatus.FAILED)

    with patch.object(shutdown_handler.notifications, "notify") as mock_notify:
        failed = await mark_active_sessions_interrupted(db)

    assert set(failed) == {"gen-run", "gen-init"}
    for gid in ("gen-run", "gen-init"):
        doc = db._db.get(COL_GENERATION_SESSIONS, gid)
        assert doc["status"] == GenerationStatus.FAILED
        assert doc["shutdown_interrupted"] is True
    # untouched
    assert _status(db, "gen-pending") == GenerationStatus.PENDING
    assert _status(db, "gen-done") == GenerationStatus.COMPLETED

    # one Slack message naming both in-flight sessions
    mock_notify.assert_called_once()
    message = mock_notify.call_args.args[0]
    assert "gen-run" in message and "gen-init" in message
    assert "gen-pending" not in message


@pytest.mark.asyncio
async def test_no_in_flight_sessions_no_slack(db):
    _seed(db, "gen-done", GenerationStatus.COMPLETED)
    with patch.object(shutdown_handler.notifications, "notify") as mock_notify:
        failed = await mark_active_sessions_interrupted(db)
    assert failed == []
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_slack_failure_is_swallowed(db):
    _seed(db, "gen-run", GenerationStatus.RUNNING)
    with patch.object(shutdown_handler.notifications, "notify", side_effect=RuntimeError("slack down")):
        failed = await mark_active_sessions_interrupted(db)
    # session still marked failed; notification error does not propagate
    assert failed == ["gen-run"]
    assert _status(db, "gen-run") == GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_does_not_release_workspaces(db):
    _seed(db, "gen-run", GenerationStatus.RUNNING, workspace_ids=["ws-1", "ws-2"])
    with patch.object(shutdown_handler.notifications, "notify"):
        await mark_active_sessions_interrupted(db)
    # Steel Commandment II — workspaces preserved
    assert db._db.get(COL_GENERATION_SESSIONS, "gen-run")["workspace_ids"] == ["ws-1", "ws-2"]
