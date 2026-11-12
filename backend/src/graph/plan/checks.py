"""Plan checks: read-only checks and metrics of the persisted plan, after every run and on request.

They read the plannable spec nodes and the plan; data of other owners is never a problem, and nothing is
repaired: the next plan run rebuilds membership, stories, coverage and waits, and the task state fields
belong to the lifecycle calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from graph.core.reader import GraphReader
from graph.plan.changes import resolve_spec_id
from graph.plan.invariants import CLOSED, OPEN, PlanState, StoryState, TaskState, problems, ready_tasks, story_progress
from graph.plan.models import CurrentPlan, PlanCheckMetrics, PlanCheckReport
from graph.plan.options import PlanContext
from graph.plan.snapshot import fingerprint, read_current_plan, select_nodes
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.plan")


async def check_plan(context: PlanContext, stale_after: timedelta | None = None) -> PlanCheckReport:
    """The plan checks and metrics of ``context``'s project."""

    reader = GraphReader(context.query_executor, context.ontology, database=context.database)
    return await check_with(context, reader, stale_after=stale_after)


async def check_with(
    context: PlanContext, reader: Any, *, stale_after: timedelta | None = None, now: datetime | None = None
) -> PlanCheckReport:
    """``check_plan`` over a given spec-graph reader and clock."""

    project_id, rules = context.project_id, context.rules
    nodes = await reader.nodes(project_id)
    edges = await reader.edges(project_id)
    plannable, context_nodes = select_nodes(nodes, edges, rules)
    plannable_ids = frozenset(node.spec_id for node in plannable)
    aliases = {alias: node.spec_id for node in nodes for alias in node.spec_id_aliases if alias != node.spec_id}
    current = await read_current_plan(context.query_executor, project_id)
    state = plan_state(
        current,
        plannable=plannable_ids,
        required=frozenset(node.spec_id for node in plannable if rules.rule_of(node.label).required),
    )
    found = problems(state, lambda spec_id: resolve_spec_id(spec_id, aliases))
    selected = [edge for edge in edges if edge.source in plannable_ids or edge.target in plannable_ids]
    graph_fingerprint = fingerprint(
        context.ontology,
        rules,
        [*plannable, *context_nodes],
        selected,
        task_size=context.options.task_size,
        planner_members=context.options.planner_members,
    )
    report = PlanCheckReport(
        ok=not any(found.values()),
        problems={check: ids for check, ids in found.items() if ids},
        metrics=_metrics(current, state, graph_fingerprint, stale_after, now or datetime.now(UTC)),
    )
    log_event(
        logger,
        INFO,
        "graph.plan.check_done",
        project_id=project_id,
        ok=report.ok,
        problems=sum(len(ids) for ids in found.values()),
    )
    return report


def plan_state(current: CurrentPlan, *, plannable: frozenset[str], required: frozenset[str]) -> PlanState:
    """The persisted plan as the invariants see it."""

    profile = json.loads(current.plan.profile) if current.plan is not None and current.plan.profile else {}
    conventions = frozenset(
        spec_id for item in profile.get("conventions") or () for spec_id in item.get("spec_ids") or ()
    )
    return PlanState(
        plannable=plannable,
        required=required,
        areas={area.area_id: area.status for area in current.areas},
        stories={
            story.story_id: StoryState(
                area_id=story.area_id,
                status=story.status,
                kind=story.kind,
                anchor=story.anchor,
                held=story.claimed_by is not None,
                claim_token=story.claim_token,
                claim_stamped=story.claimed_at is not None,
            )
            for story in current.stories
        },
        tasks={
            task.task_id: TaskState(
                area_id=task.area_id,
                story_id=task.story_id,
                status=task.status,
                implements=task.implements,
                commit=task.commit,
                claimed=task.claimed_by is not None,
                claim_token=task.claim_token,
                progress=task.progress,
                blocked_by=task.blocked_by,
                completed_by=task.completed_by,
            )
            for task in current.tasks
        },
        links=tuple((link.type, link.source, link.target) for link in current.links),
        not_buildable=frozenset(str(entry.get("spec_id")) for area in current.areas for entry in area.not_buildable),
        conventions=conventions,
    )


def _metrics(
    current: CurrentPlan,
    state: PlanState,
    graph_fingerprint: str,
    stale_after: timedelta | None,
    now: datetime,
) -> PlanCheckMetrics:
    statuses = [task.status for task in current.tasks]
    reasons: dict[str, int] = {}
    for area in current.areas:
        for entry in area.not_buildable:
            if str(entry.get("spec_id")) in state.required:
                reason = str(entry.get("reason"))
                reasons[reason] = reasons.get(reason, 0) + 1
    progress: dict[str, int] = {}
    for story in current.stories:
        if story.status == "active":
            value = story_progress(task.status for task in current.tasks if task.story_id == story.story_id)
            progress[value] = progress.get(value, 0) + 1
    stale_claims = None
    if stale_after is not None:
        stale_claims = sum(
            1
            for task in current.tasks
            if task.status == "in_progress" and task.claimed_at is not None and task.claimed_at < now - stale_after
        )
    applying = current.plan is not None and current.plan.applying
    return PlanCheckMetrics(
        outdated=current.plan is not None and current.plan.graph_fingerprint != graph_fingerprint,
        ready=len(ready_tasks(state, applying=applying)),
        blocked=statuses.count("blocked"),
        failed=statuses.count("failed"),
        stale_claims=stale_claims,
        stale_commits=sum(1 for task in current.tasks if task.status in CLOSED and task.commit),
        not_buildable=reasons,
        open_questions_on_tasks=sum(1 for task in current.tasks if task.status in OPEN and task.has_open_questions),
        stories=progress,
    )
