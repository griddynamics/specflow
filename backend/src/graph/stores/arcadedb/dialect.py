"""The ArcadeDB dialect: the canonical Cypher plus per-template overrides (design D2, D7; spike section 3).

ArcadeDB 26.10 speaks the canonical Cypher of ``graph.core`` for almost every template. A template that
cannot run as it stands is overridden, in one of three ways:

- derived: the canonical text with one or two exact replacements (a replacement that finds no match
  raises ``arcadedb_override_stale`` at plan time, so a changed canonical file never ships a silently
  wrong statement);
- a file under ``overrides/`` (static ``__LABEL__`` / ``__TYPE__`` tokens filled with validated names);
- several statements and a reducer (rows grouped by domain label, one statement per relationship type).

ArcadeDB ignores a dynamic label in ``CREATE``/``MERGE`` and creates an edge of type ``EDGE`` for a dynamic
relationship type, without an error. Every statement the dialect plans is therefore checked for a
``$(...)`` name in a write that creates (``dynamic_name_in_write``); a statement that has one is refused.

The relationship types a merge or a move must carry over are not known from the ontology alone (a foreign
owner's types), so the executor reads the database's relationship types first and hands them to ``plan``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from functools import cache
from typing import Any, Final

from graph.core.dialect import ExecutionPlan, PlannedStatement, Record
from graph.core.errors import ConformanceError
from graph.core.queries import SAFE_NAME, Query, safe_name
from graph.core.query_files import render_query
from graph.stores._bolt.cypher import CYPHER_DIALECT, binds_project_id, render_cypher, template_ids
from graph.stores.arcadedb.names import index_fields, index_names
from graph.stores.arcadedb.statements import OVERRIDE_DIRECTORY, cypher

NEEDS_EDGE_TYPES: Final = frozenset({"core.move_foreign_relationships", "build.build_merge_spec_nodes"})
# What the executor runs to learn them (unscoped: it reads the schema, not project data).
RELATIONSHIP_TYPES: Final = cypher("core", "relationship_types.cypher")

# A statement that has ``$(`` inside FOREACH, or after one of these clauses, creates with a dynamic name.
_CREATING_CLAUSES: Final = frozenset({"CREATE", "MERGE", "ON CREATE SET", "ON MATCH SET"})
_CLAUSE: Final = re.compile(
    r"\b(ON\s+CREATE\s+SET|ON\s+MATCH\s+SET|CREATE|MERGE|FOREACH|SET|REMOVE|MATCH|WHERE|WITH|RETURN|UNWIND|CALL"
    r"|DELETE|DETACH|OPTIONAL|ORDER|LIMIT|SKIP)\b",
    re.IGNORECASE,
)
_FOREACH: Final = re.compile(r"\bFOREACH\s*\(", re.IGNORECASE)

Override = Callable[[Query, Mapping[str, Any], Sequence[str] | None], ExecutionPlan]


def _foreach_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for found in _FOREACH.finditer(text):
        depth, index = 1, found.end()
        while index < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[index], 0)
            index += 1
        spans.append((found.start(), index))
    return spans


_STRING: Final = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_MAP_KEY: Final = re.compile(r"(?<![\w$.:])\w+(?=\s*:)")
_PROPERTY: Final = re.compile(r"(?<=\.)\s*\w+")


def _scrub(text: str) -> str:
    """``text`` with string literals, map keys and property names blanked, so a word inside them (a key
    ``{limit: 1}``, a property ``n.skip``, a string ``'MERGE'``) is never read as a clause keyword."""

    return _PROPERTY.sub("_", _MAP_KEY.sub("_", _STRING.sub("''", text)))


@cache
def dynamic_name_in_write(text: str) -> bool:
    """Whether ``text`` has a dynamic ``$(...)`` label or type where ArcadeDB silently drops it: inside
    ``FOREACH``, or after ``CREATE``, ``MERGE``, ``ON CREATE SET`` or ``ON MATCH SET``. A ``SET`` or
    ``REMOVE`` of a dynamic label on a stored node works and is allowed."""

    text = _scrub(text)
    spans = _foreach_spans(text)
    for found in re.finditer(r"\$\(", text):
        if any(start <= found.start() < end for start, end in spans):
            return True
        clauses = _CLAUSE.findall(text[: found.start()])
        if clauses and re.sub(r"\s+", " ", clauses[-1].upper()) in _CREATING_CLAUSES:
            return True
    return False


def _sum(column: str) -> Callable[[Sequence[list[Record]]], list[Record]]:
    """Reducer: one row whose ``column`` is the sum of that column over the statements' first rows."""

    def reduce(results: Sequence[list[Record]]) -> list[Record]:
        return [{column: sum(int(rows[0][column]) for rows in results if rows)}]

    return reduce


def _plan_upsert_spec_node(_query: Query, params: Mapping[str, Any], _types: Sequence[str] | None) -> ExecutionPlan:
    text = render_query(cypher("core", "upsert_spec_node.cypher"), label=safe_name(params.get("label")))
    return ExecutionPlan.single(text, params)


