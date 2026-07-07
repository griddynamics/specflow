"""
Physical SQLite layout registry — the single source of truth that turns the
document-shaped ``IDatabase`` model into real relational tables.

Each *known* collection (``api_keys``, ``generation_sessions``, ``workspaces``) gets
its own table with the fields that are actually filtered/ordered across the codebase
*promoted* to typed, indexed columns. The full document still lives in a ``data`` JSON
column (the source of truth on read); promoted columns are mirrored out of it on write
purely so SQL can filter/order on real, indexed columns instead of ``json_extract``.

There is no generic catch-all table: a collection that is not registered here is a
programming error and is rejected loudly (see ``sqlite._require_schema``), so a new
collection cannot silently land in an unindexed blob — you must register it. This
registry drives both DDL (table + index creation) and query routing (promoted column vs
``json_extract`` on the same table's ``data``) — no second list of column names exists
anywhere (DRY). Adding a collection later is purely additive (Open/Closed): register a
new schema, nothing else changes.

A field is promoted iff it actually appears in a query filter or ``order_by`` somewhere
in the codebase — nothing is promoted "just in case" (a promoted column that nothing
queries is pure write overhead). Every other field stays in the JSON ``data`` blob and is
still queryable via ``json_extract`` on the same table if a rare filter needs it. The
promoted set, by call site:
  - generation_sessions: ``status``, ``last_activity_at``, ``status_changed_at``,
    ``shutdown_interrupted``, ``key_uid`` (stuck_running/initializing detectors, shutdown
    recovery/handler) and ``created_at`` (per-key listing ``order_by="-created_at"``).
  - workspaces: ``status``, ``workspace_pool``, ``set_number``, ``scheduled_for_wipe``,
    ``scheduled_for_wipe_at``, ``locked_by``, ``clean_verified`` (pool allocation,
    scheduled wipe, stuck-cleaning/initializing recovery, allocation rollback).
  - api_keys: ``key_uid`` (``get_api_key_by_uid``; the auth routes read the rest off the
    document and never filter on it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


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
            Column("clean_verified", "INTEGER"),
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
        columns=(Column("key_uid"),),
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
