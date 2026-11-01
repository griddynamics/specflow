"""Compatibility shim: schema setup of earlier releases, now a store method.

``ensure_schema`` (``graph.core.store``) takes a ``GraphStore`` or, deprecated, a Neo4j driver; the Cypher
schema manager and the DDL parsing live in ``graph.stores._bolt.schema``. Kept for one release so existing
imports keep working.
"""

from __future__ import annotations

from graph.core.store import (
    FRAGMENT_TEXT_INDEX,
    SPEC_NODE_EMBEDDING_INDEX,
    SPEC_NODE_TEXT_INDEX,
    ensure_schema,
    foreign_labels,
)
from graph.stores._bolt.schema import (
    DOCUMENT,
    EDGE_KEY,
    FRAGMENT,
    GROUP_KEY,
    INDEX_ONLINE_TIMEOUT_S,
    SchemaObject,
    parse_ddl_statement,
    schema_objects,
)

__all__ = [
    "DOCUMENT",
    "EDGE_KEY",
    "FRAGMENT",
    "FRAGMENT_TEXT_INDEX",
    "GROUP_KEY",
    "INDEX_ONLINE_TIMEOUT_S",
    "SPEC_NODE_EMBEDDING_INDEX",
    "SPEC_NODE_TEXT_INDEX",
    "SchemaObject",
    "ensure_schema",
    "foreign_labels",
    "parse_ddl_statement",
    "schema_objects",
]
