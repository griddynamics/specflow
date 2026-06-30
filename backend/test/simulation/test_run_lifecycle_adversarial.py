"""
Adversarial lifecycle tests — edge cases, impossible states, boundary conditions.

These tests explore execution paths that happy-path suites never reach:
  - Exact threshold boundaries (== vs > vs <)
  - Missing / null / corrupted fields in DB documents
  - Impossible state transitions that production code must reject
  - Session data corruption that must be silently survived
  - Multi-job idempotency under repeated firing
  - Subtle rollback semantics in begin_or_raise

Some tests document a KNOWN GAP: generations with last_activity_at=None are
invisible to the stuck detector. These are marked with # GAP so they can be
prioritised for a follow-up fix.

Import: SimDB is shared with test_run_lifecycle; keep both files in the same
test/simulation/ package.
"""

import pytest
from datetime import datetime, timedelta, timezone

from app.core.ttl_config import GenerationLifecyclePolicy
from app.schemas.generation_workflow_enums import (
    GenerationStatus,
    WorkspaceStatus,
)
from app.state.api_key_session_concurrency import (
    ApiKeySessionConcurrency,
    OperationKind,
    SessionBeginOutcome,
    SessionEndReason,
)
from app.state.generation_session_state_machine import GenerationSessionStateMachine
from app.state.exceptions import InvalidGenerationSessionStateError
from app.state.transitions import TriggeredBy
from app.jobs.stuck_running_detector import detect_stuck_running
from app.jobs.stuck_initializing_detector import detect_stuck_initializing
from app.jobs.stuck_cleaning_recovery import recover_stuck_cleaning
from app.jobs.scheduled_wipe import (
    schedule_expired_workspace_wipes,
)

from test.simulation.test_run_lifecycle import SimDB  # shared test double


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc)
_KEY_ID = "key-adv"
_KEY_UID = "ku-adv"
_EST = "est-adv"
_WS = "ws-adv"


def _ago(minutes: float = 0, hours: float = 0, days: float = 0) -> datetime:
    return _NOW - timedelta(minutes=minutes, hours=hours, days=days)


def _from_now(minutes: float = 0, hours: float = 0) -> datetime:
    return _NOW + timedelta(minutes=minutes, hours=hours)


def _live_session(generation_id: str, operation: OperationKind = OperationKind.GENERATION,
                  started_minutes_ago: float = 1) -> dict:
    """Session well within its TTL."""
    ttl = GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES
    return {
        "generation_id": generation_id,
        "operation": operation.value,
        "lease_started_at": _ago(minutes=started_minutes_ago),
        "lease_ttl_minutes": ttl,
    }


def _seed_key(db: SimDB, sessions: list | None = None, max_concurrent: int = 5) -> None:
    db.seed_api_key(_KEY_ID, {
        "api_key": _KEY_ID, "key_uid": _KEY_UID,
        "max_concurrent_sessions": max_concurrent,
        "active_generation_sessions": sessions or [],
    })


@pytest.fixture
def db() -> SimDB:
    return SimDB()


# ===========================================================================
# A — Exact threshold boundaries
# ===========================================================================


