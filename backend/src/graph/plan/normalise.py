"""Deterministic repairs of planner answers, applied before validation.

A repair changes only what has one right reading, so the planner is not asked again for it and the part
does not fall back for it:

- a ``phase`` or ``complexity`` that is a vocabulary value in another letter case becomes that value;
- a ``spec`` done-when item of a task whose implemented nodes have no acceptance criteria becomes
  ``planner`` (the item restates a statement, and ``spec`` is kept for acceptance criteria);
- area tasks drop waits for setup tasks (by action or task key) and closing tasks drop every wait:
  Stitch adds those waits (rules 1 and 8).

Anything else stays a validation problem.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from graph.plan.answers import DoneWhenAnswer, ProjectAnswer, TaskAnswer, TasksAnswer
from graph.plan.names import plan_vocabularies
from graph.plan.view import PlanView

_SPEC_SOURCE = "spec"
_PLANNER_SOURCE = "planner"


def normalise_tasks(answer: TasksAnswer, *, view: PlanView, setup_waits: Collection[str]) -> TasksAnswer:
    """A Tasks answer with the repairs applied; ``setup_waits`` are the setup actions and task keys."""

    dropped = frozenset(setup_waits)
    tasks = [_task(task, view, waits=[wait for wait in task.waits_for if wait not in dropped]) for task in answer.tasks]
    return answer.model_copy(update={"tasks": tasks})


def normalise_project(answer: ProjectAnswer, *, view: PlanView) -> ProjectAnswer:
    """A Project answer with the repairs applied; closing tasks lose their waits."""

    setup = [_task(task, view, waits=list(task.waits_for)) for task in answer.setup_tasks]
    closing = [_task(task, view, waits=[]) for task in answer.closing_tasks]
    return answer.model_copy(update={"setup_tasks": setup, "closing_tasks": closing})


def _task(task: TaskAnswer, view: PlanView, *, waits: list[str]) -> TaskAnswer:
    vocabularies = plan_vocabularies()
    return task.model_copy(
        update={
            "phase": _vocabulary_value(task.phase, vocabularies["phase"]),
            "complexity": _vocabulary_value(task.complexity, vocabularies["complexity"]),
            "waits_for": waits,
            "done_when": _done_when(task.done_when, task.implements, view),
        }
    )


def _vocabulary_value(value: str, values: Collection[str]) -> str:
    if value in values:
        return value
    lowered = value.strip().lower()
    return lowered if lowered in values else value


def _done_when(items: Sequence[DoneWhenAnswer], implements: Sequence[str], view: PlanView) -> list[DoneWhenAnswer]:
    with_criteria = any(
        view.rules.acceptance_of(view.nodes[spec_id].attributes) for spec_id in implements if spec_id in view.nodes
    )
    if with_criteria:
        return list(items)
    return [
        item.model_copy(update={"source": _PLANNER_SOURCE}) if item.source == _SPEC_SOURCE else item for item in items
    ]
