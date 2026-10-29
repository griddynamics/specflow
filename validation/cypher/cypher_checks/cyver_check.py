"""CyVer's syntax, schema and strict properties validators over every `.cypher` file.

Run against the fixture graph seeded from the schema file. A leftover metadata entry other
than `ParameterNotProvided`, or a score below 1.0, is a finding: both signals are checked,
since a `|`-alternation can score 1.0 with metadata left.
"""

from __future__ import annotations

import sys
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import neo4j
from CyVer import PropertiesValidator, SchemaValidator, SyntaxValidator

from cypher_checks.findings import Finding, sort_key
from cypher_checks.query_files import statements_in

__all__ = [
    "DOC_ANCHOR",
    "PARAM_NOT_PROVIDED",
    "check_files",
    "check_query",
    "drop_expected_notifications",
    "tolerate_cyver_notifications_deprecation",
]

DOC_ANCHOR = "RULES.md#live-schema-validation"

# CyVer's own `SyntaxValidator.validate()` never binds `$param`s, so this is not a real
# defect -- the one code this wrapper drops everywhere.
PARAM_NOT_PROVIDED = "Neo.ClientNotification.Statement.ParameterNotProvided"


def drop_expected_notifications(metadata: list[dict[str, str]]) -> list[dict[str, str]]:
    return [m for m in metadata if m.get("code") != PARAM_NOT_PROVIDED]


@contextmanager
def tolerate_cyver_notifications_deprecation() -> Iterator[None]:
    """Tolerate the driver's `ResultSummary.notifications` deprecation inside CyVer 2.0.2.

    CyVer reads it internally with no replacement, and `filterwarnings = ["error"]` would
    otherwise crash every CyVer call on this one known warning."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=r".*ResultSummary\.notifications is deprecated.*")
        yield


_GENERIC_FIX = "align the query with the declared graph schema"


@dataclass(frozen=True)
class _Validators:
    """CyVer's three validators, built once per `check_files` call: `SyntaxValidator`'s
    constructor alone runs a round trip against the driver."""

    syntax: SyntaxValidator
    schema: SchemaValidator
    properties: PropertiesValidator


def _build_validators(driver: neo4j.Driver) -> _Validators:
    with tolerate_cyver_notifications_deprecation():
        return _Validators(
            SyntaxValidator(driver), SchemaValidator(driver), PropertiesValidator(driver)
        )


def _score_finding(path: str, validator: str, score: float) -> Finding:
    return Finding(
        path=path,
        line=1,
        col=1,
        rule=f"cyver-{validator}-score",
        message=f"{validator} validator score {score:.3f} (below 1.0)",
        fix=_GENERIC_FIX,
        doc=DOC_ANCHOR,
    )


def _metadata_findings(path: str, validator: str, metadata: list[dict[str, str]]) -> list[Finding]:
    return [
        Finding(
            path=path,
            line=1,
            col=1,
            rule=m["code"],
            message=f"[{validator}] {m['description']}",
            fix=_GENERIC_FIX,
            doc=DOC_ANCHOR,
        )
        for m in drop_expected_notifications(metadata)
    ]


def _check_query_with(validators: _Validators, path: str, query: str) -> list[Finding]:
    with tolerate_cyver_notifications_deprecation():
        _, syntax_meta = validators.syntax.validate(query)
        schema_score, schema_meta = validators.schema.validate(query)
        props_score, props_meta = validators.properties.validate(query, strict=True)

    findings = _metadata_findings(path, "syntax", syntax_meta)

    if schema_score < 1.0:
        findings.append(_score_finding(path, "schema", schema_score))
    findings += _metadata_findings(path, "schema", schema_meta)

    if props_score is not None and props_score < 1.0:
        findings.append(_score_finding(path, "properties", props_score))
    findings += _metadata_findings(path, "properties", props_meta)

    return findings


def check_query(driver: neo4j.Driver, path: str, query: str) -> list[Finding]:
    """Every finding CyVer's three validators raise for one query text."""
    return _check_query_with(_build_validators(driver), path, query)


def _display(path: Path, root: Path | None) -> str:
    base = (Path.cwd() if root is None else root).resolve()
    try:
        return path.resolve().relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def check_files(
    driver: neo4j.Driver,
    paths: Sequence[Path],
    ddl_globs: Sequence[str] = (),
    root: Path | None = None,
) -> list[Finding]:
    """`check_query` for every statement of every path, one validator set for the batch.

    An empty target set is a clean pass. A DDL file is split into the statements Neo4j
    receives, so CyVer checks the DDL rather than one malformed query."""
    if not paths:
        sys.stdout.write("skipped: no targets\n")
        return []
    validators = _build_validators(driver)
    findings: list[Finding] = []
    for path in paths:
        rel = _display(path, root)
        for statement in statements_in(path, ddl_globs):
            findings.extend(_check_query_with(validators, rel, statement))
    return sorted(findings, key=sort_key)