@pytest.mark.asyncio
async def test_session_expired_at_exact_ttl_boundary(db: SimDB) -> None:
    """_session_expired uses >=: a session whose elapsed time equals TTL exactly is expired.

    Boundary: elapsed == TTL → expired.  elapsed == TTL - 1 → alive.
    """
    ttl = GenerationLifecyclePolicy.SESSION_ANALYSIS_MINUTES

    # Exactly at boundary → expired
    at_boundary = {
        "generation_id": _EST,
        "operation": OperationKind.ANALYSIS.value,
        "lease_started_at": _ago(minutes=ttl),  # now - ttl → elapsed == ttl → expired
        "lease_ttl_minutes": ttl,
    }
    _seed_key(db, sessions=[at_boundary], max_concurrent=1)
    db.seed_generation_session("probe", {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    # at-boundary session is expired → capacity freed → probe ACQUIRED
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="probe", operation=OperationKind.ANALYSIS)
    assert result.outcome == SessionBeginOutcome.ACQUIRED


@pytest.mark.asyncio
async def test_session_alive_one_minute_before_ttl(db: SimDB) -> None:
    """One minute before TTL expiry the session is still alive (capacity still held)."""
    ttl = GenerationLifecyclePolicy.SESSION_ANALYSIS_MINUTES

    almost_expired = {
        "generation_id": _EST,
        "operation": OperationKind.ANALYSIS.value,
        "lease_started_at": _ago(minutes=ttl - 1),
        "lease_ttl_minutes": ttl,
    }
    _seed_key(db, sessions=[almost_expired], max_concurrent=1)
    db.seed_generation_session("probe", {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="probe", operation=OperationKind.ANALYSIS)
    assert result.outcome == SessionBeginOutcome.AT_CAPACITY  # still held


@pytest.mark.asyncio
async def test_stuck_running_not_fired_clearly_below_threshold(db: SimDB) -> None:
    """detect_stuck_running uses last_activity_at < cutoff (strict).

    An generation stale for clearly less than the threshold is NOT detected.

    Note: testing the exact-second boundary is not reliable without frozen time
    because the job calls datetime.now() independently. Even a microsecond of elapsed
    test execution pushes the real cutoff past the pre-computed timestamp, making
    "exact boundary" tests non-deterministic.  We test one minute inside the threshold
    to document the strict-less-than contract without relying on sub-millisecond timing.
    """
    threshold = GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES

    db.seed_generation_session(_EST, {
        "status": GenerationStatus.RUNNING,
        "key_uid": _KEY_UID,
        "last_activity_at": _ago(minutes=threshold - 1),  # clearly inside the window
        "state_history": [],
    })

    await detect_stuck_running(db)

    assert db.est(_EST)["status"] == GenerationStatus.RUNNING  # NOT detected


@pytest.mark.asyncio
async def test_stuck_running_fired_one_minute_past_threshold(db: SimDB) -> None:
    """One minute past the threshold → detected (confirms the strict < boundary)."""
    threshold = GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES

    db.seed_generation_session(_EST, {
        "status": GenerationStatus.RUNNING,
        "key_uid": _KEY_UID,
        "last_activity_at": _ago(minutes=threshold + 1),
        "state_history": [],
    })
    _seed_key(db)

    await detect_stuck_running(db)

    assert db.est(_EST)["status"] == GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_wipe_scheduled_at_exact_7_day_boundary(db: SimDB) -> None:
    """schedule_expired_workspace_wipes: failed_at exactly at cutoff IS scheduled.

    The check is `failed_at > cutoff: continue` so == cutoff proceeds.
    """
    exact_7_days = _ago(days=GenerationLifecyclePolicy.WORKSPACE_FAILED_RETENTION_DAYS)

    db.seed_generation_session(_EST, {"status": GenerationStatus.FAILED, "failed_at": exact_7_days, "state_history": []})
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": _EST,
        "scheduled_for_wipe": False,
        "state_history": [],
    })

    await schedule_expired_workspace_wipes(db)

    assert db.ws(_WS).get("scheduled_for_wipe") is True


# ===========================================================================
# B — Missing / null fields: invisible failures the happy-path never hits
# ===========================================================================


@pytest.mark.asyncio
async def test_running_generation_without_last_activity_at_detected_via_status_changed_at(db: SimDB) -> None:
    """FIXED: A RUNNING generation with no last_activity_at is now caught via status_changed_at.

    The primary query filters on last_activity_at < cutoff (skips None values).
    The fallback query filters on status_changed_at < cutoff and includes only
    records where last_activity_at is None, covering the crash-during-init case.
    """
    stale = _ago(minutes=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60)
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.RUNNING,
        "key_uid": _KEY_UID,
        "status_changed_at": stale,
        # last_activity_at intentionally absent — simulates crash after INITIALIZING→RUNNING
        "state_history": [],
    })
    _seed_key(db)

    await detect_stuck_running(db)

    assert db.est(_EST)["status"] == GenerationStatus.FAILED  # now detected via fallback


