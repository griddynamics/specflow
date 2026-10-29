"""The declared SQL schema (`sql-schema.json`): the reviewed contract several steps read.

```json
{
  "tables": {
    "customer_order": {
      "columns": {
        "id": {"type": "uuid"},
        "customer_id": {"type": "uuid", "fk": "customer"},
        "status": {"type": "enum", "enum": ["OPEN", "CLOSED"]},
        "reference": {"type": "text", "unique": true},
        "external_ref": {"type": "text", "unique": {"where": "status != 'CLOSED'"}}
      },
      "indexes": ["status"]
    },
    "cache_entry": ["key", "payload", "expires_at"]
  },
  "hard_delete_allowlist": ["cache_entry"],
  "append_only_tables": ["audit_log"],
  "orm_models": {"audit_log": "AuditLog"}
}
```

A table is either the full object form or a plain list of column names (enough for
`sql-rules`; `schema-parity` then checks names only). `hard_delete_allowlist` names the
tables whose rows may be deleted (every other row is deactivated), `append_only_tables`
the insert-only logs, and `orm_models` the ORM class of a table when it is not the
CamelCase of the table name. `sqlcheck build-schema` writes the structural part from
SQLAlchemy metadata or a live database and keeps the three policy keys."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str = ""
    enum: tuple[str, ...] | None = None
    fk: str = ""
    unique: bool = False
    unique_where: str = ""

    @property
    def fk_table(self) -> str:
        return self.fk.split(".", 1)[0] if self.fk else ""


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: dict[str, ColumnSpec]
    indexes: tuple[str, ...] = ()
    detailed: bool = False  # False: the plain-list form, column names only


@dataclass(frozen=True)
class SqlSchema:
    """`loaded` is False when no schema file was read: rules that need the schema do not
    run, and the hard-delete allow-list is empty, so every `DELETE` is a finding."""

    tables: dict[str, TableSpec] = field(default_factory=dict)
    hard_delete_allowlist: frozenset[str] = frozenset()
    append_only_tables: frozenset[str] = frozenset()
    orm_models: dict[str, str] = field(default_factory=dict)
    loaded: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def columns_by_table(self) -> dict[str, set[str]]:
        """`{table: {column, ...}}`, lowercased."""
        return {t.lower(): {c.lower() for c in spec.columns} for t, spec in self.tables.items()}

    def model_name(self, table: str) -> str:
        """The ORM class of `table`: `orm_models[table]`, else its CamelCase."""
        return self.orm_models.get(table) or "".join(p.capitalize() for p in table.split("_"))


def _column(name: str, spec: object) -> ColumnSpec:
    if not isinstance(spec, dict):
        return ColumnSpec(name)
    unique = spec.get("unique", False)
    enum = spec.get("enum")
    return ColumnSpec(
        name=name,
        type=str(spec.get("type", "")),
        enum=tuple(str(v) for v in enum) if isinstance(enum, list) else None,
        fk=str(spec.get("fk", "")),
        unique=bool(unique),
        unique_where=str(unique.get("where", "")) if isinstance(unique, dict) else "",
    )


def _table(name: str, spec: object) -> TableSpec:
    if isinstance(spec, list):
        return TableSpec(name, {str(c): ColumnSpec(str(c)) for c in spec})
    if not isinstance(spec, dict):
        msg = f"table {name!r}: expected a list of column names or an object"
        raise ValueError(msg)
    columns = spec.get("columns", {})
    if isinstance(columns, list):
        cols = {str(c): ColumnSpec(str(c)) for c in columns}
    else:
        cols = {str(c): _column(str(c), s) for c, s in dict(columns).items()}
    indexes = tuple(str(i) for i in spec.get("indexes", []))
    return TableSpec(name, cols, indexes, detailed=True)


def parse_schema(data: dict[str, Any]) -> SqlSchema:
    tables = data.get("tables") or {}
    return SqlSchema(
        tables={str(name): _table(str(name), spec) for name, spec in tables.items()},
        hard_delete_allowlist=frozenset(t.lower() for t in data.get("hard_delete_allowlist", [])),
        append_only_tables=frozenset(t.lower() for t in data.get("append_only_tables", [])),
        orm_models={str(k): str(v) for k, v in (data.get("orm_models") or {}).items()},
        loaded=True,
        raw=data,
    )


def load_schema(path: Path | None) -> SqlSchema:
    """Read the schema file; a missing file is an unloaded, empty schema."""
    if path is None or not path.is_file():
        return SqlSchema()
    return parse_schema(json.loads(path.read_text(encoding="utf-8")))