def _plan_create_spec_nodes(_query: Query, params: Mapping[str, Any], _types: Sequence[str] | None) -> ExecutionPlan:
    groups: dict[str, list[Any]] = {}
    for row in params["rows"]:
        groups.setdefault(safe_name(row.get("label")), []).append(row)
    text = cypher("build", "build_create_spec_nodes.cypher")
    statements = tuple(
        PlannedStatement(render_query(text, label=label), {**params, "rows": rows})
        for label, rows in sorted(groups.items())
    )
    return ExecutionPlan(statements, _sum("written"))


def foreign_types(edge_types: Sequence[str], owned: Sequence[str]) -> list[str]:
    """The relationship types a merge or move carries over: those of the database build does not own.

    A type whose name cannot be rendered into a statement is left out here; the plan's guard statement then
    finds its edges on the old node and the reducer refuses the merge or move (``arcadedb_unrenderable_edge_type``),
    so no edge is lost. A type of that kind elsewhere in the database does no harm."""

    kept = {name for name in edge_types if SAFE_NAME.fullmatch(name) and name not in set(owned)}
    return sorted(kept)


def _refuse_left_over(rows: Sequence[Record], query: str) -> None:
    """Raise when the guard statement counted a relationship no per-type statement carried over."""

    if rows and int(rows[0].get("left") or 0) > 0:
        raise ConformanceError("arcadedb_unrenderable_edge_type", query=query)


def _plan_merge_spec_nodes(query: Query, params: Mapping[str, Any], types: Sequence[str] | None) -> ExecutionPlan:
    if types is None:
        raise ConformanceError("arcadedb_edge_types_missing", query=query.name)
    move = cypher("build", "build_merge_spec_nodes_move.cypher")
    names = foreign_types(types, params.get("owned_types", ()))
    statements = (
        PlannedStatement(cypher("build", "build_merge_spec_nodes_aliases.cypher"), params),
        *(PlannedStatement(render_query(move, type=name), params) for name in names),
        PlannedStatement(cypher("build", "build_merge_spec_nodes_guard.cypher"), params),
        PlannedStatement(cypher("build", "build_merge_spec_nodes_delete.cypher"), params),
    )
    guard = len(names) + 1

    def reduce(results: Sequence[list[Record]]) -> list[Record]:
        _refuse_left_over(results[guard] if len(results) > guard else [], query.name)
        return results[0]  # the first statement's row: the survivors found

    return ExecutionPlan(statements, reduce)


def _plan_move_foreign(query: Query, params: Mapping[str, Any], types: Sequence[str] | None) -> ExecutionPlan:
    if types is None:
        raise ConformanceError("arcadedb_edge_types_missing", query=query.name)
    text = cypher("core", "move_foreign_relationships.cypher")
    names = foreign_types(types, params.get("owned_types", ()))
    statements = (
        *(PlannedStatement(render_query(text, type=name), params) for name in names),
        PlannedStatement(cypher("core", "move_foreign_relationships_guard.cypher"), params),
    )
    summed = _sum("moved")

    def reduce(results: Sequence[list[Record]]) -> list[Record]:
        _refuse_left_over(results[len(names)] if len(results) > len(names) else [], query.name)
        return summed(results[: len(names)])

    return ExecutionPlan(statements, reduce)


def search_params(params: Mapping[str, Any], *, per_field: bool = False) -> Mapping[str, Any]:
    """The parameters of an index search with ``$index`` (a library index name) mapped to the server's name.

    ``per_field``: a fulltext ``$query`` of plain terms over an index of several fields becomes one
    group per field, ``name:(a b) statement_text:(a b)``. ArcadeDB's unqualified terms are scored over
    all fields together (one document length, one idf), which ranks a node whose long text mentions the
    terms above one whose short name is the match; per-field groups restore Lucene's per-field BM25 that
    Neo4j gives. On the shop fixture it moves the top three of the fulltext-only search from 29 to 33 of 36
    places agreeing with Neo4j (the 12 golden queries); it does not make the two orders equal."""

    index = params.get("index")
    if index is None:
        return params
    bound = {**params, "index": index_names().get(index, index)}
    fields = index_fields().get(index, ())
    if per_field and len(fields) > 1 and isinstance(params.get("query"), str) and params["query"].strip():
        bound["query"] = " ".join(f"{field}:({params['query']})" for field in fields)
    return bound


@cache
def _replacements(template: str) -> tuple[tuple[str, str], ...]:
    """The exact replacements of the canonical text of ``template`` (``overrides/derived.json``)."""

    table = json.loads((OVERRIDE_DIRECTORY / "derived.json").read_text(encoding="utf-8"))
    return tuple((old, new) for old, new in table.get(template, ()))


def _derived(template: str | None = None, *, index: bool = False, per_field: bool = False) -> Override:
    """Override: the canonical text with each replacement of ``overrides/derived.json`` for ``template``
    applied once (no match, or several: ``arcadedb_override_stale``); ``index`` also maps the ``$index``
    parameter (a library index name) to the server's name for it (``search_params``)."""

    def plan(query: Query, params: Mapping[str, Any], _types: Sequence[str] | None) -> ExecutionPlan:
        text = render_cypher(query)
        for old, new in _replacements(template or query.template):
            if text.count(old) != 1:
                raise ConformanceError("arcadedb_override_stale", template=query.template)
            text = text.replace(old, new)
        return ExecutionPlan.single(text, search_params(params, per_field=per_field) if index else params)

    return plan


