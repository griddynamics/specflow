"""Self-test for `derive_schema`, the build step that drafts a schema file from a database."""

from __future__ import annotations

from typing import Any

import neo4j
import pytest
import yaml

from cypher_checks import derive_schema
from cypher_checks.schema import GraphSchema, parse_schema

#: Rows in the shapes the introspection procedures and `SHOW` commands return.
_NODES: list[dict[str, Any]] = [
    {"nodeLabels": ["Person"], "propertyName": "name", "propertyTypes": ["String"]},
    {"nodeLabels": ["Person"], "propertyName": "born", "propertyTypes": ["Long"]},
    {"nodeLabels": ["Movie"], "propertyName": "title", "propertyTypes": ["String"]},
    {"nodeLabels": ["Genre"], "propertyName": "name", "propertyTypes": ["String"]},
    {"nodeLabels": ["Tag"], "propertyName": None, "propertyTypes": None},
]
_RELS: list[dict[str, Any]] = [
    {"relType": ":`ACTED_IN`", "propertyName": "roles", "propertyTypes": ["StringArray"]},
    {"relType": ":`IN_GENRE`", "propertyName": None, "propertyTypes": None},
]
_ENDPOINTS: list[dict[str, Any]] = [
    {"start": ["Person"], "type": "ACTED_IN", "end": ["Movie"]},
    {"start": ["Movie"], "type": "IN_GENRE", "end": ["Genre"]},
]
_UNIQUE: list[dict[str, Any]] = [{"labelsOrTypes": ["Genre"], "properties": ["name"]}]
_RANGE: list[dict[str, Any]] = [
    {"labelsOrTypes": ["Movie"], "properties": ["title"]},
    {"labelsOrTypes": ["Person"], "properties": ["name", "born"]},  # composite: left out
]
_VECTOR: list[dict[str, Any]] = [{"labelsOrTypes": ["Movie"], "properties": ["plot_embedding"]}]


def _derived() -> dict[str, Any]:
    return derive_schema.build_schema(
        _NODES,
        _RELS,
        _ENDPOINTS,
        unique_rows=_UNIQUE,
        range_rows=_RANGE,
        vector_rows=_VECTOR,
    )


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["String"], "string"),
        (["Long"], "integer"),
        (["Double"], "float"),
        (["Boolean"], "boolean"),
        (["DateTime"], "timestamp"),
        (["StringArray"], "list<string>"),
        (["LongArray"], "list<integer>"),
        (["INTEGER NOT NULL"], "integer"),
        (["LIST<STRING NOT NULL> NOT NULL"], "list<string>"),
        (["ZONED DATETIME NOT NULL"], "timestamp"),
        (["FLOAT NOT NULL"], "float"),
        (["Point"], "string"),
        ([], "string"),
    ],
)
def test_neo4j_types_map_to_schema_types(names: list[str], expected: str) -> None:
    assert derive_schema.neo4j_type(names) == expected


def test_the_draft_is_a_valid_schema_file() -> None:
    schema = parse_schema(yaml.safe_load(yaml.safe_dump(_derived(), sort_keys=False)))
    assert set(schema.nodes) == {"Person", "Movie", "Genre", "Tag"}
    assert schema.relationships["ACTED_IN"].start_labels == ("Person",)
    assert schema.relationships["ACTED_IN"].end_labels == ("Movie",)
    assert schema.relationships["ACTED_IN"].properties == ("roles",)
    assert schema.unique == (("Genre", "name"),)
    assert schema.indexes == (("Movie", "title"),)
    assert schema.vector_properties_by_label()["Movie"] == {"plot_embedding"}
    assert schema.node_property_names("Tag") == set()


def test_the_draft_carries_no_policy() -> None:
    assert "policy" not in _derived()


@pytest.mark.integration
def test_fetch_reads_the_seeded_example_graph(
    neo4j_driver: neo4j.Driver, schema: GraphSchema
) -> None:
    derived = parse_schema(derive_schema.fetch(neo4j_driver))
    assert set(schema.nodes) <= set(derived.nodes)
    assert set(schema.relationships) <= set(derived.relationships)
    assert set(schema.unique) <= set(derived.unique)
    assert set(schema.range_indexed_properties()) <= set(derived.indexes)
