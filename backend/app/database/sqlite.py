"""
SQLite database implementation for local / single-node persistence.

A SQL-native document store. It persists the same document-shaped model used by
Firestore and the in-memory backend (``collection / doc_id / {JSON}`` plus
subcollections), so services, state machines, and workflows are unchanged. This is
the local/Docker-dev default backend. It is NOT a production replacement for
Firestore (single-file, single-writer — no cross-node distributed locking).

Design notes:
- Known collections (``api_keys``, ``generation_sessions``, ``workspaces``) each get
  their own real table with the frequently-queried fields promoted to typed, indexed
  columns (see ``app/database/sqlite_schema.py``). The full document still lives in a
  ``data`` JSON column (source of truth on read); promoted columns are mirrored out of
  it on write so filters/ordering hit real indexes instead of ``json_extract``. Any
  unregistered collection falls back to the generic ``documents`` table, keeping the
  interface fully generic. Subcollections use a separate ``subdocuments`` table.
- Queries push filters/order/limit into SQL (real column when promoted, ``json_extract``
  otherwise) so behavior matches the in-memory reference (``app/database/memory.py``).
- Transactions use a real ``BEGIN IMMEDIATE`` (genuine ACID), a strict upgrade over the
  write-buffering bridge in ``app/state/db_adapter.py``.
- Datetime contract: every datetime is stored as a fixed-width ISO-8601 UTC string
  (lexical order == chronological order, in both the JSON blob and the promoted TEXT
  columns) and decoded back to a tz-aware ``datetime`` on read, so background jobs
  (stuck detectors, lease recovery) that compare against ``datetime.now(timezone.utc)``
  behave identically to production.
- Concurrency: one writer process only (WAL mode). Multi-replica stays on Firestore.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
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
from app.database.sqlite_schema import (
    CollectionSchema,
    all_schemas,
    schema_for,
)

T = TypeVar("T")

# Default collections cleared by clear_all(None) when callers do not specify a set.
_DEFAULT_CLEAR_COLLECTIONS = ("api_keys", "generation_sessions", "workspaces")

# Strict-enough ISO-8601 datetime shape (must carry a time and a tz designator) so
# only values we wrote as canonical timestamps are decoded back to datetime.
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class _ServerTimestamp:
    """Sentinel for a server-assigned timestamp (resolved to now-UTC on write)."""


def _canonical_dt(value: datetime) -> str:
    """Render a datetime as a fixed-width ISO-8601 UTC string (tz-naive assumed UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _encode_for_storage(value: Any) -> Any:
    """Replace server-timestamp sentinels, normalize datetimes/enums, recurse into dict/list."""
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
    """Decode canonical ISO-8601 strings back to tz-aware datetimes, recurse into dict/list."""
    if isinstance(value, str) and _ISO_DATETIME_RE.match(value):
        return datetime.fromisoformat(value).astimezone(UTC)
    if isinstance(value, dict):
        return {k: _decode_from_storage(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_from_storage(v) for v in value]
    return value


def _to_sql_param(value: Any) -> Any:
    """Coerce a Python filter/column value to a SQLite-bindable scalar."""
    if isinstance(value, datetime):
        return _canonical_dt(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bool):
        return int(value)
    return value


def _json_path(field: str) -> str:
    """Convert a dotted field name to a JSON path (e.g. 'metadata.x' -> '$.metadata.x')."""
    return "$." + field


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier (column/table). Names come from the trusted registry."""
    return '"' + name.replace('"', '""') + '"'


# ----------------------------------------------------------------------------
# Physical operations shared by SqliteDatabase and SqliteTransactionContext.
# All take a live connection so the same per-collection table is used inside and
# outside a transaction — single source of truth.
# ----------------------------------------------------------------------------


def _require_schema(collection: str) -> CollectionSchema:
    """Resolve the relational schema for ``collection`` or fail loudly.

    There is no generic catch-all table: an unregistered collection is a programming
    error (a new collection must get its own table), not something to silently store
    in an unindexed blob.
    """
    schema = schema_for(collection)
    if schema is None:
        raise ValueError(
            f"Unknown SQLite collection {collection!r}. "
            f"Register it in app/database/sqlite_schema.py before using it."
        )
    return schema


def _column_values(schema: CollectionSchema, encoded: Dict[str, Any]) -> List[Any]:
    """Extract the promoted-column values (in schema order) from an encoded document."""
    return [_to_sql_param(encoded.get(name)) for name in schema.column_names]


def _row_get(conn: sqlite3.Connection, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
    schema = _require_schema(collection)
    row = conn.execute(
        f"SELECT data FROM {schema.table} WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    if row is None:
        return None
    return _decode_from_storage(json.loads(row[0]))


def _row_set(conn: sqlite3.Connection, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
    schema = _require_schema(collection)
    encoded = _encode_for_storage(data)
    payload = json.dumps(encoded)

    names = schema.column_names
    col_list = ", ".join(_quote_ident(n) for n in names)
    placeholders = ", ".join("?" for _ in names)
    assignments = ", ".join(
        f"{_quote_ident(n)} = excluded.{_quote_ident(n)}" for n in names
    )
    conn.execute(
        f"INSERT INTO {schema.table} (doc_id, {col_list}, data) "
        f"VALUES (?, {placeholders}, ?) "
        f"ON CONFLICT(doc_id) DO UPDATE SET {assignments}, data = excluded.data",
        [doc_id, *_column_values(schema, encoded), payload],
    )


def _row_update(conn: sqlite3.Connection, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
    existing = _row_get(conn, collection, doc_id)
    if existing is None:
        raise DocumentNotFoundError(collection, doc_id)
    existing.update(data)
    _row_set(conn, collection, doc_id, existing)


def _row_delete(conn: sqlite3.Connection, collection: str, doc_id: str) -> None:
    schema = _require_schema(collection)
    conn.execute(f"DELETE FROM {schema.table} WHERE doc_id = ?", (doc_id,))


def _field_expr(schema: CollectionSchema, field: str) -> tuple[str, List[Any]]:
    """SQL expression + bind params to read ``field`` — a promoted column or a JSON extract.

    Fields not promoted to a column are still stored in the table's ``data`` blob and
    remain queryable via ``json_extract`` on the same row.
    """
    column = schema.column_for(field)
    if column is not None:
        return _quote_ident(column), []
    return "json_extract(data, ?)", [_json_path(field)]


def _filter_clause(
    schema: CollectionSchema, field: str, operator: str, value: Any
) -> tuple[str, List[Any]]:
    """Translate a (field, op, value) filter into a SQL clause + bind params.

    Routes ``field`` to its promoted column when the collection has one, else to
    ``json_extract(data, ...)`` — identical semantics either way.
    """
    expr, expr_params = _field_expr(schema, field)

    match operator:
        case "==":
            if value is None:
                return f"{expr} IS NULL", expr_params
            return f"{expr} = ?", [*expr_params, _to_sql_param(value)]
        case "!=":
            if value is None:
                return f"{expr} IS NOT NULL", expr_params
            # Include docs missing the field (None != value is True in the reference impl).
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
            return (
                f"{expr} IN ({placeholders})",
                [*expr_params, *(_to_sql_param(v) for v in values)],
            )
        case "array_contains":
            # Arrays are never promoted to columns — always match against the JSON blob.
            return (
                "EXISTS (SELECT 1 FROM json_each(data, ?) WHERE value = ?)",
                [_json_path(field), _to_sql_param(value)],
            )
        case _:
            raise ValueError(f"Unsupported operator: {operator}")


def _row_query(
    conn: sqlite3.Connection,
    collection: str,
    filters: Optional[List[FilterTuple]],
    order_by: Optional[str],
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    schema = _require_schema(collection)

    where: List[str] = []
    params: List[Any] = []
    for field, operator, value in filters or []:
        clause, clause_params = _filter_clause(schema, field, operator, value)
        where.append(clause)
        params.extend(clause_params)

    sql = f"SELECT doc_id, data FROM {schema.table}"
    if where:
        sql += " WHERE " + " AND ".join(where)

    if order_by:
        descending = order_by.startswith("-")
        field = order_by[1:] if descending else order_by
        expr, expr_params = _field_expr(schema, field)
        params.extend(expr_params)
        sql += f" ORDER BY {expr} " + ("DESC" if descending else "ASC")

    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(sql, params).fetchall()

    results: List[Dict[str, Any]] = []
    for doc_id, data in rows:
        doc = _decode_from_storage(json.loads(data))
        doc["_id"] = doc_id
        results.append(doc)
    return results


class SqliteTransactionContext(ITransactionContext):
    """Transaction context operating directly on a connection inside BEGIN IMMEDIATE.

    Callers must perform all reads before any writes (interface contract), so
    operating on the live transaction is safe and gives real atomicity.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        return _row_get(self._conn, collection, doc_id)

    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        _row_set(self._conn, collection, doc_id, data)

    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        _row_update(self._conn, collection, doc_id, data)

    def delete(self, collection: str, doc_id: str) -> None:
        _row_delete(self._conn, collection, doc_id)

    def get_subdocument(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
        doc_id: str,
    ) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT data FROM subdocuments WHERE parent_collection = ? AND "
            "parent_doc_id = ? AND subcollection = ? AND doc_id = ?",
            (parent_collection, parent_doc_id, subcollection, doc_id),
        ).fetchone()
        if row is None:
            return None
        return _decode_from_storage(json.loads(row[0]))

    def set_subdocument(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
        doc_id: str,
        data: Dict[str, Any],
    ) -> None:
        payload = json.dumps(_encode_for_storage(data))
        self._conn.execute(
            "INSERT INTO subdocuments "
            "(parent_collection, parent_doc_id, subcollection, doc_id, data) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(parent_collection, parent_doc_id, subcollection, doc_id) "
            "DO UPDATE SET data = excluded.data",
            (parent_collection, parent_doc_id, subcollection, doc_id, payload),
        )


class SqliteDatabase(IDatabase):
    """Persistent document store backed by a single SQLite file (WAL, single-writer)."""

    def __init__(self, db_path: str, busy_timeout_ms: int = 5000, max_retries: int = 5) -> None:
        self._path = db_path
        self._max_retries = max_retries
        self._lock = threading.RLock()

        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            # Firestore-style subcollections (e.g. per-workspace model usage).
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS subdocuments ("
                "parent_collection TEXT NOT NULL, parent_doc_id TEXT NOT NULL, "
                "subcollection TEXT NOT NULL, doc_id TEXT NOT NULL, data TEXT NOT NULL, "
                "PRIMARY KEY (parent_collection, parent_doc_id, subcollection, doc_id))"
            )
            # One real table per known collection: promoted typed columns + JSON `data`.
            for schema in all_schemas():
                col_defs = ", ".join(
                    f"{_quote_ident(c.name)} {c.sql_type}" for c in schema.columns
                )
                self._conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {schema.table} "
                    f"(doc_id TEXT PRIMARY KEY, {col_defs}, data TEXT NOT NULL)"
                )
                for index_cols in schema.indexes:
                    index_name = f"idx_{schema.table}_{'_'.join(index_cols)}"
                    cols_sql = ", ".join(_quote_ident(c) for c in index_cols)
                    self._conn.execute(
                        f"CREATE INDEX IF NOT EXISTS {index_name} "
                        f"ON {schema.table} ({cols_sql})"
                    )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return _row_get(self._conn, collection, doc_id)

    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        with self._lock:
            _row_set(self._conn, collection, doc_id, data)

    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        with self._lock:
            _row_update(self._conn, collection, doc_id, data)

    def delete(self, collection: str, doc_id: str) -> None:
        with self._lock:
            _row_delete(self._conn, collection, doc_id)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(
        self,
        collection: str,
        filters: Optional[List[FilterTuple]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            return _row_query(self._conn, collection, filters, order_by, limit)

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def run_transaction(self, callback: Callable[[ITransactionContext], T]) -> T:
        with self._lock:
            for attempt in range(self._max_retries):
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = callback(SqliteTransactionContext(self._conn))
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
            # Unreachable: the final attempt either returns or re-raises above.
            raise RuntimeError("run_transaction exhausted retries without result")

    def _safe_rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.OperationalError:
            pass

    # ------------------------------------------------------------------
    # Array / subcollection / timestamp / lookups
    # ------------------------------------------------------------------

    def array_union(
        self, collection: str, doc_id: str, field: str, values: List[Any]
    ) -> None:
        with self._lock:
            doc = _row_get(self._conn, collection, doc_id)
            if doc is None:
                raise DocumentNotFoundError(collection, doc_id)
            current = doc.get(field)
            array = list(current) if isinstance(current, list) else []
            for value in values:
                if value not in array:
                    array.append(value)
            doc[field] = array
            _row_set(self._conn, collection, doc_id, doc)

    def list_subcollection(
        self,
        parent_collection: str,
        parent_doc_id: str,
        subcollection: str,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc_id, data FROM subdocuments WHERE parent_collection = ? AND "
                "parent_doc_id = ? AND subcollection = ?",
                (parent_collection, parent_doc_id, subcollection),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for doc_id, data in rows:
            row = _decode_from_storage(json.loads(data))
            row["_id"] = doc_id
            out.append(row)
        return out

    def server_timestamp(self) -> Any:
        return _ServerTimestamp()

    def get_api_key_by_uid(self, key_uid: str) -> Optional[Dict[str, Any]]:
        # Indexed lookup on the promoted key_uid column of the api_keys table.
        schema = _require_schema("api_keys")
        with self._lock:
            row = self._conn.execute(
                f"SELECT doc_id, data FROM {schema.table} WHERE key_uid = ?",
                (key_uid,),
            ).fetchone()
        if row is None:
            return None
        result = _decode_from_storage(json.loads(row[1]))
        result["_id"] = row[0]
        return result

    # ------------------------------------------------------------------
    # Test / maintenance helpers (parity with InMemoryDatabase / FirestoreDatabase)
    # ------------------------------------------------------------------

    def clear_all(self, collections: Optional[List[str]] = None) -> None:
        """Delete documents (and parented subdocuments). None clears the default test set."""
        targets = list(collections) if collections is not None else list(_DEFAULT_CLEAR_COLLECTIONS)
        with self._lock:
            if not targets:
                return
            for collection in targets:
                schema = _require_schema(collection)
                self._conn.execute(f"DELETE FROM {schema.table}")
            placeholders = ", ".join("?" for _ in targets)
            self._conn.execute(
                f"DELETE FROM subdocuments WHERE parent_collection IN ({placeholders})",
                targets,
            )

    def clear(self) -> None:
        """Drop all rows from every table (full reset)."""
        with self._lock:
            for schema in all_schemas():
                self._conn.execute(f"DELETE FROM {schema.table}")
            self._conn.execute("DELETE FROM subdocuments")

    def close(self) -> None:
        """Checkpoint the WAL back into the main file, then close the connection.

        Bounds WAL growth across restarts and ensures a bare `sqlite3 specflow.db`
        (opened outside this process) sees committed data immediately.
        """
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
            self._conn.close()