# template id -> override. The first group are the ones the canonical text cannot do on ArcadeDB (REQUIRED);
# the rest only map a name or a score and would also run unchanged except for a wrong result.
OVERRIDES: Final[Mapping[str, Override]] = {
    "core.upsert_spec_node": _plan_upsert_spec_node,
    "build.build_create_spec_nodes": _plan_create_spec_nodes,
    "build.build_update_spec_nodes": lambda _query, params, _types: ExecutionPlan.single(
        cypher("build", "build_update_spec_nodes.cypher"), params
    ),
    "build.build_repair_set_spec_ids": lambda _query, params, _types: ExecutionPlan.single(
        cypher("build", "build_repair_set_spec_ids.cypher"), params
    ),
    "build.build_merge_spec_nodes": _plan_merge_spec_nodes,
    "core.move_foreign_relationships": _plan_move_foreign,
    # a quantified path with ``nodes(p)[1..]`` in a WHERE returns no rows on ArcadeDB
    "core.scope_via": _derived(),
    # ArcadeDB's Cypher has no CYPHER 5 prefix; its procedure scores are already on the (1 + cos) / 2 scale
    "core.search_nodes_vector": _derived(index=True),
    # vector.similarity.cosine is the raw cosine on ArcadeDB; the reader's floor is on (1 + cos) / 2
    "core.search_nodes_vector_exact": _derived(),
    "core.search_nodes_fulltext": _derived(index=True, per_field=True),
    "core.search_fragments": _derived(index=True, per_field=True),
    # index state is ONLINE when a SQL index create returns: nothing to wait for
    "core.await_indexes": lambda _query, _params, _types: ExecutionPlan.single(cypher("core", "online.cypher"), {}),
    # a label write moves the record (its element id changes): verify and repair address nodes by uuid, edges by uuid
    "build.build_verify_spec_nodes": _derived(),
    "build.build_verify_domain_edges": _derived(),
    "build.build_repair_delete_edges": _derived(),
    "build.build_repair_edge_evidence": _derived(),
    "build.build_repair_delete_nodes": _derived(),
    "build.build_repair_set_labels": _derived(),
    "build.build_repair_node_evidence": _derived(),
}

# Overrides without which the statement fails or writes wrong data on ArcadeDB. A store refuses to start
# when one of these has no override (``verify_coverage``).
REQUIRED: Final = frozenset(
    {
        "core.upsert_spec_node",
        "build.build_create_spec_nodes",
        "build.build_update_spec_nodes",
        "build.build_repair_set_spec_ids",
        "build.build_merge_spec_nodes",
        "core.move_foreign_relationships",
        "core.scope_via",
        "core.search_nodes_vector",
        "core.search_nodes_vector_exact",
        "core.await_indexes",
        "build.build_verify_spec_nodes",
        "build.build_verify_domain_edges",
        "build.build_repair_delete_edges",
        "build.build_repair_edge_evidence",
        "build.build_repair_delete_nodes",
        "build.build_repair_set_labels",
        "build.build_repair_node_evidence",
    }
)


def verify_coverage() -> None:
    """Raise ``ConformanceError("arcadedb_override_missing")`` when a required override is not registered or
    one is registered for a template the Cypher dialect does not know (every other template runs as its
    canonical text)."""

    missing = REQUIRED - OVERRIDES.keys()
    unknown = OVERRIDES.keys() - template_ids()
    if missing or unknown:
        raise ConformanceError("arcadedb_override_missing", missing=len(missing), unknown=len(unknown))


class ArcadeDbDialect:
    """Plans every query as its canonical Cypher or its ArcadeDB override, refusing a scoped statement that
    does not bind ``$project_id`` and a statement that creates with a dynamic name."""

    name: Final = "arcadedb-cypher"

    def require_project_binding(self, query: Query) -> None:
        """Refuse a scoped query whose canonical statement does not bind the project id (``plan`` proves the
        planned statements bind it too)."""

        CYPHER_DIALECT.require_project_binding(query)

    def plan(self, query: Query, params: Mapping[str, Any], edge_types: Sequence[str] | None = None) -> ExecutionPlan:
        override = OVERRIDES.get(query.template)
        if override is None:
            plan = CYPHER_DIALECT.plan(query, params)
        else:
            if query.template in NEEDS_EDGE_TYPES and edge_types is None:
                raise ConformanceError("arcadedb_edge_types_missing", query=query.name)
            plan = override(query, params, edge_types)
        for statement in plan.statements:
            if dynamic_name_in_write(statement.text):
                raise ConformanceError("arcadedb_dynamic_name", query=query.name)
            if query.project_scoped and not binds_project_id(statement.text):
                raise ConformanceError("query_missing_project_id", query=query.name)
        return plan


ARCADEDB_DIALECT: Final = ArcadeDbDialect()
