"""
Physical SQLite layout registry — the single source of truth that turns the
document-shaped ``IDatabase`` model into real relational tables.

Each *known* collection (``api_keys``, ``generation_sessions``, ``workspaces``) gets
its own table with the fields that are actually filtered/ordered across the codebase
*promoted* to typed, indexed columns. The full document still lives in a ``data`` JSON
column (the source of truth on read); promoted columns are mirrored out of it on write
purely so SQL can filter/order on real, indexed columns instead of ``json_extract``.

Any collection NOT registered here transparently falls back to the generic
``documents`` table (``collection, doc_id, data``), so the interface stays fully generic
and ad-hoc/test collections keep working. This registry drives both DDL (table + index
creation) and query routing (promoted column vs JSON fallback) — no second list of
column names exists anywhere (DRY). Adding a promoted table later is purely additive
(Open/Closed): register a new schema, nothing else changes.

Promoted columns are derived from the real query call sites:
  - generation_sessions: stuck_running/initializing detectors, shutdown recovery/handler,
    per-key session listing (``order_by="-created_at"``).
  - workspaces: pool allocation, scheduled wipe, stuck-cleaning/initializing recovery.
  - api_keys: ``get_api_key_by_uid`` and the auth routes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# Generic fallback table for any collection not registered below (keeps the interface
# fully generic: unknown/ad-hoc/test collections still round-trip as JSON blobs).
GENERIC_TABLE = "documents"


@dataclass(frozen=True)
class Column:
    """A promoted column: a document field mirrored into a real, typed SQL column.

    ``sql_type`` is a SQLite column affinity. Timestamps and strings use ``TEXT`` (ISO-8601
    timestamps sort chronologically under lexical TEXT comparison — the datetime invariant
    the whole codebase relies on). Integers and booleans use ``INTEGER`` (booleans store as
    0/1, matching how filter values are coerced in ``sqlite._to_sql_param``).
    """

    name: str
    sql_type: str = "TEXT"


@dataclass(frozen=True)
class CollectionSchema:
    """Physical layout for one known collection: its own table + promoted columns + indexes."""

    collection: str
    table: str
    columns: Tuple[Column, ...]
    # Each inner tuple is one (possibly composite) index over promoted column names.
    indexes: Tuple[Tuple[str, ...], ...]

    @property
    def column_names(self) -> Tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column_for(self, field: str) -> Optional[str]:
        """Return the promoted column name for ``field``, or None to use the JSON fallback."""
        return field if field in self.column_names else None


# Immutable registry. Column/index sets mirror the actual filter + order_by call sites.
_SCHEMAS: Tuple[CollectionSchema, ...] = (
    CollectionSchema(
        collection="generation_sessions",
        table="generation_sessions",
        columns=(
            Column("status"),
            Column("status_changed_at"),
            Column("last_activity_at"),
            Column("shutdown_interrupted", "INTEGER"),
            Column("key_uid"),
            Column("created_at"),
            Column("failed_at"),
            Column("outputs_archived", "INTEGER"),
        ),
        indexes=(
            ("status", "last_activity_at"),
            ("status", "status_changed_at"),
            ("status", "shutdown_interrupted"),
            ("key_uid", "created_at"),
        ),
    ),
    CollectionSchema(
        collection="workspaces",
        table="workspaces",
        columns=(
            Column("status"),
            Column("workspace_pool"),
            Column("set_number", "INTEGER"),
            Column("scheduled_for_wipe", "INTEGER"),
            Column("scheduled_for_wipe_at"),
            Column("locked_by"),
            Column("cleaning_started_at"),
            Column("allocated_at"),
        ),
        indexes=(
            ("status",),
            ("workspace_pool", "set_number"),
            ("scheduled_for_wipe", "scheduled_for_wipe_at"),
        ),
    ),
    CollectionSchema(
        collection="api_keys",
        table="api_keys",
        columns=(
            Column("key_uid"),
            Column("is_active", "INTEGER"),
            Column("user_id"),
        ),
        indexes=(("key_uid",),),
    ),
)

_BY_COLLECTION: Dict[str, CollectionSchema] = {s.collection: s for s in _SCHEMAS}


def schema_for(collection: str) -> Optional[CollectionSchema]:
    """Return the relational schema for ``collection``, or None to use the generic table."""
    return _BY_COLLECTION.get(collection)


def all_schemas() -> Tuple[CollectionSchema, ...]:
    """All registered per-collection schemas (used for DDL and full-reset)."""
    return _SCHEMAS
