"""The one module that builds the driver and opens sessions (an example adopter layout)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import neo4j

QUERIES = Path(__file__).resolve().parents[2] / "queries"

Rows = list[dict[str, Any]]


def load_query(relative: str) -> str:
    """The text of one `.cypher` file; callers keep it as a module constant."""
    return (QUERIES / relative).read_text(encoding="utf-8")


def connect(uri: str, user: str, password: str) -> neo4j.Driver:
    """The one driver the process shares."""
    return neo4j.GraphDatabase.driver(uri, auth=(user, password))


def read(driver: neo4j.Driver, work: Callable[[neo4j.ManagedTransaction], Rows]) -> Rows:
    """Run `work` in a managed read transaction, which the driver retries on transient errors."""
    with driver.session() as session:
        return session.execute_read(work)


def write(driver: neo4j.Driver, work: Callable[[neo4j.ManagedTransaction], None]) -> None:
    """Run `work` in a managed write transaction."""
    with driver.session() as session:
        session.execute_write(work)
