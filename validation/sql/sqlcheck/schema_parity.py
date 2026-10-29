"""The declared schema (`sql-schema.json`) equals the ORM's SQLAlchemy `MetaData`:

- `schema-parity-tables`: the same tables on both sides (`ignore_tables` left out);
- `schema-parity-columns`: the same columns per table (`implicit_columns`, present on
  every table but never declared, are added to the declared side);
- `schema-parity-type` / `schema-parity-unmapped-type`: a declared type's family (its
  longest matching prefix in `[schema_parity.type_families]`) holds the ORM column type;
- `schema-parity-enum`: a declared `enum` equals the ORM enum's values, in order;
- `schema-parity-foreign-key`: a declared `fk` is on the ORM column, and an ORM foreign
  key is declared;
- `schema-parity-unique`: declared single-column uniqueness equals the ORM's; a declared
  `{"where": ...}` needs a unique single-column index whose predicate, compiled for each
  of `conditional_unique_dialects`, is that text;
- `schema-parity-index`: a declared index column is indexed (an `Index`, `index=True`, a
  single-column unique constraint, or the primary key).

Type, enum, key and index checks run for tables in the object form only."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import MetaData, Table, UniqueConstraint
from sqlalchemy.engine import make_url
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.sql.schema import ColumnCollectionConstraint

from sqlcheck.config import Config
from sqlcheck.findings import Finding, StepResult
from sqlcheck.loader import resolve, type_class
from sqlcheck.schema import ColumnSpec, SqlSchema, TableSpec, load_schema

STEP = "schema-parity"
DOC = "RULES.md#schema-parity"
_SPACES = re.compile(r"\s+")

Families = list[tuple[str, tuple[type, ...]]]


def type_families(config_families: dict[str, list[str]]) -> Families:
    """`[(prefix, classes)]`, longest prefix first."""
    resolved = [
        (prefix.lower(), tuple(type_class(n) for n in names))
        for prefix, names in config_families.items()
    ]
    return sorted(resolved, key=lambda item: -len(item[0]))


def family_of(declared: str, families: Families) -> tuple[type, ...] | None:
    lowered = declared.lower()
    return next((classes for prefix, classes in families if lowered.startswith(prefix)), None)


def matches_family(sa_type: object, classes: tuple[type, ...]) -> bool:
    """`sa_type` is one of `classes`, or a `TypeDecorator` whose `impl` is."""
    return isinstance(sa_type, classes) or isinstance(getattr(sa_type, "impl", None), classes)


_LITERAL = re.compile(r"('(?:[^']|'')*')")


def normalise_predicate(text: str, table: str = "") -> str:
    """Lower-case identifiers and keywords, strip quoting and the table prefix and fold
    whitespace, leaving single-quoted string literals untouched (their case is data)."""
    prefix = re.compile(rf"\b{re.escape(table.lower())}\.") if table else None
    parts = _LITERAL.split(text)
    for i in range(0, len(parts), 2):
        piece = parts[i].lower()
        if prefix:
            piece = prefix.sub("", piece)
        parts[i] = piece.replace('"', "")
    joined = _SPACES.sub(" ", "".join(parts)).strip()
    while joined.startswith("(") and joined.endswith(")"):
        joined = joined[1:-1].strip()
    return joined


def compiled_where(table: Table, where: object, dialect_name: str) -> str:
    if not isinstance(where, ClauseElement):
        return ""
    dialect = make_url(f"{dialect_name}://").get_dialect()()
    compiled = where.compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    return normalise_predicate(str(compiled), table.name)


def has_conditional_unique(
    table: Table, column: str, where: str, dialects: tuple[str, ...]
) -> bool:
    """A unique index on exactly `[column]` whose predicate, for every dialect, is `where`."""
    expected = normalise_predicate(where, table.name)
    for index in table.indexes:
        if not index.unique or [c.name for c in index.columns] != [column]:
            continue
        if all(
            compiled_where(table, index.dialect_options[d].get("where"), d) == expected
            for d in dialects
        ):
            return True
    return False


def is_unique(table: Table, column: str) -> bool:
    col = table.columns[column]
    return bool(col.unique) or any(
        [c.name for c in uc.columns] == [column]
        for uc in table.constraints
        if isinstance(uc, UniqueConstraint)
    )


def is_indexed(table: Table, column: str) -> bool:
    col = table.columns[column]
    if col.index or col.unique or col.primary_key:
        return True
    single = [
        [c.name for c in uc.columns]
        for uc in table.constraints
        if isinstance(uc, ColumnCollectionConstraint) and len(uc.columns) == 1
    ]
    if [column] in single:
        return True
    return any(column in [c.name for c in ix.columns] for ix in table.indexes)


def _type_problems(sa_type: object, spec: ColumnSpec, families: Families) -> list[tuple[str, str]]:
    if not spec.type:
        return []
    classes = family_of(spec.type, families)
    if classes is None:
        return [("schema-parity-unmapped-type", f"declared type {spec.type!r} has no family")]
    if matches_family(sa_type, classes):
        return []
    names = [c.__name__ for c in classes]
    actual = type(sa_type).__name__
    return [("schema-parity-type", f"declared {spec.type!r} maps to {names}, ORM has {actual}")]


def _key_problems(table: Table, spec: ColumnSpec) -> list[tuple[str, str]]:
    fk_tables = {fk.column.table.name for fk in table.columns[spec.name].foreign_keys}
    if spec.fk_table and spec.fk_table not in fk_tables:
        has = sorted(fk_tables) or "none"
        return [("schema-parity-foreign-key", f"declared FK -> {spec.fk_table} not in ORM ({has})")]
    if not spec.fk_table and fk_tables:
        return [("schema-parity-foreign-key", f"ORM has an undeclared FK -> {sorted(fk_tables)}")]
    return []


def _unique_problems(
    table: Table, spec: ColumnSpec, dialects: tuple[str, ...]
) -> list[tuple[str, str]]:
    if spec.unique_where:
        code_unique = has_conditional_unique(table, spec.name, spec.unique_where, dialects)
        declared = f"unique where {spec.unique_where!r}"
    else:
        code_unique = is_unique(table, spec.name)
        declared = f"unique={spec.unique}"
    if spec.unique == code_unique:
        return []
    return [("schema-parity-unique", f"declared {declared}, ORM unique={code_unique}")]


def column_problems(
    table: Table, spec: ColumnSpec, families: Families, dialects: tuple[str, ...]
) -> list[tuple[str, str]]:
    """`[(rule, message)]` for one column both sides have."""
    sa_type = table.columns[spec.name].type
    problems = _type_problems(sa_type, spec, families)
    if spec.enum is not None:
        code_values = list(getattr(sa_type, "enums", []))
        if code_values != list(spec.enum):
            message = f"declared enum {list(spec.enum)} != ORM enum {code_values}"
            problems.append(("schema-parity-enum", message))
    return [*problems, *_key_problems(table, spec), *_unique_problems(table, spec, dialects)]


def _index_problems(table: Table, spec: TableSpec) -> list[tuple[str, str]]:
    code_cols = {c.name for c in table.columns}
    problems: list[tuple[str, str]] = []
    for col_name in spec.indexes:
        if col_name not in code_cols:
            problems.append(("schema-parity-index", f"declared index column {col_name} missing"))
        elif not is_indexed(table, col_name):
            problems.append(("schema-parity-index", f"no ORM index covers {col_name}"))
    return problems


def check(metadata: MetaData, schema: SqlSchema, config: Config) -> list[Finding]:
    where = config.rel(config.schema) if config.schema is not None else "sql-schema.json"
    ignore = set(config.ignore_tables)
    code: dict[str, Table] = {n: t for n, t in metadata.tables.items() if n not in ignore}
    declared: dict[str, TableSpec] = {n: t for n, t in schema.tables.items() if n not in ignore}
    families = type_families(config.type_families)
    dialects = config.conditional_unique_dialects
    findings: list[Finding] = []

    def add(rule: str, message: str) -> None:
        fix = "align the ORM definition and the declared schema; the stale side changes"
        findings.append(Finding(where, 1, 1, rule, message, fix=fix, doc=DOC))

    if set(declared) != set(code):
        only_declared = sorted(set(declared) - set(code))
        only_code = sorted(set(code) - set(declared))
        add("schema-parity-tables", f"only declared: {only_declared}; only in ORM: {only_code}")
    for name in sorted(set(declared) & set(code)):
        spec, table = declared[name], code[name]
        declared_cols = set(spec.columns) | set(config.implicit_columns)
        code_cols = {c.name for c in table.columns}
        if declared_cols != code_cols:
            add(
                "schema-parity-columns",
                f"{name}: only declared {sorted(declared_cols - code_cols)}, "
                f"only in ORM {sorted(code_cols - declared_cols)}",
            )
        if not spec.detailed:
            continue
        for col_name in sorted(set(spec.columns) & code_cols):
            col_spec = spec.columns[col_name]
            for rule, message in column_problems(table, col_spec, families, dialects):
                add(rule, f"{name}.{col_name}: {message}")
        for rule, message in _index_problems(table, spec):
            add(rule, f"{name}: {message}")
    return findings


def load_metadata(reference: str) -> MetaData:
    found: Any = resolve(reference)
    if not isinstance(found, MetaData):
        msg = f"{reference!r} is a {type(found).__name__}, not a sqlalchemy MetaData"
        raise TypeError(msg)
    return found


def run(config: Config) -> StepResult:
    if not config.metadata:
        return StepResult.ok(STEP, detail="skipped: [schema_parity].metadata is not set")
    schema = load_schema(config.schema)
    if not schema.loaded:
        return StepResult.ok(STEP, detail="skipped: no schema file ([paths].schema)")
    return StepResult.from_findings(STEP, check(load_metadata(config.metadata), schema, config))
