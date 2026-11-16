"""Live: the built graph of the shop gap corpus loads into the selected graph store and reads back through the core
reader with the nodes, relations and fragments the gap requirements name."""

from __future__ import annotations

from gap_acceptance_support import Planted, write_gap_corpus_graph
from graph.core import load_ontology
from graph_probes import requires_live_graph
from graph_support import GraphProject

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()


async def test_the_gap_corpus_graph_loads_and_reads_back(graph_project: GraphProject) -> None:
    graph = await write_gap_corpus_graph(graph_project, graph_project.project_id)
    reader = graph_project.reader(ontology=load_ontology())
    project_id = graph_project.project_id

    nodes = {node.spec_id: node for node in await reader.nodes(project_id)}
    assert set(nodes) == {node.spec_id for node in graph.nodes}
    for spec_id in (
        Planted.STATED_QUESTION,
        Planted.PLACE_ORDER,
        Planted.REVIEW_LIMIT_ORDERS,
        Planted.REVIEW_LIMIT_PAYMENTS,
        Planted.INTERFACE_WITHOUT_PROVIDER,
        Planted.VAGUE_REQUIREMENT,
        Planted.OWNERLESS_CSV_EXPORT,
        Planted.ASSUMPTION,
        Planted.RELEASE_PLAN_RULE,
    ):
        assert nodes[spec_id].label == graph.node(spec_id).label
    assert nodes[Planted.ASSUMPTION].attributes.get("is_assumption") == "yes"

    edges = {(edge.source, edge.relation, edge.target) for edge in await reader.edges(project_id)}
    assert edges == {(edge.source, edge.relation, edge.target) for edge in graph.reader._fixture.edges}

    fragments = {fragment.fragment_id: fragment for fragment in await reader.fragments(project_id)}
    assert set(fragments) == {fragment.fragment_id for fragment in graph.fragments}
    question = graph.fragment("orders-spec.md", "Place order")
    assert Planted.STATED_QUESTION_LINE in fragments[question.fragment_id].text
