"""
Tests for stuck_running_detector.py (Job 1).

Critical test: job does NOT touch workspace (Invariant 4).
"""
import pytest
from datetime import datetime, timedelta, timezone

from app.jobs.stuck_running_detector import detect_stuck_running
from app.schemas.generation_workflow_enums import GenerationStatus, WorkspaceStatus
from app.state.db_adapter import COL_GENERATION_SESSIONS


@pytest.mark.asyncio
async def test_stale_running_generation_marked_failed(db):
    """Generation RUNNING with old last_activity_at → stuck_detected → FAILED."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=60)

    db.seed_generation_session("est-stale", {
        "status": GenerationStatus.RUNNING,
        "last_activity_at": stale_time,
        "state_history": [],
    })

    await detect_stuck_running(db, threshold_minutes=30)

    est = db.get_generation_session_data("est-stale")
    assert est["status"] == GenerationStatus.FAILED
    assert est["failed_at"] is not None


@pytest.mark.asyncio
async def test_fresh_running_generation_not_touched(db):
    """Generation RUNNING with recent last_activity_at → not touched."""
    now = datetime.now(timezone.utc)
    fresh_time = now - timedelta(minutes=5)

    db.seed_generation_session("est-fresh", {
        "status": GenerationStatus.RUNNING,
        "last_activity_at": fresh_time,
        "state_history": [],
    })

    await detect_stuck_running(db, threshold_minutes=30)

    est = db.get_generation_session_data("est-fresh")
    assert est["status"] == GenerationStatus.RUNNING


@pytest.mark.asyncio
async def test_already_failed_generation_skipped_without_error(db):
    """Already FAILED generation → job skips it, no error raised."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=60)

    db.seed_generation_session("est-already-failed", {
        "status": GenerationStatus.FAILED,
        "last_activity_at": stale_time,
        "state_history": [],
    })

    # Should not raise
    await detect_stuck_running(db, threshold_minutes=30)

    # Status unchanged
    est = db.get_generation_session_data("est-already-failed")
    assert est["status"] == GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_does_not_touch_workspace(db):
    """Critical Invariant 4: job does not write to any workspace document."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=60)

    db.seed_generation_session("est-ws", {
        "status": GenerationStatus.RUNNING,
        "last_activity_at": stale_time,
        "workspace_ids": ["ws-001", "ws-002"],
        "state_history": [],
    })
    db.seed_workspace("ws-001", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-ws",
    })
    db.seed_workspace("ws-002", {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-ws",
    })

    await detect_stuck_running(db, threshold_minutes=30)

    # Generation marked FAILED
    est = db.get_generation_session_data("est-ws")
    assert est["status"] == GenerationStatus.FAILED

    # Workspaces untouched — still ALLOCATED (Invariant 4)
    ws1 = db.get_workspace_data("ws-001")
    ws2 = db.get_workspace_data("ws-002")
    assert ws1["status"] == WorkspaceStatus.ALLOCATED
    assert ws2["status"] == WorkspaceStatus.ALLOCATED


@pytest.mark.asyncio
async def test_multiple_stale_generations_all_marked_failed(db):
    """Multiple stale RUNNING generations → all marked FAILED."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=60)

    for i in range(3):
        db.seed_generation_session(f"est-{i}", {
            "status": GenerationStatus.RUNNING,
            "last_activity_at": stale_time,
            "state_history": [],
        })

    await detect_stuck_running(db, threshold_minutes=30)

    for i in range(3):
        est = db.get_generation_session_data(f"est-{i}")
        assert est["status"] == GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_state_history_entry_added(db):
    """stuck_detected appends a state_history entry with triggered_by."""
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=60)

    db.seed_generation_session("est-hist", {
        "status": GenerationStatus.RUNNING,
        "last_activity_at": stale_time,
        "state_history": [],
    })

    await detect_stuck_running(db, threshold_minutes=30)

    est = db.get_generation_session_data("est-hist")
    history = est.get("state_history", [])
    assert len(history) >= 1
    last = history[-1]
    assert last["triggered_by"] == "job:stuck_running_detector"


@pytest.mark.asyncio
async def test_generation_without_last_activity_at_not_queried(db):
    """Generation with None last_activity_at is excluded by query filter."""
    db.seed_generation_session("est-no-activity", {
        "status": GenerationStatus.RUNNING,
        "last_activity_at": None,
        "state_history": [],
    })

    # JobFakeDB._matches returns False for None < cutoff, so this generation
    # won't be included in query results
    await detect_stuck_running(db, threshold_minutes=30)

    est = db.get_generation_session_data("est-no-activity")
    # Not modified — query filter excluded it (None < datetime is False)
    assert est["status"] == GenerationStatus.RUNNING