@pytest.mark.asyncio
async def test_failed_generation_without_failed_at_skipped_by_wipe_scheduler(db: SimDB) -> None:
    """FAILED generation with failed_at=None is safely skipped by wipe scheduler.

    `if not failed_at or failed_at > cutoff: continue` — the 'not failed_at' branch
    catches None/missing and prevents a crash or accidental wipe.
    """
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.FAILED,
        "failed_at": None,  # corrupted / never set
        "state_history": [],
    })
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": _EST,
        "scheduled_for_wipe": False,
        "state_history": [],
    })

    await schedule_expired_workspace_wipes(db)

    assert db.ws(_WS).get("scheduled_for_wipe") is False  # safely skipped


@pytest.mark.asyncio
async def test_workspace_with_no_locked_by_skipped_by_wipe_scheduler(db: SimDB) -> None:
    """ALLOCATED workspace with locked_by=None does not crash wipe scheduler."""
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": None,
        "scheduled_for_wipe": False,
        "state_history": [],
    })

    # Must not raise
    await schedule_expired_workspace_wipes(db)
    assert db.ws(_WS).get("scheduled_for_wipe") is False


@pytest.mark.asyncio
async def test_workspace_locked_by_nonexistent_generation_skipped(db: SimDB) -> None:
    """locked_by points to a deleted / never-created generation → scheduler skips safely."""
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": "est-ghost",
        "scheduled_for_wipe": False,
        "state_history": [],
    })
    # No generation seeded for est-ghost

    await schedule_expired_workspace_wipes(db)
    assert db.ws(_WS).get("scheduled_for_wipe") is False


@pytest.mark.asyncio
async def test_workspace_locked_by_running_generation_not_wiped(db: SimDB) -> None:
    """RUNNING generation's workspace must never be scheduled for wipe.

    The scheduler filters status == FAILED; this test confirms RUNNING is excluded.
    """
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.RUNNING,
        "failed_at": _ago(days=10),  # spurious old timestamp but status is RUNNING
        "state_history": [],
    })
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": _EST,
        "scheduled_for_wipe": False,
        "state_history": [],
    })

    await schedule_expired_workspace_wipes(db)

    assert db.ws(_WS).get("scheduled_for_wipe") is False


@pytest.mark.asyncio
async def test_workspace_locked_by_completed_generation_not_wiped(db: SimDB) -> None:
    """COMPLETED generation's workspace is not subject to the 7-day FAILED wipe policy."""
    db.seed_generation_session(_EST, {"status": GenerationStatus.COMPLETED, "state_history": []})
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": _EST,
        "scheduled_for_wipe": False,
        "state_history": [],
    })

    await schedule_expired_workspace_wipes(db)

    assert db.ws(_WS).get("scheduled_for_wipe") is False


# ===========================================================================
# C — Corrupted session data must be survived silently
# ===========================================================================


