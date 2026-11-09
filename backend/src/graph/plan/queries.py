"""Plan queries: every query the plan runs, as named ``Query`` objects (architecture.md I3, design D2).

Like the core and build catalogs these are dialect-neutral: a name, a template id (``plan.<file stem>``)
and typed semantic arguments. The canonical statements live in ``.cypher`` files under ``queries/`` next
to this module and are loaded through ``load_plan_query`` by a Cypher store's dialect; a query whose
relationship type is only known at run time is a file with ``__NAME__`` tokens that the dialect fills
with the validated name its factory gives. The plan's constraints and indexes are ``ddl/plan_schema.cypher``.

Every query binds the project id; the executor injects it and the dialect refuses one that does not.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

from graph.core.queries import Query, Relation, safe_name
from graph.core.query_files import read_ddl_file
from graph.plan.names import PlanRelation

_DDL_FILE: Final = Path(__file__).resolve().parent / "ddl" / "plan_schema.cypher"


READ_PLAN: Final = Query("plan_read_plan", "plan.plan_read_plan")

READ_AREAS: Final = Query("plan_read_areas", "plan.plan_read_areas")

READ_STORIES: Final = Query("plan_read_stories", "plan.plan_read_stories")

READ_TASKS: Final = Query("plan_read_tasks", "plan.plan_read_tasks")


SET_APPLYING: Final = Query("plan_set_applying", "plan.plan_set_applying")

WRITE_PLAN: Final = Query("plan_write_plan", "plan.plan_write_plan")

UPSERT_AREAS: Final = Query("plan_upsert_areas", "plan.plan_upsert_areas")

UPSERT_STORIES: Final = Query("plan_upsert_stories", "plan.plan_upsert_stories")

CREATE_TASKS: Final = Query("plan_create_tasks", "plan.plan_create_tasks")

UPDATE_TASKS: Final = Query("plan_update_tasks", "plan.plan_update_tasks")

UPSERT_IN_AREA: Final = Query("plan_upsert_in_area", "plan.plan_upsert_in_area")

UPSERT_IN_STORY: Final = Query("plan_upsert_in_story", "plan.plan_upsert_in_story")

UPSERT_TASK_IMPLEMENTS: Final = Query("plan_upsert_task_implements", "plan.plan_upsert_task_implements")

UPSERT_STORY_IMPLEMENTS: Final = Query("plan_upsert_story_implements", "plan.plan_upsert_story_implements")

UPSERT_WAITS_FOR: Final = Query("plan_upsert_waits_for", "plan.plan_upsert_waits_for")

UPSERT_SUPERSEDES: Final = Query("plan_upsert_supersedes", "plan.plan_upsert_supersedes")

TRANSITION_TASK: Final = Query("plan_transition_task", "plan.plan_transition_task")

CLAIM_TASK: Final = Query("plan_claim_task", "plan.plan_claim_task")

_READ_LINKS_PREFIX: Final = "plan_read_links_"


@cache
def read_links(relation: PlanRelation) -> Query:
    """Every persisted relationship of one plan relation with its stored key and endpoint ids."""

    rel = safe_name(relation)
    return Query(f"{_READ_LINKS_PREFIX}{rel}", "plan.plan_read_links", Relation(rel))


_DELETE_LINKS_PREFIX: Final = "plan_delete_links_"


@cache
def delete_links(relation: PlanRelation) -> Query:
    """Delete plan relationships of one type by stored key and endpoint ids."""

    rel = safe_name(relation)
    return Query(f"{_DELETE_LINKS_PREFIX}{rel}", "plan.plan_delete_links", Relation(rel))


PLAN_DDL: Final = read_ddl_file(_DDL_FILE)
