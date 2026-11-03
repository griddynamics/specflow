"""The Stitch step: waits across areas and from setup and closing work, then one acyclic task graph.

Planners never write waits across areas; code adds them by eight rules, in order. A root task is a task
with no wait inside its area; setup and closing tasks are never roots. A sink is a task no other task of
its group waits for. Rules 2 to 5 use only stated and implied edges: inferred edges place nodes but
never order work. A cycle is broken by dropping the stitch wait in it that was added last, in the order
rule, then area id, then task id.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Final

from graph.plan.dag import find_cycle
from graph.plan.models import PlannedTask, ProjectArtifact, StitchArtifact, TasksArtifact, Wait
from graph.plan.view import PlanView
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.plan")

_ORDERING_BASES: Final = frozenset({"stated", "implied"})
_SETUP_RULE, _INTERFACE_RULE, _INTEGRATION_RULE, _DEPENDENCY_RULE = 1, 2, 3, 4
_DATA_RULE, _DEPLOYMENT_RULE, _CLOSING_RULE = 5, 6, 8


@dataclass
class _Graph:
    """The target tasks and the waits collected so far."""

    tasks: Mapping[str, PlannedTask]
    setup: frozenset[str]
    closing: frozenset[str]
    waits: dict[tuple[str, str], Wait] = field(default_factory=dict)

    def area_tasks(self, area_id: str) -> list[PlannedTask]:
        return [
            task
            for key, task in sorted(self.tasks.items())
            if task.area_id == area_id and key not in self.setup and key not in self.closing
        ]

    def add(self, source: str, target: str, rule: int) -> None:
        if source == target or (source, target) in self.waits:
            return
        waiter, prerequisite = self.tasks[source], self.tasks[target]
        if (
            waiter.phase == "acceptance"
            and prerequisite.phase == "acceptance"
            and waiter.area_id != prerequisite.area_id
        ):
            return  # rule 7: an acceptance task never waits for another area's acceptance task
        self.waits[(source, target)] = Wait(source=source, target=target, kind="stitch", rule=rule)

    def roots(self, area_id: str) -> list[str]:
        tasks = self.area_tasks(area_id)
        inside = {task.task_key for task in tasks}
        return [task.task_key for task in tasks if not set(task.waits) & inside]

    def sinks(self, keys: Iterable[str]) -> list[str]:
        group = set(keys)
        waited = {target for key in group for target in self.tasks[key].waits if target in group}
        return sorted(group - waited)


def stitch(view: PlanView, project: ProjectArtifact, tasks: TasksArtifact) -> StitchArtifact:
    """Every wait of the target plan: planner waits and the stitch rules, with cycles broken."""

    all_tasks = {task.task_key: task for task in (*project.setup, *project.closing, *tasks.tasks)}
    graph = _Graph(
        tasks=all_tasks,
        setup=frozenset(task.task_key for task in project.setup),
        closing=frozenset(task.task_key for task in project.closing),
    )
    planner = [
        Wait(source=task.task_key, target=target, kind="planner")
        for task in all_tasks.values()
        for target in task.waits
        if target in all_tasks and target != task.task_key
    ]
    _setup(view, graph)
    _interfaces(view, graph)
    _integrations(view, graph)
    _dependencies(view, graph)
    _data(view, graph)
    _deployment(view, graph)
    _closing(view, graph)
    waits, dropped = _break_cycles(planner, list(graph.waits.values()), all_tasks)
    for wait in dropped:
        log_event(logger, INFO, "graph.plan.stitch_dropped", source=wait.source, target=wait.target, rule=wait.rule)
    return StitchArtifact(
        waits=tuple(sorted(waits, key=lambda item: (item.source, item.target))),
        dropped=tuple(dropped),
    )


def _setup(view: PlanView, graph: _Graph) -> None:
    sinks = graph.sinks(graph.setup)
    for area in view.active_areas:
        for root in graph.roots(area.area_id):
            for sink in sinks:
                graph.add(root, sink, _SETUP_RULE)


def _ordering(view: PlanView, role: str) -> list[tuple[str, str]]:
    return [(edge.source, edge.target) for edge in view.edges_of(role) if edge.basis in _ORDERING_BASES]


def _phase_tasks(graph: _Graph, area_id: str, *phases: str) -> list[str]:
    """The tasks of the first phase in ``phases`` that has tasks in the area."""

    tasks = graph.area_tasks(area_id)
    for phase in phases:
        found = [task.task_key for task in tasks if task.phase == phase]
        if found:
            return found
    return []


def _implementing(graph: _Graph, area_id: str | None, spec_ids: Iterable[str], phase: str | None = None) -> list[str]:
    wanted = frozenset(spec_ids)
    return [
        key
        for key, task in sorted(graph.tasks.items())
        if (area_id is None or task.area_id == area_id)
        and (phase is None or task.phase == phase)
        and wanted & set(task.implements)
    ]


def _interfaces(view: PlanView, graph: _Graph) -> None:
    for interface, module in _ordering(view, "consumption"):
        provider, consumer = view.provider_area(interface), view.area_of(module)
        if provider is None or consumer is None or provider == consumer:
            continue
        contracts = _implementing(graph, provider, [interface], "contract")
        for waiter in _phase_tasks(graph, consumer, "integration", "logic"):
            for contract in contracts:
                graph.add(waiter, contract, _INTERFACE_RULE)


def _integrations(view: PlanView, graph: _Graph) -> None:
    consumed = {
        (view.area_of(module), view.provider_area(interface)) for interface, module in _ordering(view, "consumption")
    }
    for caller, callee in _ordering(view, "integration"):
        source, target = view.area_of(caller), view.area_of(callee)
        if source is None or target is None or source == target or (source, target) in consumed:
            continue
        prerequisites = _phase_tasks(graph, target, "contract") or [
            key
            for key in graph.sinks(task.task_key for task in graph.area_tasks(target))
            if graph.tasks[key].phase != "acceptance"
        ]
        for waiter in _phase_tasks(graph, source, "integration", "logic"):
            for prerequisite in prerequisites:
                graph.add(waiter, prerequisite, _INTEGRATION_RULE)


def _dependencies(view: PlanView, graph: _Graph) -> None:
    for source, target in _ordering(view, "dependency"):
        for waiter in _implementing(graph, None, [source]):
            for prerequisite in _implementing(graph, None, [target]):
                graph.add(waiter, prerequisite, _DEPENDENCY_RULE)


def _data(view: PlanView, graph: _Graph) -> None:
    owned: dict[str, set[str]] = {}
    for source, target in _ordering(view, "containment"):
        owned.setdefault(target, set()).add(source)
    for source, target in _ordering(view, "reference"):
        owned.setdefault(target, set()).add(source)
        owned.setdefault(source, set()).add(target)
    for entity in sorted(spec_id for spec_id in view.nodes if view.is_type(spec_id, "data_entity")):
        home = view.area_of(entity)
        data_tasks = _implementing(graph, home, [entity], "data")
        users = [key for key in _implementing(graph, None, owned.get(entity, ())) if graph.tasks[key].area_id != home]
        for waiter in users:
            for prerequisite in data_tasks:
                graph.add(waiter, prerequisite, _DATA_RULE)


def _deployment(view: PlanView, graph: _Graph) -> None:
    targets = sorted(
        {area for _, target in _ordering(view, "deploy") if (area := view.area_of(target)) is not None}
        | {area for edge in view.edges_of("deploy") if (area := view.area_of(edge.target)) is not None}
    )
    sinks = [sink for area in targets for sink in graph.sinks(task.task_key for task in graph.area_tasks(area))]
    for closing in sorted(graph.closing):
        if graph.tasks[closing].phase == "acceptance":
            for sink in sinks:
                graph.add(closing, sink, _DEPLOYMENT_RULE)


def _closing(view: PlanView, graph: _Graph) -> None:
    for closing in sorted(graph.closing):
        implemented = set(graph.tasks[closing].implements)
        areas = sorted({area for spec_id in implemented if (area := view.area_of(spec_id)) is not None}) or [
            area.area_id for area in view.active_areas
        ]
        for area in areas:
            for sink in graph.sinks(task.task_key for task in graph.area_tasks(area)):
                graph.add(closing, sink, _CLOSING_RULE)


def _break_cycles(
    planner: Sequence[Wait], stitched: Sequence[Wait], tasks: Mapping[str, PlannedTask]
) -> tuple[list[Wait], list[Wait]]:
    kept = {(wait.source, wait.target): wait for wait in (*planner, *stitched)}
    dropped: list[Wait] = []
    while cycle := find_cycle(tasks, kept):
        in_cycle = [kept[(source, target)] for source, target in pairwise(cycle)]
        candidates = [wait for wait in in_cycle if wait.kind == "stitch"] or in_cycle
        last = max(candidates, key=lambda wait: (wait.rule, tasks[wait.source].area_id, wait.source, wait.target))
        dropped.append(kept.pop((last.source, last.target)))
    return list(kept.values()), dropped
