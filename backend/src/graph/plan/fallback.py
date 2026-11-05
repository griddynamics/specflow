"""Fallbacks used when a planner answer stays invalid or its call fails with a non-fatal error.

Where the area or project has current plan content, the fallback keeps it; else rules write the minimum:
for Tasks, the members of each story grouped by phase and cut into tasks of at most ``task_size`` members,
each titled after its members and described by their statements. What a fallback writes is recorded
``planned_by: rules`` and never stored for reuse.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Final, cast

from graph.plan.identity import PROJECT_AREA_ID, story_id
from graph.plan.models import (
    AreaInterface,
    Convention,
    CurrentPlan,
    DoneWhen,
    PlannedArea,
    PlannedTask,
    PlanningPart,
    Profile,
)
from graph.plan.names import Complexity, InterfaceKind, Phase, plan_vocabularies
from graph.plan.planned import current_tasks, from_current, planned_task
from graph.plan.rules import PhaseRule
from graph.plan.view import PlanView

SETUP_SLUG: Final = "SETUP"
CLOSING_SLUG: Final = "CLOSING"
WORKSPACE_ACTION: Final = "WORKSPACE"
_SEPARATOR: Final = "_"
_PROJECT_TITLE: Final = "Project"
_WORKSPACE_TITLE: Final = "Set up the workspace"
_WORKSPACE_DONE: Final = "The workspace builds and its test command runs"
_DEFAULT_INTERFACE_KIND: Final = "api"
_TITLE_LIMIT: Final = 120
_DESCRIPTION_LIMIT: Final = 3000
_ELLIPSIS: Final = "…"


def setup_story_id() -> str:
    return story_id(PROJECT_AREA_ID, SETUP_SLUG)


def closing_story_id() -> str:
    return story_id(PROJECT_AREA_ID, CLOSING_SLUG)


def spec_interfaces(view: PlanView, area_id: str) -> tuple[AreaInterface, ...]:
    """The spec Interfaces an area provides, as code sets them (no summary); a spec kind outside the plan's
    interface kinds becomes ``api``."""

    kinds = frozenset(plan_vocabularies()["interface_kind"])
    entries = []
    for spec_id in view.provided(area_id):
        node = view.nodes[spec_id]
        kind = next((str(value) for value in node.attributes.values() if str(value) in kinds), _DEFAULT_INTERFACE_KIND)
        entries.append(
            AreaInterface(name=node.name, kind=cast(InterfaceKind, kind), spec_id=spec_id, planned_by="rules")
        )
    return tuple(entries)


def describe_fallback(view: PlanView, area_id: str) -> tuple[PlannedArea, dict[str, str]]:
    """Keep the current description of the area; else title it after its anchor."""

    current = next((area for area in view.snapshot.current.areas if area.area_id == area_id), None)
    rules_interfaces = spec_interfaces(view, area_id)
    if current is not None and current.title:
        kept = tuple(AreaInterface.model_validate(item) for item in current.interfaces)
        known = {item.spec_id for item in kept if item.spec_id}
        interfaces = (*kept, *(item for item in rules_interfaces if item.spec_id not in known))
        summaries = {
            story.story_id: story.summary
            for story in view.snapshot.current.stories
            if story.area_id == area_id and story.summary and story.story_id in view.stories
        }
        area = PlannedArea(
            title=current.title, description=current.description, interfaces=interfaces, planned_by="rules"
        )
        return area, summaries
    anchor = view.area(area_id).anchor
    title = view.nodes[anchor].name if anchor is not None and anchor in view.nodes else _PROJECT_TITLE
    return PlannedArea(title=title, interfaces=rules_interfaces, planned_by="rules"), {}


def project_fallback(
    view: PlanView, setup_members: Collection[str] = ()
) -> tuple[Profile, tuple[PlannedTask, ...], tuple[PlannedTask, ...]]:
    """Keep the current profile, setup and closing tasks; else one ``WORKSPACE`` setup task. Setup members
    no kept task implements go to a rules setup task that waits for the setup root."""

    current = view.snapshot.current
    setup = current_tasks(current, PROJECT_AREA_ID, [setup_story_id()])
    closing = current_tasks(current, PROJECT_AREA_ID, [closing_story_id()])
    profile = _current_profile(current)
    if setup:
        kept_setup = tuple(from_current(view, current, task) for task in setup)
        kept_closing = tuple(from_current(view, current, task) for task in closing)
        implemented = {spec_id for task in (*kept_setup, *kept_closing) for spec_id in task.implements}
        cited = {spec_id for item in profile.conventions for spec_id in item.spec_ids}
        missing = sorted(set(setup_members) - implemented - cited)
        if missing:
            root = next((task.task_key for task in kept_setup if not task.waits), kept_setup[0].task_key)
            extra = _setup_task(
                view,
                action=_free_action(
                    _SEPARATOR.join((WORKSPACE_ACTION, "MEMBERS")), {task.action for task in kept_setup}
                ),
                implements=missing,
                waits=(root,),
            )
            kept_setup = (*kept_setup, extra)
        return profile, kept_setup, kept_closing
    return profile, (_setup_task(view, action=WORKSPACE_ACTION, implements=sorted(setup_members), waits=()),), ()


def _setup_task(view: PlanView, *, action: str, implements: Sequence[str], waits: Sequence[str]) -> PlannedTask:
    return planned_task(
        view,
        area_id=PROJECT_AREA_ID,
        story_id=setup_story_id(),
        action=action,
        phase="setup",
        title=_WORKSPACE_TITLE,
        description=None,
        done_when=[DoneWhen(text=_WORKSPACE_DONE, source="planner"), *_done_when(view, implements)],
        complexity=_phase_complexity(view, "setup"),
        implements=implements,
        waits=waits,
        planned_by="rules",
        prompt_ref=None,
    )


def tasks_fallback(
    view: PlanView,
    part: PlanningPart,
    *,
    area_tasks: Sequence[PlannedTask],
    not_buildable: Collection[str],
    convention_ids: Collection[str],
    task_size: int,
) -> tuple[PlannedTask, ...]:
    """Keep the current tasks of the part's stories; add rules tasks for required members nothing covers.

    ``area_tasks`` are the tasks already planned for earlier parts of the area (their actions are taken).
    Without current tasks every member of the part gets a rules task.
    """

    current = view.snapshot.current
    kept = [from_current(view, current, task) for task in current_tasks(current, part.area_id, part.story_ids)]
    covered = {spec_id for task in kept for spec_id in task.implements} | set(not_buildable) | set(convention_ids)
    if kept:
        missing = [spec_id for spec_id in part.members if view.required(spec_id) and spec_id not in covered]
    else:
        missing = list(part.members)
    taken = {task.action for task in (*area_tasks, *kept)}
    added = rules_tasks(view, part, members=missing, taken=taken, area_tasks=[*area_tasks, *kept], task_size=task_size)
    return (*kept, *added)


def rules_tasks(
    view: PlanView,
    part: PlanningPart,
    *,
    members: Iterable[str],
    taken: Collection[str],
    area_tasks: Sequence[PlannedTask],
    task_size: int,
) -> tuple[PlannedTask, ...]:
    """Per story, the ``members`` of each phase in phase order, cut into tasks of at most ``task_size``
    members (in spec id order), with waits by phase rules. The first task of a story and phase has the
    action ``<PHASE>_<ordinal>``, the next ones the lowest free suffix from ``_2``."""

    wanted = frozenset(members)
    held = set(taken)
    phases = view.rules.phases
    order = {phase.name: index for index, phase in enumerate(phases)}
    created: list[PlannedTask] = []
    for story in (view.stories[part_story] for part_story in part.story_ids if part_story in view.stories):
        by_phase: dict[Phase, list[str]] = {}
        for spec_id in sorted(member for member in story.members if member in wanted and member in part.members):
            by_phase.setdefault(view.phase_of(spec_id), []).append(spec_id)
        for phase in sorted(by_phase, key=lambda item: order.get(item, len(order))):
            waits = _phase_waits(phase, phases, [*area_tasks, *created])
            for implemented in _chunks(by_phase[phase], task_size):
                action = _free_action(_SEPARATOR.join((phase.upper(), str(story.ordinal))), held)
                held.add(action)
                created.append(
                    planned_task(
                        view,
                        area_id=part.area_id,
                        story_id=story.story_id,
                        action=action,
                        phase=phase,
                        title=_rules_title(view, phase, implemented),
                        description=_rules_description(view, implemented),
                        done_when=_done_when(view, implemented),
                        complexity=_phase_complexity(view, phase),
                        implements=implemented,
                        waits=waits,
                        planned_by="rules",
                        prompt_ref=None,
                    )
                )
    return tuple(created)


def _chunks(spec_ids: Sequence[str], size: int) -> list[list[str]]:
    step = max(size, 1)
    return [list(spec_ids[start : start + step]) for start in range(0, len(spec_ids), step)]


def _rules_title(view: PlanView, phase: str, implemented: Sequence[str]) -> str:
    """``<Phase>: <member names>``, cut to the title limit."""

    names = ", ".join(view.nodes[spec_id].name for spec_id in implemented)
    return _cut(f"{phase.capitalize()}: {names}", _TITLE_LIMIT)


def _rules_description(view: PlanView, implemented: Sequence[str]) -> str:
    """One line per implemented member: its name and statement; an assumption is marked as such."""

    lines = ["Implement what these specification items state:"]
    for spec_id in implemented:
        node = view.nodes[spec_id]
        statement = view.statement(spec_id)
        assumed = " (assumed, not committed)" if view.rules.is_assumption(node.attributes) else ""
        lines.append(f"- {node.name}{assumed}: {statement}" if statement else f"- {node.name}{assumed}")
    return _cut("\n".join(lines), _DESCRIPTION_LIMIT)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def _free_action(action: str, held: Collection[str]) -> str:
    if action not in held:
        return action
    suffix = 2
    while _SEPARATOR.join((action, str(suffix))) in held:
        suffix += 1
    return _SEPARATOR.join((action, str(suffix)))


def _phase_waits(phase: Phase, phases: Sequence[PhaseRule], tasks: Sequence[PlannedTask]) -> list[str]:
    rule = next((item for item in phases if item.name == phase), None)
    if rule is None:
        return []
    rules_tasks_by_phase: dict[str, list[str]] = {}
    for task in tasks:
        if task.planned_by == "rules":
            rules_tasks_by_phase.setdefault(task.phase, []).append(task.task_key)
    present = [name for name in rule.after if rules_tasks_by_phase.get(name)]
    if not present:
        return []
    chosen = present if rule.after_mode == "all" else present[:1]
    return sorted(key for name in chosen for key in rules_tasks_by_phase[name])


def _done_when(view: PlanView, implemented: Sequence[str]) -> list[DoneWhen]:
    items: list[DoneWhen] = []
    for spec_id in implemented:
        statement = view.statement(spec_id) or view.nodes[spec_id].name
        items.append(DoneWhen(text=statement, source="planner"))
        items.extend(
            DoneWhen(text=criterion, source="spec")
            for criterion in view.rules.acceptance_of(view.nodes[spec_id].attributes)
        )
    return items


def _phase_complexity(view: PlanView, phase: str) -> Complexity:
    rule = next((item for item in view.rules.phases if item.name == phase), None)
    return cast(Complexity, rule.complexity) if rule is not None else "medium"


def _current_profile(current: CurrentPlan) -> Profile:
    if current.plan is None or not current.plan.profile:
        return Profile()
    data: Mapping[str, object] = json.loads(current.plan.profile)
    conventions = data.get("conventions") or []
    return Profile.model_validate(
        {
            **data,
            "conventions": [Convention.model_validate(item) for item in conventions]
            if isinstance(conventions, list)
            else [],
        }
    )
