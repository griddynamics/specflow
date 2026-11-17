"""Gap schema setup (docs/graph/gap/api.md "Setup", data-model.md "Constraints and indexes").

The gap's constraints and indexes ship as ``ddl/gap_schema.cypher``; a Cypher store's default schema discovers that
file, so the store's ``ensure_schema`` creates the gap objects together with the core ones (PostgreSQL keeps gap nodes
and relationships in its generic tables, whose keys its own schema covers). ``ensure_gap_schema`` is the gap's entry
point for the same call (FR-GONT-05). After the schema it runs the migration of first-slice findings for each project
the host names (lifecycle.md "Migration of first-slice findings", ``graph.gap.migrate``): every gap query binds the
project id, so there is no query across projects and the host passes its project ids.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from graph.core.ontology import Ontology
from graph.core.store import ensure_schema
from graph.gap.migrate import migrate_first_slice
from graph.gap.ports import GapRecords
from logger.logger import INFO, get_logger, log_event

__all__ = ["ensure_gap_schema"]

logger = get_logger("graph.gap")


async def ensure_gap_schema(
    source: Any,
    ontology: Ontology,
    llm_config: Any,
    database: str | None = None,
    *,
    records: GapRecords | None = None,
    project_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Create the missing graph objects, the gap's included, then migrate the first-slice findings of each of
    ``project_ids`` that still has ``Finding`` nodes; return the names of the objects created (idempotent: a second
    call returns ``()`` and migrates nothing).

    ``source`` is a ``GraphStore`` or, deprecated, a Neo4j driver (with ``database``); ``ontology`` must hold the gap
    ontology file. Without ``records`` (the host's relational rows) the migration is skipped. Errors are those of the
    store's ``ensure_schema`` and of the migration's reads and writes.
    """

    created = await ensure_schema(source, ontology, llm_config, database)
    if not project_ids:
        return created
    if records is None:
        log_event(logger, INFO, "graph.gap.first_slice_migration_skipped", projects=len(project_ids))
        return created
    for project_id in dict.fromkeys(project_ids):
        await migrate_first_slice(source, ontology, project_id, records, database=database)
    return created
