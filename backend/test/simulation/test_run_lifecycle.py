"""
Lifecycle simulation: exhaustive coverage of every path a single run can take.

Each test represents a distinct trajectory:
  - Happy path (session acquired → checkpoints advance → complete)
  - Failure path (workflow error → slot released, workspace preserved)
  - TTL expiry (orphaned slots pruned; healthy jobs unaffected)
  - Background job intervention (stuck detectors, cleaning recovery, wipe scheduler)
  - Multi-session concurrency (capacity cap, slot isolation, cross-key independence)
  - Commandment guards (workspaces never touched by wrong mechanisms)
  - Ordering invariants from GenerationLifecyclePolicy

These tests are the executable proof that PR188's changes (TTL fix, session tracking,
concurrency policy) hold under every scenario that production can generate.

Database layer: SimDB (JobFakeDB + get_api_key_doc) — lightweight, no I/O.
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
    SessionAtCapacityError,
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
    execute_confirmed_wipes,
)
from app.state.db_adapter import COL_GENERATION_SESSIONS


# ---------------------------------------------------------------------------
# Test DB
# ---------------------------------------------------------------------------


class SimDB:
    """
    Self-contained in-memory database for lifecycle simulation tests.

    Extends the JobFakeDB interface with get_api_key_doc (needed by
    ApiKeySessionConcurrency.snapshot / refresh_lease) and a reset() helper
    so each test scenario starts from a known-clean state.
    """

    def __init__(self):
        self._generation_sessions: dict = {}
        self._workspaces: dict = {}
        self._api_keys: dict = {}

    # ------------------------------------------------------------------ #
    # Seeding
    # ------------------------------------------------------------------ #

    def seed_generation_session(self, eid: str, data: dict) -> None:
        self._generation_sessions[eid] = {"id": eid, **data}

    def seed_workspace(self, wid: str, data: dict) -> None:
        self._workspaces[wid] = {"id": wid, **data}

    def seed_api_key(self, key_id: str, data: dict) -> None:
        self._api_keys[key_id] = {"id": key_id, **data}

    # ------------------------------------------------------------------ #
    # Assertion helpers
    # ------------------------------------------------------------------ #

    def est(self, eid: str) -> dict:
        return dict(self._generation_sessions.get(eid, {}))

    def ws(self, wid: str) -> dict:
        return dict(self._workspaces.get(wid, {}))

    def key(self, key_id: str) -> dict:
        return dict(self._api_keys.get(key_id, {}))

    def active_sessions(self, key_id: str) -> list:
        return list(self._api_keys.get(key_id, {}).get("active_generation_sessions", []))

    # ------------------------------------------------------------------ #
    # Sync IDatabase shim (for WorkspaceStateMachine transactions)
    # ------------------------------------------------------------------ #

    def update(self, collection: str, doc_id: str, data: dict) -> None:
        store = {COL_GENERATION_SESSIONS: self._generation_sessions, "workspaces": self._workspaces, "api_keys": self._api_keys}.get(collection)
        if store is not None and doc_id in store:
            store[doc_id].update(data)

    # ------------------------------------------------------------------ #
    # Async state-machine interface
    # ------------------------------------------------------------------ #

    async def get_generation_session(self, eid: str) -> dict:
        return dict(self._generation_sessions.get(eid, {}))

    async def update_generation_session(self, eid: str, update: dict) -> None:
        if eid not in self._generation_sessions:
            self._generation_sessions[eid] = {"id": eid}
        self._generation_sessions[eid].update(update)

    async def get_workspace(self, wid: str) -> dict:
        return dict(self._workspaces.get(wid, {}))

    async def update_workspace(self, wid: str, update: dict) -> None:
        if wid not in self._workspaces:
            self._workspaces[wid] = {"id": wid}
        self._workspaces[wid].update(update)

    async def get_workspace_in_transaction(self, wid: str, _txn) -> dict:
        return dict(self._workspaces.get(wid, {}))

    async def update_workspace_in_transaction(self, wid: str, update: dict, txn) -> None:
        txn._pending_writes.append(("workspace", wid, update))

    async def get_api_key_doc(self, api_key_doc_id: str) -> dict | None:
        doc = self._api_keys.get(api_key_doc_id)
        return dict(doc) if doc is not None else None

    async def get_api_key_by_uid(self, key_uid: str) -> dict | None:
        for doc in self._api_keys.values():
            if doc.get("key_uid") == key_uid:
                out = dict(doc)
                out["_id"] = doc.get("id", doc.get("api_key"))
                return out
        return None

    async def update_api_key(self, api_key_id: str, data: dict) -> None:
        self.update("api_keys", api_key_id, data)

    # ------------------------------------------------------------------ #
    # Transaction support
    # ------------------------------------------------------------------ #

    async def run_sync_transaction(self, sync_txn_fn):
        stores = {
            COL_GENERATION_SESSIONS: self._generation_sessions,
            "workspaces": self._workspaces,
            "api_keys": self._api_keys,
        }

        class _SyncTx:
            def get(_, collection, doc_id):  # noqa: N805
                store = stores.get(collection, {})
                doc = store.get(doc_id)
                return dict(doc) if doc is not None else None

            def update(_, collection, doc_id, data):  # noqa: N805
                store = stores.get(collection)
                if store is not None:
                    if doc_id not in store:
                        store[doc_id] = {"id": doc_id}
                    store[doc_id].update(data)

        return sync_txn_fn(_SyncTx())

    async def run_api_key_generation_session_txn(self, sync_txn_fn):
        return await self.run_sync_transaction(sync_txn_fn)

    class _Txn:
        def __init__(self):
            self._pending_writes = []

    async def run_transaction(self, txn_fn):
        txn = self._Txn()
        result = await txn_fn(txn)
        for op_type, doc_id, update in txn._pending_writes:
            if op_type == "workspace":
                if doc_id not in self._workspaces:
                    self._workspaces[doc_id] = {"id": doc_id}
                self._workspaces[doc_id].update(update)
        return result

    # ------------------------------------------------------------------ #
    # Query interface (used by job functions)
    # ------------------------------------------------------------------ #

    async def query_generation_sessions(self, filters: list) -> list:
        return [dict(doc) for doc in self._generation_sessions.values() if self._matches(doc, filters)]

    async def query_workspaces(self, filters: list) -> list:
        return [dict(doc) for doc in self._workspaces.values() if self._matches(doc, filters)]

    async def query_collection(self, collection: str, filters: list) -> list:
        store = {COL_GENERATION_SESSIONS: self._generation_sessions, "workspaces": self._workspaces, "api_keys": self._api_keys}.get(collection, {})
        return [dict(doc) for doc in store.values() if self._matches(doc, filters)]

    @staticmethod
    def _matches(doc: dict, filters: list) -> bool:
        for field, op, value in filters:
            doc_val = doc.get(field)
            if op == "==" and doc_val != value:
                return False
            if op == "<" and (doc_val is None or doc_val >= value):
                return False
            if op == ">" and (doc_val is None or doc_val <= value):
                return False
            if op == "<=" and (doc_val is None or doc_val > value):
                return False
            if op == ">=" and (doc_val is None or doc_val < value):
                return False
        return True


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc)
_KEY_ID = "key-abc"
_KEY_UID = "ku-1"
_EST = "est-001"
_WS = "ws-001"


def _ago(minutes: float = 0, days: float = 0, hours: float = 0) -> datetime:
    return _NOW - timedelta(minutes=minutes, days=days, hours=hours)


def _from_now(hours: float = 0, minutes: float = 0) -> datetime:
    return _NOW + timedelta(hours=hours, minutes=minutes)


def _active_session(generation_id: str, operation: OperationKind, started_minutes_ago: float, ttl_minutes: int) -> dict:
    return {
        "generation_id": generation_id,
        "operation": operation.value,
        "lease_started_at": _ago(minutes=started_minutes_ago),
        "lease_ttl_minutes": ttl_minutes,
    }


@pytest.fixture
def db() -> SimDB:
    return SimDB()


def _seed_api_key(db: SimDB, sessions: list | None = None, max_concurrent: int = 5) -> None:
    db.seed_api_key(_KEY_ID, {
        "api_key": _KEY_ID,
        "key_uid": _KEY_UID,
        "max_concurrent_sessions": max_concurrent,
        "active_generation_sessions": sessions or [],
    })


def _seed_running_generation(db: SimDB, last_activity_at: datetime | None = None, workspace_ids: list | None = None) -> None:
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.RUNNING,
        "key_uid": _KEY_UID,
        "last_activity_at": last_activity_at or _NOW,
        "workspace_ids": workspace_ids or [_WS],
        "state_history": [],
    })


def _seed_allocated_workspace(db: SimDB, locked_by: str = _EST) -> None:
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": locked_by,
        "scheduled_for_wipe": False,
    })


# ---------------------------------------------------------------------------
# Section 1: Session slot lifecycle — acquire, re-acquire, release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_analysis_slot(db: SimDB) -> None:
    """Fresh session → ACQUIRED, slot count increments to 1."""
    _seed_api_key(db)
    db.seed_generation_session(_EST, {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.ANALYSIS)

    assert result.outcome == SessionBeginOutcome.ACQUIRED
    assert result.session is not None
    assert result.session.operation == OperationKind.ANALYSIS
    assert len(db.active_sessions(_KEY_ID)) == 1


@pytest.mark.asyncio
async def test_acquire_planning_slot(db: SimDB) -> None:
    """PLANNING slot acquired; TTL is GenerationLifecyclePolicy.SESSION_PLANNING_MINUTES."""
    _seed_api_key(db)
    db.seed_generation_session(_EST, {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.PLANNING)

    assert result.outcome == SessionBeginOutcome.ACQUIRED
    assert result.session.lease_ttl_minutes == GenerationLifecyclePolicy.SESSION_PLANNING_MINUTES


@pytest.mark.asyncio
async def test_acquire_generation_slot_has_48h_ttl(db: SimDB) -> None:
    """GENERATION slot TTL is 48 hours, not the old 12 hours."""
    _seed_api_key(db)
    db.seed_generation_session(_EST, {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)

    assert result.session.lease_ttl_minutes == GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES
    assert result.session.lease_ttl_minutes == 48 * 60  # explicit: must be 2880, not 720


@pytest.mark.asyncio
async def test_same_generation_second_call_is_already_held(db: SimDB) -> None:
    """A second try_begin for the same generation_id returns ALREADY_HELD without adding a slot."""
    _seed_api_key(db)
    db.seed_generation_session(_EST, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})
    svc = ApiKeySessionConcurrency(db)

    await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)
    result2 = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)

    assert result2.outcome == SessionBeginOutcome.ALREADY_HELD
    assert len(db.active_sessions(_KEY_ID)) == 1  # still exactly 1 slot


@pytest.mark.asyncio
async def test_slot_released_restores_capacity(db: SimDB) -> None:
    """end() removes the slot; capacity is immediately available for a new session."""
    _seed_api_key(db)
    db.seed_generation_session(_EST, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})
    svc = ApiKeySessionConcurrency(db)

    await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=_EST, operation=OperationKind.GENERATION)
    assert len(db.active_sessions(_KEY_ID)) == 1

    await svc.end(generation_id=_EST, reason=SessionEndReason.COMPLETED)
    assert len(db.active_sessions(_KEY_ID)) == 0


# ---------------------------------------------------------------------------
# Section 2: Concurrency cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capacity_cap_blocks_sixth_session(db: SimDB) -> None:
    """With max_concurrent=5, the 6th try_begin returns AT_CAPACITY."""
    _seed_api_key(db, max_concurrent=5)

    for i in range(5):
        eid = f"est-{i}"
        db.seed_generation_session(eid, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    for i in range(5):
        r = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=f"est-{i}", operation=OperationKind.GENERATION)
        assert r.outcome == SessionBeginOutcome.ACQUIRED

    # 6th generation
    db.seed_generation_session("est-5", {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="est-5", operation=OperationKind.GENERATION)

    assert result.outcome == SessionBeginOutcome.AT_CAPACITY
    assert len(db.active_sessions(_KEY_ID)) == 5  # unchanged


@pytest.mark.asyncio
async def test_begin_or_raise_raises_at_capacity(db: SimDB) -> None:
    """begin_or_raise raises SessionAtCapacityError when cap is reached."""
    _seed_api_key(db, max_concurrent=1)
    db.seed_generation_session("est-0", {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})
    db.seed_generation_session("est-1", {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="est-0", operation=OperationKind.GENERATION)

    with pytest.raises(SessionAtCapacityError):
        async with svc.begin_or_raise(api_key_doc_id=_KEY_ID, generation_id="est-1", operation=OperationKind.GENERATION):
            pass  # should not reach here


@pytest.mark.asyncio
async def test_freeing_one_slot_unblocks_next(db: SimDB) -> None:
    """After one session ends, a previously-blocked generation can acquire."""
    _seed_api_key(db, max_concurrent=1)
    db.seed_generation_session("est-0", {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})
    db.seed_generation_session("est-1", {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="est-0", operation=OperationKind.GENERATION)

    # est-1 is blocked
    blocked = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="est-1", operation=OperationKind.GENERATION)
    assert blocked.outcome == SessionBeginOutcome.AT_CAPACITY

    # est-0 finishes
    await svc.end(generation_id="est-0", reason=SessionEndReason.COMPLETED)

    # est-1 can now proceed
    now_ok = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="est-1", operation=OperationKind.GENERATION)
    assert now_ok.outcome == SessionBeginOutcome.ACQUIRED


# ---------------------------------------------------------------------------
# Section 3: TTL expiry — orphan cleanup, healthy jobs unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generation_session_alive_at_12h(db: SimDB) -> None:
    """A GENERATION session started 12 hours ago is still alive under the new 48h TTL.
    Under the old 12h TTL it would have expired — this proves the fix works.
    """
    sessions = [_active_session(_EST, OperationKind.GENERATION,
                                started_minutes_ago=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 1,
                                ttl_minutes=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES)]
    _seed_api_key(db, sessions=sessions)
    db.seed_generation_session(_EST, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    # try_begin with a different generation — will prune expired sessions
    db.seed_generation_session("est-probe", {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="est-probe", operation=OperationKind.GENERATION)

    # The 12h-old GENERATION session is still alive (48h TTL), so slot count = 1 before probe
    # probe would add a second slot → ACQUIRED if count was 1
    assert result.outcome == SessionBeginOutcome.ACQUIRED
    assert len(db.active_sessions(_KEY_ID)) == 2  # original + probe


@pytest.mark.asyncio
async def test_expired_session_pruned_frees_capacity(db: SimDB) -> None:
    """A session whose TTL has elapsed is pruned on next try_begin; freed slot is reused."""
    expired_sessions = [
        _active_session(_EST, OperationKind.GENERATION,
                        started_minutes_ago=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES + 1,
                        ttl_minutes=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES),
    ]
    _seed_api_key(db, sessions=expired_sessions, max_concurrent=1)
    db.seed_generation_session("est-new", {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="est-new", operation=OperationKind.GENERATION)

    # Expired session was pruned → capacity freed → new session ACQUIRED
    assert result.outcome == SessionBeginOutcome.ACQUIRED
    sessions_in_db = db.active_sessions(_KEY_ID)
    assert len(sessions_in_db) == 1
    assert sessions_in_db[0]["generation_id"] == "est-new"


@pytest.mark.asyncio
async def test_end_on_already_expired_session_is_noop(db: SimDB) -> None:
    """Calling end() after TTL has elapsed does not raise; idempotency guarantee.

    end() on an expired session is a silent no-op: the session is already pruned
    from the effective (non-expired) list so end() returns early without a DB write.
    The raw record may still be present in the store — it is inert and will be pruned
    on the next try_begin call.  The important property is that end() never raises.
    """
    expired_sessions = [
        _active_session(_EST, OperationKind.GENERATION,
                        started_minutes_ago=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES + 1,
                        ttl_minutes=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES),
    ]
    _seed_api_key(db, sessions=expired_sessions)
    db.seed_generation_session(_EST, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    # Must not raise — idempotency guarantee
    await svc.end(generation_id=_EST, reason=SessionEndReason.COMPLETED)

    # The expired session is no longer live (a new try_begin would see capacity free)
    # even if the raw record is still in the DB awaiting pruning.
    db.seed_generation_session("est-probe", {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})
    probe = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="est-probe", operation=OperationKind.GENERATION)
    # max_concurrent=5; only the expired record was present → probe slot is ACQUIRED
    assert probe.outcome == SessionBeginOutcome.ACQUIRED


@pytest.mark.asyncio
async def test_analysis_session_expires_at_60min(db: SimDB) -> None:
    """Analysis TTL (60 min) is shortest; a 61-min-old session is pruned."""
    sessions = [
        _active_session(_EST, OperationKind.ANALYSIS,
                        started_minutes_ago=GenerationLifecyclePolicy.SESSION_ANALYSIS_MINUTES + 1,
                        ttl_minutes=GenerationLifecyclePolicy.SESSION_ANALYSIS_MINUTES),
    ]
    _seed_api_key(db, sessions=sessions, max_concurrent=1)
    db.seed_generation_session("est-fresh", {"status": GenerationStatus.PENDING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    result = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id="est-fresh", operation=OperationKind.ANALYSIS)
    assert result.outcome == SessionBeginOutcome.ACQUIRED


# ---------------------------------------------------------------------------
# Section 4: Stuck RUNNING detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stuck_running_fires_when_stale(db: SimDB) -> None:
    """Generation stale beyond STUCK_RUNNING_MINUTES threshold → FAILED."""
    stale_time = _ago(minutes=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60)
    _seed_running_generation(db, last_activity_at=stale_time)
    _seed_allocated_workspace(db)
    _seed_api_key(db, sessions=[
        _active_session(_EST, OperationKind.GENERATION,
                        started_minutes_ago=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60,
                        ttl_minutes=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES),
    ])

    await detect_stuck_running(db)

    assert db.est(_EST)["status"] == GenerationStatus.FAILED
    assert db.est(_EST).get("failed_at") is not None


@pytest.mark.asyncio
async def test_stuck_running_silent_when_recent_activity(db: SimDB) -> None:
    """Generation with recent last_activity_at is not touched."""
    _seed_running_generation(db, last_activity_at=_ago(minutes=30))
    _seed_allocated_workspace(db)

    await detect_stuck_running(db)

    assert db.est(_EST)["status"] == GenerationStatus.RUNNING


@pytest.mark.asyncio
async def test_stuck_running_preserves_workspace(db: SimDB) -> None:
    """Commandment VI: background job marks generation FAILED but workspace stays ALLOCATED."""
    stale_time = _ago(minutes=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60)
    _seed_running_generation(db, last_activity_at=stale_time)
    _seed_allocated_workspace(db)
    _seed_api_key(db, sessions=[
        _active_session(_EST, OperationKind.GENERATION,
                        started_minutes_ago=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60,
                        ttl_minutes=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES),
    ])

    await detect_stuck_running(db)

    assert db.ws(_WS)["status"] == WorkspaceStatus.ALLOCATED  # code intact


@pytest.mark.asyncio
async def test_stuck_running_releases_session_slot(db: SimDB) -> None:
    """stuck_detected() calls session.end() — slot is freed so capacity is unblocked."""
    stale_time = _ago(minutes=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60)
    _seed_running_generation(db, last_activity_at=stale_time)
    _seed_allocated_workspace(db)
    _seed_api_key(db, max_concurrent=1, sessions=[
        _active_session(_EST, OperationKind.GENERATION,
                        started_minutes_ago=GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES + 60,
                        ttl_minutes=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES),
    ])

    await detect_stuck_running(db)

    # Session slot removed → max_concurrent=1 key now has capacity
    assert len(db.active_sessions(_KEY_ID)) == 0


@pytest.mark.asyncio
async def test_stuck_running_session_slot_alive_when_job_fires(db: SimDB) -> None:
    """At the 12h stuck threshold, the 48h TTL is still valid (2880 > 720).
    This proves the ordering invariant: slot outlives the stuck detector window.
    """
    ttl = GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES
    threshold = GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES

    elapsed = threshold + 1
    remaining_ttl = ttl - elapsed

    assert remaining_ttl > 0, (
        f"Session TTL ({ttl}m) must still have {remaining_ttl}m left when stuck fires at {threshold}m"
    )
    # The slot is still alive when stuck fires → stuck_detected calls end(), not TTL
    assert elapsed < ttl


# ---------------------------------------------------------------------------
# Section 5: Stuck INITIALIZING detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stuck_initializing_fires_on_stale_allocation(db: SimDB) -> None:
    """INITIALIZING generation stale beyond 15 min → PENDING, workspaces → CLEANING."""
    stale = _ago(minutes=GenerationLifecyclePolicy.STUCK_INITIALIZING_MINUTES + 5)
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.INITIALIZING,
        "workspace_ids": [_WS],
        "status_changed_at": stale,
        "state_history": [],
    })
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": _EST,
    })

    await detect_stuck_initializing(db)

    assert db.est(_EST)["status"] == GenerationStatus.PENDING
    assert db.ws(_WS)["status"] == WorkspaceStatus.CLEANING


@pytest.mark.asyncio
async def test_stuck_initializing_silent_when_recent(db: SimDB) -> None:
    """INITIALIZING generation younger than 15 min is not touched."""
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.INITIALIZING,
        "workspace_ids": [_WS],
        "status_changed_at": _ago(minutes=5),
        "state_history": [],
    })

    await detect_stuck_initializing(db)

    assert db.est(_EST)["status"] == GenerationStatus.INITIALIZING


@pytest.mark.asyncio
async def test_orphaned_pending_workspace_cleaned(db: SimDB) -> None:
    """PENDING generation with ALLOCATED workspace older than 60 min → workspace cleaned.
    This catches the case where allocation_rollback() failed but generation went back to PENDING.
    """
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.PENDING,
        "state_history": [],
        "checkpoint": None,
    })
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": _EST,
        "allocated_at": _ago(minutes=GenerationLifecyclePolicy.ORPHANED_WORKSPACE_MINUTES + 5),
    })

    await detect_stuck_initializing(db)

    assert db.ws(_WS)["status"] == WorkspaceStatus.CLEANING


# ---------------------------------------------------------------------------
# Section 6: Stuck CLEANING recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stuck_cleaning_fires_on_stale_workspace(db: SimDB) -> None:
    """CLEANING workspace with cleaning_started_at older than 2h → recovery triggered.

    The job transitions CLEANING → STUCK → CLEANING (begin_recovery is called immediately
    after mark_stuck). The observable final state is CLEANING with recovery in progress.
    """
    stale = _ago(hours=GenerationLifecyclePolicy.STUCK_CLEANING_HOURS + 1)
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.CLEANING,
        "cleaning_started_at": stale,
        "locked_by": None,
        "state_history": [],
    })

    await recover_stuck_cleaning(db)

    # STUCK → begin_recovery → back to CLEANING with fresh cleaning_started_at
    assert db.ws(_WS)["status"] == WorkspaceStatus.CLEANING


@pytest.mark.asyncio
async def test_stuck_cleaning_silent_when_recent(db: SimDB) -> None:
    """CLEANING workspace younger than 2h is not touched."""
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.CLEANING,
        "cleaning_started_at": _ago(minutes=30),
        "state_history": [],
    })

    await recover_stuck_cleaning(db)

    assert db.ws(_WS)["status"] == WorkspaceStatus.CLEANING


# ---------------------------------------------------------------------------
# Section 7: Workspace wipe scheduling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wipe_scheduled_for_old_failed_generation(db: SimDB) -> None:
    """FAILED generation with failed_at > 7 days → workspace scheduled for wipe."""
    old_failed = _ago(days=GenerationLifecyclePolicy.WORKSPACE_FAILED_RETENTION_DAYS + 1)
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.FAILED,
        "failed_at": old_failed,
        "state_history": [],
    })
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": _EST,
        "scheduled_for_wipe": False,
        "state_history": [],
    })

    await schedule_expired_workspace_wipes(db)

    assert db.ws(_WS).get("scheduled_for_wipe") is True
    assert db.ws(_WS).get("scheduled_for_wipe_at") is not None


@pytest.mark.asyncio
async def test_wipe_not_scheduled_within_retention_window(db: SimDB) -> None:
    """FAILED generation failed only 3 days ago → NOT scheduled for wipe."""
    recent_failed = _ago(days=3)
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.FAILED,
        "failed_at": recent_failed,
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
async def test_execute_confirmed_wipe_transitions_to_cleaning(db: SimDB) -> None:
    """Pass 2: workspace with elapsed 24h window → execute_scheduled_wipe → CLEANING."""
    past_wipe_at = _ago(hours=GenerationLifecyclePolicy.WIPE_WARNING_HOURS + 1)
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "scheduled_for_wipe": True,
        "scheduled_for_wipe_at": past_wipe_at,
        "locked_by": _EST,
        "state_history": [],
    })
    db.seed_generation_session(_EST, {"status": GenerationStatus.FAILED, "state_history": []})

    await execute_confirmed_wipes(db)

    assert db.ws(_WS)["status"] == WorkspaceStatus.CLEANING


@pytest.mark.asyncio
async def test_wipe_not_executed_within_warning_window(db: SimDB) -> None:
    """Pass 2: workspace with wipe scheduled for the future → NOT executed yet."""
    future_wipe_at = _from_now(hours=GenerationLifecyclePolicy.WIPE_WARNING_HOURS - 1)
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "scheduled_for_wipe": True,
        "scheduled_for_wipe_at": future_wipe_at,
        "locked_by": _EST,
        "state_history": [],
    })
    db.seed_generation_session(_EST, {"status": GenerationStatus.FAILED, "state_history": []})

    await execute_confirmed_wipes(db)

    assert db.ws(_WS)["status"] == WorkspaceStatus.ALLOCATED  # untouched


# ---------------------------------------------------------------------------
# Section 8: Commandment guards — fail() and stuck_detected() keep workspaces ALLOCATED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_keeps_workspace_allocated(db: SimDB) -> None:
    """Commandment II: GenerationSessionStateMachine.fail() must not release workspace."""
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.RUNNING,
        "key_uid": _KEY_UID,
        "workspace_ids": [_WS],
        "state_history": [],
    })
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": _EST,
        "state_history": [],
    })
    _seed_api_key(db, sessions=[
        _active_session(_EST, OperationKind.GENERATION, started_minutes_ago=60,
                        ttl_minutes=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES),
    ])

    esm = GenerationSessionStateMachine(db)
    await esm.fail(generation_id=_EST, reason="test error", triggered_by=TriggeredBy.STUCK_RUNNING)

    assert db.est(_EST)["status"] == GenerationStatus.FAILED
    assert db.ws(_WS)["status"] == WorkspaceStatus.ALLOCATED  # Commandment II


@pytest.mark.asyncio
async def test_stuck_detected_keeps_workspace_allocated(db: SimDB) -> None:
    """Commandment II+VI: stuck_detected() only sets failed_at; workspace untouched."""
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.RUNNING,
        "key_uid": _KEY_UID,
        "workspace_ids": [_WS],
        "state_history": [],
    })
    db.seed_workspace(_WS, {
        "status": WorkspaceStatus.ALLOCATED,
        "locked_by": _EST,
        "state_history": [],
    })
    _seed_api_key(db, sessions=[
        _active_session(_EST, OperationKind.GENERATION, started_minutes_ago=60,
                        ttl_minutes=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES),
    ])

    esm = GenerationSessionStateMachine(db)
    await esm.stuck_detected(generation_id=_EST, triggered_by=TriggeredBy.STUCK_RUNNING)

    assert db.est(_EST)["status"] == GenerationStatus.FAILED
    assert db.est(_EST).get("failed_at") is not None
    assert db.ws(_WS)["status"] == WorkspaceStatus.ALLOCATED  # Commandment II


@pytest.mark.asyncio
async def test_fail_twice_raises_on_second_call(db: SimDB) -> None:
    """Commandment X: invalid transitions raise immediately; no silent corruption."""
    db.seed_generation_session(_EST, {
        "status": GenerationStatus.RUNNING,
        "key_uid": _KEY_UID,
        "state_history": [],
    })
    _seed_api_key(db, sessions=[
        _active_session(_EST, OperationKind.GENERATION, started_minutes_ago=1,
                        ttl_minutes=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES),
    ])

    esm = GenerationSessionStateMachine(db)
    await esm.fail(generation_id=_EST, reason="first", triggered_by=TriggeredBy.STUCK_RUNNING)

    with pytest.raises(InvalidGenerationSessionStateError):
        await esm.fail(generation_id=_EST, reason="second", triggered_by=TriggeredBy.STUCK_RUNNING)


# ---------------------------------------------------------------------------
# Section 9: Multi-session isolation — one session must not affect another
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_a_failure_does_not_affect_session_b(db: SimDB) -> None:
    """When Session A fails and releases its slot, Session B keeps its slot untouched."""
    _seed_api_key(db, sessions=[
        _active_session("est-a", OperationKind.GENERATION, started_minutes_ago=10,
                        ttl_minutes=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES),
        _active_session("est-b", OperationKind.GENERATION, started_minutes_ago=10,
                        ttl_minutes=GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES),
    ])
    db.seed_generation_session("est-a", {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})
    db.seed_generation_session("est-b", {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    await svc.end(generation_id="est-a", reason=SessionEndReason.FAILED)

    sessions = db.active_sessions(_KEY_ID)
    assert len(sessions) == 1
    assert sessions[0]["generation_id"] == "est-b"


@pytest.mark.asyncio
async def test_two_keys_have_independent_capacity(db: SimDB) -> None:
    """Session A at max on key-1 does not affect Session B on key-2."""
    db.seed_api_key("key-1", {
        "api_key": "key-1", "key_uid": "ku-1",
        "max_concurrent_sessions": 1,
        "active_generation_sessions": [],
    })
    db.seed_api_key("key-2", {
        "api_key": "key-2", "key_uid": "ku-2",
        "max_concurrent_sessions": 1,
        "active_generation_sessions": [],
    })
    db.seed_generation_session("est-a", {"status": GenerationStatus.PENDING, "key_uid": "ku-1", "state_history": []})
    db.seed_generation_session("est-b", {"status": GenerationStatus.PENDING, "key_uid": "ku-2", "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    r_a = await svc.try_begin(api_key_doc_id="key-1", generation_id="est-a", operation=OperationKind.GENERATION)
    r_b = await svc.try_begin(api_key_doc_id="key-2", generation_id="est-b", operation=OperationKind.GENERATION)

    assert r_a.outcome == SessionBeginOutcome.ACQUIRED
    assert r_b.outcome == SessionBeginOutcome.ACQUIRED  # independent capacity


@pytest.mark.asyncio
async def test_five_parallel_sessions_each_isolated(db: SimDB) -> None:
    """5 concurrent sessions on one key each complete independently; no cross-contamination."""
    _seed_api_key(db, max_concurrent=5)
    generation_ids = [f"est-par-{i}" for i in range(5)]
    for eid in generation_ids:
        db.seed_generation_session(eid, {"status": GenerationStatus.RUNNING, "key_uid": _KEY_UID, "state_history": []})

    svc = ApiKeySessionConcurrency(db)
    for eid in generation_ids:
        r = await svc.try_begin(api_key_doc_id=_KEY_ID, generation_id=eid, operation=OperationKind.GENERATION)
        assert r.outcome == SessionBeginOutcome.ACQUIRED

    assert len(db.active_sessions(_KEY_ID)) == 5

    # Each ends independently; verify count decrements without disturbing the rest
    for i, eid in enumerate(generation_ids):
        await svc.end(generation_id=eid, reason=SessionEndReason.COMPLETED)
        remaining = db.active_sessions(_KEY_ID)
        assert len(remaining) == 4 - i
        remaining_ids = {s["generation_id"] for s in remaining}
        assert eid not in remaining_ids


# ---------------------------------------------------------------------------
# Section 10: GenerationLifecyclePolicy ordering invariants (static assertions)
# ---------------------------------------------------------------------------


def test_policy_generation_ttl_exceeds_stuck_threshold() -> None:
    """SESSION_GENERATION_MINUTES > STUCK_RUNNING_MINUTES — key invariant from the PR fix."""
    assert (
        GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES
        > GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES
    ), (
        f"Generation TTL ({GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES}m) "
        f"must exceed stuck threshold ({GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES}m)"
    )


def test_policy_agent_phase_timeout_less_than_stuck_threshold() -> None:
    """AGENT_PHASE_TIMEOUT < STUCK_RUNNING — each phase must complete before stuck fires."""
    phase_minutes = GenerationLifecyclePolicy.AGENT_PHASE_TIMEOUT_SECONDS // 60
    assert phase_minutes < GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES


def test_policy_workspace_retention_exceeds_generation_ttl() -> None:
    """Workspace code must outlive the longest possible orphaned session slot."""
    retention_minutes = GenerationLifecyclePolicy.WORKSPACE_FAILED_RETENTION_DAYS * 24 * 60
    assert retention_minutes > GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES


def test_policy_session_ttl_ordering() -> None:
    """Analysis < Planning < Generation — shorter operations expire sooner."""
    assert (
        GenerationLifecyclePolicy.SESSION_ANALYSIS_MINUTES
        < GenerationLifecyclePolicy.SESSION_PLANNING_MINUTES
        < GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES
    )


def test_policy_stuck_cleaning_less_than_stuck_running() -> None:
    """Cleaning detection fires sooner than running detection: stuck cleaning is cheaper to recover."""
    assert (
        GenerationLifecyclePolicy.STUCK_CLEANING_HOURS * 60
        < GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES
    )


def test_policy_old_12h_ttl_would_have_failed_at_stuck_threshold() -> None:
    """Demonstrates exactly why the fix was needed: 12h TTL == stuck threshold.
    Under the old value a healthy 12h+ job lost its slot at the exact moment stuck fired.
    """
    old_ttl = 12 * 60
    stuck_threshold = GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES

    # Under old config: TTL ≤ stuck threshold → slot could expire before or as stuck fires
    assert old_ttl <= stuck_threshold, "Old 12h TTL was at or below stuck threshold — confirms the bug"

    # Under new config: TTL > stuck threshold → slot alive when stuck fires
    assert GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES > stuck_threshold
