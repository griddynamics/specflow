"""The schema fixture graph: one node per declared label, one edge per declared triple.

Every property gets a typed dummy value. The live validators only mean something when the
data is exactly the declared schema, so the graph is built from the schema file alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import neo4j

from cypher_checks.schema import GraphSchema

__all__ = [
    "FixtureGraphError",
    "NodeFixture",
    "RelFixture",
    "apply_constraints",
    "assert_identifier",
    "build_node_fixtures",
    "build_rel_fixtures",
    "dummy_value",
    "seed",
]

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LIST_TYPE = re.compile(r"^list\s*<\s*(?P<item>.+?)\s*>$")


class FixtureGraphError(Exception):
    """A label, type or property is not a safe Cypher identifier: no graph is built."""


def assert_identifier(name: str, kind: str) -> None:
    """`name` is a safe, literal Cypher label/type/property identifier -- every name this
    module writes into an f-string comes from the schema file, never user input, but an
    f-string is still only as safe as its value."""
    if not _IDENTIFIER.match(name):
        message = f"{kind} {name!r} is not a safe Cypher identifier"
        raise FixtureGraphError(message)


#: The first marker the type text contains picks the dummy, so order matters.
_DUMMY_BY_TYPE_MARKER: tuple[tuple[str, Callable[[], Any]], ...] = (
    ("json", lambda: "{}"),
    ("timestamp", lambda: "2024-01-01T00:00:00Z"),
    ("datetime", lambda: "2024-01-01T00:00:00Z"),
    ("bool", lambda: True),
    ("float", lambda: 1.0),
    ("double", lambda: 1.0),
    ("int", lambda: 1),
)


def dummy_value(name: str, type_text: str = "", enum: tuple[str, ...] | None = None) -> Any:
    """A typed dummy for a declared property, from its `type` and `enum` alone, never its name.

    Relationship properties carry no type and get the generic string: CyVer only checks
    `IS NOT NULL`. Deterministic and small."""
    t = type_text.strip().lower()
    list_item = _LIST_TYPE.match(t)
    if enum:
        return enum[0]
    if list_item is not None:
        return [dummy_value(name, list_item["item"])]
    if "list of" in t:
        return [dummy_value(name, t.split("list of", 1)[1])]
    if t == "vector":
        return [1.0]
    if t == "id":
        return f"{name}-fixture"
    return next((sample() for marker, sample in _DUMMY_BY_TYPE_MARKER if marker in t), "fixture")


@dataclass(frozen=True)
class NodeFixture:
    label: str
    properties: dict[str, Any]


@dataclass(frozen=True)
class RelFixture:
    rel_type: str
    start_label: str
    end_label: str
    properties: dict[str, Any]


def build_node_fixtures(schema: GraphSchema) -> list[NodeFixture]:
    """One `NodeFixture` per declared label, common properties plus its own.

    A property unique through the common label every node carries gets a label-qualified
    value, or every node would collide on that uniqueness constraint."""
    shared_unique = {prop for label, prop in schema.unique if label == schema.common_label}
    fixtures = []
    for label in sorted(schema.nodes):
        assert_identifier(label, "node label")
        props: dict[str, Any] = {}
        for prop in schema.all_properties(label) or ():
            if prop.name in shared_unique:
                props[prop.name] = f"{label}-{prop.name}-fixture"
            else:
                props[prop.name] = dummy_value(prop.name, prop.type, prop.enum)
        fixtures.append(NodeFixture(label, props))
    return fixtures


def build_rel_fixtures(schema: GraphSchema) -> list[RelFixture]:
    """One `RelFixture` per declared (type, start label, end label) triple."""
    fixtures: list[RelFixture] = []
    for rel_type, spec in sorted(schema.relationships.items()):
        assert_identifier(rel_type, "relationship type")
        base_props = {name: dummy_value(name) for name in spec.properties}
        for start_label in spec.start_labels:
            for end_label in spec.end_labels:
                assert_identifier(start_label, "node label")
                assert_identifier(end_label, "node label")
                fixtures.append(RelFixture(rel_type, start_label, end_label, dict(base_props)))
    return fixtures


def apply_constraints(session: neo4j.Session, schema: GraphSchema) -> None:
    """The declared uniqueness constraints and range indexes, idempotently (`IF NOT EXISTS`).

    Anchored lookups then plan as index seeks, so the plan check's label-scan ban does not
    misfire. Full-text and vector indexes are not modelled."""
    for label, prop in schema.unique:
        assert_identifier(label, "node label")
        assert_identifier(prop, "property")
        session.run(
            f"CREATE CONSTRAINT fixture_unique_{label.lower()}_{prop} IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
        ).consume()
    for label, prop in schema.range_indexed_properties():
        assert_identifier(label, "node label")
        assert_identifier(prop, "property")
        session.run(
            f"CREATE INDEX fixture_range_{label.lower()}_{prop} IF NOT EXISTS "
            f"FOR (n:{label}) ON (n.{prop})"
        ).consume()


def seed(session: neo4j.Session, schema: GraphSchema) -> None:
    """Apply the constraints, wipe the database, then load the fixture graph.

    One node per label by `CREATE` (also carrying the common label, when declared), then one
    edge per (type, start, end) triple, matched by label alone since each label has exactly
    one node. Only ever run against a throwaway database."""
    apply_constraints(session, schema)
    session.run("MATCH (n) DETACH DELETE n").consume()
    extra = ""
    if schema.common_label is not None:
        assert_identifier(schema.common_label, "node label")
        extra = f":{schema.common_label}"
    for node in build_node_fixtures(schema):
        session.run(
            f"CREATE (n:{node.label}{extra}) SET n = $props", props=node.properties
        ).consume()
    for rel in build_rel_fixtures(schema):
        session.run(
            f"MATCH (a:{rel.start_label}), (b:{rel.end_label}) "
            f"MERGE (a)-[r:{rel.rel_type}]->(b) SET r = $props",
            props=rel.properties,
        ).consume()
