"""Gap schema setup (docs/graph/gap/api.md "Setup", data-model.md "Constraints and indexes").

The gap's constraints and indexes ship as ``ddl/gap_schema.cypher``; a Cypher store's default schema discovers that
file, so the store's ``ensure_schema`` creates the gap objects together with the core ones (PostgreSQL keeps gap nodes
and relationships in its generic tables, whose keys its own schema covers). ``ensure_gap_schema`` is the gap's entry
point for the same call (FR-GONT-05). The migration of first-slice findings it will also run is not part of it yet
(lifecycle.md "Migration of first-slice findings").
"""

from __future__ import annotations

from typing import Any

from graph.core.ontology import Ontology
from graph.core.store import ensure_schema

__all__ = ["ensure_gap_schema"]


async def ensure_gap_schema(
    source: Any,
    ontology: Ontology,
    llm_config: Any,
    database: str | None = None,
) -> tuple[str, ...]:
    """Create the missing graph objects, the gap's included; return the names created (idempotent: a second call
    returns ``()``).

    ``source`` is a ``GraphStore`` or, deprecated, a Neo4j driver (with ``database``); ``ontology`` must hold the gap
    ontology file. Errors are those of the store's ``ensure_schema``.
    """

    return await ensure_schema(source, ontology, llm_config, database)
