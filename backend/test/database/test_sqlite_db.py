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
    TestTransactions as _TestTransactions,
)


@pytest.fixture
def db():
    """Create a fresh in-memory SQLite database for each test."""
    database = SqliteDatabase(":memory:")
    yield database
    database.close()


def test_every_used_collection_is_registered():
    """Every collection/subcollection the app addresses must map to a registered table.

    The SQLite backend does not check this at runtime — an unregistered name is a
    developer error (all names are internal constants), so this test is the guard: adding
    a collection without a table fails here at CI, not with a KeyError in production.
    """
    from app.database.sqlite import _TABLE
    from app.schemas.workspace_model_usage_store import WORKSPACE_MODEL_USAGE_SUBCOLLECTION
    from app.state.db_adapter import (
        COL_API_KEYS,
        COL_GENERATION_SESSIONS,
        COL_WORKSPACES,
    )

    used = {
        COL_API_KEYS,
        COL_GENERATION_SESSIONS,
        COL_WORKSPACES,
        WORKSPACE_MODEL_USAGE_SUBCOLLECTION,
    }
    missing = used - set(_TABLE)
    assert not missing, f"collections used by the app but not registered in sqlite._TABLE: {missing}"


class TestBasicCRUD(_TestBasicCRUD):
    pass


class TestQuery(_TestQuery):
    pass


class TestTransactions(_TestTransactions):
    pass


class TestArrayOperations(_TestArrayOperations):
    pass


class TestIsolation(_TestIsolation):
    pass


