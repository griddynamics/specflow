"""The schema file and the DDL files declare the same constraints and indexes.

The checks read one schema file (`constraints.unique`, `constraints.indexes`); the database is
built from DDL files. This check holds the two to each other: every declared uniqueness
constraint and range index has a `CREATE` statement, every such statement is declared, and every
`CREATE` is idempotent (`IF NOT EXISTS`). An empty set of DDL files is a finding, never a skip.

A statement targets a node (`FOR (n:Label)`) or a relationship (`FOR ()-[r:TYPE]-()`) and names one
property (`REQUIRE n.id IS UNIQUE`, `ON (n.id)`) or a parenthesised list. In a list the properties
of `policy.merge_scope_keys` (a tenant key every key is composite with) are set aside: a
`(n.tenant, n.id)` constraint is the declared `Label.id`. A list that still holds more than one
property is a composite outside this comparison, as are vector and full-text indexes. A declared
relationship index is a `constraints.indexes` entry `TYPE.property` on a declared property of a
declared relationship type.

A declared entry on a placeholder label of `policy.placeholder_labels` (a token a template stands
for) is expanded to the same property of each concrete label it maps to: an empty list asks no DDL
of it, a non-empty one the DDL of every concrete label. A DDL template that targets the placeholder
itself creates that declared entry, so it is neither missing nor undeclared.
Run as `python -m cypher_checks parity --schema schema.yaml <ddl-file-or-dir> [...]`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from cypher_checks.findings import Finding, sort_key
from cypher_checks.query_files import matches_any, split_statements
from cypher_checks.schema import GraphSchema

__all__ = ["DOC", "check", "ddl_files_among"]

DOC = "RULES.md#schema-parity"

_CREATE = re.compile(r"CREATE\b", re.IGNORECASE)
_IF_NOT_EXISTS = re.compile(r"\bIF\s+NOT\s+EXISTS\b", re.IGNORECASE)
#: `(n:Label)` or `()-[r:TYPE]-()`, either direction; the name is `label` or `type`.
_TARGET = (
    r"(?:\(\s*\w*\s*:\s*(?P<label>\w+)\s*\)"
    r"|\(\s*\)\s*<?-\s*\[\s*\w*\s*:\s*(?P<type>\w+)\s*\]\s*->?\s*\(\s*\))"
)
_PROPERTY = r"\w+\.\w+"
_PROPERTY_LIST = rf"{_PROPERTY}(?:\s*,\s*{_PROPERTY})*"
_CONSTRAINT = re.compile(
    rf"CREATE\s+CONSTRAINT\b.*?\bFOR\s*{_TARGET}\s*"
    rf"REQUIRE\s+(?:(?P<prop>{_PROPERTY})|\(\s*(?P<props>{_PROPERTY_LIST})\s*\))\s+IS\s+UNIQUE",
    re.IGNORECASE | re.DOTALL,
)
_INDEX = re.compile(
    rf"CREATE\s+(?:RANGE\s+)?INDEX\b.*?\bFOR\s*{_TARGET}\s*"
    rf"ON\s*\(\s*(?P<props>{_PROPERTY_LIST})\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def ddl_files_among(paths: Sequence[Path], schema: GraphSchema) -> list[Path]:
    """The targets `policy.ddl_globs` names; every target when that register is empty."""
    globs = schema.policy.ddl_globs
    return [p for p in paths if not globs or matches_any(p, globs)]


def _display(path: Path, root: Path | None) -> str:
    base = (Path.cwd() if root is None else root).resolve()
    try:
        return path.resolve().relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _pair(found: re.Match[str], scope_keys: Sequence[str]) -> tuple[str, str] | None:
    """The `(label or type, property)` one statement declares, or `None` for a composite.

    The properties of `scope_keys` are set aside unless nothing else is left."""
    groups = found.groupdict()
    listed = groups.get("prop") or groups["props"]
    props = [entry.strip().split(".", 1)[1] for entry in listed.split(",")]
    declared = [prop for prop in props if prop not in scope_keys] or props
    if len(declared) != 1:
        return None
    return (found["label"] or found["type"], declared[0])


def _declared_indexes(schema: GraphSchema) -> set[tuple[str, str]]:
    """The declared node range indexes plus the declared relationship ones (`TYPE.property`)."""
    relationship = {
        (rel_type, prop)
        for rel_type, prop in schema.indexes
        if prop in (schema.relationship_property_names(rel_type) or set())
    }
    return set(schema.range_indexed_properties()) | relationship


def _expand(
    declared: set[tuple[str, str]], placeholders: Mapping[str, Sequence[str]]
) -> set[tuple[str, str]]:
    """`declared` with each placeholder entry replaced by one entry per concrete label."""
    expanded: set[tuple[str, str]] = set()
    for label, prop in declared:
        if label in placeholders:
            expanded.update((concrete, prop) for concrete in placeholders[label])
        else:
            expanded.add((label, prop))
    return expanded


def _concrete(
    found: set[tuple[str, str]],
    declared: set[tuple[str, str]],
    placeholders: Mapping[str, Sequence[str]],
) -> set[tuple[str, str]]:
    """`found` without the declared placeholder entries a DDL template creates."""
    return {pair for pair in found if not (pair[0] in placeholders and pair in declared)}


def _line_of(text: str, statement: str) -> int:
    return text.count("\n", 0, max(text.find(statement), 0)) + 1


def _diff(
    kind: str, declared: set[tuple[str, str]], found: set[tuple[str, str]], where: str
) -> list[Finding]:
    """Findings for each declared pair without DDL, and each DDL pair that is not declared."""
    missing = [
        Finding(
            where,
            1,
            1,
            f"parity-{kind}-missing-in-ddl",
            f"{label}.{prop} is declared in the schema file but no DDL file creates it",
            fix="add the CREATE statement to the DDL files, or drop the declaration",
            doc=DOC,
        )
        for label, prop in sorted(declared - found)
    ]
    undeclared = [
        Finding(
            where,
            1,
            1,
            f"parity-{kind}-undeclared",
            f"{label}.{prop} is created by a DDL file but not declared in the schema file",
            fix="declare it under constraints in the schema file, or drop the statement",
            doc=DOC,
        )
        for label, prop in sorted(found - declared)
    ]
    return [*missing, *undeclared]


def check(
    ddl_files: Sequence[Path],
    schema: GraphSchema,
    *,
    schema_label: str = "schema",
    root: Path | None = None,
) -> list[Finding]:
    """Every parity finding between `schema` and the statements of `ddl_files`."""
    if not ddl_files:
        return [
            Finding(
                schema_label,
                1,
                1,
                "parity-no-ddl-files",
                "no DDL files to compare with the declared constraints and indexes",
                fix="name the DDL files or directories (policy.ddl_globs selects among them)",
                doc=DOC,
            )
        ]
    findings: list[Finding] = []
    unique: set[tuple[str, str]] = set()
    indexes: set[tuple[str, str]] = set()
    for path in ddl_files:
        rel = _display(path, root)
        text = path.read_text(encoding="utf-8")
        for statement in split_statements(text):
            squashed = " ".join(statement.split())
            if _CREATE.match(squashed) and not _IF_NOT_EXISTS.search(squashed):
                findings.append(
                    Finding(
                        rel,
                        _line_of(text, statement),
                        1,
                        "parity-ddl-not-idempotent",
                        "a CREATE statement without IF NOT EXISTS fails on the second start",
                        fix="add IF NOT EXISTS to every CREATE",
                        doc=DOC,
                    )
                )
            constraint = _CONSTRAINT.search(squashed)
            index = _INDEX.search(squashed)
            scope_keys = schema.policy.merge_scope_keys
            if constraint is not None:
                pair = _pair(constraint, scope_keys)
                if pair is not None:
                    unique.add(pair)
            elif index is not None:
                pair = _pair(index, scope_keys)
                if pair is not None:
                    indexes.add(pair)
    placeholders = schema.policy.placeholder_labels
    declared_unique = set(schema.unique)
    declared_indexes = _declared_indexes(schema)
    findings += _diff(
        "unique",
        _expand(declared_unique, placeholders),
        _concrete(unique, declared_unique, placeholders),
        schema_label,
    )
    findings += _diff(
        "index",
        _expand(declared_indexes, placeholders),
        _concrete(indexes, declared_indexes, placeholders),
        schema_label,
    )
    return sorted(findings, key=sort_key)
