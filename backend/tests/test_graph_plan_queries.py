"""Plan query module and schema setup."""

from __future__ import annotations

from graph.core import load_ontology
from graph.core.queries import CORE_DDL, submodule_ddl
from graph.plan.queries import PLAN_DDL, READ_PLAN
from graph.plan.schema import ensure_plan_schema
from graph_plan_support import SchemaDriver


def test_plan_ddl_is_discovered_by_the_default_schema() -> None:
    assert submodule_ddl() == PLAN_DDL


def test_plan_read_plan_binds_the_project() -> None:
    assert READ_PLAN.name == "plan_read_plan"
    assert READ_PLAN.binds_project_id()


async def test_ensure_plan_schema_is_idempotent() -> None:
    driver = SchemaDriver(load_ontology(), ddl=(*CORE_DDL, *PLAN_DDL))
    driver.add_all()

    assert await ensure_plan_schema(driver, ddl=PLAN_DDL) == ()
