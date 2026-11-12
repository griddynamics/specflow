"""Plan query module and schema setup."""

from __future__ import annotations

import graph.plan.queries as plan_queries
from graph.core import load_ontology
from graph.core.queries import CORE_DDL, Query, submodule_ddl
from graph.gap.queries import GAP_DDL
from graph.plan.names import PLAN_RELATIONS
from graph.plan.queries import PLAN_DDL, READ_PLAN, delete_links, read_links
from graph.plan.schema import ensure_plan_schema
from graph.stores._bolt.cypher import CYPHER_DIALECT, render_cypher
from graph_plan_support import SchemaDriver
from graph_support import llm_config


def plan_catalog() -> list[Query]:
    """Every query of the plan catalog: its constants and one query of each factory per plan relation."""

    constants = [value for value in vars(plan_queries).values() if isinstance(value, Query)]
    typed = [factory(relation) for relation in PLAN_RELATIONS for factory in (read_links, delete_links)]
    return [*constants, *typed]


def test_the_submodule_ddl_is_discovered_by_the_default_schema_in_submodule_order() -> None:
    assert submodule_ddl() == (*GAP_DDL, *PLAN_DDL)


def test_plan_read_plan_is_a_plan_template() -> None:
    assert (READ_PLAN.name, READ_PLAN.template) == (
        "plan_read_plan",
        "plan.plan_read_plan",
    )


def test_every_plan_query_renders_and_binds_the_project() -> None:
    for query in plan_catalog():
        assert render_cypher(query), query.name
        CYPHER_DIALECT.require_project_binding(query)


async def test_ensure_plan_schema_is_idempotent() -> None:
    ontology = load_ontology()
    driver = SchemaDriver(ontology, ddl=(*CORE_DDL, *GAP_DDL, *PLAN_DDL))
    driver.add_all()

    assert await ensure_plan_schema(driver, ontology, llm_config(embedding_dim=16)) == ()
