"""``estimate_plan``: the model calls and input tokens of the run a context would start, without a call.

It reads the spec graph, the current plan and the ``PlanStore`` like Snapshot and Partition, renders the
planner messages the run would send and counts a reused answer as no call. It writes nothing. Describe
and Project outputs are not known before the run, so later messages are rendered with the current plan
content (or the rules fallback): the token counts are estimates.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from graph.core.llm.gateway import request_tokens
from graph.core.reader import GraphReader
from graph.plan.answers import ProjectAnswer
from graph.plan.fallback import describe_fallback, project_fallback, tasks_fallback
from graph.plan.inputs import (
    describe_messages,
    project_edges,
    project_inputs,
    project_messages,
    review_messages,
    setup_members,
    tasks_messages,
)
from graph.plan.models import (
    DescribeArtifact,
    PlanEstimate,
    PlannedTask,
    PlannerOutcome,
    PlanStepEstimate,
    ProjectArtifact,
    TasksArtifact,
)
from graph.plan.names import PlanMode
from graph.plan.options import PlanContext
from graph.plan.partition import partition
from graph.plan.ports import PlanAnswerKind
from graph.plan.project import PROJECT_KEY, project_artifact
from graph.plan.reuse import describe_key, project_key, tasks_key
from graph.plan.snapshot import snapshot
from graph.plan.view import PlanView


async def estimate_plan(context: PlanContext) -> PlanEstimate:
    """Calls and input tokens per planning step of the run ``context`` would start."""

    return await estimate_with(
        context, GraphReader(context.query_executor, context.ontology, database=context.database)
    )


async def estimate_with(context: PlanContext, reader: Any) -> PlanEstimate:
    """``estimate_plan`` over a given spec-graph reader."""

    artifact = await snapshot(reader, context.query_executor, context)
    if artifact.unchanged:
        return PlanEstimate(mode=artifact.mode, unchanged=True)
    view = PlanView(
        artifact,
        partition(artifact, context.rules, context.ontology, planner_members=context.options.planner_members),
        context.rules,
    )
    mode = artifact.mode
    describe = DescribeArtifact(
        areas={area.area_id: describe_fallback(view, area.area_id)[0] for area in view.active_areas}
    )
    profile, setup, closing = project_fallback(view, setup_members(view))
    project = ProjectArtifact(profile=profile, setup=setup, closing=closing)
    key = project_key(view, context.project_id, project_inputs(view), project_edges(view))
    stored_project = (
        None if mode == "replan" else await context.store.get_answer(context.project_id, "project", PROJECT_KEY, key)
    )
    if stored_project is not None:
        project = project_artifact(view, ProjectAnswer.model_validate(stored_project), PlannerOutcome(reused=True))
        profile, setup, closing = project.profile, project.setup, project.closing

    describe_requests = []
    for area in view.active_areas:
        key = describe_key(view, context.project_id, area.area_id)
        if not await _reused(context, "describe", area.area_id, key, mode):
            describe_requests.append(describe_messages(view, area.area_id, context.options.planner_members)[0])

    project_requests = [] if stored_project is not None else [project_messages(view, describe)]

    tasks_requests = []
    planned: list[PlannedTask] = [*setup, *closing]
    for part in view.partition.parts:
        area_tasks = [task for task in planned if task.area_id == part.area_id]
        key = tasks_key(
            view,
            context.project_id,
            part,
            setup_tasks=[task.task_key for task in setup],
            profile=profile,
            task_size=context.options.task_size,
            planner_members=context.options.planner_members,
        )
        if not await _reused(context, "tasks", part.part_id, key, mode):
            tasks_requests.append(
                tasks_messages(
                    view,
                    part,
                    describe=describe,
                    project=project,
                    earlier=area_tasks,
                    contract_tasks={},
                    changed=(),
                    task_size=context.options.task_size,
                )
            )
        planned.extend(tasks_fallback(view, part, area_tasks=area_tasks, not_buildable=(), convention_ids=()))

    review_requests = []
    if context.options.review:
        waits = {task.task_key: task.waits for task in planned}
        review_requests.append(
            review_messages(
                view, describe=describe, project=project, tasks=planned, waits=waits, tasks_artifact=TasksArtifact()
            )
        )
    return PlanEstimate(
        mode=mode,
        steps={
            "describe": _step(describe_requests),
            "project": _step(project_requests),
            "tasks": _step(tasks_requests),
            "review": _step(review_requests),
        },
    )


async def _reused(context: PlanContext, kind: PlanAnswerKind, key: str, reuse_key: str, mode: PlanMode) -> bool:
    if mode == "replan":
        return False
    return await context.store.get_answer(context.project_id, kind, key, reuse_key) is not None


def _step(requests: Sequence[Sequence[dict[str, Any]]]) -> PlanStepEstimate:
    return PlanStepEstimate(calls=len(requests), input_tokens=sum(request_tokens(request) for request in requests))
