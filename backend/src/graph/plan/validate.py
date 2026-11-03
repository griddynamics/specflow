"""Checks of planner answers beyond their JSON schema.

Every check returns problem codes of the form ``<code>:<ids>``: ids and codes only, never text, so a
problem list can be sent back to the planner, stored and logged. An empty list means the answer is valid.
Oversized tasks are warnings, never problems.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Final

from graph.plan.answers import DescribeAnswer, DoneWhenAnswer, ProjectAnswer, TaskAnswer, TasksAnswer
from graph.plan.dag import find_cycle
from graph.plan.identity import ACTION_PATTERN
from graph.plan.models import PlanningPart
from graph.plan.names import plan_vocabularies
from graph.plan.view import PlanView

_REVISION_SUFFIX: Final = re.compile(r"_R\d+$")
_SETUP_PHASE: Final = "setup"
_SPEC_SOURCE: Final = "spec"


def validate_tasks(
    answer: TasksAnswer,
    *,
    view: PlanView,
    part: PlanningPart,
    earlier_actions: Collection[str],
    convention_ids: Collection[str],
) -> list[str]:
    """Problems of a Tasks answer for one planning part."""

    vocabularies = plan_vocabularies()
    members = frozenset(part.members)
    problems = _task_problems(
        answer.tasks,
        view=view,
        allowed_members=members,
        held_actions=earlier_actions,
        known_waits=frozenset(earlier_actions),
    )
    for task in answer.tasks:
        if task.phase == _SETUP_PHASE:
            problems.append(problem("invalid_phase", task.action))
        if task.story not in part.story_ids:
            problems.append(problem("wrong_story", task.action))
    implemented = {spec_id for task in answer.tasks for spec_id in task.implements}
    listed: set[str] = set()
    for entry in answer.not_buildable:
        if entry.spec_id not in members:
            problems.append(problem("unknown_member", "not_buildable", entry.spec_id))
        if entry.reason not in vocabularies["not_buildable_reason"]:
            problems.append(problem("invalid_reason", entry.spec_id))
        if entry.spec_id in implemented:
            problems.append(problem("not_buildable_implemented", entry.spec_id))
        listed.add(entry.spec_id)
    covered = implemented | listed | set(convention_ids)
    problems.extend(
        problem("uncovered_member", spec_id)
        for spec_id in sorted(members)
        if spec_id in view.nodes and view.required(spec_id) and spec_id not in covered
    )
    return problems


def validate_project(answer: ProjectAnswer, *, view: PlanView, setup_members: Collection[str] = ()) -> list[str]:
    """Problems of a Project answer: setup phase, one setup root, acyclic setup waits, known spec ids, and
    every required ``SETUP`` member implemented by a setup or closing task or cited by a convention."""

    plannable = frozenset(view.nodes)
    tasks = [*answer.setup_tasks, *answer.closing_tasks]
    setup_actions = frozenset(task.action for task in answer.setup_tasks)
    problems = _task_problems(tasks, view=view, allowed_members=plannable, held_actions=(), known_waits=setup_actions)
    problems.extend(problem("invalid_phase", task.action) for task in answer.setup_tasks if task.phase != _SETUP_PHASE)
    problems.extend(problem("closing_waits", task.action) for task in answer.closing_tasks if task.waits_for)
    roots = [task.action for task in answer.setup_tasks if not task.waits_for]
    if len(roots) != 1:
        problems.append(problem("setup_roots", str(len(roots))))
    cited = [*answer.profile.spec_ids, *(spec_id for item in answer.profile.conventions for spec_id in item.spec_ids)]
    problems.extend(problem("unknown_member", "profile", spec_id) for spec_id in sorted(set(cited) - plannable))
    covered = {spec_id for task in tasks for spec_id in task.implements} | set(cited)
    problems.extend(
        problem("uncovered_member", spec_id)
        for spec_id in sorted(setup_members)
        if spec_id in view.nodes and view.required(spec_id) and spec_id not in covered
    )
    return problems


def validate_describe(answer: DescribeAnswer, *, view: PlanView, area_id: str) -> list[str]:
    """Problems of a Describe answer: interface kinds and ids, story ids of the area."""

    kinds = plan_vocabularies()["interface_kind"]
    provided = frozenset(view.provided(area_id))
    members = frozenset(view.area(area_id).members)
    stories = frozenset(
        story.story_id for story in view.area_stories(area_id) if story.kind not in ("setup", "closing")
    )
    problems: list[str] = []
    if not answer.title.strip():
        problems.append("empty_title")
    for index, interface in enumerate(answer.interfaces):
        where = interface.spec_id or str(index)
        if interface.kind not in kinds:
            problems.append(problem("invalid_interface_kind", where))
        if interface.spec_id and interface.spec_id not in provided:
            problems.append(problem("unknown_interface", interface.spec_id))
        problems.extend(
            problem("unknown_member", where, spec_id) for spec_id in interface.spec_ids if spec_id not in members
        )
    problems.extend(problem("unknown_story", item.story_id) for item in answer.stories if item.story_id not in stories)
    return problems


def problem(code: str, *ids: str) -> str:
    """A problem code with the ids it concerns, joined by ``:``."""

    return ":".join((code, *ids))


def oversized(tasks: Iterable[TaskAnswer], task_size: int) -> list[str]:
    """Actions of tasks that implement more than ``task_size`` nodes (a warning)."""

    return [task.action for task in tasks if len(task.implements) > task_size]


def _task_problems(
    tasks: Sequence[TaskAnswer],
    *,
    view: PlanView,
    allowed_members: Collection[str],
    held_actions: Collection[str],
    known_waits: Collection[str],
) -> list[str]:
    vocabularies = plan_vocabularies()
    problems: list[str] = []
    seen = set(held_actions)
    for task in tasks:
        if not ACTION_PATTERN.fullmatch(task.action) or _REVISION_SUFFIX.search(task.action):
            problems.append(problem("invalid_action", task.action))
        elif task.action in seen:
            problems.append(problem("duplicate_action", task.action))
        seen.add(task.action)
        if task.phase not in vocabularies["phase"]:
            problems.append(problem("invalid_phase", task.action))
        if task.complexity not in vocabularies["complexity"]:
            problems.append(problem("invalid_complexity", task.action))
        problems.extend(
            problem("unknown_member", task.action, spec_id)
            for spec_id in task.implements
            if spec_id not in allowed_members
        )
        problems.extend(_done_when_problems(task.action, task.done_when, task.implements, view))
    actions = {task.action for task in tasks} | set(known_waits)
    for task in tasks:
        problems.extend(problem("unknown_wait", task.action, wait) for wait in task.waits_for if wait not in actions)
    cycle = find_cycle(
        sorted(actions),
        [(task.action, wait) for task in tasks for wait in task.waits_for if wait in actions],
    )
    if cycle:
        problems.append(problem("wait_cycle", ",".join(cycle)))
    return problems


def _done_when_problems(
    action: str, items: Sequence[DoneWhenAnswer], implements: Sequence[str], view: PlanView
) -> list[str]:
    sources = plan_vocabularies()["done_when_source"]
    if not items:
        return [problem("empty_done_when", action)]
    problems = [problem("invalid_done_when_source", action) for item in items if item.source not in sources]
    with_criteria = any(
        view.rules.acceptance_of(view.nodes[spec_id].attributes) for spec_id in implements if spec_id in view.nodes
    )
    if any(item.source == _SPEC_SOURCE for item in items) and not with_criteria:
        problems.append(problem("spec_done_when_without_criteria", action))
    return problems


def problem_counts(problems: Iterable[str]) -> Mapping[str, int]:
    """Problem codes counted by code (the part before the first ``:``)."""

    counts: dict[str, int] = {}
    for problem in problems:
        code = problem.split(":", 1)[0]
        counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))
