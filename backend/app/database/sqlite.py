"""
SQLite database implementation for local / single-node persistence.

A SQL-native document store implementing ``IDatabase`` for local/Docker-dev. Every
collection is a real table (registered in ``_TABLES``); the fields actually filtered or
ordered are promoted to typed, indexed columns while the full document lives in a ``data``
JSON column (source of truth on read). What Firestore calls a "subcollection" is just a
table with a compound primary key (e.g. ``workspace_model_usage`` keyed by
``generation_id, workspace_id``) — the Firestore vocabulary survives only in the shared
``IDatabase`` method names, not in the storage. An unregistered table is rejected — there
is no generic blob table. Datetimes are stored as fixed-width ISO-8601 UTC text so lexical
order == chronological order in both the columns and the blob. Single writer only (WAL);
multi-replica stays on Firestore.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from app.database.interface import (
    DocumentNotFoundError,
    FilterTuple,
    IDatabase,
    ITransactionContext,
)

T = TypeVar("T")

_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


# ---------------------------------------------------------------------------
# Physical layout registry. Everything is a table (there is no "subcollection" —
# that is a Firestore word for what SQL calls a table with a compound primary key).
#
# ``columns`` promotes the stable scalar core of each document to real, typed SQL
# columns — every field that is a plain scalar (str/int/float/bool/datetime) written
# by the app, so the table is genuinely inspectable in a SQL browser. Nested/open-ended
# structures (arrays, dicts, per-workflow maps) stay in the JSON `data` blob, which
# remains the source of truth on every read; promoted columns are write-through
# mirrors. ``indexes`` lists only the column combinations something actually
# filters/orders on — promotion for inspectability and indexing for query performance
# are separate concerns.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Table:
    """A SQLite table.

    ``primary_key`` is one column for a document store (``doc_id``) or several for a
    child table (``generation_id, workspace_id``). ``columns`` are the scalar fields
    promoted out of the JSON ``data`` blob. ``indexes`` are the promoted-column
    combinations worth a SQL index. ``parent`` links a child table to its owner
    (cascade-clear + subdocument addressing).
    """

    name: str
    primary_key: tuple[str, ...]
    columns: Dict[str, str] = field(default_factory=dict)
    indexes: tuple[tuple[str, ...], ...] = ()
    parent: Optional[str] = None


_TABLES: tuple[_Table, ...] = (
    _Table(
        "generation_sessions",
        ("doc_id",),
        {
            # Queried/ordered (see indexes below)
            "status": "TEXT",
            "status_changed_at": "TEXT",
            "last_activity_at": "TEXT",
            "shutdown_interrupted": "INTEGER",
            "key_uid": "TEXT",
            "created_at": "TEXT",
            # Rest of the stable scalar core (not queried, promoted for inspectability)
            "checkpoint": "TEXT",
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "failed_at": "TEXT",
            "error": "TEXT",
            "retry_count": "INTEGER",
            "max_retries": "INTEGER",
            "user_email": "TEXT",
            "workspace_pool": "TEXT",
            "specification_dir": "TEXT",
            "outputs_archived": "INTEGER",
            "code_archived": "INTEGER",
            "archive_status": "TEXT",
            "artifact_path": "TEXT",
            "emergency_archived": "INTEGER",
            "total_usd_cost": "REAL",
        },
        (
            ("status", "last_activity_at"),
            ("status", "status_changed_at"),
            ("status", "shutdown_interrupted"),
            ("key_uid", "created_at"),
        ),
    ),
    _Table(
        "workspaces",
        ("doc_id",),
        {
            # Queried/ordered (see indexes below)
            "status": "TEXT",
            "workspace_pool": "TEXT",
            "set_number": "INTEGER",
            "scheduled_for_wipe": "INTEGER",
            "scheduled_for_wipe_at": "TEXT",
            "locked_by": "TEXT",
            "clean_verified": "INTEGER",
            # Rest of the stable scalar core (not queried, promoted for inspectability)
            "repo_url": "TEXT",
            "p10y_repository_id": "INTEGER",
            "locked_at": "TEXT",
            "lease_expires_at": "TEXT",
            "cleaning_started_at": "TEXT",
            "last_used_by": "TEXT",
            "last_cleaned_at": "TEXT",
            "error": "TEXT",
            "stuck_reason": "TEXT",
            "stuck_at": "TEXT",
            "force_released": "INTEGER",
            "force_release_reason": "TEXT",
            "force_released_by": "TEXT",
            "force_released_at": "TEXT",
        },
        (
            ("status",),
            ("workspace_pool", "set_number"),
            ("scheduled_for_wipe", "scheduled_for_wipe_at"),
        ),
    ),
    _Table(
        "api_keys",
        ("doc_id",),
        {
            # Queried/ordered (see indexes below)
            "key_uid": "TEXT",
            # Rest of the stable scalar core (not queried, promoted for inspectability)
            "workspace_pool": "TEXT",
            "user_id": "TEXT",
            "user_name": "TEXT",
            "created_at": "TEXT",
            "last_used_at": "TEXT",
            "expires_at": "TEXT",
            "is_active": "INTEGER",
            "github_token_ciphertext": "TEXT",
            "github_token_set_at": "TEXT",
            "git_user_name": "TEXT",
            "max_concurrent_sessions": "INTEGER",
        },
        (("key_uid",),),
    ),
    _Table(
        "workspace_model_usage",
        ("generation_id", "workspace_id"),
        parent="generation_sessions",
    ),
)

_TABLE: Dict[str, _Table] = {t.name: t for t in _TABLES}


def _canonical_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _encode_for_storage(value: Any) -> Any:
    if isinstance(value, datetime):
        return _canonical_dt(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _encode_for_storage(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode_for_storage(v) for v in value]
    return value


def _decode_from_storage(value: Any) -> Any:
    if isinstance(value, str) and _ISO_DATETIME_RE.match(value):
        return datetime.fromisoformat(value).astimezone(UTC)
    if isinstance(value, dict):
        return {k: _decode_from_storage(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_from_storage(v) for v in value]
    return value


def _to_sql_param(value: Any) -> Any:
    if isinstance(value, datetime):
        return _canonical_dt(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bool):
        return int(value)
    return value


def _json_path(field: str) -> str:
    return "$." + field


class SqliteTransactionContext(ITransactionContext):
    """All connection-level SQL for the SQLite backend, in one place.

    ``SqliteDatabase`` composes one over its own connection and wraps each call with its
    lock; ``run_transaction`` hands the same object to the callback (as an
    ``ITransactionContext``) inside ``BEGIN IMMEDIATE``. These methods do not lock — the
    caller owns concurrency — and callers must read before writing (interface contract).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            f"SELECT data FROM {collection} WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return None if row is None else _decode_from_storage(json.loads(row[0]))

    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        table = _TABLE[collection]
        encoded = _encode_for_storage(data)
        names = list(table.columns)
        col_list = ", ".join(names)
        placeholders = ", ".join("?" for _ in names)
        assignments = ", ".join(f"{n} = excluded.{n}" for n in names)
        values = [_to_sql_param(encoded.get(n)) for n in names]
        self._conn.execute(
            f"INSERT INTO {collection} (doc_id, {col_list}, data) "
            f"VALUES (?, {placeholders}, ?) "
            f"ON CONFLICT(doc_id) DO UPDATE SET {assignments}, data = excluded.data",
            [doc_id, *values, json.dumps(encoded)],
        )

    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        existing = self.get(collection, doc_id)
        if existing is None:
            raise DocumentNotFoundError(collection, doc_id)
        existing.update(data)
        self.set(collection, doc_id, existing)

    def delete(self, collection: str, doc_id: str) -> None:
        self._conn.execute(f"DELETE FROM {collection} WHERE doc_id = ?", (doc_id,))

    def query(
        self,
        collection: str,
        filters: Optional[List[FilterTuple]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        table = _TABLE[collection]

        where: List[str] = []
        params: List[Any] = []
        for f, operator, value in filters or []:
            clause, clause_params = self._filter_clause(table, f, operator, value)
            where.append(clause)
            params.extend(clause_params)

        sql = f"SELECT doc_id, data FROM {collection}"
        if where:
            sql += " WHERE " + " AND ".join(where)

        if order_by:
            descending = order_by.startswith("-")
            expr, expr_params = self._field_expr(table, order_by[1:] if descending else order_by)
            params.extend(expr_params)
            sql += f" ORDER BY {expr} " + ("DESC" if descending else "ASC")

        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        results: List[Dict[str, Any]] = []
        for doc_id, data in self._conn.execute(sql, params).fetchall():
            doc = _decode_from_storage(json.loads(data))
            doc["_id"] = doc_id
            results.append(doc)
        return results

    @staticmethod
    def _field_expr(table: _Table, field: str) -> tuple[str, List[Any]]:
        if field in table.columns:
            return field, []
        return "json_extract(data, ?)", [_json_path(field)]

    @classmethod
    def _filter_clause(
        cls, table: _Table, field: str, operator: str, value: Any
    ) -> tuple[str, List[Any]]:
        expr, expr_params = cls._field_expr(table, field)
        match operator:
            case "==":
                if value is None:
                    return f"{expr} IS NULL", expr_params
                return f"{expr} = ?", [*expr_params, _to_sql_param(value)]
            case "!=":
                if value is None:
                    return f"{expr} IS NOT NULL", expr_params
                # Missing field counts as != value (matches the in-memory/Firestore reference).
                return (
                    f"({expr} <> ? OR {expr} IS NULL)",
                    [*expr_params, _to_sql_param(value), *expr_params],
                )
            case "<" | "<=" | ">" | ">=":
                return f"{expr} {operator} ?", [*expr_params, _to_sql_param(value)]
            case "in":
                values = list(value)
                if not values:
                    return "0", []
                placeholders = ", ".join("?" for _ in values)
                return f"{expr} IN ({placeholders})", [*expr_params, *(_to_sql_param(v) for v in values)]
            case "array_contains":
                return (
                    "EXISTS (SELECT 1 FROM json_each(data, ?) WHERE value = ?)",
                    [_json_path(field), _to_sql_param(value)],
                )
            case _:
                raise ValueError(f"Unsupported operator: {operator}")

    def array_union(self, collection: str, doc_id: str, field: str, values: List[Any]) -> None:
        doc = self.get(collection, doc_id)
        if doc is None:
            raise DocumentNotFoundError(collection, doc_id)
        current = doc.get(field)
        array = list(current) if isinstance(current, list) else []
        for value in values:
            if value not in array:
                array.append(value)
        doc[field] = array
        self.set(collection, doc_id, doc)

    def list_subcollection(
        self, parent_collection: str, parent_doc_id: str, subcollection: str
    ) -> List[Dict[str, Any]]:
        # A child table is identified by its own name; the parent_collection arg is
        # Firestore addressing that SQL doesn't need (kept only for the interface).
        parent_key, doc_key = _TABLE[subcollection].primary_key
        rows = self._conn.execute(
            f"SELECT {doc_key}, data FROM {subcollection} WHERE {parent_key} = ?",
            (parent_doc_id,),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for doc_id, data in rows:
            row = _decode_from_storage(json.loads(data))
            row["_id"] = doc_id
            out.append(row)
        return out

    def get_subdocument(
        self, parent_collection: str, parent_doc_id: str, subcollection: str, doc_id: str
    ) -> Optional[Dict[str, Any]]:
        parent_key, doc_key = _TABLE[subcollection].primary_key
        row = self._conn.execute(
            f"SELECT data FROM {subcollection} WHERE {parent_key} = ? AND {doc_key} = ?",
            (parent_doc_id, doc_id),
        ).fetchone()
        return None if row is None else _decode_from_storage(json.loads(row[0]))

    def set_subdocument(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
        doc_id: str,
        data: Dict[str, Any],
    ) -> None:
        parent_key, doc_key = _TABLE[subcollection].primary_key
        self._conn.execute(
            f"INSERT INTO {subcollection} ({parent_key}, {doc_key}, data) VALUES (?, ?, ?) "
            f"ON CONFLICT({parent_key}, {doc_key}) DO UPDATE SET data = excluded.data",
            (parent_doc_id, doc_id, json.dumps(_encode_for_storage(data))),
        )

    def get_api_key_by_uid(self, key_uid: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT doc_id, data FROM api_keys WHERE key_uid = ?", (key_uid,)
        ).fetchone()
        if row is None:
            return None
        result = _decode_from_storage(json.loads(row[1]))
        result["_id"] = row[0]
        return result


class SqliteDatabase(IDatabase):
    """Persistent document store backed by a single SQLite file (WAL, single-writer).

    Holds the connection, a re-entrant lock, and one ``SqliteTransactionContext`` bound to
    it; every operation delegates under the lock. Only lifecycle lives here directly.
    """

    def __init__(self, db_path: str, busy_timeout_ms: int = 5000, max_retries: int = 5) -> None:
        self._max_retries = max_retries
        self._lock = threading.RLock()

        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._ops = SqliteTransactionContext(self._conn)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            for table in _TABLES:
                cols = [f"{k} TEXT NOT NULL" for k in table.primary_key]
                cols += [f"{n} {t}" for n, t in table.columns.items()]
                cols.append("data TEXT NOT NULL")
                self._conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table.name} "
                    f"({', '.join(cols)}, PRIMARY KEY ({', '.join(table.primary_key)}))"
                )
                self._auto_migrate_columns(table)
                for index_cols in table.indexes:
                    idx = f"idx_{table.name}_{'_'.join(index_cols)}"
                    self._conn.execute(
                        f"CREATE INDEX IF NOT EXISTS {idx} ON {table.name} ({', '.join(index_cols)})"
                    )

    def _auto_migrate_columns(self, table: _Table) -> None:
        """Add + backfill any registry column missing from an existing table file.

        Lets a db file created by an older version of the registry self-upgrade: a
        newly-promoted column is added via ALTER TABLE and backfilled from the JSON
        `data` blob (the source of truth), so no manual reset/migration is needed.
        """
        existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table.name})")}
        missing = [n for n in table.columns if n not in existing]
        for name in missing:
            sql_type = table.columns[name]
            self._conn.execute(f"ALTER TABLE {table.name} ADD COLUMN {name} {sql_type}")
            self._conn.execute(
                f"UPDATE {table.name} SET {name} = json_extract(data, ?)",
                (_json_path(name),),
            )

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._ops.get(collection, doc_id)

    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        with self._lock:
            self._ops.set(collection, doc_id, data)

    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        with self._lock:
            self._ops.update(collection, doc_id, data)

    def delete(self, collection: str, doc_id: str) -> None:
        with self._lock:
            self._ops.delete(collection, doc_id)

    def query(
        self,
        collection: str,
        filters: Optional[List[FilterTuple]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            return self._ops.query(collection, filters, order_by, limit)

    def run_transaction(self, callback: Callable[[ITransactionContext], T]) -> T:
        with self._lock:
            for attempt in range(self._max_retries):
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = callback(self._ops)
                    except Exception:
                        self._conn.rollback()
                        raise
                    self._conn.commit()
                    return result
                except sqlite3.OperationalError as exc:
                    self._safe_rollback()
                    if "locked" in str(exc).lower() and attempt < self._max_retries - 1:
                        continue
                    raise
            raise RuntimeError("run_transaction exhausted retries without result")

    def _safe_rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.OperationalError:
            pass

    def array_union(self, collection: str, doc_id: str, field: str, values: List[Any]) -> None:
        with self._lock:
            self._ops.array_union(collection, doc_id, field, values)

    def list_subcollection(
        self, parent_collection: str, parent_doc_id: str, subcollection: str
    ) -> List[Dict[str, Any]]:
        with self._lock:
            return self._ops.list_subcollection(parent_collection, parent_doc_id, subcollection)

    def get_api_key_by_uid(self, key_uid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._ops.get_api_key_by_uid(key_uid)

    def clear_all(self, collections: Optional[List[str]] = None) -> None:
        """Delete rows from the named tables and their child tables. None clears every table."""
        with self._lock:
            if collections is None:
                self.clear()
                return
            targets = set(collections)
            for collection in collections:
                self._conn.execute(f"DELETE FROM {collection}")
            for table in _TABLES:
                if table.parent in targets:
                    self._conn.execute(f"DELETE FROM {table.name}")

    def clear(self) -> None:
        """Drop all rows from every table (full reset)."""
        with self._lock:
            for table in _TABLES:
                self._conn.execute(f"DELETE FROM {table.name}")

    def close(self) -> None:
        """Checkpoint the WAL back into the main file, then close the connection."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
            self._conn.close()
