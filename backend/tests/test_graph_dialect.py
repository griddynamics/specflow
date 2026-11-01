"""Offline tests of query dialects and execution plans (design D2): the plan and its reducer, the Cypher
dialect's guard and template coverage, and the neutral ``Query`` identity. No database."""

from __future__ import annotations

from typing import Any, Final

import pytest

import graph.build.queries as build_queries
import graph.core.queries as core_queries
from graph.core.dialect import (
    ExecutionPlan,
    PlannedStatement,
    identity_reducer,
    run_plan,
)
from graph.core.errors import ConformanceError
from graph.core.queries import NodeShape, Query, scope_sequence
from graph.core.query_files import BUILD_QUERY_DIRECTORY, CORE_QUERY_DIRECTORY
from graph.stores._bolt.cypher import CYPHER_DIALECT, binds_project_id, render_cypher

FRAGMENT_FILES: Final = frozenset(
    {
        "match_node",
        "match_node_checked",
        "infer_endpoint_via",
        "infer_endpoint_mentions",
    }
)


def test_a_single_statement_plan_returns_the_rows_as_they_are() -> None:
    plan = ExecutionPlan.single("RETURN 1", {"a": 1})
    assert plan.statements == (PlannedStatement("RETURN 1", {"a": 1}),)
    assert plan.reduce([[{"x": 1}]]) == [{"x": 1}]
    assert identity_reducer([]) == []


async def test_a_plan_runs_its_statements_in_order_and_reduces_their_results() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def run(text: str, params: Any) -> list[dict[str, Any]]:
        seen.append((text, dict(params)))
        return [{"written": len(seen)}]

    def total(results: Any) -> list[dict[str, Any]]:
        return [{"written": sum(rows[0]["written"] for rows in results)}]

    plan = ExecutionPlan(
        (PlannedStatement("first", {"a": 1}), PlannedStatement("second", {"b": 2})),
        total,
    )
    assert await run_plan(plan, run) == [{"written": 3}]
    assert seen == [("first", {"a": 1}), ("second", {"b": 2})]


def test_the_cypher_dialect_plans_one_statement_with_the_given_parameters() -> None:
    plan = CYPHER_DIALECT.plan(core_queries.READ_NODES, {"project_id": "p", "limit": 3})
    assert len(plan.statements) == 1
    assert plan.statements[0].text == render_cypher(core_queries.READ_NODES)
    assert plan.statements[0].params == {"project_id": "p", "limit": 3}


def test_the_guard_refuses_a_scoped_statement_that_does_not_bind_the_project_id() -> None:
    unbound = Query.literal("unbound", "MATCH (n:SpecNode) RETURN n.uuid AS uuid")
    lookalike = Query.literal("lookalike", "MATCH (n {group_id: $project_ids}) RETURN n")
    for query in (unbound, lookalike):
        with pytest.raises(ConformanceError) as raised:
            CYPHER_DIALECT.plan(query, {})
        assert raised.value.code == "query_missing_project_id"
        assert raised.value.fields["query"] == query.name


def test_an_unscoped_statement_needs_no_project_id() -> None:
    plan = CYPHER_DIALECT.plan(core_queries.PING, {})
    assert not binds_project_id(plan.statements[0].text)


@pytest.mark.parametrize("template", ["core.nothing_like_this", "build.", "other.ping", "ping"])
def test_an_unknown_template_is_refused(template: str) -> None:
    with pytest.raises(ConformanceError) as raised:
        CYPHER_DIALECT.plan(Query("x", template, project_scoped=False), {})
    assert raised.value.code == "query_template_unknown"


def test_a_hand_built_query_cannot_render_an_unsafe_name() -> None:
    unsafe = Query("x", "core.delete_relationship", core_queries.Relation("X) DETACH DELETE (n"))
    with pytest.raises(ConformanceError) as raised:
        CYPHER_DIALECT.plan(unsafe, {})
    assert raised.value.code == "cypher_name_unsafe"


def test_a_template_with_run_time_parts_refuses_missing_or_wrong_arguments() -> None:
    with pytest.raises(ConformanceError) as raised:
        CYPHER_DIALECT.plan(Query("x", "core.delete_relationship"), {})
    with pytest.raises(ConformanceError):
        CYPHER_DIALECT.plan(Query("x", "core.delete_relationship", core_queries.LabelKey("A", "b")), {})
    assert raised.value.code == "query_arguments_invalid"


def test_query_identity_is_name_and_arguments_never_text() -> None:
    directed = scope_sequence(("A", "B"), ())
    either = scope_sequence(("A", "B"), ("A",))
    assert directed.name == either.name
    assert directed.identity != either.identity
    assert scope_sequence(("A", "B"), ()).identity == directed.identity
    assert render_cypher(directed) != render_cypher(either)


def test_factories_validate_names_before_building_arguments() -> None:
    with pytest.raises(ConformanceError):
        core_queries.upsert_relationship("OK", NodeShape("Bad label", "uuid"), NodeShape("SpecNode", "uuid"))


def test_every_canonical_template_file_is_rendered_by_some_query() -> None:
    """Every ``.cypher`` file that is a template (not a fragment of another) is reached by a catalog query."""

    queries = [
        value for module in (core_queries, build_queries) for value in vars(module).values() if isinstance(value, Query)
    ]
    queries += [
        core_queries.scope_via(("A",), 1, 2),
        core_queries.scope_sequence(("A", "B"), ("A",)),
        core_queries.upsert_relationship("R", NodeShape("SpecNode", "uuid", check_label=True), NodeShape("D", "uuid")),
        core_queries.delete_relationship("R"),
        core_queries.delete_keyed_node("Document", "document_id"),
        core_queries.keyed_node_relationship_types("Document", "document_id"),
        core_queries.upsert_foreign_node("Finding"),
        build_queries.read_current_edges("R"),
        build_queries.upsert_edges("R"),
        build_queries.update_edges("R"),
        build_queries.delete_edges("R"),
        build_queries.infer_endpoint_subjects(("R",), (), mentions=True),
        build_queries.infer_realization_subjects(
            "M", "C", member_symmetric=False, container_symmetric=True, max_hops=2
        ),
        build_queries.infer_similar_targets("R", symmetric=True),
        build_queries.infer_exclusive_holders("R"),
        build_queries.infer_write_edges("R"),
        build_queries.infer_delete_edges("R"),
    ]
    reached = set()
    for query in queries:
        assert render_cypher(query)
        reached.add(query.template)
    expected = {
        f"{catalog}.{path.stem}"
        for catalog, directory in (
            ("core", CORE_QUERY_DIRECTORY),
            ("build", BUILD_QUERY_DIRECTORY),
        )
        for path in directory.glob("*.cypher")
        if path.stem not in FRAGMENT_FILES
    }
    assert expected - reached == set()
