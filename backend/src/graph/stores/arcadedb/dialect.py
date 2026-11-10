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
from graph.core.query_files import fragment, render_query
from graph.stores._bolt.cypher import CYPHER_DIALECT, binds_project_id, render_cypher, template_ids
from graph.stores.arcadedb.chunking import chunked
from graph.stores.arcadedb.names import index_fields, index_names
from graph.stores.arcadedb.statements import OVERRIDE_DIRECTORY, cypher

NEEDS_EDGE_TYPES: Final = frozenset(
    {"core.move_foreign_relationships", "build.build_merge_spec_nodes", "build.build_repair_set_labels"}
)
SPEC_NODE: Final = "SpecNode"
# Templates whose plan needs the current labels of the nodes of ``$rows``: the executor reads them with
# ``NODE_LABELS`` (in the caller's transaction when there is one) and hands them to ``plan`` as ``node_labels``.
NEEDS_NODE_LABELS: Final = frozenset({"build.build_repair_set_labels"})
NODE_LABELS_READ: Final = cypher("build", "build_repair_set_labels_read.cypher")


def node_labels_statement(params: Mapping[str, Any]) -> PlannedStatement:
    """The read of the labels of the keyed nodes of ``params["rows"]`` (its ``project_id`` already bound)."""

    return PlannedStatement(
        NODE_LABELS_READ, {"rows": list(params.get("rows", ())), "project_id": params["project_id"]}
    )


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


# Subjects per statement of ``infer_similar_targets``. Each statement reads the project's targets once and scores
# every subject of its chunk against them; the statements of one call run one after the other.
SIMILAR_SUBJECTS_PER_STATEMENT: Final = 50


def _plan_similar_targets(query: Query, params: Mapping[str, Any], _types: Sequence[str] | None) -> ExecutionPlan:
    """Nearest targets per subject on ArcadeDB, with a bounded heap.

    The canonical statement ends with ``ORDER BY uuid`` over one row per subject. ArcadeDB sorts those rows in
    heap with every variable of the row, and each row carries the list of all target nodes (embeddings
    included): measured on 1300 targets of 3072 floats, about 31 MB per subject, so a call with 20 or more subjects
    exceeded the 768 MB query heap budget (``arcadedb.queryMaxHeapRAM``). The override leaves that sort out
    (the per-subject top-k sort inside the subquery is bounded by the targets of one subject), cuts ``$uuids``
    into chunks of ``SIMILAR_SUBJECTS_PER_STATEMENT`` and restores the order in the reducer. The score is on the
    (1 + cosine) / 2 scale of the other stores (ArcadeDB's cosine function is the raw cosine); the order is
    unchanged by that (best first, ties by spec_id, then uuid, inside the statement; subjects by uuid here)."""

    text = _derived_text(query)
    uuids = list(params["uuids"])
    size = SIMILAR_SUBJECTS_PER_STATEMENT
    statements = tuple(
        PlannedStatement(text, {**params, "uuids": uuids[start : start + size]})
        for start in range(0, max(len(uuids), 1), size)
    )

    def reduce(results: Sequence[list[Record]]) -> list[Record]:
        return sorted((row for rows in results for row in rows), key=lambda row: str(row["uuid"]))

    return ExecutionPlan(statements, reduce)


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


def _copy_statement(copy: str, labels: Sequence[str]) -> str:
    """The copy statement of one final label set: its first label in ``__LABEL__``, one ``__MORE_LABELS__``
    piece of the file per further label."""

    first, *more = labels
    piece = fragment(copy, "more_labels")
    return render_query(copy, more_labels="".join(render_query(piece, label=name) for name in more), label=first)


