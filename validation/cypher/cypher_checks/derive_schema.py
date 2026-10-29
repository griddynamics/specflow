"""Build step: derive a starting schema file from a live Neo4j database.

Reads the labels, properties, relationship types (with their endpoint labels), uniqueness
constraints, range indexes and vector indexes a running database holds, and writes them as the
YAML every other check reads. The result is a draft: add `common_label`, `common_properties`,
`enum` lists and the `policy` registers by hand. Run as
`python -m cypher_checks derive-schema --uri bolt://localhost:7687 > schema.yaml` against a
development database that holds a representative import of your data.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import neo4j
import yaml

from cypher_checks.findings import EXIT_ERROR, EXIT_OK

__all__ = ["build_schema", "fetch", "main", "neo4j_type"]

_SCALARS = {
    "string": "string",
    "long": "integer",
    "integer": "integer",
    "double": "float",
    "float": "float",
    "boolean": "boolean",
    "date": "timestamp",
    "datetime": "timestamp",
    "localdatetime": "timestamp",
    "time": "timestamp",
    "localtime": "timestamp",
    "zoned datetime": "timestamp",
    "local datetime": "timestamp",
    "zoned time": "timestamp",
    "local time": "timestamp",
    "duration": "timestamp",
}

_NODE_PROPERTIES = "CALL db.schema.nodeTypeProperties()"
_REL_PROPERTIES = "CALL db.schema.relTypeProperties()"
_ENDPOINTS = (
    "MATCH (a)-[r]->(b) RETURN DISTINCT labels(a) AS start, type(r) AS type, labels(b) AS end"
)
_UNIQUE = (
    "SHOW CONSTRAINTS YIELD type, entityType, labelsOrTypes, properties "
    "WHERE type IN ['UNIQUENESS', 'NODE_PROPERTY_UNIQUENESS'] AND entityType = 'NODE' "
    "RETURN labelsOrTypes, properties"
)
_RANGE = (
    "SHOW INDEXES YIELD type, entityType, labelsOrTypes, properties, owningConstraint "
    "WHERE type = 'RANGE' AND entityType = 'NODE' AND owningConstraint IS NULL "
    "RETURN labelsOrTypes, properties"
)
_VECTOR = (
    "SHOW INDEXES YIELD type, entityType, labelsOrTypes, properties "
    "WHERE type = 'VECTOR' AND entityType = 'NODE' RETURN labelsOrTypes, properties"
)

Rows = Sequence[Mapping[str, Any]]


def neo4j_type(names: Sequence[str]) -> str:
    """The schema file's `type` text for the `propertyTypes` the procedures report.

    `StringArray` becomes `list<string>`; a type this table does not know becomes `string`."""
    key = (names[0] if names else "").lower().strip()
    key = key.removesuffix(" not null").strip()
    if key.startswith("list<") and key.endswith(">"):
        inner = key[len("list<") : -1].strip().removesuffix(" not null").strip()
        return f"list<{_SCALARS.get(inner, 'string')}>"
    if key.endswith("array"):
        return f"list<{_SCALARS.get(key.removesuffix('array'), 'string')}>"
    return _SCALARS.get(key, "string")


def _pairs(rows: Rows) -> list[tuple[str, str]]:
    """`(label, property)` of every single-label, single-property row."""
    return [
        (row["labelsOrTypes"][0], row["properties"][0])
        for row in rows
        if len(row["labelsOrTypes"]) == 1 and len(row["properties"]) == 1
    ]


def build_schema(
    node_rows: Rows,
    rel_rows: Rows,
    endpoint_rows: Rows,
    *,
    unique_rows: Rows,
    range_rows: Rows,
    vector_rows: Rows,
) -> dict[str, Any]:
    """The schema mapping for the query results, in the shape `parse_schema` reads."""
    nodes: dict[str, dict[str, str]] = {}
    for row in node_rows:
        for label in row["nodeLabels"]:
            props = nodes.setdefault(label, {})
            if row.get("propertyName"):
                props.setdefault(row["propertyName"], neo4j_type(row.get("propertyTypes") or []))
    for label, prop in _pairs(vector_rows):
        nodes.setdefault(label, {})[prop] = "vector"

    ends: dict[str, tuple[set[str], set[str]]] = {}
    for row in endpoint_rows:
        starts, finishes = ends.setdefault(row["type"], (set(), set()))
        starts.update(row["start"])
        finishes.update(row["end"])
    rel_props: dict[str, set[str]] = {}
    for row in rel_rows:
        name = str(row["relType"]).strip(":`")
        if row.get("propertyName"):
            rel_props.setdefault(name, set()).add(row["propertyName"])

    return {
        "nodes": {
            label: {"properties": {name: {"type": kind} for name, kind in props.items()}}
            for label, props in sorted(nodes.items())
        },
        "relationships": {
            name: {
                "from": sorted(starts),
                "to": sorted(finishes),
                "properties": sorted(rel_props.get(name, set())),
            }
            for name, (starts, finishes) in sorted(ends.items())
        },
        "constraints": {
            "unique": [f"{label}.{prop}" for label, prop in sorted(_pairs(unique_rows))],
            "indexes": [f"{label}.{prop}" for label, prop in sorted(_pairs(range_rows))],
        },
    }


def fetch(driver: neo4j.Driver, database: str | None = None) -> dict[str, Any]:
    """Run the introspection queries on `driver` and build the schema mapping."""
    with driver.session(database=database) as session:

        def rows(query: str) -> list[dict[str, Any]]:
            return [dict(record) for record in session.run(query)]

        return build_schema(
            rows(_NODE_PROPERTIES),
            rows(_REL_PROPERTIES),
            rows(_ENDPOINTS),
            unique_rows=rows(_UNIQUE),
            range_rows=rows(_RANGE),
            vector_rows=rows(_VECTOR),
        )


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m cypher_checks derive-schema")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument(
        "--password-env",
        default="NEO4J_PASSWORD",
        help="name of the environment variable that holds the password (never a flag value)",
    )
    parser.add_argument("--database", help="database name (the server default when omitted)")
    args = parser.parse_args(argv)
    password = os.environ.get(args.password_env, "")
    try:
        with neo4j.GraphDatabase.driver(args.uri, auth=(args.user, password)) as driver:
            schema = fetch(driver, args.database)
    except (neo4j.exceptions.Neo4jError, neo4j.exceptions.DriverError) as exc:
        sys.stderr.write(f"error: cannot read the schema from {args.uri}: {exc}\n")
        return EXIT_ERROR
    sys.stdout.write(yaml.safe_dump(schema, sort_keys=False))
    return EXIT_OK
