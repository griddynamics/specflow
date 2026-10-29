"""SQL policy rules over `sqlfluff`'s parse tree, for every `.sql` file:

- `sql-rules-no-where`: an `UPDATE`/`DELETE` with no `WHERE` of its own;
- `sql-rules-delete-not-allowlisted`: a `DELETE` on a table off the hard-delete allow-list;
- `sql-rules-append-only-table`: an `UPDATE`/`DELETE` on an append-only table;
- `sql-rules-drop-outside-migration`: a `DROP` outside the migration SQL directories;
- `sql-rules-unknown-table` / `-unknown-column` / `-unknown-qualifier`: a table, a qualified
  column or a qualifier the declared schema does not know (aliases and CTEs resolved;
  unqualified columns are not judged, since which table they belong to is ambiguous);
- `sql-rules-noqa-comment`: an inline `noqa` comment, which `.sqlfluff` disables anyway;
- `sql-rules-parse-error`: a file `sqlfluff` cannot parse.

Tables, allow-list and append-only tables come from the declared schema (`sql-schema.json`)."""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
from pathlib import Path

from sqlfluff.core import FluffConfig, Linter
from sqlfluff.core.parser import BaseSegment

from sqlcheck.config import Config
from sqlcheck.files import iter_files
from sqlcheck.findings import Finding, StepResult
from sqlcheck.schema import SqlSchema, load_schema

STEP = "sql-rules"
DOC = "RULES.md#sql-rules"

_MUTATING_TYPES = ("delete_statement", "update_statement")
# DDL drop statements across the dialects sqlfluff ships; a type a dialect lacks is
# simply never found.
_DROP_TYPES = (
    "drop_table_statement",
    "drop_index_statement",
    "drop_trigger_statement",
    "drop_view_statement",
    "drop_schema_statement",
    "drop_sequence_statement",
    "drop_function_statement",
    "drop_type_statement",
    "drop_database_statement",
)
# Segment types a WHERE-clause search must not descend into: a subquery or a scalar
# expression can carry its own WHERE that says nothing about the enclosing statement's
# own rows -- `UPDATE t SET c = (SELECT x FROM y WHERE ...)` still lacks a WHERE.
_SUBQUERY_SEG_TYPES = ["select_statement", "with_compound_statement", "expression"]
_QUOTES = '"`[]'


# project: sqlcheck.toml carries the SQLAlchemy dialect name; sqlfluff spells PostgreSQL "postgres".
_SQLFLUFF_NAMES = {"postgresql": "postgres"}


@cache
def _linter(dialect: str) -> Linter:
    name = _SQLFLUFF_NAMES.get(dialect, dialect)
    return Linter(config=FluffConfig(overrides={"dialect": name, "templater": "raw"}))


def _pos(seg: BaseSegment) -> tuple[int, int]:
    marker = seg.pos_marker
    return (marker.working_line_no, marker.working_line_pos) if marker else (1, 1)


def _name(raw: str) -> str:
    return raw.rsplit(".", 1)[-1].strip(_QUOTES).lower()


def _first_table_name(stmt: BaseSegment) -> str | None:
    ref = next(stmt.recursive_crawl("table_reference"), None)
    return None if ref is None else _name(ref.raw)


def _check_mutating_statement(rel: str, stmt: BaseSegment, schema: SqlSchema) -> list[Finding]:
    kind = stmt.get_type()
    verb = "DELETE" if kind == "delete_statement" else "UPDATE"
    line, col = _pos(stmt)
    table = _first_table_name(stmt)
    findings: list[Finding] = []
    own_where = next(
        stmt.recursive_crawl("where_clause", no_recursive_seg_type=_SUBQUERY_SEG_TYPES), None
    )
    if own_where is None:
        findings.append(
            Finding(
                rel,
                line,
                col,
                "sql-rules-no-where",
                f"{verb} with no WHERE clause",
                fix="add a WHERE clause that names the affected rows",
                doc=DOC,
            )
        )
    if table in schema.append_only_tables:
        findings.append(
            Finding(
                rel,
                line,
                col,
                "sql-rules-append-only-table",
                f"{verb} on append-only table {table!r}",
                fix=f"{table} rows are only ever inserted, never updated or deleted",
                doc=DOC,
            )
        )
    allowed = schema.hard_delete_allowlist
    if kind == "delete_statement" and table is not None and table not in allowed:
        findings.append(
            Finding(
                rel,
                line,
                col,
                "sql-rules-delete-not-allowlisted",
                f"DELETE on {table!r}, which is not in the hard-delete allow-list",
                fix="deactivate the row instead, or add the table to the schema's allow-list",
                doc=DOC,
            )
        )
    return findings


