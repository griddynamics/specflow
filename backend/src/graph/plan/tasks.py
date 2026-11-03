"""The Tasks step: the tasks of every planning part, areas in parallel and the parts of one area in order
(later parts see the tasks of earlier ones)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import cast

from graph.plan.answers import TasksAnswer
from graph.plan.fallback import tasks_fallback
from graph.plan.identity import PROJECT_AREA_ID, task_key
from graph.plan.inputs import tasks_messages
from graph.plan.models import (
    DescribeArtifact,
    NotBuildable,
    PlannedTask,
    PlannerOutcome,
    PlanningPart,
    ProjectArtifact,
    TasksArtifact,
)
from graph.plan.names import NotBuildableReason, PlanMode
from graph.plan.options import PlanContext
from graph.plan.planned import current_tasks, from_answer
from graph.plan.planners import PlannerCall, run_planner
from graph.plan.prompts import prompt_ref
from graph.plan.reuse import tasks_key
from graph.plan.validate import validate_tasks
from graph.plan.view import PlanView

_NOTE_LIMIT = 300


async def plan_tasks(
    context: PlanContext,
    view: PlanView,
    describe: DescribeArtifact,
    project: ProjectArtifact,
    *,
    mode: PlanMode,
) -> TasksArtifact:
    """Plan the tasks of every part."""

    parts: dict[str, list[PlanningPart]] = {}
    for part in view.partition.parts:
        parts.setdefault(part.area_id, []).append(part)
    contract_tasks = _contract_tasks(view)
    changed = _changed_tasks(view)
    results = await asyncio.gather(
        *(
            _plan_area(
                context,
                view,
                area_parts,
                describe=describe,
                project=project,
                contract_tasks=contract_tasks,
                changed=changed,
                mode=mode,
            )
            for _, area_parts in sorted(parts.items())
        )
    )
    tasks: list[PlannedTask] = []
    not_buildable: dict[str, tuple[NotBuildable, ...]] = {}
    outcomes: dict[str, PlannerOutcome] = {}
    for (area_id, _), (area_tasks, area_not_buildable, area_outcomes) in zip(
        sorted(parts.items()), results, strict=True
    ):
        tasks.extend(area_tasks)
        if area_not_buildable:
            not_buildable[area_id] = area_not_buildable
        outcomes.update(area_outcomes)
    size = context.options.task_size
    return TasksArtifact(
        tasks=tuple(sorted(tasks, key=lambda task: task.task_key)),
        not_buildable=not_buildable,
        outcomes=outcomes,
        oversized=tuple(sorted(task.task_key for task in tasks if len(task.implements) > size)),
    )


async def _plan_area(
    context: PlanContext,
    view: PlanView,
    parts: Sequence[PlanningPart],
    *,
    describe: DescribeArtifact,
    project: ProjectArtifact,
    contract_tasks: Mapping[str, Sequence[str]],
    changed: frozenset[str],
    mode: PlanMode,
) -> tuple[list[PlannedTask], tuple[NotBuildable, ...], dict[str, PlannerOutcome]]:
    area_tasks: list[PlannedTask] = []
    not_buildable: list[NotBuildable] = []
    outcomes: dict[str, PlannerOutcome] = {}
    conventions = frozenset(spec_id for item in project.profile.conventions for spec_id in item.spec_ids)
    project_tasks = [*project.setup, *project.closing] if parts and parts[0].area_id == PROJECT_AREA_ID else []
    for part in parts:
        held = [task.action for task in (*project_tasks, *area_tasks)]
        call = PlannerCall(
            kind="tasks",
            key=part.part_id,
            reuse_key=tasks_key(
                view,
                context.project_id,
                part,
                setup_tasks=[task.task_key for task in project.setup],
                profile=project.profile,
                task_size=context.options.task_size,
                planner_members=context.options.planner_members,
            ),
            messages=tasks_messages(
                view,
                part,
                describe=describe,
                project=project,
                earlier=area_tasks,
                contract_tasks=contract_tasks,
                changed=changed,
                task_size=context.options.task_size,
            ),
            answer_type=TasksAnswer,
            validate=_validator(view, part, held, conventions),
        )
        answer, outcome = await run_planner(context, call, mode=mode)
        outcomes[part.part_id] = outcome
        if answer is None:
            kept = _current_not_buildable(view, part)
            not_buildable.extend(kept)
            area_tasks.extend(
                tasks_fallback(
                    view,
                    part,
                    area_tasks=area_tasks,
                    not_buildable=[entry.spec_id for entry in kept],
                    convention_ids=conventions,
                )
            )
            continue
        known = {task.action: task.task_key for task in (*project_tasks, *area_tasks)}
        known.update({task.action: task_key(part.area_id, task.action) for task in answer.tasks})
        area_tasks.extend(
            from_answer(
                view, task, area_id=part.area_id, story_id=task.story, prompt_ref=prompt_ref("tasks"), wait_keys=known
            )
            for task in answer.tasks
        )
        not_buildable.extend(
            NotBuildable(
                spec_id=entry.spec_id, reason=cast(NotBuildableReason, entry.reason), note=entry.note[:_NOTE_LIMIT]
            )
            for entry in answer.not_buildable
        )
    return area_tasks, tuple(sorted(not_buildable, key=lambda entry: entry.spec_id)), outcomes


def _validator(
    view: PlanView, part: PlanningPart, held: Collection[str], conventions: Collection[str]
) -> Callable[[TasksAnswer], list[str]]:
    def check(answer: TasksAnswer) -> list[str]:
        return validate_tasks(answer, view=view, part=part, earlier_actions=held, convention_ids=conventions)

    return check


def _contract_tasks(view: PlanView) -> dict[str, tuple[str, ...]]:
    """Interface -> current open or completed tasks that implement it (provider contract work)."""

    found: dict[str, set[str]] = {}
    for area in view.active_areas:
        for task in current_tasks(view.snapshot.current, area.area_id):
            for spec_id in task.implements:
                if view.is_type(spec_id, "interface"):
                    found.setdefault(spec_id, set()).add(task.task_id)
    return {interface: tuple(sorted(ids)) for interface, ids in sorted(found.items())}


def _changed_tasks(view: PlanView) -> frozenset[str]:
    return frozenset(
        task.task_id
        for task in view.snapshot.current.tasks
        if view.spec_change(task.implements, task.spec_build_id) is not None
    )


def _current_not_buildable(view: PlanView, part: PlanningPart) -> list[NotBuildable]:
    area = next((item for item in view.snapshot.current.areas if item.area_id == part.area_id), None)
    if area is None:
        return []
    members = frozenset(part.members)
    return [NotBuildable.model_validate(entry) for entry in area.not_buildable if entry.get("spec_id") in members]
