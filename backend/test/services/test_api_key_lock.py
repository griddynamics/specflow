"""Regression tests for generation-session begin semantics (replaces legacy api_key_lock)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.database.memory import InMemoryDatabase
from app.state.api_key_session_concurrency import (
    ApiKeyDocMissingError,
    ApiKeySessionConcurrency,
    OperationKind,
    SessionBeginOutcome,
)
from app.state.db_adapter import COL_GENERATION_SESSIONS, StateMachineDBAdapter


@pytest.fixture()
def db():
    d = InMemoryDatabase()
    yield d
    d.clear()


@pytest.fixture()
def adapter(db):
    return StateMachineDBAdapter(db)


@pytest.mark.asyncio
async def test_try_begin_raises_when_api_key_doc_missing(adapter):
    """Missing api_keys doc raises ApiKeyDocMissingError (distinct from at-capacity)."""
    svc = ApiKeySessionConcurrency(adapter)
    with pytest.raises(ApiKeyDocMissingError) as exc_info:
        await svc.try_begin(
            api_key_doc_id="missing-key",
            generation_id="est-1",
            operation=OperationKind.ANALYSIS,
        )
    assert exc_info.value.api_key_doc_id == "missing-key"


@pytest.mark.asyncio
async def test_try_begin_acquired_writes_active_sessions(db, adapter):
    db.set(
        "api_keys",
        "key-ok",
        {"api_key": "key-ok", "key_uid": "u1", "active_generation_sessions": [], "max_concurrent_sessions": 5},
    )
    db.set(COL_GENERATION_SESSIONS, "est-abc", {"generation_id": "est-abc", "key_uid": "u1", "status": "pending"})

    svc = ApiKeySessionConcurrency(adapter)
    result = await svc.try_begin(
        api_key_doc_id="key-ok",
        generation_id="est-abc",
        operation=OperationKind.ANALYSIS,
    )
    assert result.outcome == SessionBeginOutcome.ACQUIRED
    doc = db.get("api_keys", "key-ok")
    assert len(doc.get("active_generation_sessions") or []) == 1
    assert doc["active_generation_sessions"][0]["generation_id"] == "est-abc"


@pytest.mark.asyncio
async def test_try_begin_at_capacity_when_slots_full(db, adapter):
    now = datetime.now(timezone.utc)
    db.set(
        "api_keys",
        "key-locked",
        {
            "api_key": "key-locked",
            "key_uid": "u1",
            "max_concurrent_sessions": 1,
            "active_generation_sessions": [
                {
                    "generation_id": "est-other",
                    "operation": "analysis",
                    "lease_started_at": now,
                    "lease_ttl_minutes": 90,
                }
            ],
        },
    )
    db.set(COL_GENERATION_SESSIONS, "est-new", {"generation_id": "est-new", "key_uid": "u1", "status": "pending"})

    svc = ApiKeySessionConcurrency(adapter)
    result = await svc.try_begin(
        api_key_doc_id="key-locked",
        generation_id="est-new",
        operation=OperationKind.ANALYSIS,
    )
    assert result.outcome == SessionBeginOutcome.AT_CAPACITY
