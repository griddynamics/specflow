"""Plan schema setup.

The plan's constraints and indexes ship as ``ddl/plan_schema.cypher``; a Cypher store's default schema
discovers that file, so the store's ``ensure_schema`` creates the plan objects together with the core
ones. ``ensure_plan_schema`` is the convenience entry point of the plan for the same call.
"""

from __future__ import annotations

from typing import Any

from graph.core.ontology import Ontology
from graph.core.store import ensure_schema

__all__ = ["ensure_plan_schema"]


async def ensure_plan_schema(
    source: Any,
    ontology: Ontology,
    llm_config: Any,
    database: str | None = None,
) -> tuple[str, ...]:
    """Create the missing graph objects, the plan's included; return the names created (idempotent).

    ``source`` is a ``GraphStore`` or, deprecated, a Neo4j driver (with ``database``); ``ontology`` must
    hold the plan's ontology file. Errors are those of the store's ``ensure_schema``.
    """

    return await ensure_schema(source, ontology, llm_config, database)
