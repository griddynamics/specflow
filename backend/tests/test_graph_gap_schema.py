"""Gap schema setup offline: ``ensure_gap_schema`` creates every ``graph_gap_*`` object once (FR-GONT-05)."""

from __future__ import annotations

from graph.core import load_ontology
from graph.core.queries import CORE_DDL, submodule_ddl
from graph.gap import ensure_gap_schema
from graph.gap.queries import GAP_DDL
from graph_plan_support import SchemaDriver
from graph_support import llm_config


def test_the_gap_ddl_is_discovered_by_the_default_schema() -> None:
    assert set(GAP_DDL) <= set(submodule_ddl())


async def test_fr_gont_05_ensure_gap_schema_creates_the_gap_objects_and_is_idempotent() -> None:
    ontology = load_ontology()
    driver = SchemaDriver(ontology, ddl=(*CORE_DDL, *submodule_ddl()))
    config = llm_config(embedding_dim=16)

    created = await ensure_gap_schema(driver, ontology, config)

    assert {
        "graph_gap_id",
        "graph_clarification_id",
        "graph_gap_status",
        "graph_gap_signature",
        "graph_foreign_uuid_gap",
        "graph_foreign_uuid_clarification",
        "graph_edge_key_impacts",
        "graph_edge_key_cites",
        "graph_edge_key_follows_up",
        "graph_edge_key_answers",
        "graph_edge_key_placed_in",
        "graph_edge_key_grounded_in",
        "graph_edge_key_replaces",
    } <= set(created)
    assert await ensure_gap_schema(driver, ontology, config) == ()