def _plan_repair_set_labels(query: Query, params: Mapping[str, Any], types: Sequence[str] | None) -> ExecutionPlan:
    """Set the kept label of each node and remove its other build-owned labels, one record move per node.

    A label written in place moves the ArcadeDB record to another composite type; the canonical statement
    removes and sets a label, two moves of one record, and ArcadeDB refuses the second in one transaction
    (``DuplicatedKeyException``: the record of the first move still holds the unique keys ``(group_id, uuid)``
    and ``spec_key``). So a node with a uuid is copied: a copy of its final labels (``node_labels``, read by the
    executor, less the removed labels, with the kept one; one statement per label set, the labels static) under
    a temporary uuid, every relationship moved to it (one statement per type and row: a relationship between two
    nodes of the repair moves with the first and must then be seen on the second, which a later row of the same
    statement does not see), the old node deleted, and the uuid and spec_key restored on the copy. A guard
    refuses a relationship no move statement carried. A node without a uuid (addressed by element id) changes its
    labels in place, one move. ``written`` counts both."""

    if types is None:
        raise ConformanceError("arcadedb_edge_types_missing", query=query.name)
    rows = list(params.get("rows", ()))
    current: Mapping[str, Sequence[str]] = params.get("node_labels") or {}
    groups: dict[tuple[str, ...], list[Any]] = {}
    for row in rows:
        if row.get("id") not in current:
            continue  # no keyed node: the unkeyed statement handles it (or nothing matches)
        removed = {*row.get("remove", ()), SPEC_NODE}
        final = {safe_name(label) for label in current[row["id"]] if label not in removed} | {safe_name(row["label"])}
        groups.setdefault(tuple(sorted(final)), []).append(row["id"])
    statement_params = {key: value for key, value in params.items() if key != "node_labels"}
    copy = cypher("build", "build_repair_set_labels_copy.cypher")
    move = cypher("build", "build_repair_set_labels_move.cypher")
    names = foreign_types(types, ())
    statements = (
        PlannedStatement(cypher("build", "build_repair_set_labels_unkeyed.cypher"), statement_params),
        *(
            PlannedStatement(_copy_statement(copy, labels), {**statement_params, "ids": ids})
            for labels, ids in sorted(groups.items())
        ),
        *(
            PlannedStatement(render_query(move, type=name), {**statement_params, "rows": [row]})
            for name in names
            for row in rows
        ),
        PlannedStatement(cypher("build", "build_repair_set_labels_left.cypher"), statement_params),
        PlannedStatement(cypher("build", "build_repair_set_labels_delete.cypher"), statement_params),
        PlannedStatement(cypher("build", "build_repair_set_labels_keys.cypher"), statement_params),
    )
    left = 1 + len(groups) + len(names) * len(rows)

    def reduce(results: Sequence[list[Record]]) -> list[Record]:
        _refuse_left_over(results[left] if len(results) > left else [], query.name)
        unkeyed = int(results[0][0]["written"]) if results[0] else 0
        keyed = int(results[-1][0]["written"]) if results[-1] else 0
        return [{"written": unkeyed + keyed}]

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


def _derived_text(query: Query, template: str | None = None) -> str:
    """The canonical text of ``query`` with the replacements of ``overrides/derived.json`` applied once each (no
    match, or several: ``arcadedb_override_stale``)."""

    text = render_cypher(query)
    for old, new in _replacements(template or query.template):
        if text.count(old) != 1:
            raise ConformanceError("arcadedb_override_stale", template=query.template)
        text = text.replace(old, new)
    return text


@cache
def _rendered_replacements() -> Mapping[str, Mapping[str, tuple[tuple[str, str], ...]]]:
    """The replacements rendered from a call's parameters (``overrides/rendered.json``), by template and kind."""

    table = json.loads((OVERRIDE_DIRECTORY / "rendered.json").read_text(encoding="utf-8"))
    return {
        template: {kind: tuple((old, new) for old, new in pairs) for kind, pairs in kinds.items()}
        for template, kinds in table.items()
        if not template.startswith("_")
    }


def _rendered_text(query: Query, text: str, params: Mapping[str, Any]) -> str:
    """``text`` with the replacements of ``overrides/rendered.json`` for the query's template that its parameters
    call for: ``relations`` when ``$relations`` is not empty (the types rendered into the pattern, so the planner
    reads only those relationship types instead of filtering every one), ``one_label`` when ``$labels`` holds
    exactly one label (the node type matched instead of every spec node of the project). The parameters still
    filter as before; each replacement must match once (``arcadedb_override_stale``)."""

    kinds = _rendered_replacements().get(query.template, {})
    names: dict[str, dict[str, str]] = {}
    relations = params.get("relations")
    if "relations" in kinds and isinstance(relations, (list, tuple)) and relations:
        names["relations"] = {"types": "|".join(safe_name(name) for name in relations)}
    labels = params.get("labels")
    if "one_label" in kinds and isinstance(labels, (list, tuple)) and len(labels) == 1:
        names["one_label"] = {"label": safe_name(labels[0])}
    for kind, values in names.items():
        for old, new in kinds[kind]:
            if text.count(old) != 1:
                raise ConformanceError("arcadedb_override_stale", template=query.template)
            text = text.replace(old, render_query(new, **values))
    return text


