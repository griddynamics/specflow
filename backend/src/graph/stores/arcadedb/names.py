"""Names ArcadeDB fixes itself: the server names an index ``<Type>[<property>,...]`` and offers no way to
choose another, so the library's ``graph_*`` search index names are mapped to those names here."""

from __future__ import annotations

from functools import cache
from typing import Final

from graph.core.queries import CORE_DDL
from graph.stores._bolt.schema import parse_ddl_statement

# The kinds of schema object whose server name is fixed (they are created by SQL, not by Cypher DDL).
SERVER_NAMED_KINDS: Final = frozenset({"vector", "fulltext"})


def server_index_name(label: str, properties: tuple[str, ...]) -> str:
    """The name ArcadeDB gives an index on ``label`` over ``properties``: ``SpecNode[name,statement_text]``."""

    return f"{label}[{','.join(properties)}]"


@cache
def _server_named() -> dict[str, tuple[str, tuple[str, ...]]]:
    found: dict[str, tuple[str, tuple[str, ...]]] = {}
    for position, statement in enumerate(CORE_DDL):
        item = parse_ddl_statement(statement, position=position)
        if item.kind in SERVER_NAMED_KINDS:
            found[item.name] = (item.label, item.properties)
    return found


def index_names() -> dict[str, str]:
    """Library index name (``graph_spec_node_embedding``) -> the name of that index on the server."""

    return {name: server_index_name(label, properties) for name, (label, properties) in _server_named().items()}


def index_fields() -> dict[str, tuple[str, ...]]:
    """Library index name -> the properties it spans (the fields of a fulltext index)."""

    return {name: properties for name, (_label, properties) in _server_named().items()}
