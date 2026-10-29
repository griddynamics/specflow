"""Shared fixtures: the example schema, and one seeded Neo4j testcontainer per session.

Only `integration`-marked tests ask for `neo4j_driver`, so a static-only run never needs
Docker."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import neo4j
import pytest

from cypher_checks import container
from cypher_checks.schema import GraphSchema, load_schema

KIT = Path(__file__).resolve().parents[1]
EXAMPLES = KIT / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def schema() -> GraphSchema:
    return load_schema(EXAMPLES / "schema.yaml")


@pytest.fixture(scope="session")
def neo4j_driver(schema: GraphSchema) -> Iterator[neo4j.Driver]:
    """A driver on one throwaway container seeded with the example fixture graph. The checks
    only `EXPLAIN`, so no test writes to it."""
    with container.seeded_driver(schema) as driver:
        yield driver
