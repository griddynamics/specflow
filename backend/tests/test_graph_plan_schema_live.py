"""Live Neo4j tests of the plan schema setup."""

from __future__ import annotations

from graph.core import ensure_schema, load_ontology
from graph.core.reader import GraphReader
from graph.plan.schema import ensure_plan_schema
from graph_plan_support import plan_ontology, shop_graph, write_spec_graph
from graph_support import Neo4jProject, live_llm_config
from specflow_live import requires_live

pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")


async def test_plan_schema_entry_point_is_idempotent_and_default_schema_covers_it(
    neo4j_project: Neo4jProject,
) -> None:
    ontology = load_ontology()
    created = await ensure_plan_schema(neo4j_project.driver, neo4j_project.database)

    assert created == ()
    assert await ensure_plan_schema(neo4j_project.driver, neo4j_project.database) == ()
    assert (
        await ensure_schema(
            neo4j_project.driver,
            ontology,
            live_llm_config(),
            neo4j_project.database,
        )
        == ()
    )


async def test_shop_fixture_written_to_neo4j_reads_back_like_the_fake_reader(
    neo4j_project: Neo4jProject,
) -> None:
    project_id = neo4j_project.new_project()
    ontology = plan_ontology()
    fixture = shop_graph()

    await write_spec_graph(neo4j_project, project_id, fixture, ontology)
    reader = GraphReader(neo4j_project.driver, ontology, database=neo4j_project.database)

    written_nodes = await reader.nodes(project_id)
    written_edges = await reader.edges(project_id)
    written_documents = await reader.documents(project_id)
    written_fragments = await reader.fragments(project_id)

    expected_nodes = await fixture.nodes(project_id)
    assert [node.spec_id for node in written_nodes] == [node.spec_id for node in expected_nodes]
    assert [len(node.provenance) for node in written_nodes] == [len(node.provenance) for node in expected_nodes]
    assert [(edge.source, edge.relation, edge.target) for edge in written_edges] == [
        (edge.source, edge.relation, edge.target) for edge in await fixture.edges(project_id)
    ]
    assert [document.document_id for document in written_documents] == [
        document.document_id for document in await fixture.documents(project_id)
    ]
    assert [fragment.fragment_id for fragment in written_fragments] == [
        fragment.fragment_id for fragment in await fixture.fragments(project_id)
    ]
