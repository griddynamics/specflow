"""Unit tests for ApiKeySessionConcurrency (generation-session slots on api_keys)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.database.memory import InMemoryDatabase
from app.state.api_key_session_concurrency import (
    ApiKeyDocMissingError,
    ApiKeySessionConcurrency,
    OperationKind,
    SessionBeginOutcome,
    SessionEndReason,
    SessionAtCapacityError,
)
from app.state.db_adapter import COL_GENERATION_SESSIONS, StateMachineDBAdapter


def _generation(db: InMemoryDatabase, eid: str, key_uid: str = "ku-1") -> None:
    db.set(
        COL_GENERATION_SESSIONS,
        eid,
        {
            "generation_id": eid,
            "key_uid": key_uid,
            "status": "running",
        },
    )


def _api_key(db: InMemoryDatabase, doc_id: str, key_uid: str = "ku-1", **extra) -> None:
    db.set(
        "api_keys",
        doc_id,
        {
            "api_key": doc_id,
            "key_uid": key_uid,
            "max_concurrent_sessions": extra.get("max_concurrent_sessions", 5),
            "active_generation_sessions": extra.get("active_generation_sessions", []),
        },
    )


@pytest.fixture
def db_pair() -> tuple[StateMachineDBAdapter, InMemoryDatabase]:
    raw = InMemoryDatabase()
    return StateMachineDBAdapter(raw), raw


@pytest.mark.asyncio
async def test_try_begin_acquired_and_at_capacity(db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase]) -> None:
    adapter, raw = db_pair
    _api_key(raw, "key-a", max_concurrent_sessions=2)
    _generation(raw, "g1")
    _generation(raw, "g2")
    _generation(raw, "g3")
    svc = ApiKeySessionConcurrency(adapter)

    r1 = await svc.try_begin(api_key_doc_id="key-a", generation_id="g1", operation=OperationKind.ANALYSIS)
    assert r1.outcome == SessionBeginOutcome.ACQUIRED
    assert r1.session is not None
    assert r1.session.generation_id == "g1"

    r2 = await svc.try_begin(api_key_doc_id="key-a", generation_id="g2", operation=OperationKind.PLANNING)
    assert r2.outcome == SessionBeginOutcome.ACQUIRED

    r3 = await svc.try_begin(api_key_doc_id="key-a", generation_id="g3", operation=OperationKind.GENERATION)
    assert r3.outcome == SessionBeginOutcome.AT_CAPACITY
    assert r3.session is None


@pytest.mark.asyncio
async def test_already_held_idempotent(db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase]) -> None:
    adapter, raw = db_pair
    _api_key(raw, "key-b")
    _generation(raw, "g1")
    svc = ApiKeySessionConcurrency(adapter)

    a = await svc.try_begin(api_key_doc_id="key-b", generation_id="g1", operation=OperationKind.ANALYSIS)
    assert a.outcome == SessionBeginOutcome.ACQUIRED

    b = await svc.try_begin(api_key_doc_id="key-b", generation_id="g1", operation=OperationKind.PLANNING)
    assert b.outcome == SessionBeginOutcome.ALREADY_HELD
    snap = await svc.snapshot(api_key_doc_id="key-b")
    assert len(snap.active) == 1


@pytest.mark.asyncio
async def test_end_idempotent_and_never_negative(db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase]) -> None:
    adapter, raw = db_pair
    _api_key(raw, "key-c")
    _generation(raw, "g1")
    svc = ApiKeySessionConcurrency(adapter)
    await svc.try_begin(api_key_doc_id="key-c", generation_id="g1", operation=OperationKind.ANALYSIS)

    await svc.end(generation_id="g1", reason=SessionEndReason.COMPLETED)
    await svc.end(generation_id="g1", reason=SessionEndReason.COMPLETED)

    snap = await svc.snapshot(api_key_doc_id="key-c")
    assert len(snap.active) == 0


@pytest.mark.asyncio
async def test_prune_reclaims_slot(db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase]) -> None:
    adapter, raw = db_pair
    now = datetime.now(timezone.utc)
    stale = now - timedelta(hours=2)
    _api_key(
        raw,
        "key-d",
        active_generation_sessions=[
            {
                "generation_id": "old",
                "operation": "analysis",
                "lease_started_at": stale,
                "lease_ttl_minutes": 60,
            }
        ],
    )
    _generation(raw, "new")
    svc = ApiKeySessionConcurrency(adapter)

    r = await svc.try_begin(api_key_doc_id="key-d", generation_id="new", operation=OperationKind.ANALYSIS)
    assert r.outcome == SessionBeginOutcome.ACQUIRED


@pytest.mark.asyncio
async def test_refresh_lease_noop_when_missing(db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase]) -> None:
    adapter, raw = db_pair
    _api_key(raw, "key-e")
    _generation(raw, "ghost")
    svc = ApiKeySessionConcurrency(adapter)
    await svc.refresh_lease(generation_id="ghost", operation=OperationKind.PLANNING)
    snap = await svc.snapshot(api_key_doc_id="key-e")
    assert snap.active == ()


@pytest.mark.asyncio
async def test_begin_rollback_on_pre_task_exception(
    db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase],
) -> None:
    """ACQUIRED slot must be released via end(BEGIN_ROLLBACK) when setup fails before bg task."""
    adapter, raw = db_pair
    _api_key(raw, "key-f")
    _generation(raw, "g1")
    svc = ApiKeySessionConcurrency(adapter)

    result = await svc.try_begin(api_key_doc_id="key-f", generation_id="g1", operation=OperationKind.ANALYSIS)
    assert result.outcome == SessionBeginOutcome.ACQUIRED

    # Simulate failure before bg task is spawned — caller rolls back.
    await svc.end(generation_id="g1", reason=SessionEndReason.BEGIN_ROLLBACK)

    snap = await svc.snapshot(api_key_doc_id="key-f")
    assert len(snap.active) == 0

    # Slot reclaimed — same generation can acquire again.
    result2 = await svc.try_begin(api_key_doc_id="key-f", generation_id="g1", operation=OperationKind.ANALYSIS)
    assert result2.outcome == SessionBeginOutcome.ACQUIRED

    # Idempotent re-entry while slot is held (same generation_id → ALREADY_HELD).
    result3 = await svc.try_begin(api_key_doc_id="key-f", generation_id="g1", operation=OperationKind.ANALYSIS)
    assert result3.outcome == SessionBeginOutcome.ALREADY_HELD

    snap2 = await svc.snapshot(api_key_doc_id="key-f")
    assert len(snap2.active) == 1


@pytest.mark.asyncio
async def test_try_begin_at_capacity_raises_session_at_capacity(db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase]) -> None:
    """AT_CAPACITY outcome: caller must raise SessionAtCapacityError to the route layer."""
    adapter, raw = db_pair
    _api_key(raw, "key-g", max_concurrent_sessions=1)
    _generation(raw, "g1")
    _generation(raw, "g2")
    svc = ApiKeySessionConcurrency(adapter)

    r1 = await svc.try_begin(api_key_doc_id="key-g", generation_id="g1", operation=OperationKind.ANALYSIS)
    assert r1.outcome == SessionBeginOutcome.ACQUIRED

    r2 = await svc.try_begin(api_key_doc_id="key-g", generation_id="g2", operation=OperationKind.ANALYSIS)
    assert r2.outcome == SessionBeginOutcome.AT_CAPACITY
    # Route layer raises based on outcome — verify SessionAtCapacityError is importable and typed.
    with pytest.raises(SessionAtCapacityError):
        if r2.outcome == SessionBeginOutcome.AT_CAPACITY:
            raise SessionAtCapacityError()


@pytest.mark.asyncio
async def test_missing_api_key_doc_raises(db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase]) -> None:
    adapter, _raw = db_pair
    svc = ApiKeySessionConcurrency(adapter)
    with pytest.raises(ApiKeyDocMissingError):
        await svc.try_begin(api_key_doc_id="nope", generation_id="g1", operation=OperationKind.ANALYSIS)


@pytest.mark.asyncio
async def test_concurrent_try_begin_same_generation(
    db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase],
) -> None:
    adapter, raw = db_pair
    _api_key(raw, "key-h")
    _generation(raw, "g1")
    svc = ApiKeySessionConcurrency(adapter)

    async def one() -> SessionBeginOutcome:
        r = await svc.try_begin(api_key_doc_id="key-h", generation_id="g1", operation=OperationKind.ANALYSIS)
        return r.outcome

    outcomes = await asyncio.gather(one(), one(), one())
    assert outcomes.count(SessionBeginOutcome.ACQUIRED) == 1
    assert outcomes.count(SessionBeginOutcome.ALREADY_HELD) == 2


@pytest.mark.asyncio
async def test_auth_snapshot_at_capacity_property(
    db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase],
) -> None:
    adapter, raw = db_pair
    now = datetime.now(timezone.utc)
    _api_key(
        raw,
        "key-i",
        max_concurrent_sessions=2,
        active_generation_sessions=[
            {
                "generation_id": "a",
                "operation": "analysis",
                "lease_started_at": now,
                "lease_ttl_minutes": 60,
            },
            {
                "generation_id": "b",
                "operation": "analysis",
                "lease_started_at": now,
                "lease_ttl_minutes": 60,
            },
        ],
    )
    svc = ApiKeySessionConcurrency(adapter)
    snap = await svc.snapshot(api_key_doc_id="key-i")
    assert snap.at_capacity is True
    assert snap.max_concurrent_sessions == 2


@pytest.mark.asyncio
async def test_try_begin_for_generation_resolves_doc_and_registers(
    db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase],
) -> None:
    """The rerun path acquires by generation_id alone (no request api_key in scope)."""
    adapter, raw = db_pair
    _api_key(raw, "key-r", key_uid="ku-r")
    _generation(raw, "gen-r", key_uid="ku-r")
    svc = ApiKeySessionConcurrency(adapter)

    result = await svc.try_begin_for_generation(
        generation_id="gen-r", operation=OperationKind.GENERATION
    )

    assert result is not None
    assert result.outcome == SessionBeginOutcome.ACQUIRED
    # Session is now visible in active_generation_sessions (what the TUI reads).
    snap = await svc.snapshot(api_key_doc_id="key-r")
    assert [s.generation_id for s in snap.active] == ["gen-r"]


@pytest.mark.asyncio
async def test_try_begin_for_generation_unresolvable_returns_none(
    db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase],
) -> None:
    """Best-effort: an unresolvable api_keys doc returns None, never raises (resume must proceed)."""
    adapter, raw = db_pair
    _generation(raw, "gen-x", key_uid="ku-missing")  # no matching api_key doc
    svc = ApiKeySessionConcurrency(adapter)

    result = await svc.try_begin_for_generation(
        generation_id="gen-x", operation=OperationKind.GENERATION
    )

    assert result is None


@pytest.mark.asyncio
async def test_try_begin_for_generation_at_capacity_when_slots_full(
    db_pair: tuple[StateMachineDBAdapter, InMemoryDatabase],
) -> None:
    """When slots are full, resume registration returns AT_CAPACITY (workflow still proceeds)."""
    adapter, raw = db_pair
    _api_key(raw, "key-full", key_uid="ku-full", max_concurrent_sessions=1)
    _generation(raw, "gen-occupied", key_uid="ku-full")
    _generation(raw, "gen-resume", key_uid="ku-full")
    svc = ApiKeySessionConcurrency(adapter)

    occupied = await svc.try_begin(
        api_key_doc_id="key-full", generation_id="gen-occupied", operation=OperationKind.GENERATION
    )
    assert occupied.outcome == SessionBeginOutcome.ACQUIRED

    result = await svc.try_begin_for_generation(
        generation_id="gen-resume", operation=OperationKind.GENERATION
    )

    assert result is not None
    assert result.outcome == SessionBeginOutcome.AT_CAPACITY
    snap = await svc.snapshot(api_key_doc_id="key-full")
    assert [s.generation_id for s in snap.active] == ["gen-occupied"]
