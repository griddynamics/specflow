"""Plan Cypher: every query text the plan runs, as named ``Query`` constants.

Statements live in ``.cypher`` files under ``queries/`` next to this module and load through
``load_plan_query``; a query whose label or relationship type is known only at run time is a file with
``__NAME__`` tokens that its factory fills with validated names. Every project-scoped query binds
``$project_id``.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

from graph.core.queries import Query, safe_name
from graph.core.query_files import read_ddl_file, read_query_file, render_query
from graph.plan.names import PlanRelation

_QUERY_DIRECTORY: Final = Path(__file__).resolve().parent / "queries"
_DDL_FILE: Final = Path(__file__).resolve().parent / "ddl" / "plan_schema.cypher"


def load_plan_query(name: str) -> str:
    """The text of the plan query file ``name`` (relative to ``queries/``)."""

    return read_query_file(_QUERY_DIRECTORY, name)


READ_PLAN: Final = Query(
    "plan_read_plan",
    load_plan_query("plan_read_plan.cypher"),
)

READ_AREAS: Final = Query(
    "plan_read_areas",
    load_plan_query("plan_read_areas.cypher"),
)

READ_STORIES: Final = Query(
    "plan_read_stories",
    load_plan_query("plan_read_stories.cypher"),
)

READ_TASKS: Final = Query(
    "plan_read_tasks",
    load_plan_query("plan_read_tasks.cypher"),
)


_READ_LINKS_PREFIX: Final = "plan_read_links_"


@cache
def read_links(relation: PlanRelation) -> Query:
    """Every persisted relationship of one plan relation with its stored key and endpoint ids."""

    rel = safe_name(relation)
    return Query(
        f"{_READ_LINKS_PREFIX}{rel}",
        render_query(load_plan_query("plan_read_links.cypher"), type=rel),
    )


PLAN_DDL: Final = read_ddl_file(_DDL_FILE)