def in_migration_dir(path: Path, migration_dirs: Iterable[str]) -> bool:
    """`path`'s own parent directories end with one of `migration_dirs` (`alembic/sql`):
    a file merely somewhere under a path holding both names does not count."""
    parents = path.parent.parts
    for entry in migration_dirs:
        suffix = tuple(p for p in Path(entry).parts if p not in (".", ""))
        if suffix and parents[-len(suffix) :] == suffix:
            return True
    return False


def _check_drop(rel: str, stmt: BaseSegment, migration_dirs: tuple[str, ...]) -> Finding:
    line, col = _pos(stmt)
    where = ", ".join(f"{d}/" for d in migration_dirs) or "a migration directory"
    return Finding(
        rel,
        line,
        col,
        "sql-rules-drop-outside-migration",
        f"{stmt.get_type()} outside {where} -- schema changes are migrations",
        fix="move this statement into a migration",
        doc=DOC,
    )


def _alias_name(alias_expr: BaseSegment) -> str | None:
    alias_ident = next(alias_expr.recursive_crawl("naked_identifier", "quoted_identifier"), None)
    return None if alias_ident is None else alias_ident.raw.strip(_QUOTES).lower()


def _is_derived_source(source: BaseSegment) -> bool:
    """A `FROM`/`JOIN` source that is a subquery or a function call, not a named table."""
    table_expr = source.get_child("table_expression")
    return table_expr is not None and table_expr.get_child("table_reference") is None


def _table_aliases(tree: BaseSegment) -> dict[str, str]:
    """`{alias: table}` for every `FROM`/`JOIN` source and every `UPDATE` target that names
    an alias, read off the parse tree, so `s.col` is checked against the table `s` names."""
    aliases: dict[str, str] = {}
    for source in tree.recursive_crawl("from_expression_element"):
        if _is_derived_source(source):
            continue  # its alias names the subquery's or function's rows: `_derived_aliases`
        table_ref = next(source.recursive_crawl("table_reference"), None)
        alias_expr = next(source.recursive_crawl("alias_expression"), None)
        if table_ref is None or alias_expr is None:
            continue
        alias = _alias_name(alias_expr)
        if alias is None:
            continue
        aliases[alias] = _name(table_ref.raw)
    for update in tree.recursive_crawl("update_statement"):
        table_ref = update.get_child("table_reference")
        alias_expr = update.get_child("alias_expression")
        if table_ref is None or alias_expr is None:
            continue
        alias = _alias_name(alias_expr)
        if alias is not None:
            aliases[alias] = _name(table_ref.raw)
    return aliases


def _derived_aliases(tree: BaseSegment) -> set[str]:
    """Aliases of subqueries and set-returning functions in `FROM`/`JOIN` (`(SELECT ...) s`,
    `unnest(:ids) AS u (id)`): known qualifiers whose columns, like a CTE's, aren't modeled."""
    derived: set[str] = set()
    for source in tree.recursive_crawl("from_expression_element"):
        alias_expr = source.get_child("alias_expression")
        if alias_expr is None or not _is_derived_source(source):
            continue
        alias = _alias_name(alias_expr)
        if alias is not None:
            derived.add(alias)
    return derived


def _unknown_table(rel: str, ref: BaseSegment) -> Finding:
    line, col = _pos(ref)
    return Finding(
        rel,
        line,
        col,
        "sql-rules-unknown-table",
        f"table {ref.raw!r} is not in the declared schema",
        fix="use a declared table name, or declare this one first",
        doc=DOC,
    )


