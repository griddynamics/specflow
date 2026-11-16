"""Live graph-store tests of the gap schema setup: idempotent on every store, every object present on the Cypher
stores (FR-GONT-05)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import graph.gap
from graph.core import load_ontology
from graph.gap import ensure_gap_schema
from graph_probes import requires_live_graph
from graph_support import GraphProject, live_llm_config

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

GAP_DDL_FILE: Final = Path(graph.gap.__file__).resolve().parent / "ddl" / "gap_schema.cypher"
_STATEMENT: Final = re.compile(
    r"CREATE[ ](?:CONSTRAINT|INDEX) (?P<name>\w+) IF NOT EXISTS "
    r"FOR (?:\(\w+:(?P<label>\w+)\)|\(\)-\[\w+:(?P<relation>\w+)\]-\(\)) "
    r"(?:REQUIRE|ON) \((?P<properties>[^)]*)\)"
)
GAP_OBJECTS: Final = 20


def _gap_objects() -> dict[str, tuple[str, str, tuple[str, ...]]]:
    """Name -> (entity, label or relationship type, properties) of every object ``gap_schema.cypher`` declares."""

    objects: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for found in _STATEMENT.finditer(GAP_DDL_FILE.read_text(encoding="utf-8")):
        properties = tuple(item.strip().split(".", 1)[1] for item in found["properties"].split(","))
        if found["label"]:
            objects[found["name"]] = ("node", found["label"], properties)
        else:
            objects[found["name"]] = ("relationship", found["relation"], properties)
    return objects


async def test_fr_gont_05_ensure_gap_schema_is_idempotent(
    graph_project: GraphProject,
) -> None:
    ontology = load_ontology()

    await ensure_gap_schema(graph_project.store, ontology, live_llm_config())
    assert await ensure_gap_schema(graph_project.store, ontology, live_llm_config()) == ()


# gap_schema.cypher declares the gap objects of the Cypher stores; PostgreSQL keeps gap nodes and relationships in
# its generic tables, whose keys its own schema suite checks.


@requires_live_graph("neo4j")
async def test_neo4j_holds_every_gap_object_by_name(
    graph_project: GraphProject,
) -> None:
    await ensure_gap_schema(graph_project.store, load_ontology(), live_llm_config())
    expected = _gap_objects()
    assert len(expected) == GAP_OBJECTS, "gap_schema.cypher was not parsed"
    names = {item.name for item in await graph_project.probe.schema_objects()}
    assert set(expected) <= names, sorted(set(expected) - names)


@requires_live_graph("arcadedb")
async def test_arcadedb_holds_every_gap_object_by_shape(
    graph_project: GraphProject,
) -> None:
    """ArcadeDB names its indexes itself: compare by what each object covers."""

    await ensure_gap_schema(graph_project.store, load_ontology(), live_llm_config())
    expected = _gap_objects()
    assert len(expected) == GAP_OBJECTS, "gap_schema.cypher was not parsed"
    shapes = {(item.entity, item.label, item.properties) for item in await graph_project.probe.schema_objects()}
    missing = {name for name, shape in expected.items() if shape not in shapes}
    assert not missing, sorted(missing)
