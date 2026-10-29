"""`EXPLAIN` each query on the seeded fixture graph with dummy `$param`s and read the plan.

Fails on any server notification (deprecations, cartesian products, unknown labels, exhaustive
shortest paths ...), an `AllNodesScan`, a label scan of a `policy.banned_scan_labels` label
outside `policy.scan_shaped_queries`, and a registered capped walk not planned to its cap.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import neo4j

from cypher_checks.findings import Finding, sort_key
from cypher_checks.query_files import statements_in
from cypher_checks.schema import GraphSchema

__all__ = ["CAP_DOC_ANCHOR", "DOC_ANCHOR", "check_files", "check_query"]

DOC_ANCHOR = "RULES.md#reading-the-plan"
CAP_DOC_ANCHOR = "RULES.md#bound-every-variable-length-path"

_PARAM = re.compile(r"\$(\w+)")
_SUBSCRIPTED_PARAM = re.compile(r"\$(\w+)\[")
_VAR_LENGTH_BOUND = re.compile(r"\*\s*\d*\s*\.\.\s*(\d+)")

_PARAM_TYPE_MISMATCH = re.compile(
    r"Type mismatch for parameter '(?P<name>\w+)': expected (?P<type>.+?) but was"
)
#: The server's text for a mismatch it pins on no parameter: an element a query takes
#: out of a parameter by subscript (`$parts[i].embedding`, `$limits[i]`) is typed from the
#: bound value, and its mismatch names no parameter.
_UNNAMED_TYPE_MISMATCH = "Type mismatch: expected"


def _typed_value_for(expected_type: str) -> Any:
    """A dummy value of the type the driver's `expected <Type>` error text names.

    The server names the type it wants, so nothing is guessed from a parameter's name (a
    guess from the name mistypes a list parameter and crashes `EXPLAIN`)."""
    t = expected_type.lower()
    return next((sample() for marker, sample in _SAMPLE_BY_EXPECTED_TYPE if marker in t), "fixture")


#: The first marker the expected-type text contains picks the dummy value, so order matters.
#: A number list satisfies every vector-argument member; an empty map satisfies "expected
#: Map, Node or Relationship"; a plain `List<T>` gets an empty list, which fixes no element
#: type a later use could contradict. Each sample is built per call, so none is shared.
_SAMPLE_BY_EXPECTED_TYPE: tuple[tuple[str, Callable[[], Any]], ...] = (
    ("vector", lambda: [1.0]),
    ("list<float>", lambda: [1.0]),
    ("list", list),
    ("map", dict),
    ("integer", lambda: 1),
    ("float", lambda: 1.0),
    ("boolean", lambda: True),
)


def _plan_operators(plan: dict[str, Any]) -> list[dict[str, Any]]:
    ops = [plan]
    for child in plan.get("children", []):
        ops.extend(_plan_operators(child))
    return ops


_LABEL_SCAN_SUFFIXES = ("NodeByLabelScan", "NodeByLabelsScan")


def _is_label_scan(op_type: str) -> bool:
    """`NodeByLabelScan` and Neo4j's multi-label variants, which a plain `==` would miss.

    `(m:Movie:Entity)` plans as `IntersectionNodeByLabelsScan`, `(n:Movie|Person)` as
    `UnionNodeByLabelsScan`."""
    return op_type.endswith(_LABEL_SCAN_SUFFIXES)


def _scan_labels(details: str) -> set[str]:
    """The label set in a label-scan operator's `Details` text, after the variable's `:`.

    `"n:Movie"` -> `{"Movie"}`, `"m:Entity&Movie"` -> both, `"n:Movie|Person"` -> both.
    Splits on the first `:`, then on `&`/`|`."""
    _, sep, rest = details.partition(":")
    if not sep:
        return set()
    return {label.strip() for label in re.split(r"[&|]", rest) if label.strip()}


def _finding(path: str, rule: str, message: str, *, line: int = 1, col: int = 1) -> Finding:
    doc = CAP_DOC_ANCHOR if rule == "cap-mismatch" else DOC_ANCHOR
    fix = (
        "bound the path to the registered cap and filter it"
        if rule == "cap-mismatch"
        else "rewrite the query so the plan no longer does this"
    )
    return Finding(path=path, line=line, col=col, rule=rule, message=message, fix=fix, doc=doc)


def _notification_findings(path: str, summary: neo4j.ResultSummary) -> list[Finding]:
    """Every server notification for one `EXPLAIN`, read through `gql_status_objects`.

    `is_notification` drops the plain success status every query carries; `.position` gives
    the line/column when the server supplies one, else `1:1`."""
    findings = []
    for status in summary.gql_status_objects:
        if not status.is_notification:
            continue
        pos = status.position
        findings.append(
            _finding(
                path,
                status.gql_status,
                status.status_description,
                line=pos.line if pos else 1,
                col=pos.column if pos else 1,
            )
        )
    return findings


def _scan_findings(
    path: str, plan: dict[str, Any], *, scan_exempt: bool, banned: frozenset[str]
) -> tuple[list[Finding], set[int]]:
    """`(AllNodesScan/banned-label-scan findings, every var-length upper bound
    the plan's operator `Details` text names)` -- one walk of the plan tree."""
    findings: list[Finding] = []
    var_length_bounds: set[int] = set()
    for op in _plan_operators(plan):
        op_type = op.get("operatorType", "").split("@", 1)[0]
        details = op.get("args", {}).get("Details", "")
        if op_type == "AllNodesScan" and not scan_exempt:
            findings.append(_finding(path, "AllNodesScan", f"full node scan: {details}"))
        elif _is_label_scan(op_type) and not scan_exempt:
            hit = _scan_labels(details) & banned
            if hit:
                labels = ", ".join(sorted(hit))
                findings.append(
                    _finding(path, "NodeByLabelScan", f"unindexed scan of {labels}: {details}")
                )
        var_length_bounds.update(int(m) for m in _VAR_LENGTH_BOUND.findall(details))
    return findings, var_length_bounds


def _retyped(query: str, params: dict[str, Any], retried: set[str], msg: str) -> bool:
    """Rebind one parameter from a driver type error; `False` when nothing can be retyped.

    A named mismatch retypes that parameter from the expected type the server names; an
    unnamed one makes a subscripted `$name[` a list; a map expected from an `UNWIND`ed
    string makes that parameter a list of one map. Each parameter is retyped at most once.
    """
    match = _PARAM_TYPE_MISMATCH.search(msg)
    if match is not None and match["name"] in params and match["name"] not in retried:
        params[match["name"]] = _typed_value_for(match["type"])
        retried.add(match["name"])
        return True
    if _UNNAMED_TYPE_MISMATCH in msg and "but was String" in msg:
        subscripted = next(
            (n for n in _SUBSCRIPTED_PARAM.findall(query) if n in params and n not in retried),
            None,
        )
        if subscripted is not None:
            params[subscripted] = []
            retried.add(subscripted)
            return True
    if "expected Map" in msg and "but was String" in msg:
        unwind = re.search(r"UNWIND\s+\$(\w+)\s+AS\s+(\w+)", query, re.IGNORECASE)
        if unwind and unwind.group(1) in params and unwind.group(1) not in retried:
            params[unwind.group(1)] = [{}]
            retried.add(unwind.group(1))
            return True
    return False


def _explain(session: neo4j.Session, path: str, query: str) -> neo4j.ResultSummary | Finding:
    """`EXPLAIN <query>` with every `$param` a `"fixture"` string, retyped from driver errors.

    Any driver error no retype resolves is one finding for the query, never an escaping
    exception."""
    params: dict[str, Any] = dict.fromkeys(set(_PARAM.findall(query)), "fixture")
    retried: set[str] = set()
    while True:
        try:
            return session.run(f"EXPLAIN {query}", params).consume()
        except neo4j.exceptions.Neo4jError as exc:
            msg = exc.message or str(exc)
            if _retyped(query, params, retried, msg):
                continue
            return _finding(path, exc.code or "cypher-explain-error", msg)


def _check_query_in(
    session: neo4j.Session, path: str, query: str, schema: GraphSchema
) -> list[Finding]:
    """Every `EXPLAIN` finding for one query text, planned on `session`. `EXPLAIN` plans
    without executing, so a leading `//` header comment is harmless right after it."""
    summary = _explain(session, path, query)
    if isinstance(summary, Finding):
        return [summary]

    findings = _notification_findings(path, summary)

    plan = summary.plan
    if plan is None:
        return findings

    policy = schema.policy
    scan_findings, var_length_bounds = _scan_findings(
        path,
        dict(plan),
        scan_exempt=policy.is_scan_shaped(path),
        banned=policy.banned_scan_labels,
    )
    findings.extend(scan_findings)

    # The cap and key are the capped-traversal register's, read through the same resolver
    # as the static lint, so a walk is never capped here and uncapped there.
    capped = policy.capped_for(path)
    if capped is not None and var_length_bounds != {capped.hard_cap}:
        findings.append(
            _finding(
                path,
                "cap-mismatch",
                f"plan expands to {sorted(var_length_bounds) or 'no'} bound(s), "
                f"not the registered cap {capped.hard_cap} ({capped.key})",
            )
        )
    return findings


def check_query(driver: neo4j.Driver, path: str, query: str, schema: GraphSchema) -> list[Finding]:
    """Every `EXPLAIN` finding for one query text, on one session closed on exit."""
    with driver.session() as session:
        return _check_query_in(session, path, query, schema)


def _display(path: Path, root: Path | None) -> str:
    base = (Path.cwd() if root is None else root).resolve()
    try:
        return path.resolve().relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def check_files(
    driver: neo4j.Driver, paths: Sequence[Path], schema: GraphSchema, root: Path | None = None
) -> list[Finding]:
    """`check_query` for every statement of every path; an empty target set is a clean pass.

    A DDL file (`policy.ddl_globs`) is split, since `EXPLAIN` plans one statement per call.
    One session serves the whole batch: a session per statement spends far more round trips
    on the same plans."""
    if not paths:
        sys.stdout.write("skipped: no targets\n")
        return []
    findings: list[Finding] = []
    with driver.session() as session:
        for path in paths:
            rel = _display(path, root)
            for statement in statements_in(path, schema.policy.ddl_globs):
                findings.extend(_check_query_in(session, rel, statement, schema))
    return sorted(findings, key=sort_key)
