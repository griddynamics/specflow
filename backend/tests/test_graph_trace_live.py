"""Live graph-store tests of ``GraphReader.trace``: the worked example of trace.md on the store the switch
selects (Neo4j, PostgreSQL or ArcadeDB), through the real ``core.read_trace_hop`` of that store."""

from __future__ import annotations

import pytest

from graph.core import GraphReader
from graph_probes import requires_live_graph
from graph_support import GraphProject, shipped_ontology
from graph_trace_support import shop_graph, write_trace_graph
from test_graph_trace import AFFECTS_DEPTH_2, BASED_ON_DEPTH_3, OWNER, SEEDS, rows

# The graph fixtures (graph_project) live in graph_support.
pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()


async def shop_reader(graph_project: GraphProject) -> GraphReader:
    ontology = shipped_ontology()
    graph = shop_graph()
    graph.alias("FEAT::CHECKOUT", "FEAT::PAY_AND_GO")
    graph.edge(
        "DEPENDS_ON", "FEAT::PLACE_ORDER", "FEAT::CARD_PAYMENT", basis="inferred", confidence=0.8, inference_rule="R-1"
    )
    await write_trace_graph(graph_project.writer(ontology=ontology), graph_project.project_id, graph)
    return graph_project.reader(ontology=ontology)


async def test_the_worked_example_on_the_live_store(graph_project: GraphProject) -> None:
    reader = await shop_reader(graph_project)
    project_id = graph_project.project_id
    based_on = await reader.trace(project_id, SEEDS, direction="based_on", depth=3)
    affects = await reader.trace(project_id, SEEDS, direction="affects", depth=2)
    assert rows(based_on) == BASED_ON_DEPTH_3
    assert rows(affects) == AFFECTS_DEPTH_2
    assert [(item.spec_id, item.name) for item in based_on.context] == [(OWNER, "Payments service")]
    assert based_on.sources == tuple(SEEDS) and not based_on.truncated


async def test_inferred_edges_aliases_and_unknown_ids_on_the_live_store(graph_project: GraphProject) -> None:
    reader = await shop_reader(graph_project)
    project_id = graph_project.project_id
    seeds = ["FEAT::CARD_PAYMENT"]
    trusted = await reader.trace(project_id, seeds, direction="affects", depth=1, include_owner=False)
    every = await reader.trace(project_id, seeds, direction="affects", depth=1, include_owner=False, basis="any")
    assert "FEAT::PLACE_ORDER" not in {node.spec_id for node in trusted.nodes}
    inferred = next(node for node in every.nodes if node.spec_id == "FEAT::PLACE_ORDER")
    assert (inferred.basis, inferred.confidence, inferred.rule, inferred.step) == ("inferred", 0.8, "R-1", "backward")
    aliased = await reader.trace(project_id, ["FEAT::PAY_AND_GO", "FEAT::NOPE"], direction="based_on", depth=1)
    assert (aliased.sources, aliased.unknown) == (("FEAT::CHECKOUT",), ("FEAT::NOPE",))


@pytest.mark.parametrize("limit", [1, 3])
async def test_limit_and_truncated_on_the_live_store(graph_project: GraphProject, limit: int) -> None:
    reader = await shop_reader(graph_project)
    result = await reader.trace(graph_project.project_id, SEEDS, direction="affects", depth=2, limit=limit)
    assert rows(result) == AFFECTS_DEPTH_2[:limit] and result.truncated
