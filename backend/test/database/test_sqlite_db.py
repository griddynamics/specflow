"""
SqliteDatabase contract + datetime tests.

Runs the shared IDatabase contract (db_contract.py) against the SQLite backend so it
must satisfy the exact same behavior as the in-memory reference, plus SQLite-specific
datetime-boundary tests (the one place "works on memory" can differ from "works
persisted").
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.database.sqlite import SqliteDatabase

from test.database.db_contract import (
    TestArrayOperations as _TestArrayOperations,
    TestBasicCRUD as _TestBasicCRUD,
    TestIsolation as _TestIsolation,
    TestQuery as _TestQuery,
    TestServerTimestamp as _TestServerTimestamp,
    TestTransactions as _TestTransactions,
)


@pytest.fixture
def db():
    """Create a fresh in-memory SQLite database for each test."""
    database = SqliteDatabase(":memory:")
    yield database
    database.close()


class TestBasicCRUD(_TestBasicCRUD):
    pass


class TestQuery(_TestQuery):
    pass


class TestTransactions(_TestTransactions):
    pass


class TestArrayOperations(_TestArrayOperations):
    pass


class TestServerTimestamp(_TestServerTimestamp):
    pass


class TestIsolation(_TestIsolation):
    pass


class TestSqliteDatetime:
    """Datetime contract: round-trips as tz-aware UTC and compares correctly in SQL."""

    def test_datetime_roundtrip_is_tz_aware(self, db):
        stored = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db.set("c", "d", {"ts": stored})

        got = db.get("c", "d")["ts"]
        assert isinstance(got, datetime)
        assert got.tzinfo is not None
        assert got == stored

    def test_naive_datetime_assumed_utc(self, db):
        db.set("c", "d", {"ts": datetime(2026, 1, 1, 12, 0, 0)})

        got = db.get("c", "d")["ts"]
        assert got == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_datetime_filter_less_than(self, db):
        now = datetime.now(timezone.utc)
        db.set("gen", "old", {"last_activity_at": now - timedelta(minutes=60)})
        db.set("gen", "fresh", {"last_activity_at": now - timedelta(minutes=1)})

        cutoff = now - timedelta(minutes=30)
        results = db.query("gen", filters=[("last_activity_at", "<", cutoff)])

        assert {r["_id"] for r in results} == {"old"}

    def test_none_datetime_excluded_by_less_than(self, db):
        """None last_activity_at must be invisible to '<' (matches reference impl)."""
        now = datetime.now(timezone.utc)
        db.set("gen", "none", {"last_activity_at": None})

        results = db.query("gen", filters=[("last_activity_at", "<", now)])
        assert results == []

    def test_nested_datetime_in_list_roundtrips(self, db):
        ts = datetime(2026, 3, 1, 9, 30, 0, tzinfo=timezone.utc)
        db.set("api_keys", "k", {
            "active_generation_sessions": [{"generation_id": "g1", "lease_started_at": ts}],
        })

        got = db.get("api_keys", "k")
        assert got["active_generation_sessions"][0]["lease_started_at"] == ts


class TestSqliteRelationalSchema:
    """Known collections are real relational tables — not JSON rows in one `documents` table."""

    def _tables(self, db):
        rows = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}

    def test_known_collections_have_dedicated_tables(self, db):
        tables = self._tables(db)
        assert {"api_keys", "generation_sessions", "workspaces"} <= tables
        # Generic fallback + subcollection tables still exist.
        assert {"documents", "subdocuments"} <= tables

    def test_promoted_columns_are_populated_on_write(self, db):
        db.set("generation_sessions", "gen-1", {
            "status": "running",
            "key_uid": "uid-abc",
            "note": "not a promoted column",
        })

        row = db._conn.execute(
            "SELECT status, key_uid FROM generation_sessions WHERE doc_id = ?",
            ("gen-1",),
        ).fetchone()
        assert row == ("running", "uid-abc")

        # The full document (incl. non-promoted fields) round-trips from the JSON column.
        assert db.get("generation_sessions", "gen-1")["note"] == "not a promoted column"

    def test_workspace_promoted_columns_populated(self, db):
        db.set("workspaces", "ws-1", {
            "status": "available",
            "workspace_pool": "standard",
            "set_number": 3,
        })
        row = db._conn.execute(
            "SELECT status, workspace_pool, set_number FROM workspaces WHERE doc_id = ?",
            ("ws-1",),
        ).fetchone()
        assert row == ("available", "standard", 3)

    def test_datetime_promoted_column_stored_as_iso_text(self, db):
        ts = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        db.set("generation_sessions", "gen-ts", {"last_activity_at": ts})

        raw = db._conn.execute(
            "SELECT last_activity_at FROM generation_sessions WHERE doc_id = ?",
            ("gen-ts",),
        ).fetchone()[0]
        assert isinstance(raw, str)
        assert raw == "2026-01-02T03:04:05.000000+00:00"

    def test_unregistered_collection_falls_back_to_documents(self, db):
        db.set("widgets", "w-1", {"color": "blue"})

        # No dedicated table was created; the row lives in the generic documents table.
        assert "widgets" not in self._tables(db)
        count = db._conn.execute(
            "SELECT COUNT(*) FROM documents WHERE collection = ?", ("widgets",)
        ).fetchone()[0]
        assert count == 1
        assert db.get("widgets", "w-1") == {"color": "blue"}

    def test_query_on_promoted_column_uses_index(self, db):
        plan = db._conn.execute(
            "EXPLAIN QUERY PLAN SELECT doc_id, data FROM generation_sessions "
            "WHERE status = ? AND last_activity_at < ?",
            ("running", "2026-01-01T00:00:00.000000+00:00"),
        ).fetchall()
        detail = " ".join(str(part) for row in plan for part in row)
        # A real indexed search, not a full-table scan of a JSON blob column.
        assert "USING INDEX" in detail
        assert "SCAN" not in detail

    def test_get_api_key_by_uid_returns_document(self, db):
        db.set("api_keys", "secret-key", {
            "key_uid": "uid-42",
            "user_id": "alice@example.com",
            "is_active": True,
        })
        got = db.get_api_key_by_uid("uid-42")
        assert got is not None
        assert got["_id"] == "secret-key"
        assert got["user_id"] == "alice@example.com"
        assert db.get_api_key_by_uid("missing") is None


class TestSqlitePersistence:
    """A SQLite file persists across connections (the whole point vs in-memory)."""

    def test_state_survives_reopen(self, tmp_path):
        db_path = str(tmp_path / "specflow.db")

        db1 = SqliteDatabase(db_path)
        db1.set("generation_sessions", "est-1", {"status": "running"})
        db1.close()

        db2 = SqliteDatabase(db_path)
        try:
            result = db2.get("generation_sessions", "est-1")
            assert result == {"status": "running"}
        finally:
            db2.close()


@pytest.mark.asyncio
async def test_stuck_running_detector_against_sqlite():
    """
    End-to-end datetime boundary: a RUNNING generation with a stale last_activity_at
    stored in SQLite must be detected and marked FAILED by the background job (proves
    tz-aware cutoff vs SQL-stored datetime agree, and that the async
    StateMachineDBAdapter works on the SQLite backend).
    """
    from app.database.sqlite import SqliteDatabase as _SqliteDatabase
    from app.state.db_adapter import StateMachineDBAdapter, COL_GENERATION_SESSIONS
    from app.jobs.stuck_running_detector import detect_stuck_running
    from app.schemas.generation_workflow_enums import GenerationStatus

    raw = _SqliteDatabase(":memory:")
    adapter = StateMachineDBAdapter(raw)

    now = datetime.now(timezone.utc)
    raw.set(COL_GENERATION_SESSIONS, "est-stale", {
        "status": GenerationStatus.RUNNING,
        "last_activity_at": now - timedelta(minutes=60),
        "state_history": [],
    })

    try:
        await detect_stuck_running(adapter, threshold_minutes=30)
        est = raw.get(COL_GENERATION_SESSIONS, "est-stale")
        assert est["status"] == GenerationStatus.FAILED
        assert est.get("failed_at") is not None
    finally:
        raw.close()