# ---------------------------------------------------------------------------
# Phase 6: stuck detection fires for generation stuck in deploy phase
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stuck_in_deploy_phase_detected_and_failed(db):
    """
    Phase 6: An generation that is RUNNING with checkpoint=DEPLOY_AND_E2E_DONE
    but has stale last_activity_at must be detected as stuck and marked FAILED.
    Stuck detection is checkpoint-agnostic — any stale RUNNING generation is caught.
    """
    from app.schemas.generation_workflow_enums import GenerationCheckpoint
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=90)

    db.seed_generation_session("est-stuck-deploy", {
        "status": GenerationStatus.RUNNING,
        "checkpoint": GenerationCheckpoint.DEPLOY_AND_E2E_DONE,
        "last_activity_at": stale_time,
        "state_history": [],
    })

    await detect_stuck_running(db, threshold_minutes=30)

    est = db.get_generation_session_data("est-stuck-deploy")
    assert est["status"] == GenerationStatus.FAILED, (
        "Generation stuck in deploy phase must be detected and marked FAILED"
    )
    assert est.get("failed_at") is not None


# ---------------------------------------------------------------------------
# Bug fixes: lock release and last_activity_at refresh
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_key_lock_released_on_stuck_detection(db):
    """
    Bug fix: release_locks_for_generation_session must succeed when the generation holds
    an API key lock. Previously crashed with AttributeError because the db passed
    to the job lacked query_collection.
    """
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=60)

    db.seed_generation_session("est-lock", {
        "status": GenerationStatus.RUNNING,
        "last_activity_at": stale_time,
        "state_history": [],
        "key_uid": "kuid-lock",
    })
    db.seed_api_key("key-abc", {
        "api_key": "key-abc",
        "key_uid": "kuid-lock",
        "max_concurrent_sessions": 5,
        "active_generation_sessions": [
            {
                "generation_id": "est-lock",
                "operation": "generation",
                "lease_started_at": now - timedelta(minutes=5),
                "lease_ttl_minutes": 720,
            }
        ],
    })

    await detect_stuck_running(db, threshold_minutes=30)

    est = db.get_generation_session_data("est-lock")
    assert est["status"] == GenerationStatus.FAILED

    key = db.get_api_key_data("key-abc")
    assert key.get("active_generation_sessions") == []


@pytest.mark.asyncio
async def test_lock_released_via_adapter_update_api_key():
    """
    Regression: detect_stuck_running must release API key locks when db is
    StateMachineDBAdapter (the production setup from main.py).

    Uses InMemoryDatabase + StateMachineDBAdapter directly — JobFakeDB would
    mask a missing update_api_key on the adapter.
    """
    from datetime import datetime, timezone, timedelta
    from app.database.memory import InMemoryDatabase
    from app.state.db_adapter import StateMachineDBAdapter
    from app.schemas.generation_workflow_enums import GenerationStatus

    raw_db = InMemoryDatabase()
    adapter = StateMachineDBAdapter(raw_db)

    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(minutes=60)

    raw_db.set(COL_GENERATION_SESSIONS, "est-raw", {
        "status": GenerationStatus.RUNNING,
        "last_activity_at": stale_time,
        "state_history": [],
        "key_uid": "kuid-raw",
    })
    raw_db.set("api_keys", "key-raw", {
        "api_key": "key-raw",
        "key_uid": "kuid-raw",
        "max_concurrent_sessions": 5,
        "active_generation_sessions": [
            {
                "generation_id": "est-raw",
                "operation": "generation",
                "lease_started_at": now - timedelta(minutes=5),
                "lease_ttl_minutes": 720,
            }
        ],
    })

    # mirrors main.py: adapter is the sole db argument
    await detect_stuck_running(adapter, threshold_minutes=30)

    est = raw_db.get(COL_GENERATION_SESSIONS, "est-raw")
    assert est["status"] == GenerationStatus.FAILED

    key = raw_db.get("api_keys", "key-raw")
    assert key.get("active_generation_sessions") == []


@pytest.mark.asyncio
async def test_last_activity_at_refreshed_on_retry(db):
    """
    Bug fix: allocation_succeeded must set last_activity_at to now so that a
    retried generation with a stale timestamp is not immediately killed by the
    stuck_running_detector.
    """
    from datetime import datetime, timezone, timedelta
    from app.state import GenerationSessionStateMachine
    from app.schemas.generation_workflow_enums import GenerationStatus

    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(days=3)

    db.seed_generation_session("est-retry", {
        "status": GenerationStatus.INITIALIZING,
        "last_activity_at": stale_time,
        "state_history": [],
    })

    esm = GenerationSessionStateMachine(db)
    await esm.allocation_succeeded("est-retry", triggered_by="api:start_generation_session")

    est = db.get_generation_session_data("est-retry")
    assert est["status"] == GenerationStatus.RUNNING

    # last_activity_at must now be recent — within the last 5 seconds
    last_activity = est.get("last_activity_at")
    assert last_activity is not None
    age_seconds = (now - last_activity).total_seconds()
    assert age_seconds < 5, f"last_activity_at not refreshed: age={age_seconds}s"
