"""The Persist step: the diff written to Neo4j in batches, each its own idempotent transaction.

The first batch sets ``Plan.applying`` (claims wait while it is set); then areas, stories, new tasks,
task updates, relationship deletes and creates; the last batch writes the ``Plan`` node and clears
``applying``. A cursor in the ``PlanStore`` records the last applied batch, so an interrupted persist
resumes there. A task update applies only while the task's status is the one the run read; a task
claimed or reported meanwhile is skipped into ``conflicts``, and so are its relationship writes. Spec
nodes are only ever endpoints: no property of a spec node, document or fragment is written.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from graph.core.queries import Query
from graph.plan.diff import PLAN_ID
from graph.plan.identity import STORY_PREFIX, plan_uuid
from graph.plan.models import DiffArtifact, LinkOp, NodeOp, PersistArtifact
from graph.plan.names import PLAN_RELATIONS
from graph.plan.ports import PlanStore
from graph.plan.queries import (
    CREATE_TASKS,
    SET_APPLYING,
    UPDATE_TASKS,
    UPSERT_AREAS,
    UPSERT_IN_AREA,
    UPSERT_IN_STORY,
    UPSERT_STORIES,
    UPSERT_STORY_IMPLEMENTS,
    UPSERT_SUPERSEDES,
    UPSERT_TASK_IMPLEMENTS,
    UPSERT_WAITS_FOR,
    WRITE_PLAN,
    delete_links,
)
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.plan")

BATCH_ROWS: Final = 500
_TASK_SOURCED: Final = frozenset({"IN_STORY", "IMPLEMENTS", "WAITS_FOR", "SUPERSEDES"})
_CREATES: Final[Mapping[str, Query]] = {
    "IN_AREA": UPSERT_IN_AREA,
    "IN_STORY": UPSERT_IN_STORY,
    "IMPLEMENTS": UPSERT_TASK_IMPLEMENTS,
    "WAITS_FOR": UPSERT_WAITS_FOR,
    "SUPERSEDES": UPSERT_SUPERSEDES,
}


@dataclass(frozen=True)
class Batch:
    """One write statement; ``links`` marks a relationship batch whose task-sourced rows a conflict drops."""

    query: Query
    params: Mapping[str, Any]
    rows: tuple[Mapping[str, Any], ...] = ()
    links: bool = False


def batches(diff: DiffArtifact, project_id: str) -> tuple[Batch, ...]:
    """Every write of a diff, in order; the same diff always gives the same batches (a resume relies on it)."""

    plan = {"plan_id": PLAN_ID}
    found: list[Batch] = [Batch(SET_APPLYING, {**plan, "uuid": plan_uuid(project_id, PLAN_ID)})]
    found.extend(_node_batches(UPSERT_AREAS, _node_rows(diff.areas, project_id)))
    found.extend(_node_batches(UPSERT_STORIES, _node_rows(diff.stories, project_id)))
    found.extend(_node_batches(CREATE_TASKS, _node_rows((op for op in diff.tasks if op.op == "create"), project_id)))
    updates = [
        {"id": op.id, "expected_status": op.expected_status, "properties": op.properties}
        for op in diff.tasks
        if op.op != "create"
    ]
    found.extend(_node_batches(UPDATE_TASKS, updates))
    for relation in PLAN_RELATIONS:
        rows = [_link_row(link) for link in diff.links_delete if link.type == relation]
        found.extend(_node_batches(delete_links(relation), rows, links=True))
    for relation in PLAN_RELATIONS:
        for query, rows in _create_rows(diff.links_create, relation):
            found.extend(_node_batches(query, rows, links=True))
    found.append(Batch(WRITE_PLAN, {**plan, "properties": dict(diff.plan_properties)}))
    return tuple(found)


async def persist(
    executor: Any,
    store: PlanStore,
    diff: DiffArtifact,
    *,
    project_id: str,
    run_id: str,
    now: Callable[[], str],
) -> PersistArtifact:
    """Apply the diff from the stored cursor on; store the cursor after each batch and the meaning
    revisions after the last one."""

    planned = batches(diff, project_id)
    stored = await store.get_artifact(project_id, run_id, "persist")
    progress = PersistArtifact.model_validate(stored) if stored is not None else PersistArtifact()
    conflicts = set(progress.conflicts)
    for index in range(progress.cursor, len(planned)):
        batch = planned[index]
        rows = [row for row in batch.rows if not (batch.links and _blocked(row, conflicts))]
        if batch.rows and not rows:
            records: list[Any] = []
        else:
            params: dict[str, Any] = {**batch.params, "run_id": run_id, "now": now()}
            if batch.rows:
                params["rows"] = rows
            records = await executor.write(batch.query, project_id, **params)
        if batch.query is UPDATE_TASKS:
            conflicts.update(record["task_id"] for record in records if not record["applies"])
        progress = PersistArtifact(cursor=index + 1, batches=len(planned), conflicts=tuple(sorted(conflicts)))
        await store.put_artifact(project_id, run_id, "persist", progress.model_dump(mode="json"))
        log_event(
            logger,
            INFO,
            "graph.plan.persist_batch",
            run_id=run_id,
            batch=index + 1,
            batches=len(planned),
            query=batch.query.name,
            rows=len(rows),
            conflicts=len(conflicts),
        )
    await store.put_revisions(project_id, diff.revisions)
    return progress


def _blocked(row: Mapping[str, Any], conflicts: set[str]) -> bool:
    return row.get("relation") in _TASK_SOURCED and row.get("source") in conflicts


def _node_rows(ops: Iterable[NodeOp], project_id: str) -> list[dict[str, Any]]:
    return [{"id": op.id, "uuid": plan_uuid(project_id, op.id), "properties": op.properties} for op in ops]


def _link_row(link: LinkOp) -> dict[str, Any]:
    return {"relation": link.type, "source": link.source, "target": link.target, "key": link.key}


def _create_rows(links: Iterable[LinkOp], relation: str) -> list[tuple[Query, list[dict[str, Any]]]]:
    tasks: list[dict[str, Any]] = []
    stories: list[dict[str, Any]] = []
    for link in links:
        if link.type != relation:
            continue
        row = {**_link_row(link), "properties": link.properties}
        (stories if link.source.startswith(STORY_PREFIX) else tasks).append(row)
    found = [(_CREATES[relation], tasks)]
    if stories:
        found.append((UPSERT_STORY_IMPLEMENTS, stories))
    return found


def _node_batches(query: Query, rows: Sequence[Mapping[str, Any]], *, links: bool = False) -> list[Batch]:
    return [
        Batch(query, {}, tuple(rows[start : start + BATCH_ROWS]), links=links)
        for start in range(0, len(rows), BATCH_ROWS)
    ]