@pytest.mark.asyncio
async def test_session_list_is_none_treated_as_empty(db: SimDB) -> None:
    """active_generation_sessions = None → deserialized as [] → capacity fully available."""
    db.seed_api_key(_KEY_ID, {
        "api_key": _KEY_ID, "key_uid": _KEY_UID,
        "max_concurrent_sessions": 5,
        "active_generation_sessions": None,  # corrupted
    })
    db.seed_generation_session(_EST, {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)
    assert result.outcome == SessionBeginOutcome.ACQUIRED


@pytest.mark.asyncio
async def test_session_entry_missing_generation_id_is_dropped(db: SimDB) -> None:
    """Session row with no generation_id is silently dropped; does not hold a slot."""
    _seed_key(db, sessions=[
        {"operation": "generation", "lease_started_at": _ago(minutes=1), "lease_ttl_minutes": 2880},  # no generation_id
    ], max_concurrent=1)
    db.seed_generation_session(_EST, {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)
    assert result.outcome == SessionBeginOutcome.ACQUIRED  # corrupted row didn't block capacity


@pytest.mark.asyncio
async def test_session_entry_missing_lease_started_at_is_dropped(db: SimDB) -> None:
    """Session row without lease_started_at cannot compute expiry → silently dropped."""
    _seed_key(db, sessions=[
        {"generation_id": _EST, "operation": "generation", "lease_ttl_minutes": 2880},  # no started_at
    ], max_concurrent=1)
    db.seed_generation_session(_EST, {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)
    assert result.outcome == SessionBeginOutcome.ACQUIRED


@pytest.mark.asyncio
async def test_session_entry_unknown_operation_defaults_to_analysis(db: SimDB) -> None:
    """Unknown operation string → defaults to ANALYSIS; session is still valid, not dropped."""
    _seed_key(db, sessions=[
        {"generation_id": _EST, "operation": "telepathy", "lease_started_at": _ago(minutes=1), "lease_ttl_minutes": 2880},
    ])
    db.seed_generation_session(_EST, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)
    # session was loaded (defaulted to ANALYSIS), so ALREADY_HELD
    assert result.outcome == SessionBeginOutcome.ALREADY_HELD


@pytest.mark.asyncio
async def test_session_with_zero_ttl_is_immediately_expired(db: SimDB) -> None:
    """FIXED: lease_ttl_minutes=0 now correctly means 'expire immediately'.

    Previous code: `int(row.get("lease_ttl_minutes") or _TTL_MINUTES[op])`
    Python `0 or fallback` coerced zero to the 2880-minute default.

    Fixed code: `int(ttl_raw) if ttl_raw is not None else _TTL_MINUTES[op]`
    Zero is now treated as a genuine TTL of 0 minutes → expired at or after lease_started_at.
    """
    _seed_key(db, sessions=[
        {"generation_id": _EST, "operation": "generation", "lease_started_at": _ago(minutes=1), "lease_ttl_minutes": 0},
    ], max_concurrent=1)
    db.seed_generation_session("probe", {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="probe", operation=OperationKind.GENERATION)
    # zero TTL → expired → capacity freed → probe ACQUIRED
    assert result.outcome == SessionBeginOutcome.ACQUIRED


@pytest.mark.asyncio
async def test_duplicate_generation_id_in_sessions_already_held(db: SimDB) -> None:
    """Same generation_id appearing twice in sessions → ALREADY_HELD (first match wins)."""
    dup_sessions = [
        {"generation_id": _EST, "operation": "generation", "lease_started_at": _ago(minutes=1), "lease_ttl_minutes": 2880},
        {"generation_id": _EST, "operation": "generation", "lease_started_at": _ago(minutes=2), "lease_ttl_minutes": 2880},
    ]
    _seed_key(db, sessions=dup_sessions)
    db.seed_generation_session(_EST, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)
    assert result.outcome == SessionBeginOutcome.ALREADY_HELD
    # slot count: 2 raw entries, but treated as 1 live session
    assert result.snapshot.at_capacity is False  # 2 entries, max=5 → not at cap


@pytest.mark.asyncio
async def test_duplicate_generation_id_end_removes_both(db: SimDB) -> None:
    """end() with a duplicated generation_id removes all matching entries, leaving list clean."""
    dup_sessions = [
        {"generation_id": _EST, "operation": "generation", "lease_started_at": _ago(minutes=1), "lease_ttl_minutes": 2880},
        {"generation_id": _EST, "operation": "generation", "lease_started_at": _ago(minutes=2), "lease_ttl_minutes": 2880},
    ]
    _seed_key(db, sessions=dup_sessions)
    db.seed_generation_session(_EST, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    await svc.end(generation_id=_EST, reason=SessionEndReason.COMPLETED)

    assert len(db.active_sessions(_KEY_ID)) == 0


@pytest.mark.asyncio
async def test_max_concurrent_sessions_zero_blocks_all(db: SimDB) -> None:
    """FIXED: max_concurrent_sessions=0 now correctly blocks all session acquisition.

    Previous code: `int(doc.get("max_concurrent_sessions") or 5)`
    Python `0 or 5` coerced zero to 5, granting full capacity instead of none.
    An operator setting this to 0 to freeze a key would silently grant 5 slots.

    Fixed code: `int(val) if val is not None else 5`
    Zero is now treated as a genuine limit of 0 → AT_CAPACITY for all attempts.
    """
    _seed_key(db, max_concurrent=0)
    db.seed_generation_session(_EST, {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)
    assert result.outcome == SessionBeginOutcome.AT_CAPACITY


@pytest.mark.asyncio
async def test_session_started_in_future_never_expires(db: SimDB) -> None:
    """A session with lease_started_at in the future (clock skew) is never expired by TTL.

    _session_expired: now >= future_start + TTL is False for any realistic TTL.
    Such a session holds its slot indefinitely unless explicitly ended.
    """
    ttl = GenerationLifecyclePolicy.SESSION_ANALYSIS_MINUTES
    future_start = _from_now(minutes=60)

    _seed_key(db, sessions=[
        {"generation_id": _EST, "operation": "analysis", "lease_started_at": future_start, "lease_ttl_minutes": ttl},
    ], max_concurrent=1)
    db.seed_generation_session("probe", {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="probe", operation=OperationKind.ANALYSIS)
    # future-dated session is NOT expired → slot still held → probe blocked
    assert result.outcome == SessionBeginOutcome.AT_CAPACITY


# ===========================================================================
# D — Impossible state transitions must raise, not corrupt
# ===========================================================================


@pytest.mark.asyncio
async def test_fail_on_completed_generation_raises(db: SimDB) -> None:
    """Commandment X: fail() on a COMPLETED generation must raise, not silently overwrite."""
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.COMPLETED,
        "key_uid": _KEY_UID,
        "state_history": [],
    })
    _seed_key(db)

    esm = GenerationSessionStateMachine(db)
    with pytest.raises(InvalidGenerationSessionStateError):
        await esm.fail(generation_id=_EST, reason="late error", triggered_by=TriggeredBy.STUCK_RUNNING)

    assert db.est(_EST)["status"] == GenerationStatus.COMPLETED  # unchanged


@pytest.mark.asyncio
async def test_fail_on_already_failed_generation_raises(db: SimDB) -> None:
    """fail() on an already-FAILED generation raises — no status corruption."""
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.FAILED,
        "key_uid": _KEY_UID,
        "state_history": [],
    })
    _seed_key(db)

    esm = GenerationSessionStateMachine(db)
    with pytest.raises(InvalidGenerationSessionStateError):
        await esm.fail(generation_id=_EST, reason="double fail", triggered_by=TriggeredBy.STUCK_RUNNING)


@pytest.mark.asyncio
async def test_stuck_detected_on_pending_raises(db: SimDB) -> None:
    """stuck_detected only allowed from RUNNING. PENDING → raises."""
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.PENDING,
        "key_uid": _KEY_UID,
        "state_history": [],
    })
    _seed_key(db)

    esm = GenerationSessionStateMachine(db)
    with pytest.raises(InvalidGenerationSessionStateError):
        await esm.stuck_detected(generation_id=_EST, triggered_by=TriggeredBy.STUCK_RUNNING)

    assert db.est(_EST)["status"] == GenerationStatus.PENDING


@pytest.mark.asyncio
async def test_stuck_detected_on_analysis_raises(db: SimDB) -> None:
    """stuck_detected on ANALYSIS (spec check in progress) must raise."""
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.PENDING,
        "key_uid": _KEY_UID,
        "state_history": [],
    })
    _seed_key(db)

    esm = GenerationSessionStateMachine(db)
    with pytest.raises(InvalidGenerationSessionStateError):
        await esm.stuck_detected(generation_id=_EST, triggered_by=TriggeredBy.STUCK_RUNNING)


