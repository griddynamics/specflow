"""Live graph-store tests of the plan schema setup."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import graph.plan
from graph.core.reader import GraphReader
from graph.plan.schema import ensure_plan_schema
from graph_plan_support import plan_ontology, shop_graph, write_spec_graph
from graph_probes import requires_live_graph
from graph_support import GraphProject, live_llm_config

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

PLAN_DDL: Final = Path(graph.plan.__file__).resolve().parent / "ddl" / "plan_schema.cypher"
_STATEMENT: Final = re.compile(
    r"CREATE[ ](?:CONSTRAINT|INDEX) (?P<name>\w+) IF NOT EXISTS "
    r"FOR (?:\(\w+:(?P<label>\w+)\)|\(\)-\[\w+:(?P<relation>\w+)\]-\(\)) "
    r"(?:REQUIRE|ON) \((?P<properties>[^)]*)\)"
)


def _plan_objects() -> dict[str, tuple[str, str, tuple[str, ...]]]:
    """Name -> (entity, label or relationship type, properties) of every object ``plan_schema.cypher`` declares."""

    objects: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for found in _STATEMENT.finditer(PLAN_DDL.read_text(encoding="utf-8")):
        properties = tuple(item.strip().split(".", 1)[1] for item in found["properties"].split(","))
        if found["label"]:
            objects[found["name"]] = ("node", found["label"], properties)
        else:
            objects[found["name"]] = ("relationship", found["relation"], properties)
    return objects


async def test_plan_schema_entry_point_is_idempotent(graph_project: GraphProject) -> None:
    ontology = plan_ontology()

    await ensure_plan_schema(graph_project.store, ontology, live_llm_config())
    assert await ensure_plan_schema(graph_project.store, ontology, live_llm_config()) == ()


# plan_schema.cypher declares the plan objects of the Cypher stores; PostgreSQL keeps plan nodes and
# relationships in its generic tables, whose keys its own schema suite checks.


@requires_live_graph("neo4j")
async def test_neo4j_holds_every_plan_object_by_name(graph_project: GraphProject) -> None:
    await ensure_plan_schema(graph_project.store, plan_ontology(), live_llm_config())
    expected = _plan_objects()
    assert len(expected) == 16, "plan_schema.cypher was not parsed"
    names = {item.name for item in await graph_project.probe.schema_objects()}
    assert set(expected) <= names, sorted(set(expected) - names)


@requires_live_graph("arcadedb")
async def test_arcadedb_holds_every_plan_object_by_shape(graph_project: GraphProject) -> None:
    """ArcadeDB names its indexes itself: compare by what each object covers."""

    await ensure_plan_schema(graph_project.store, plan_ontology(), live_llm_config())
    expected = _plan_objects()
    assert len(expected) == 16, "plan_schema.cypher was not parsed"
    shapes = {(item.entity, item.label, item.properties) for item in await graph_project.probe.schema_objects()}
    missing = {name for name, shape in expected.items() if shape not in shapes}
    assert not missing, sorted(missing)


async def test_shop_fixture_written_to_the_store_reads_back_like_the_fake_reader(graph_project: GraphProject) -> None:
    project_id = graph_project.new_project()
    ontology = plan_ontology()
    fixture = shop_graph()

    await write_spec_graph(graph_project, project_id, fixture, ontology)
    reader = GraphReader(graph_project.store, ontology)

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
