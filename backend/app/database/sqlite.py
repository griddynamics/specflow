"""
SQLite database implementation for local / single-node persistence.

A SQL-native document store implementing ``IDatabase`` for local/Docker-dev. Known
collections and subcollections each get their own real table (table name == collection
name); the fields actually filtered/ordered are promoted to typed, indexed columns while
the full document lives in a ``data`` JSON column (source of truth on read). An
unregistered collection or subcollection is rejected — there is no generic blob table.
Datetimes are stored as fixed-width ISO-8601 UTC text so lexical order == chronological
order in both the columns and the blob. Single writer only (WAL); multi-replica stays on
Firestore.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
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

_DEFAULT_CLEAR_COLLECTIONS = ("api_keys", "generation_sessions", "workspaces")

_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


# ---------------------------------------------------------------------------
# Physical layout registry (table name == collection / subcollection name).
# Promote a field to a column only if it's actually filtered or ordered somewhere;
# everything else stays in the `data` blob and is read via json_extract.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Schema:
    columns: Dict[str, str]  # promoted field -> SQLite type
    indexes: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class _SubSchema:
    parent_key: str
    doc_key: str


_SCHEMAS: Dict[str, _Schema] = {
    "generation_sessions": _Schema(
        {
            "status": "TEXT",
            "status_changed_at": "TEXT",
            "last_activity_at": "TEXT",
            "shutdown_interrupted": "INTEGER",
            "key_uid": "TEXT",
            "created_at": "TEXT",
        },
        (
            ("status", "last_activity_at"),
            ("status", "status_changed_at"),
            ("status", "shutdown_interrupted"),
            ("key_uid", "created_at"),
        ),
    ),
    "workspaces": _Schema(
        {
            "status": "TEXT",
            "workspace_pool": "TEXT",
            "set_number": "INTEGER",
            "scheduled_for_wipe": "INTEGER",
            "scheduled_for_wipe_at": "TEXT",
            "locked_by": "TEXT",
            "clean_verified": "INTEGER",
        },
        (
            ("status",),
            ("workspace_pool", "set_number"),
            ("scheduled_for_wipe", "scheduled_for_wipe_at"),
        ),
    ),
    "api_keys": _Schema({"key_uid": "TEXT"}, (("key_uid",),)),
}

# (parent_collection, subcollection) -> child-table key columns.
_SUBSCHEMAS: Dict[tuple[str, str], _SubSchema] = {
    ("generation_sessions", "workspace_model_usage"): _SubSchema("generation_id", "workspace_id"),
}


def _canonical_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _encode_for_storage(value: Any) -> Any:
    if isinstance(value, _ServerTimestamp):
        return _canonical_dt(datetime.now(UTC))
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


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class _ServerTimestamp:
    """Sentinel for a server-assigned timestamp (resolved to now-UTC on write)."""


class SqliteTransactionContext(ITransactionContext):
    """All connection-level SQL for the SQLite backend, in one place.

    ``SqliteDatabase`` composes one over its own connection and wraps each call with its
    lock; ``run_transaction`` hands the same object to the callback (as an
    ``ITransactionContext``) inside ``BEGIN IMMEDIATE``. These methods do not lock — the
    caller owns concurrency — and callers must read before writing (interface contract).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @staticmethod
    def _schema(collection: str) -> _Schema:
        schema = _SCHEMAS.get(collection)
        if schema is None:
            raise ValueError(
                f"Unknown SQLite collection {collection!r}. "
                f"Register it in app/database/sqlite.py before using it."
            )
        return schema

    @staticmethod
    def _sub_schema(parent_collection: str, subcollection: str) -> _SubSchema:
        sub = _SUBSCHEMAS.get((parent_collection, subcollection))
        if sub is None:
            raise ValueError(
                f"Unknown SQLite subcollection {subcollection!r} under "
                f"{parent_collection!r}. Register it in app/database/sqlite.py before using it."
            )
        return sub

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        self._schema(collection)
        row = self._conn.execute(
            f"SELECT data FROM {_quote_ident(collection)} WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return None if row is None else _decode_from_storage(json.loads(row[0]))

    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        schema = self._schema(collection)
        encoded = _encode_for_storage(data)
        names = list(schema.columns)
        col_list = ", ".join(_quote_ident(n) for n in names)
        placeholders = ", ".join("?" for _ in names)
        assignments = ", ".join(f"{_quote_ident(n)} = excluded.{_quote_ident(n)}" for n in names)
        values = [_to_sql_param(encoded.get(n)) for n in names]
        self._conn.execute(
            f"INSERT INTO {_quote_ident(collection)} (doc_id, {col_list}, data) "
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
        self._schema(collection)
        self._conn.execute(f"DELETE FROM {_quote_ident(collection)} WHERE doc_id = ?", (doc_id,))

    def query(
        self,
        collection: str,
        filters: Optional[List[FilterTuple]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        schema = self._schema(collection)

        where: List[str] = []
        params: List[Any] = []
        for f, operator, value in filters or []:
            clause, clause_params = self._filter_clause(schema, f, operator, value)
            where.append(clause)
            params.extend(clause_params)

        sql = f"SELECT doc_id, data FROM {_quote_ident(collection)}"
        if where:
            sql += " WHERE " + " AND ".join(where)

        if order_by:
            descending = order_by.startswith("-")
            expr, expr_params = self._field_expr(schema, order_by[1:] if descending else order_by)
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
    def _field_expr(schema: _Schema, field: str) -> tuple[str, List[Any]]:
        if field in schema.columns:
            return _quote_ident(field), []
        return "json_extract(data, ?)", [_json_path(field)]

    @classmethod
    def _filter_clause(
        cls, schema: _Schema, field: str, operator: str, value: Any
    ) -> tuple[str, List[Any]]:
        expr, expr_params = cls._field_expr(schema, field)
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
        sub = self._sub_schema(parent_collection, subcollection)
        rows = self._conn.execute(
            f"SELECT {_quote_ident(sub.doc_key)}, data FROM {_quote_ident(subcollection)} "
            f"WHERE {_quote_ident(sub.parent_key)} = ?",
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
        sub = self._sub_schema(parent_collection, subcollection)
        row = self._conn.execute(
            f"SELECT data FROM {_quote_ident(subcollection)} WHERE "
            f"{_quote_ident(sub.parent_key)} = ? AND {_quote_ident(sub.doc_key)} = ?",
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
        sub = self._sub_schema(parent_collection, subcollection)
        pk, dk = _quote_ident(sub.parent_key), _quote_ident(sub.doc_key)
        self._conn.execute(
            f"INSERT INTO {_quote_ident(subcollection)} ({pk}, {dk}, data) VALUES (?, ?, ?) "
            f"ON CONFLICT({pk}, {dk}) DO UPDATE SET data = excluded.data",
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
            for (_parent, subcollection), sub in _SUBSCHEMAS.items():
                pk, dk = _quote_ident(sub.parent_key), _quote_ident(sub.doc_key)
                self._conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {_quote_ident(subcollection)} "
                    f"({pk} TEXT NOT NULL, {dk} TEXT NOT NULL, data TEXT NOT NULL, "
                    f"PRIMARY KEY ({pk}, {dk}))"
                )
            for collection, schema in _SCHEMAS.items():
                col_defs = ", ".join(f"{_quote_ident(n)} {t}" for n, t in schema.columns.items())
                self._conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {_quote_ident(collection)} "
                    f"(doc_id TEXT PRIMARY KEY, {col_defs}, data TEXT NOT NULL)"
                )
                for cols in schema.indexes:
                    name = f"idx_{collection}_{'_'.join(cols)}"
                    cols_sql = ", ".join(_quote_ident(c) for c in cols)
                    self._conn.execute(
                        f"CREATE INDEX IF NOT EXISTS {name} ON {_quote_ident(collection)} ({cols_sql})"
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

    def server_timestamp(self) -> Any:
        return _ServerTimestamp()

    def get_api_key_by_uid(self, key_uid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._ops.get_api_key_by_uid(key_uid)

    def clear_all(self, collections: Optional[List[str]] = None) -> None:
        """Delete documents (and their child subcollections). None clears the default test set."""
        targets = list(collections) if collections is not None else list(_DEFAULT_CLEAR_COLLECTIONS)
        with self._lock:
            if not targets:
                return
            for collection in targets:
                SqliteTransactionContext._schema(collection)
                self._conn.execute(f"DELETE FROM {_quote_ident(collection)}")
            target_set = set(targets)
            for (parent, subcollection) in _SUBSCHEMAS:
                if parent in target_set:
                    self._conn.execute(f"DELETE FROM {_quote_ident(subcollection)}")

    def clear(self) -> None:
        """Drop all rows from every table (full reset)."""
        with self._lock:
            for collection in _SCHEMAS:
                self._conn.execute(f"DELETE FROM {_quote_ident(collection)}")
            for _parent, subcollection in _SUBSCHEMAS:
                self._conn.execute(f"DELETE FROM {_quote_ident(subcollection)}")

    def close(self) -> None:
        """Checkpoint the WAL back into the main file, then close the connection."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
            self._conn.close()