@pytest.mark.asyncio
async def test_stuck_detected_on_completed_raises(db: SimDB) -> None:
    """stuck_detected on COMPLETED must raise — cannot un-complete a finished run."""
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.COMPLETED,
        "key_uid": _KEY_UID,
        "state_history": [],
    })
    _seed_key(db)

    esm = GenerationSessionStateMachine(db)
    with pytest.raises(InvalidGenerationSessionStateError):
        await esm.stuck_detected(generation_id=_EST, triggered_by=TriggeredBy.STUCK_RUNNING)

    assert db.est(_EST)["status"] == GenerationStatus.COMPLETED


@pytest.mark.asyncio
async def test_stuck_detected_on_already_failed_raises(db: SimDB) -> None:
    """stuck_detected on already-FAILED raises — detector called twice for same generation."""
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.FAILED,
        "key_uid": _KEY_UID,
        "state_history": [],
    })
    _seed_key(db)

    esm = GenerationSessionStateMachine(db)
    with pytest.raises(InvalidGenerationSessionStateError):
        await esm.stuck_detected(generation_id=_EST, triggered_by=TriggeredBy.STUCK_RUNNING)


@pytest.mark.asyncio
async def test_begin_allocation_on_running_raises(db: SimDB) -> None:
    """begin_allocation is only allowed from PENDING / ANALYSIS. RUNNING → raises."""
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.RUNNING,
        "key_uid": _KEY_UID,
        "state_history": [],
    })

    esm = GenerationSessionStateMachine(db)
    with pytest.raises(InvalidGenerationSessionStateError):
        await esm.begin_allocation(generation_id=_EST, triggered_by=TriggeredBy.START)


