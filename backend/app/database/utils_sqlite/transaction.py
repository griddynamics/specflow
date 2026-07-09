"""All connection-level SQL for the SQLite backend, in one place."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from app.database.interface import DocumentNotFoundError, FilterTuple, ITransactionContext
from app.database.utils_sqlite.schemas import DOC_ID, _Table
from app.database.utils_sqlite.ser_deser import (
    _decode_from_storage,
    _encode_for_storage,
    _json_path,
    _to_sql_param,
)
from app.database.utils_sqlite.tables import _TABLE


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
            f"SELECT data FROM {collection} WHERE {DOC_ID} = ?", (doc_id,)
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
            f"INSERT INTO {collection} ({DOC_ID}, {col_list}, data) "
            f"VALUES (?, {placeholders}, ?) "
            f"ON CONFLICT({DOC_ID}) DO UPDATE SET {assignments}, data = excluded.data",
            [doc_id, *values, json.dumps(encoded)],
        )

    def update(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        existing = self.get(collection, doc_id)
        if existing is None:
            raise DocumentNotFoundError(collection, doc_id)
        existing.update(data)
        self.set(collection, doc_id, existing)

    def delete(self, collection: str, doc_id: str) -> None:
        self._conn.execute(f"DELETE FROM {collection} WHERE {DOC_ID} = ?", (doc_id,))

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

        sql = f"SELECT {DOC_ID}, data FROM {collection}"
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
            f"SELECT {DOC_ID}, data FROM api_keys WHERE key_uid = ?", (key_uid,)
        ).fetchone()
        if row is None:
            return None
        result = _decode_from_storage(json.loads(row[1]))
        result["_id"] = row[0]
        return result
