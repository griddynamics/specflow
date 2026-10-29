"""Fixture: violates no_inline_queries in every way the rule checks."""

from __future__ import annotations

CYPHER_TEXT = "MATCH (p:Person {id: $id}) RETURN p"

SQL_TEXT = "SELECT id, name FROM person WHERE id = :id"


def load_query(path: str) -> str: ...


def query_missing_file() -> None:
    load_query("people/queries/read/does_not_exist.cypher")