class TestSqliteDatetime:
    """Datetime contract: round-trips as tz-aware UTC and compares correctly in SQL."""

    def test_datetime_roundtrip_is_tz_aware(self, db):
        stored = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        db.set("generation_sessions", "d", {"ts": stored})

        got = db.get("generation_sessions", "d")["ts"]
        assert isinstance(got, datetime)
        assert got.tzinfo is not None
        assert got == stored

    def test_naive_datetime_assumed_utc(self, db):
        db.set("generation_sessions", "d", {"ts": datetime(2026, 1, 1, 12, 0, 0)})

        got = db.get("generation_sessions", "d")["ts"]
        assert got == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_datetime_filter_less_than(self, db):
        now = datetime.now(timezone.utc)
        db.set("generation_sessions", "old", {"last_activity_at": now - timedelta(minutes=60)})
        db.set("generation_sessions", "fresh", {"last_activity_at": now - timedelta(minutes=1)})

        cutoff = now - timedelta(minutes=30)
        results = db.query("generation_sessions", filters=[("last_activity_at", "<", cutoff)])

        assert {r["_id"] for r in results} == {"old"}

    def test_none_datetime_excluded_by_less_than(self, db):
        """None last_activity_at must be invisible to '<' (matches reference impl)."""
        now = datetime.now(timezone.utc)
        db.set("generation_sessions", "none", {"last_activity_at": None})

        results = db.query("generation_sessions", filters=[("last_activity_at", "<", now)])
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
        # Subcollections get their own named child table too; no generic catch-all tables.
        assert "workspace_model_usage" in tables
        assert "documents" not in tables
        assert "subdocuments" not in tables

    def test_subcollection_uses_named_table_with_key_columns(self, db):
        db.run_transaction(
            lambda tx: tx.set_subdocument(
                "generation_sessions", "gen-1", "workspace_model_usage", "ws-a",
                {"total_usd_cost": 1.5},
            )
        )
        # Real, inspectable key columns — not opaque parent_collection/subcollection rows.
        row = db._conn.execute(
            "SELECT generation_id, workspace_id FROM workspace_model_usage"
        ).fetchone()
        assert row == ("gen-1", "ws-a")

        listed = db.list_subcollection("generation_sessions", "gen-1", "workspace_model_usage")
        assert listed[0]["_id"] == "ws-a"
        assert listed[0]["total_usd_cost"] == 1.5

    def test_unregistered_subcollection_fails_loudly(self, db):
        # An unregistered name is a developer error (all names are internal constants),
        # so it just fails loudly via KeyError — see test_every_used_collection_is_registered
        # for the guard that catches it at test time rather than runtime.
        with pytest.raises(KeyError):
            db.list_subcollection("generation_sessions", "gen-1", "unknown_sub")
        with pytest.raises(KeyError):
            db.run_transaction(
                lambda tx: tx.set_subdocument("workspaces", "w", "nope", "d", {})
            )

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

    def test_unregistered_collection_fails_loudly(self, db):
        # No generic catch-all table: an unknown collection is a developer error, so it
        # fails loudly (KeyError) rather than silently landing in an unindexed blob.
        with pytest.raises(KeyError):
            db.set("widgets", "w-1", {"color": "blue"})
        assert "widgets" not in self._tables(db)

        with pytest.raises(KeyError):
            db.query("widgets")

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

    def test_generation_sessions_full_scalar_core_promoted(self, db):
        """Not just queried fields — the whole stable scalar core is a real column."""
        db.set("generation_sessions", "gen-full", {
            "status": "completed",
            "checkpoint": "outputs_archived",
            "started_at": "2026-01-01T00:00:00.000000+00:00",
            "completed_at": "2026-01-02T00:00:00.000000+00:00",
            "error": None,
            "retry_count": 2,
            "max_retries": 3,
            "user_email": "dev@example.com",
            "workspace_pool": "standard",
            "specification_dir": "specs",
            "outputs_archived": True,
            "code_archived": True,
            "archive_status": "confirmed",
            "artifact_path": "/artifacts/gen-full",
            "emergency_archived": False,
            "total_usd_cost": 12.5,
            "state_history": [{"status": "completed"}],  # stays in the JSON blob
        })
        row = db._conn.execute(
            "SELECT checkpoint, retry_count, outputs_archived, code_archived, "
            "emergency_archived, total_usd_cost FROM generation_sessions WHERE doc_id = ?",
            ("gen-full",),
        ).fetchone()
        assert row == ("outputs_archived", 2, 1, 1, 0, 12.5)
        # Arrays never get promoted — they stay queryable via the JSON blob only.
        assert "state_history" not in [c[1] for c in db._conn.execute(
            "PRAGMA table_info(generation_sessions)"
        )]
        assert db.get("generation_sessions", "gen-full")["state_history"] == [{"status": "completed"}]

    def test_workspaces_full_scalar_core_promoted(self, db):
        db.set("workspaces", "ws-full", {
            "status": "allocated",
            "repo_url": "https://github.com/org/ws-1",
            "p10y_repository_id": 12345,
            "locked_at": "2026-01-01T00:00:00.000000+00:00",
            "lease_expires_at": "2026-01-01T01:00:00.000000+00:00",
            "cleaning_started_at": None,
            "last_used_by": "est-1",
            "last_cleaned_at": "2026-01-01T00:00:00.000000+00:00",
            "error": None,
            "stuck_reason": None,
            "force_released": True,
            "force_release_reason": "operator override",
            "force_released_by": "admin",
            "force_released_at": "2026-01-01T00:00:00.000000+00:00",
            "allocation_history": [{"generation_id": "est-1"}],  # stays in the JSON blob
        })
        row = db._conn.execute(
            "SELECT repo_url, p10y_repository_id, last_used_by, force_released, "
            "force_release_reason FROM workspaces WHERE doc_id = ?",
            ("ws-full",),
        ).fetchone()
        assert row == ("https://github.com/org/ws-1", 12345, "est-1", 1, "operator override")
        assert "allocation_history" not in [c[1] for c in db._conn.execute(
            "PRAGMA table_info(workspaces)"
        )]


class TestSqliteSchemaReconciliation:
    """A db file created by an older registry self-upgrades — no reset/migration needed."""

    def test_new_registry_column_is_added_and_backfilled(self, tmp_path):
        import sqlite3
        import json as _json

        db_path = str(tmp_path / "old.db")
        # Hand-build a table with only the ORIGINAL (pre-expansion) workspace columns.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE workspaces (doc_id TEXT NOT NULL, status TEXT, "
            "workspace_pool TEXT, set_number INTEGER, scheduled_for_wipe INTEGER, "
            "scheduled_for_wipe_at TEXT, locked_by TEXT, clean_verified INTEGER, "
            "data TEXT NOT NULL, PRIMARY KEY (doc_id))"
        )
        doc = {"status": "available", "repo_url": "https://github.com/x/y", "force_released": True}
        conn.execute(
            "INSERT INTO workspaces (doc_id, status, data) VALUES (?, ?, ?)",
            ("ws-old", "available", _json.dumps(doc)),
        )
        conn.commit()
        conn.close()

        db = SqliteDatabase(db_path)
        try:
            columns = [c[1] for c in db._conn.execute("PRAGMA table_info(workspaces)")]
            assert "repo_url" in columns
            assert "force_released" in columns

            row = db._conn.execute(
                "SELECT repo_url, force_released FROM workspaces WHERE doc_id = ?",
                ("ws-old",),
            ).fetchone()
            assert row == ("https://github.com/x/y", 1)
        finally:
            db.close()


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
