"""
SQLite database implementation for local / single-node persistence.

A SQL-native document store implementing ``IDatabase`` for local/Docker-dev. Every
collection is a real table (registered in ``app.database.utils_sqlite.tables``); the
fields actually filtered or ordered are promoted to typed, indexed columns while the
full document lives in a ``data`` JSON column (source of truth on read). Schema, blob
serialization, and connection-level SQL live in ``app.database.utils_sqlite`` — this
module holds only the ``IDatabase`` implementation and its lifecycle. Single writer
only, using the rollback journal (``journal_mode=DELETE``, not WAL): the file is a host
bind mount and WAL's ``-shm`` mmap is not coherent across the container/host boundary.
Multi-replica stays on Firestore.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from app.database.interface import FilterTuple, IDatabase, ITransactionContext
from app.database.utils_sqlite.schemas import _Table
from app.database.utils_sqlite.ser_deser import _json_path
from app.database.utils_sqlite.tables import _TABLES
from app.database.utils_sqlite.transaction import SqliteTransactionContext

T = TypeVar("T")


class SqliteDatabase(IDatabase):
    """Persistent document store backed by a single SQLite file (rollback-journal, single-writer).

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
            # Rollback journal, not WAL — see module docstring (bind-mount / -shm incoherence).
            self._conn.execute("PRAGMA journal_mode=DELETE")
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
        """Close the connection. In rollback-journal (DELETE) mode every committed
        transaction is already in the main ``.db`` file, so there is nothing to flush."""
        with self._lock:
            self._conn.close()
