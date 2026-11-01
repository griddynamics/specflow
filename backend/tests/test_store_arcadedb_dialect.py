"""Offline tests of the ArcadeDB dialect: every override as rendered text, the fail-fast rule for dynamic
names, and the coverage of every template (design D2, D8; spike section 3 of the ArcadeDB plan).

No database. The queries the library renders are collected from the golden test's scenario code (every
factory output the callers use, for three ontologies), then planned through the ArcadeDB dialect.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import pytest

import test_graph_query_golden as golden
from graph.build import queries as build_queries
from graph.core import Query
from graph.core import queries as core_queries
from graph.core.errors import ConformanceError
from graph.core.queries import ScopeVia
from graph.stores._bolt.cypher import render_cypher, template_ids
from graph.stores.arcadedb import dialect as arcade
from graph.stores.arcadedb.chunking import AGGREGATE_COLUMNS
from graph.stores.arcadedb.dialect import (
    ARCADEDB_DIALECT,
    OVERRIDES,
    REQUIRED,
    dynamic_name_in_write,
    foreign_types,
    verify_coverage,
)
from graph.stores.arcadedb.names import index_names

EDGE_TYPES = ("FLAGS", "PART_OF", "REFERENCED_BY", "weird-name", "ZETA")
OWNED = ["PART_OF", "REFERENCED_BY"]
PARAMS: dict[str, Any] = {
    "project_id": "p1",
    "label": "Module",
    "key": "k",
    "props": {"spec_id": "M-1"},
    "rows": [{"label": "Module", "uuid": "u1", "props": {}}],
    "uuids": ["u1"],
    "owned_types": OWNED,
    "index": "graph_spec_node_text",
}


@pytest.fixture(scope="module")
def all_queries(tmp_path_factory: pytest.TempPathFactory) -> list[Query]:
    """Every query the library renders: the module constants and every factory output of the scenarios."""

    seen: list[Query] = []

    def collect(query: Query) -> dict[str, Any]:
        seen.append(query)
        return {}

    patch = pytest.MonkeyPatch()
    patch.setattr(golden, "entry", collect)
    try:
        golden.build_golden(tmp_path_factory.mktemp("arcade-golden"))
    finally:
        patch.undo()
    return seen


def plan(query: Query, /, **params: Any):
    return ARCADEDB_DIALECT.plan(query, {**PARAMS, **params}, EDGE_TYPES)


def texts(query: Query, /, **params: Any) -> list[str]:
    return [statement.text for statement in plan(query, **params).statements]


# Coverage


def test_the_scenarios_cover_most_of_the_catalog(all_queries: list[Query]) -> None:
    assert len(all_queries) > 400
    assert {query.template for query in all_queries} >= {
        "core.upsert_spec_node",
        "build.build_create_spec_nodes",
        "build.build_merge_spec_nodes",
        "core.move_foreign_relationships",
        "core.scope_via",
        "core.scope_sequence",
        "core.upsert_relationship",
    }


def test_every_override_is_for_a_template_the_cypher_dialect_knows() -> None:
    assert set(OVERRIDES) <= template_ids()


def test_every_required_override_is_registered_and_the_coverage_check_passes() -> None:
    assert set(OVERRIDES) >= REQUIRED
    verify_coverage()


def test_a_missing_required_override_fails_the_coverage_check_at_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(OVERRIDES, "core.move_foreign_relationships")
    with pytest.raises(ConformanceError) as raised:
        verify_coverage()
    assert raised.value.code == "arcadedb_override_missing"
    assert raised.value.fields["missing"] == 1


def test_an_override_for_an_unknown_template_fails_the_coverage_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(OVERRIDES, "core.no_such_template", OVERRIDES["core.await_indexes"])
    with pytest.raises(ConformanceError) as raised:
        verify_coverage()
    assert raised.value.fields["unknown"] == 1


def test_every_template_has_its_canonical_text_or_an_override(all_queries: list[Query]) -> None:
    """A template without override renders its canonical text (it is what the Cypher dialect does)."""

    for query in all_queries:
        planned = plan(query)
        assert planned.statements, query.name
        if query.template not in OVERRIDES:
            assert [s.text for s in planned.statements] == [render_cypher(query)], query.name


# Fail fast: no dynamic name where ArcadeDB silently drops it


@pytest.mark.parametrize(
    ("text", "dynamic"),
    [
        ("MERGE (n:SpecNode {uuid: $k}) ON CREATE SET n:$($label)", True),
        ("CREATE (n:SpecNode:$(row.label) {uuid: 1})", True),
        ("UNWIND $rows AS row CREATE (n)-[r:$(row.type)]->(m)", True),
        ("MATCH (a) FOREACH (_ IN [1] | CREATE (a)-[:$(t)]->(b))", True),
        ("MATCH (a) FOREACH (_ IN [1] | SET a:$(t))", True),
        ("MERGE (n:X {k: 1}) ON MATCH SET n:$(l)", True),
        ("MATCH (n:SpecNode {uuid: $k}) REMOVE n:$(row.remove) SET n:$(row.label), n.x = 1", False),
        ("MATCH (n:SpecNode) SET n:$($label) RETURN n", False),
        ("MATCH (n:SpecNode) WHERE n.x = 1 RETURN n", False),
        ("MERGE (n:SpecNode:Module {uuid: $k}) SET n.x = '$('", False),
        ("MATCH (a) CREATE (n:X {limit: 1, k: $(l)})", True),  # a map key named like a clause
        ("MATCH (a) WITH a.skip AS s CREATE (n:$(s))", True),  # a property named like a clause
        ("MATCH (a) SET a.merge = 'CREATE' REMOVE a:$(l)", False),  # keywords inside a string and a property
        ("MERGE (n:X {k: 'a\\'b'}) ON CREATE SET n:$(l)", True),  # an escaped quote inside a string
    ],
)
def test_the_detector_finds_a_dynamic_name_in_a_creating_clause(text: str, *, dynamic: bool) -> None:
    # the last case holds ``$(`` in a string: it is text after SET, not a dynamic name
    assert dynamic_name_in_write(text) is dynamic


@pytest.mark.parametrize(
    "template",
    [
        "core.upsert_spec_node",
        "build.build_create_spec_nodes",
        "build.build_merge_spec_nodes",
        "core.move_foreign_relationships",
    ],
)
def test_the_canonical_text_of_an_overridden_template_would_be_refused(template: str) -> None:
    canonical = next(
        value
        for module in (core_queries, build_queries)
        for value in vars(module).values()
        if isinstance(value, Query) and value.template == template
    )
    assert dynamic_name_in_write(render_cypher(canonical))


def test_no_statement_of_any_query_creates_with_a_dynamic_name(all_queries: list[Query]) -> None:
    for query in all_queries:
        for text in texts(query):
            assert not dynamic_name_in_write(text), query.name


def test_the_only_dynamic_name_left_sets_or_removes_a_label_of_a_stored_node(all_queries: list[Query]) -> None:
    holders = {query.name for query in all_queries for text in texts(query) if "$(" in text}
    assert holders == {"build_repair_set_labels"}


def test_a_literal_query_with_a_dynamic_label_is_refused() -> None:
    query = Query.literal("raw", "UNWIND $rows AS row CREATE (n:SpecNode:$(row.l) {group_id: $project_id})")
    with pytest.raises(ConformanceError) as raised:
        ARCADEDB_DIALECT.plan(query, {"project_id": "p"})
    assert raised.value.code == "arcadedb_dynamic_name"


def test_a_scoped_statement_without_the_project_id_is_refused() -> None:
    with pytest.raises(ConformanceError) as raised:
        ARCADEDB_DIALECT.plan(Query.literal("raw", "MATCH (n) RETURN n"), {"project_id": "p"})
    assert raised.value.code == "query_missing_project_id"


def test_every_planned_statement_of_a_scoped_query_binds_the_project_id(all_queries: list[Query]) -> None:
    for query in all_queries:
        if query.project_scoped:
            assert all("$project_id" in text for text in texts(query)), query.name


# spec_key: written by every statement that sets or changes spec_id of a SpecNode


def test_every_statement_that_writes_spec_id_of_a_spec_node_writes_spec_key(all_queries: list[Query]) -> None:
    writes_spec_id = re.compile(r"SET (?:[^\n]*?, )?(n\.spec_id\s*=[^=]|n\s*\+=\s*(row\.props|\$props))")
    checked = set()
    for query in all_queries:
        for text in texts(query):
            if writes_spec_id.search(text) and "SpecNode" in text:
                checked.add(query.name)
                assert "spec_key" in text, query.name
    assert checked == {
        "upsert_spec_node",
        "build_create_spec_nodes",
        "build_update_spec_nodes",
        "build_repair_set_spec_ids",
    }


def test_spec_key_is_the_group_and_the_spec_id_and_follows_a_removed_spec_id() -> None:
    (text,) = texts(core_queries.UPSERT_SPEC_NODE)
    assert (
        "n.spec_key = CASE WHEN 'spec_id' IN keys($props) THEN $project_id + ':' + $props.spec_id ELSE n.spec_key END"
        in text
    )
    (text,) = texts(build_queries.UPDATE_SPEC_NODES)
    assert "n.spec_key = CASE WHEN 'spec_id' IN keys(row.props) THEN $project_id + ':' + row.props.spec_id" in text
    (text,) = texts(build_queries.REPAIR_SET_SPEC_IDS)
    assert "n.spec_key = $project_id + ':' + row.spec_id" in text


# Static label per statement


def test_upsert_spec_node_has_the_label_in_the_merge() -> None:
    (text,) = texts(core_queries.UPSERT_SPEC_NODE, label="Capability")
    assert "MERGE (n:SpecNode:Capability {group_id: $project_id, uuid: $key})" in text
    assert "ON CREATE SET" not in text and "$($label)" not in text
    assert "NOT $label IN labels(old)" in text  # the label_mismatch pre-check stays


@pytest.mark.parametrize("label", ["Bad Label", "x}) DETACH DELETE (n", None, ""])
def test_a_label_that_is_not_a_safe_name_never_reaches_the_text(label: Any) -> None:
    with pytest.raises(ConformanceError) as raised:
        plan(core_queries.UPSERT_SPEC_NODE, label=label)
    assert raised.value.code == "cypher_name_unsafe"


def test_create_spec_nodes_runs_one_statement_per_label_with_its_rows() -> None:
    rows = [
        {"label": "Module", "uuid": "a", "props": {}},
        {"label": "Capability", "uuid": "b", "props": {}},
        {"label": "Module", "uuid": "c", "props": {}},
    ]
    planned = plan(build_queries.CREATE_SPEC_NODES, rows=rows, build_id="b1")
    assert [s.text.split("MERGE ")[1].split(" ")[0] for s in planned.statements] == [
        "(n:SpecNode:Capability",
        "(n:SpecNode:Module",
    ]
    assert [[row["uuid"] for row in s.params["rows"]] for s in planned.statements] == [["b"], ["a", "c"]]
    assert all(s.params["build_id"] == "b1" and s.params["project_id"] == "p1" for s in planned.statements)
    assert planned.reduce([[{"written": 1}], [{"written": 2}]]) == [{"written": 3}]


def test_create_spec_nodes_without_rows_writes_nothing_and_reports_zero() -> None:
    planned = plan(build_queries.CREATE_SPEC_NODES, rows=[])
    assert planned.statements == () and planned.reduce([]) == [{"written": 0}]


def test_a_row_label_that_is_not_a_safe_name_is_refused() -> None:
    with pytest.raises(ConformanceError):
        plan(build_queries.CREATE_SPEC_NODES, rows=[{"label": "A) DETACH DELETE (x", "uuid": "a", "props": {}}])


# Static relationship type per statement


def test_foreign_types_are_the_database_types_build_does_not_own_and_that_can_be_rendered() -> None:
    assert foreign_types(EDGE_TYPES, OWNED) == ["FLAGS", "ZETA"]
    assert foreign_types((), OWNED) == []


def test_merge_spec_nodes_is_aliases_then_one_move_per_foreign_type_then_guard_then_delete() -> None:
    planned = plan(build_queries.MERGE_SPEC_NODES, rows=[], build_id="b")
    assert len(planned.statements) == 5
    first, *moves, guard, last = (s.text for s in planned.statements)
    assert "SET new.spec_id_aliases = row.aliases, new.last_build_id = $build_id RETURN count(new) AS written" in first
    assert [("[r:FLAGS]" in m, "[r:ZETA]" in m) for m in moves] == [(True, False), (False, True)]
    for move, name in zip(moves, ("FLAGS", "ZETA"), strict=True):
        assert f"CREATE (new)-[moved:{name}]->(other)" in move and f"CREATE (other)-[moved:{name}]->(new)" in move
        assert f"MATCH (new)-[x:{name}]->(other)" in move and "type(" not in move
        assert "DELETE r" in move and "DETACH" not in move
    assert "NOT type(x) IN $owned_types RETURN count(x) AS left" in guard
    assert last.endswith("DETACH DELETE old") and "old <> new" in last
    nothing_left = [{"left": 0}]
    assert planned.reduce([[{"written": 4}], [], [], nothing_left, []]) == [{"written": 4}]  # the survivors found


def test_a_merge_refuses_when_a_relationship_could_not_be_carried_over() -> None:
    """A type whose name cannot be rendered has no move statement: its edges would go with the node."""

    planned = ARCADEDB_DIALECT.plan(
        build_queries.MERGE_SPEC_NODES, {**PARAMS, "build_id": "b"}, ("PART_OF", "weird-name")
    )
    assert len(planned.statements) == 3  # aliases, guard, delete: no statement for the weird type
    with pytest.raises(ConformanceError) as raised:
        planned.reduce([[{"written": 1}], [{"left": 2}], []])
    assert raised.value.code == "arcadedb_unrenderable_edge_type"


def test_a_move_refuses_when_a_relationship_could_not_be_carried_over() -> None:
    planned = ARCADEDB_DIALECT.plan(core_queries.MOVE_FOREIGN_RELATIONSHIPS, PARAMS, ("weird-name",))
    assert len(planned.statements) == 1
    with pytest.raises(ConformanceError) as raised:
        planned.reduce([[{"left": 1}]])
    assert raised.value.code == "arcadedb_unrenderable_edge_type"


def test_merge_with_no_foreign_type_is_aliases_guard_and_delete() -> None:
    planned = ARCADEDB_DIALECT.plan(build_queries.MERGE_SPEC_NODES, {**PARAMS, "build_id": "b"}, ("PART_OF",))
    assert len(planned.statements) == 3


def test_move_foreign_relationships_is_one_statement_per_foreign_type_summed_then_a_guard() -> None:
    planned = plan(core_queries.MOVE_FOREIGN_RELATIONSHIPS, from_key="a", from_label="X", to_key="b", to_label="Y")
    assert len(planned.statements) == 3
    assert "MATCH (old)-[r:FLAGS]-(other)" in planned.statements[0].text
    assert "RETURN count(r) AS moved" in planned.statements[1].text
    assert "RETURN count(x) AS left" in planned.statements[2].text
    assert planned.reduce([[{"moved": 2}], [{"moved": 1}], [{"left": 0}]]) == [{"moved": 3}]
    assert planned.reduce([[], [{"moved": 1}], []]) == [{"moved": 1}]


def test_move_with_nothing_to_carry_over_reports_zero() -> None:
    planned = ARCADEDB_DIALECT.plan(core_queries.MOVE_FOREIGN_RELATIONSHIPS, PARAMS, ())
    assert len(planned.statements) == 1 and planned.reduce([[{"left": 0}]]) == [{"moved": 0}]


@pytest.mark.parametrize("query", [core_queries.MOVE_FOREIGN_RELATIONSHIPS, build_queries.MERGE_SPEC_NODES])
def test_a_merge_or_move_cannot_be_planned_without_the_database_relationship_types(query: Query) -> None:
    with pytest.raises(ConformanceError) as raised:
        ARCADEDB_DIALECT.plan(query, PARAMS)
    assert raised.value.code == "arcadedb_edge_types_missing"


# Other overrides


def test_scope_via_keeps_its_slots_and_rewrites_the_all_over_the_path_nodes() -> None:
    query = core_queries.scope_via(("PART_OF", "REALIZED_BY"), 1, 3)
    assert isinstance(query.args, ScopeVia)
    (text,) = texts(query)
    assert "all(b IN nodes(p) WHERE b = n OR b = root OR none(label IN labels(b) WHERE label IN $roots))" in text
    assert "nodes(p)[1..]" not in text
    assert "[r:PART_OF|REALIZED_BY WHERE r.group_id = $project_id]" in text and "{1,3}" in text


def test_the_vector_index_page_has_no_cypher_prefix_and_names_the_server_index() -> None:
    planned = plan(core_queries.SEARCH_NODES_VECTOR, index="graph_spec_node_embedding")
    assert not planned.statements[0].text.startswith("CYPHER")
    assert "CALL db.index.vector.queryNodes($index, $k, $vector)" in planned.statements[0].text
    assert planned.statements[0].params["index"] == "SpecNode[embedding]"


def test_the_exact_vector_search_is_on_the_same_scale_as_the_index() -> None:
    (text,) = texts(core_queries.SEARCH_NODES_VECTOR_EXACT)
    assert "(1 + vector.similarity.cosine(n.embedding, $vector)) / 2 AS score" in text


@pytest.mark.parametrize(
    ("query", "library", "server"),
    [
        (core_queries.SEARCH_NODES_FULLTEXT, "graph_spec_node_text", "SpecNode[name,statement_text]"),
        (core_queries.SEARCH_FRAGMENTS, "graph_fragment_text", "Fragment[text]"),
        (core_queries.SEARCH_NODES_VECTOR, "graph_spec_node_name_embedding", "SpecNode[name_embedding]"),
    ],
)
def test_the_library_index_names_map_to_the_names_the_server_fixes(query: Query, library: str, server: str) -> None:
    assert plan(query, index=library).statements[0].params["index"] == server
    assert index_names()[library] == server


def test_a_node_search_text_becomes_one_group_per_field_of_the_index() -> None:
    planned = plan(core_queries.SEARCH_NODES_FULLTEXT, index="graph_spec_node_text", query="checkout service")
    assert planned.statements[0].params["query"] == "name:(checkout service) statement_text:(checkout service)"


def test_a_search_text_over_an_index_of_one_field_is_left_as_it_is() -> None:
    planned = plan(core_queries.SEARCH_FRAGMENTS, index="graph_fragment_text", query="audit logging")
    assert planned.statements[0].params["query"] == "audit logging"


@pytest.mark.parametrize("query", ["", "  ", None])
def test_a_blank_or_missing_search_text_is_not_grouped(query: Any) -> None:
    planned = plan(core_queries.SEARCH_NODES_FULLTEXT, index="graph_spec_node_text", query=query)
    assert planned.statements[0].params["query"] == query


def test_a_text_the_server_cannot_parse_stays_unparseable_inside_the_groups() -> None:
    planned = plan(core_queries.SEARCH_NODES_FULLTEXT, index="graph_spec_node_text", query="payment AND (")
    assert planned.statements[0].params["query"].startswith("name:(payment AND ()")


def test_an_index_name_of_another_owner_is_passed_through() -> None:
    assert plan(core_queries.SEARCH_NODES_FULLTEXT, index="custom").statements[0].params["index"] == "custom"


def test_there_is_nothing_to_wait_for_after_a_sql_index_create() -> None:
    (text,) = texts(core_queries.AWAIT_INDEXES)
    assert "awaitIndexes" not in text


def test_verify_reads_nodes_by_uuid_and_edge_ends_by_the_same_ids() -> None:
    (nodes,) = texts(build_queries.VERIFY_SPEC_NODES)
    assert "coalesce(n.uuid, elementId(n)) AS id" in nodes
    (edges,) = texts(build_queries.VERIFY_DOMAIN_EDGES)
    # an edge moves with its end: addressed by uuid, else by type and edge key, else by element id
    assert "coalesce(r.uuid, type(r) + '|' + r.edge_key, elementId(r)) AS id" in edges
    assert (
        "THEN coalesce(startNode(r).uuid, elementId(startNode(r))) ELSE elementId(startNode(r)) END AS source" in edges
    )
    assert "THEN coalesce(endNode(r).uuid, elementId(endNode(r))) ELSE elementId(endNode(r)) END AS target" in edges


@pytest.mark.parametrize(
    ("query", "key"),
    [
        (build_queries.REPAIR_DELETE_NODES, "= id"),
        (build_queries.REPAIR_SET_LABELS, "= row.id"),
        (build_queries.REPAIR_NODE_EVIDENCE, "= row.id"),
        (build_queries.REPAIR_SET_SPEC_IDS, "= row.id"),
    ],
)
def test_repairs_address_a_node_by_its_uuid_and_a_node_without_one_by_element_id(query: Query, key: str) -> None:
    (text,) = texts(query)
    assert "MATCH (n:SpecNode {group_id: $project_id})" in text
    assert f"coalesce(n.uuid, elementId(n)) {key}" in text
    assert text.count("elementId(n)") == 1  # only as the fallback


def test_edge_repairs_address_an_edge_by_uuid_then_type_and_edge_key_never_by_a_stale_element_id_first() -> None:
    expression = "coalesce(r.uuid, type(r) + '|' + r.edge_key, elementId(r))"
    for query, key in ((build_queries.REPAIR_DELETE_EDGES, "id"), (build_queries.REPAIR_EDGE_EVIDENCE, "row.id")):
        (text,) = texts(query)
        assert f"{expression} = {key}" in text


def test_a_replacement_that_no_longer_matches_the_canonical_text_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arcade, "_replacements", lambda _template: (("text that is not in the query", "x"),))
    with pytest.raises(ConformanceError) as raised:
        arcade._derived()(core_queries.SEARCH_NODES_VECTOR_EXACT, PARAMS, None)
    assert raised.value.code == "arcadedb_override_stale"


def test_every_derived_override_is_its_canonical_text_with_the_listed_replacements(all_queries: list[Query]) -> None:
    """The replacements file is the whole difference: nothing else of the canonical statement is changed."""

    table = json.loads((arcade.OVERRIDE_DIRECTORY / "derived.json").read_text(encoding="utf-8"))
    assert set(table) <= set(OVERRIDES)
    for query in all_queries:
        pairs = table.get(query.template)
        if not pairs:
            continue
        expected = render_cypher(query)
        for old, new in pairs:
            assert expected.count(old) == 1, (query.name, old)
            expected = expected.replace(old, new)
        assert texts(query) == [expected], query.name


def test_the_override_files_hold_no_unfilled_token(all_queries: list[Query]) -> None:
    for query in all_queries:
        for text in texts(query):
            assert re.search(r"__[A-Z][A-Z_]*__", text) is None, query.name


def test_the_dialect_is_stateless_and_returns_equal_plans_for_equal_input() -> None:
    first = plan(core_queries.UPSERT_SPEC_NODE)
    again = plan(core_queries.UPSERT_SPEC_NODE)
    assert first.statements == again.statements


def test_params_are_passed_unchanged_to_the_statements() -> None:
    params: Mapping[str, Any] = plan(core_queries.UPSERT_SPEC_NODE).statements[0].params
    assert params["key"] == "k" and params["props"] == {"spec_id": "M-1"}


# Row chunking over every template that takes ``rows`` (graph.stores.arcadedb.chunking)

PAD = "x" * 300_000  # 60 rows are about 18 MB: well over one message
_RETURN = re.compile(r"\bRETURN\b", re.IGNORECASE)
_ALIAS = re.compile(r"\bAS\s+(\w+)", re.IGNORECASE)


def _rows(count: int) -> list[dict[str, Any]]:
    return [{"label": "Module", "uuid": f"u{i}", "props": {"pad": PAD}, "pad": PAD} for i in range(count)]


def _templates_with_rows(queries: list[Query]) -> list[Query]:
    found = {}
    for query in queries:
        statements = plan(query, rows=_rows(1)).statements
        if any("rows" in statement.params and "$rows" in statement.text for statement in statements):
            found.setdefault(query.name, query)
    return list(found.values())


def test_every_template_with_rows_is_cut_into_chunks_that_hold_every_row_once_in_order(
    all_queries: list[Query],
) -> None:
    templates = _templates_with_rows(all_queries)
    assert len({q.template for q in templates}) >= 18  # persist, infer, repair: a new template is covered as well
    rows = _rows(60)
    for query in templates:
        small, big = plan(query, rows=_rows(1)).statements, plan(query, rows=rows).statements
        assert len(big) > len(small), query.name
        by_text: dict[str, list[Any]] = {}
        for statement in big:
            if "$rows" in statement.text:
                by_text.setdefault(statement.text, []).extend(statement.params["rows"])
        assert by_text and all(grouped == rows for grouped in by_text.values()), query.name


def test_the_result_columns_of_every_template_with_rows_are_the_ones_chunking_combines(
    all_queries: list[Query],
) -> None:
    """A template that returns another column must be reviewed: ``combine`` adds counts and concatenates lists."""

    unknown = {}
    for query in _templates_with_rows(all_queries):
        for statement in plan(query, rows=_rows(1)).statements:
            if "$rows" not in statement.text:
                continue
            tail = (
                statement.text[list(_RETURN.finditer(statement.text))[-1].end() :]
                if _RETURN.search(statement.text)
                else ""
            )
            columns = set(_ALIAS.findall(tail))
            if tail.strip() and not columns:
                columns = {tail.strip()}  # a RETURN without an alias
            if not columns <= AGGREGATE_COLUMNS:
                unknown[query.name] = sorted(columns - AGGREGATE_COLUMNS)
    assert unknown == {}