def _check_references(rel: str, tree: BaseSegment, known: dict[str, set[str]]) -> list[Finding]:
    if not known:
        return []
    cte_names = {
        ident.raw.strip(_QUOTES).lower()
        for cte in tree.recursive_crawl("common_table_expression")
        for ident in cte.segments[:1]
        if ident.get_type() in ("naked_identifier", "quoted_identifier")
    }
    referenced = {_name(ref.raw) for ref in tree.recursive_crawl("table_reference")}
    findings = [
        _unknown_table(rel, ref)
        for ref in tree.recursive_crawl("table_reference")
        if _name(ref.raw) not in known and _name(ref.raw) not in cte_names
    ]
    aliases = _table_aliases(tree)
    derived = _derived_aliases(tree)
    for ref in tree.recursive_crawl("column_reference"):
        if "." not in ref.raw:
            continue  # unqualified: which table it belongs to is ambiguous here
        qualifier, _, column = ref.raw.rpartition(".")
        qualifier = _name(qualifier)
        table = aliases.get(qualifier, qualifier)
        line, col = _pos(ref)
        columns = known.get(table)
        if columns is not None:
            if column.strip(_QUOTES).lower() not in columns:
                findings.append(
                    Finding(
                        rel,
                        line,
                        col,
                        "sql-rules-unknown-column",
                        f"column {ref.raw!r} is not in the declared schema for {table!r}",
                        fix="use a declared column name, or declare this one first",
                        doc=DOC,
                    )
                )
        elif table in cte_names or table in derived or table in referenced:
            continue  # CTE/subquery columns aren't modeled; an unknown table is reported once
        else:
            findings.append(
                Finding(
                    rel,
                    line,
                    col,
                    "sql-rules-unknown-qualifier",
                    f"qualifier {qualifier!r} is not a known table alias, CTE, or declared table",
                    fix="alias the source table, or use a declared table name",
                    doc=DOC,
                )
            )
    return findings


def _check_noqa_comments(rel: str, tree: BaseSegment) -> list[Finding]:
    findings = []
    for seg in tree.recursive_crawl("inline_comment", "block_comment"):
        if "noqa" in seg.raw.lower():
            line, col = _pos(seg)
            findings.append(
                Finding(
                    rel,
                    line,
                    col,
                    "sql-rules-noqa-comment",
                    "a noqa comment suppresses a rule; this comment is itself the finding",
                    fix="fix the underlying SQLFluff finding and delete this comment",
                    doc=DOC,
                )
            )
    return findings


def check_file(
    path: Path,
    schema: SqlSchema,
    rel: str | None = None,
    dialect: str = "sqlite",
    migration_dirs: tuple[str, ...] = ("alembic/sql",),
) -> list[Finding]:
    rel = rel or path.as_posix()
    parsed = _linter(dialect).parse_string(path.read_text(encoding="utf-8"))
    # `ParsedString.tree` asserts success; walk the variant so an unparsable file becomes
    # a Finding, never an uncaught AssertionError.
    variants = parsed.parsed_variants
    if not variants or variants[0].tree is None:
        message = "sqlfluff could not parse this file"
        return [Finding(rel, 1, 1, "sql-rules-parse-error", message, doc=DOC)]
    tree = variants[0].tree
    findings: list[Finding] = []
    # sqlfluff keeps going past text it cannot parse and wraps it in `unparsable`
    # segments; the rules below never see what those hold, so each one is a finding.
    for seg in tree.recursive_crawl("unparsable"):
        line, col = _pos(seg)
        message = "sqlfluff could not parse this statement"
        findings.append(Finding(rel, line, col, "sql-rules-parse-error", message, doc=DOC))
    for stmt in tree.recursive_crawl(*_MUTATING_TYPES):
        findings += _check_mutating_statement(rel, stmt, schema)
    if not in_migration_dir(path, migration_dirs):
        drops = tree.recursive_crawl(*_DROP_TYPES)
        findings += [_check_drop(rel, stmt, migration_dirs) for stmt in drops]
    findings += _check_references(rel, tree, schema.columns_by_table())
    findings += _check_noqa_comments(rel, tree)
    return findings


def run(config: Config, paths: list[Path] | None = None) -> StepResult:
    files = paths if paths is not None else iter_files(config.root, config.sql_dirs, ".sql")
    if not files:
        return StepResult.ok(STEP, detail="skipped: no .sql files")
    schema = load_schema(config.schema)
    findings = [
        f
        for path in files
        for f in check_file(
            path, schema, config.rel(path), config.dialect, config.migration_sql_dirs
        )
    ]
    detail = (
        ""
        if schema.loaded
        else "degraded: no schema file ([paths].schema), so the unknown-table, -column and "
        "-qualifier rules did not run and no table is hard-delete allow-listed"
    )
    return StepResult.from_findings(STEP, findings, detail)
