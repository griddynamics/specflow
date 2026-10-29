"""Movie reads and person writes: each query is loaded once from its `.cypher` file."""

from __future__ import annotations

from typing import Any

import neo4j
from graph.store import load_query

MOVIE_BY_TITLE = load_query("read/movie_by_title.cypher")
MERGE_PERSON = load_query("write/merge_person.cypher")


def movie_by_title(tx: neo4j.ManagedTransaction, title: str) -> list[dict[str, Any]]:
    """Every value reaches the query as a `$parameter` named like the file declares it."""
    return [record.data() for record in tx.run(MOVIE_BY_TITLE, title=title)]


def merge_person(tx: neo4j.ManagedTransaction, person_id: str, name: str, born: int) -> None:
    tx.run(MERGE_PERSON, id=person_id, name=name, born=born)
