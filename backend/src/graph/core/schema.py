"""Compatibility shim: schema setup of earlier releases, now a store method.

``ensure_schema`` (``graph.core.store``) takes a ``GraphStore`` or, deprecated, a Neo4j driver; the Cypher
schema manager and the DDL parsing live in ``graph.stores._bolt.schema``. Kept for one release so existing
imports keep working.
"""

from __future__ import annotations

import importlib
from typing import Any

from graph.core.store import (
    FRAGMENT_TEXT_INDEX,
    SPEC_NODE_EMBEDDING_INDEX,
    SPEC_NODE_TEXT_INDEX,
    ensure_schema,
    foreign_labels,
)

# The Cypher schema manager imports the Neo4j SDK, so its names resolve on first access (PEP 562).
_LAZY: frozenset[str] = frozenset(
    {
        "DOCUMENT",
        "EDGE_KEY",
        "FRAGMENT",
        "GROUP_KEY",
        "INDEX_ONLINE_TIMEOUT_S",
        "SchemaObject",
        "parse_ddl_statement",
        "schema_objects",
    }
)


def __getattr__(name: str) -> Any:
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module("graph.stores._bolt.schema"), name)
    globals()[name] = value
    return value


# The lazily resolved names (see above) stay out of ``__all__``: the linter cannot see them defined.
__all__ = [
    "FRAGMENT_TEXT_INDEX",
    "SPEC_NODE_EMBEDDING_INDEX",
    "SPEC_NODE_TEXT_INDEX",
    "ensure_schema",
    "foreign_labels",
]