def spec_key(project_id: Any, spec_id: Any) -> str | None:
    """The derived unique key of a spec node (``schema.SPEC_KEY``): ``group_id + ':' + spec_id``; None (matches
    nothing, as a null ``spec_id`` does) without a string ``spec_id``."""

    return f"{project_id}:{spec_id}" if isinstance(spec_id, str) else None


def _derived(
    template: str | None = None, *, index: bool = False, per_field: bool = False, by_spec_key: bool = False
) -> Override:
    """Override: the canonical text with each replacement of ``overrides/derived.json`` for ``template``
    applied once (no match, or several: ``arcadedb_override_stale``), then those of ``overrides/rendered.json``
    the parameters call for; ``index`` also maps the ``$index`` parameter (a library index name) to the server's
    name for it (``search_params``). ``by_spec_key`` binds ``$spec_key`` from ``$project_id`` and ``$spec_id``:
    ArcadeDB plans a ``(group_id, spec_id)`` lookup on the first index that starts with ``group_id`` and scans
    the project, while the unique ``spec_key`` index is a seek (it must be a parameter, not a value computed in
    the statement, for the planner to use it)."""

    def plan(query: Query, params: Mapping[str, Any], _types: Sequence[str] | None) -> ExecutionPlan:
        text = _rendered_text(query, _derived_text(query, template), params)
        bound = search_params(params, per_field=per_field) if index else params
        if by_spec_key:
            bound = {**bound, "spec_key": spec_key(params.get("project_id"), params.get("spec_id"))}
        return ExecutionPlan.single(text, bound)

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
    # a quantified path with ``nodes(p)[1..]`` in a WHERE returns no rows on ArcadeDB (and the root by spec_key)
    "core.scope_via": _derived(by_spec_key=True),
    "build.infer_similar_targets": _plan_similar_targets,
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
    "build.build_repair_set_labels": _plan_repair_set_labels,
    "build.build_repair_node_evidence": _derived(),
    # Planner workarounds (measured on ArcadeDB 26.10, same rows): the root by its unique spec_key; a uuid list
    # unwound into key lookups; relationship types and a single label rendered into the pattern; node props
    # projected as a map (the [key, value] pairs the canonical text returns are slower here).
    "core.read_node_by_spec_id": _derived(by_spec_key=True),
    "core.read_neighbours": _derived(by_spec_key=True),
    "core.scope_sequence": _derived(by_spec_key=True),
    "core.read_node_by_alias": _derived(),
    "core.read_nodes": _derived(),
    "core.read_nodes_page": _derived(),
    "core.read_nodes_for_lines": _derived(),
    "core.read_nodes_by_uuid": _derived(),
    "core.read_impact_hop": _derived(),
    "core.read_edges": _derived(),
    "core.count_nodes": _derived(),
    "plan.plan_read_links": _derived(),
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
        "build.infer_similar_targets",
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
    """Plans every query as its canonical Cypher or its ArcadeDB override, cuts a large ``rows`` parameter into
    chunks (``graph.stores.arcadedb.chunking``), refusing a scoped statement that does not bind ``$project_id``
    and a statement that creates with a dynamic name."""

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
        plan = chunked(plan)
        for statement in plan.statements:
            if dynamic_name_in_write(statement.text):
                raise ConformanceError("arcadedb_dynamic_name", query=query.name)
            if query.project_scoped and not binds_project_id(statement.text):
                raise ConformanceError("query_missing_project_id", query=query.name)
        return plan


ARCADEDB_DIALECT: Final = ArcadeDbDialect()