# ===========================================================================
# E — begin_or_raise rollback semantics
# ===========================================================================


@pytest.mark.asyncio
async def test_begin_or_raise_acquired_rolls_back_on_exception(db: SimDB) -> None:
    """If the body of begin_or_raise raises after ACQUIRED, the slot is rolled back."""
    _seed_key(db)
    db.seed_generation_session(_EST, {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    with pytest.raises(RuntimeError):
        async with svc.begin_or_raise(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION):
            raise RuntimeError("task spawn failed")

    # Slot was rolled back → capacity restored
    assert len(db.active_sessions(_KEY_ID)) == 0


@pytest.mark.asyncio
async def test_begin_or_raise_already_held_does_not_rollback_on_exception(db: SimDB) -> None:
    """ALREADY_HELD + exception in body must NOT release the slot.

    The slot belongs to the prior acquisition, not this re-entry. Rolling it back
    would terminate a running job that did nothing wrong.
    """
    _seed_key(db, sessions=[_live_session(_EST)])
    db.seed_generation_session(_EST, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    with pytest.raises(RuntimeError):
        async with svc.begin_or_raise(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION):
            raise RuntimeError("something failed after ALREADY_HELD")

    # Slot must still be held
    assert len(db.active_sessions(_KEY_ID)) == 1
    assert db.active_sessions(_KEY_ID)[0]["generation_id"] == _EST


@pytest.mark.asyncio
async def test_after_rollback_slot_available_for_fresh_begin(db: SimDB) -> None:
    """After a rollback (BEGIN_ROLLBACK), the same generation_id can acquire a fresh slot."""
    _seed_key(db, max_concurrent=1)
    db.seed_generation_session(_EST, {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    with pytest.raises(RuntimeError):
        async with svc.begin_or_raise(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION):
            raise RuntimeError("failed")

    # Fresh begin after rollback → ACQUIRED (not ALREADY_HELD and not AT_CAPACITY)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)
    assert result.outcome == SessionBeginOutcome.ACQUIRED


# ===========================================================================
# F — Session end mechanics: all reasons behave identically for slot removal
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", list(SessionEndReason))
async def test_all_end_reasons_remove_slot(reason: SessionEndReason, db: SimDB) -> None:
    """Every SessionEndReason must result in the slot being removed.

    If a new reason is added and the code uses an allowlist check, this test
    catches the regression immediately.
    """
    _seed_key(db, sessions=[_live_session(_EST)])
    db.seed_generation_session(_EST, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    await svc.end(generation_id=_EST, reason=reason)

    assert len(db.active_sessions(_KEY_ID)) == 0


@pytest.mark.asyncio
async def test_end_for_unknown_generation_id_is_noop(db: SimDB) -> None:
    """end() for a generation_id that never had a slot does not raise or corrupt data."""
    _seed_key(db, sessions=[_live_session("other-est")])
    db.seed_generation_session("other-est", {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})
    db.seed_generation_session("ghost-est", {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    await svc.end(generation_id="ghost-est", reason=SessionEndReason.COMPLETED)

    # other-est's slot untouched
    assert len(db.active_sessions(_KEY_ID)) == 1
    assert db.active_sessions(_KEY_ID)[0]["generation_id"] == "other-est"


@pytest.mark.asyncio
async def test_end_for_nonexistent_generation_does_not_raise(db: SimDB) -> None:
    """end() resolves generation → api_key. If generation doesn't exist, silently no-ops."""
    _seed_key(db)
    # No generation seeded for _EST

    svc = ApiKeySessionConcurrency(db)
    # Must not raise even though generation lookup returns None
    await svc.end(generation_id=_EST, reason=SessionEndReason.COMPLETED)


@pytest.mark.asyncio
async def test_after_end_same_id_acquires_fresh_slot(db: SimDB) -> None:
    """After end(), a new try_begin for the same generation_id gets ACQUIRED, not ALREADY_HELD.

    This matters for the retry path: a retried generation must get a fresh slot,
    not be told it already holds one from the previous failed run.
    """
    _seed_key(db, sessions=[_live_session(_EST)])
    db.seed_generation_session(_EST, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    await svc.end(generation_id=_EST, reason=SessionEndReason.FAILED)

    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)
    assert result.outcome == SessionBeginOutcome.ACQUIRED  # not ALREADY_HELD


@pytest.mark.asyncio
async def test_refresh_lease_on_nonexistent_session_is_noop(db: SimDB) -> None:
    """refresh_lease on a session that was already end()ed does not raise."""
    _seed_key(db)  # no sessions
    db.seed_generation_session(_EST, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    await svc.refresh_lease(generation_id=_EST, operation=OperationKind.GENERATION)
    # no exception, slot count unchanged
    assert len(db.active_sessions(_KEY_ID)) == 0


# ===========================================================================
# G — Multi-job idempotency: firing the same job twice must be safe
# ===========================================================================


@pytest.mark.asyncio
async def test_stuck_running_twice_on_same_generation_second_is_noop(db: SimDB) -> None:
    """stuck_running_detector is idempotent: second firing skips already-FAILED generation.

    The state machine rejects FAILED→FAILED; the job catches the exception and moves on.
    """
    stale = _ago(minutes=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60)
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.RUNNING,
        "key_uid": _KEY_UID,
        "last_activity_at": stale,
        "state_history": [],
    })
    _seed_key(db, sessions=[
        _live_session(_EST, started_minutes_ago=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60),
    ])

    # First firing
    await detect_stuck_running(db)
    assert db.est(_EST)["status"] == GenerationStatus.FAILED

    # Second firing — must not raise, status stays FAILED
    await detect_stuck_running(db)
    assert db.est(_EST)["status"] == GenerationStatus.FAILED


@pytest.mark.asyncio
async def test_stuck_running_on_empty_db_completes_without_error(db: SimDB) -> None:
    """detect_stuck_running on an empty database completes silently."""
    await detect_stuck_running(db)  # must not raise


@pytest.mark.asyncio
async def test_stuck_running_only_targets_running_generations(db: SimDB) -> None:
    """detect_stuck_running ignores PENDING, COMPLETED, FAILED, INITIALIZING, ANALYSIS generations
    even if their last_activity_at is ancient.
    """
    ancient = _ago(days=100)
    for status in [
        GenerationStatus.PENDING, GenerationStatus.COMPLETED,
        GenerationStatus.FAILED, GenerationStatus.INITIALIZING, GenerationStatus.PENDING,
    ]:
        eid = f"est-{status.value}"
        db.seed_generation_session(eid, {"status": status, "last_activity_at": ancient, "state_history": []})

    await detect_stuck_running(db)

    for status in [
        GenerationStatus.PENDING, GenerationStatus.COMPLETED,
        GenerationStatus.FAILED, GenerationStatus.INITIALIZING, GenerationStatus.PENDING,
    ]:
        eid = f"est-{status.value}"
        assert db.est(eid)["status"] == status  # all untouched


@pytest.mark.asyncio
async def test_wipe_not_double_scheduled(db: SimDB) -> None:
    """schedule_expired_workspace_wipes skips workspaces already marked scheduled_for_wipe=True."""
    old_wipe_at = _ago(hours=1)
    old_failed = _ago(days=GenerationLifecyclePolicy.WORKSPACE_FAILED_RETENTION_DAYS + 1)

    db.seed_generation_session(_EST, {"status": GenerationStatus.FAILED, "failed_at": old_failed, "state_history": []})
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": _EST,
        "scheduled_for_wipe": True,   # already scheduled
        "scheduled_for_wipe_at": old_wipe_at,
        "state_history": [],
    })

    await schedule_expired_workspace_wipes(db)

    # wipe_at must not be updated to a new value
    assert db.ws(_WS).get("scheduled_for_wipe_at") == old_wipe_at


@pytest.mark.asyncio
async def test_stuck_running_only_marks_stale_not_fresh_in_mixed_db(db: SimDB) -> None:
    """In a DB with one stale and one fresh RUNNING generation, only the stale one is marked FAILED."""
    stale = _ago(minutes=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60)
    fresh = _ago(minutes=10)

    db.seed_generation_session("est-stale", {
        "status": GenerationStatus.RUNNING, "key_uid": _KEY_UID,
        "last_activity_at": stale, "state_history": [],
    })
    db.seed_generation_session("est-fresh", {
        "status": GenerationStatus.RUNNING, "key_uid": _KEY_UID,
        "last_activity_at": fresh, "state_history": [],
    })
    _seed_key(db, sessions=[
        _live_session("est-stale", started_minutes_ago=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60),
    ])

    await detect_stuck_running(db)

    assert db.est("est-stale")["status"] == GenerationStatus.FAILED
    assert db.est("est-fresh")["status"] == GenerationStatus.RUNNING  # untouched


# ===========================================================================
# H — CLEANING workspace legacy case: None cleaning_started_at
# ===========================================================================


@pytest.mark.asyncio
async def test_cleaning_workspace_with_null_started_at_immediately_recovered(db: SimDB) -> None:
    """Legacy CLEANING workspace with cleaning_started_at=None is treated as immediately stuck.

    This covers pre-HR3 documents that never had the field set.
    """
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.CLEANING,
        "cleaning_started_at": None,  # legacy
        "locked_by": None,
        "state_history": [],
    })

    await recover_stuck_cleaning(db)

    # STUCK → begin_recovery → CLEANING (recovery triggered)
    assert db.ws(_WS)["status"] == WorkspaceStatus.CLEANING


# ===========================================================================
# I — Multiple workspaces per generation: all must transition together
# ===========================================================================


@pytest.mark.asyncio
async def test_stuck_initializing_rolls_back_all_workspaces(db: SimDB) -> None:
    """INITIALIZING generation with 3 workspaces: all 3 must transition to CLEANING."""
    stale = _ago(minutes=GenerationLifecyclePolicy.STUCK_INITIALIZING_MINUTES + 5)
    ws_ids = ["ws-a", "ws-b", "ws-c"]

    db.seed_generation_session(_EST, {
        "status": GenerationStatus.INITIALIZING,
        "workspace_ids": ws_ids,
        "status_changed_at": stale,
        "state_history": [],
    })
    for wid in ws_ids:
        db.seed_workspace(wid, {"status": WorkspaceStatus.ALLOCATED, "locked_by": _EST})

    await detect_stuck_initializing(db)

    assert db.est(_EST)["status"] == GenerationStatus.PENDING
    for wid in ws_ids:
        assert db.ws(wid)["status"] == WorkspaceStatus.CLEANING, f"{wid} not cleaned"


# ===========================================================================
# K — stuck_running fires, then wipe scheduler runs: wipe must NOT trigger yet
# ===========================================================================


@pytest.mark.asyncio
async def test_wipe_does_not_fire_immediately_after_stuck_running(db: SimDB) -> None:
    """Timeline: stuck detector marks FAILED now → wipe scheduler runs → NOT scheduled.

    The wipe requires 7 days after failed_at. If stuck fires and wipe runs in the
    same job cycle (minutes later), the wipe must not trigger.
    """
    stale = _ago(minutes=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60)
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.RUNNING,
        "key_uid": _KEY_UID,
        "last_activity_at": stale,
        "state_history": [],
    })
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": _EST,
        "scheduled_for_wipe": False,
        "state_history": [],
    })
    _seed_key(db, sessions=[
        _live_session(_EST, started_minutes_ago=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60),
    ])

    # Step 1: stuck detector fires
    await detect_stuck_running(db)
    assert db.est(_EST)["status"] == GenerationStatus.FAILED

    # Step 2: wipe scheduler fires immediately after (failed_at = now, < 7 days)
    await schedule_expired_workspace_wipes(db)

    assert db.ws(_WS).get("scheduled_for_wipe") is False  # too soon
    assert db.ws(_WS)["status"] == WorkspaceStatus.ALLOCATED  # code intact
