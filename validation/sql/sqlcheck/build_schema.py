"""Builds the structural part of `sql-schema.json` from SQLAlchemy `MetaData` (the ORM's,
or one reflected from a live database), keeping the policy keys an existing file holds
(`hard_delete_allowlist`, `append_only_tables`, `orm_models`).

Run it once to bootstrap the declared schema, review and commit the file, and rerun it
only for an intended schema change: `schema-parity` then reports every later drift between
the ORM and the reviewed file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import Column, MetaData, Table, create_engine

from sqlcheck.schema_parity import (
    Families,
    compiled_where,
    is_unique,
    type_families,
)

POLICY_KEYS = ("hard_delete_allowlist", "append_only_tables", "orm_models")


def _family_name(sa_type: object, families: Families) -> str:
    """The family whose most derived class holds `sa_type` (an `Enum` is also a `String`,
    so `enum` wins over `text`); a `TypeDecorator` is judged by its `impl`."""
    candidates = [sa_type, getattr(sa_type, "impl", None)]
    for candidate in candidates:
        best: tuple[int, str] | None = None
        for prefix, classes in families:
            for cls in classes:
                if isinstance(candidate, cls):
                    depth = len(cls.__mro__)
                    if best is None or depth > best[0]:
                        best = (depth, prefix)
        if best is not None:
            return best[1]
    return type(sa_type).__name__.lower()


def _column(table: Table, col: Column[Any], families: Families, dialect: str) -> dict[str, Any]:
    spec: dict[str, Any] = {"type": _family_name(col.type, families)}
    enums = getattr(col.type, "enums", None)
    if isinstance(enums, list) and enums:
        spec["enum"] = list(enums)
    fks = sorted(f"{fk.column.table.name}.{fk.column.name}" for fk in col.foreign_keys)
    if fks:
        spec["fk"] = fks[0]
    if is_unique(table, col.name):
        spec["unique"] = True
    else:
        for index in table.indexes:
            if index.unique and [c.name for c in index.columns] == [col.name]:
                predicate = index.dialect_options[dialect].get("where")
                where = compiled_where(table, predicate, dialect)
                if where:
                    spec["unique"] = {"where": where}
                    break
    return spec


def _indexes(table: Table) -> list[str]:
    explicit = {c.name for c in table.columns if c.index}
    explicit |= {
        next(iter(ix.columns)).name
        for ix in table.indexes
        if not ix.unique and len(ix.columns) == 1
    }
    return sorted(explicit)


def structure(
    metadata: MetaData, families: Families, ignore: tuple[str, ...], dialect: str
) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for name in sorted(metadata.tables):
        if name in ignore:
            continue
        table = metadata.tables[name]
        entry: dict[str, Any] = {
            "columns": {c.name: _column(table, c, families, dialect) for c in table.columns}
        }
        indexes = _indexes(table)
        if indexes:
            entry["indexes"] = indexes
        tables[name] = entry
    return tables


def reflect(url: str) -> MetaData:
    """The metadata of a live database (a synchronous SQLAlchemy URL)."""
    engine = create_engine(url)
    try:
        metadata = MetaData()
        metadata.reflect(bind=engine)
    finally:
        engine.dispose()
    return metadata


def build(
    metadata: MetaData,
    existing: Path | None,
    families_config: dict[str, list[str]],
    ignore: tuple[str, ...] = ("alembic_version",),
    dialect: str = "sqlite",
) -> dict[str, Any]:
    previous: dict[str, Any] = {}
    if existing is not None and existing.is_file():
        previous = json.loads(existing.read_text(encoding="utf-8"))
    document: dict[str, Any] = {
        "tables": structure(metadata, type_families(families_config), ignore, dialect)
    }
    for key in POLICY_KEYS:
        document[key] = previous.get(key, {} if key == "orm_models" else [])
    return document


def write(document: dict[str, Any], target: Path) -> None:
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
