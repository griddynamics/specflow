"""Plan reads: the plan, areas, stories with derived progress, story detail and work, the DAG and tasks.

Reads return plan data only; spec nodes are named by ``spec_id`` (merged ids resolve to the survivor).
Story progress is derived from the story's tasks at read time, never stored. A task never shows its
claim token.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Literal, cast

from graph.core.ontology import Ontology
from graph.core.reader import GraphEdge, GraphNode, GraphReader
from graph.core.store import Executor, as_executor
from graph.plan.changes import resolve_spec_id
from graph.plan.checks import plan_state
from graph.plan.dag import strongly_connected, to_mermaid, topological_order, transitive_prerequisites
from graph.plan.invariants import CLOSED, OPEN, ready_tasks, story_progress
from graph.plan.models import (
    CurrentPlan,
    CurrentTask,
    DagStory,
    DagTask,
    PlanArea,
    PlanDag,
    PlanInfo,
    PlanStory,
    PlanTask,
    StoryDetail,
    StoryMember,
    StoryWork,
    WorkEntry,
)
from graph.plan.names import AreaStatus, NotBuildableReason, Phase, StoryProgress, StoryStatus, TaskStatus
from graph.plan.rules import PlanRules
from graph.plan.snapshot import read_current_plan

_IN_AREA, _IN_STORY, _WAITS_FOR, _SUPERSEDES = "IN_AREA", "IN_STORY", "WAITS_FOR", "SUPERSEDES"
_STORY_STATUSES = frozenset({"active", "retired"})


@dataclass(frozen=True)
class LoadedPlan:
    """The persisted plan of a project with its wait and revision indexes."""

    current: CurrentPlan

    @cached_property
    def tasks(self) -> dict[str, CurrentTask]:
        return {task.task_id: task for task in self.current.tasks}

    @cached_property
    def waits(self) -> dict[str, tuple[str, ...]]:
        return _index(self.current, _WAITS_FOR)

    @cached_property
    def waited_by(self) -> dict[str, tuple[str, ...]]:
        return _index(self.current, _WAITS_FOR, reverse=True)

    @cached_property
    def successors(self) -> dict[str, str]:
        return {link.target: link.source for link in self.current.links if link.type == _SUPERSEDES}

    @cached_property
    def predecessors(self) -> dict[str, str]:
        return {link.source: link.target for link in self.current.links if link.type == _SUPERSEDES}

    @property
    def applying(self) -> bool:
        return self.current.plan is not None and self.current.plan.applying

    def edges(self, task_ids: Iterable[str]) -> list[tuple[str, str]]:
        """The waits among ``task_ids``."""

        ids = frozenset(task_ids)
        return [(source, target) for source in ids for target in self.waits.get(source, ()) if target in ids]

    def plan_task(self, task: CurrentTask) -> PlanTask:
        return PlanTask(
            **task.model_dump(),
            waits_for=self.waits.get(task.task_id, ()),
            waited_by=self.waited_by.get(task.task_id, ()),
            predecessor=self.predecessors.get(task.task_id),
            successor=self.successors.get(task.task_id),
        )

    def story_tasks(self, story_id: str) -> list[CurrentTask]:
        return [task for task in self.current.tasks if task.story_id == story_id]

    def progress(self, story_id: str) -> StoryProgress:
        return story_progress(task.status for task in self.story_tasks(story_id))

    def ready(self) -> frozenset[str]:
        state = plan_state(self.current, plannable=frozenset(), required=frozenset())
        return frozenset(ready_tasks(state, applying=self.applying))

    def unfinished_prerequisites(self, task_ids: Iterable[str]) -> tuple[str, ...]:
        """Transitive prerequisites that are not completed, in topological order; a closed one (superseded
        or retired after the wait was frozen) is listed too, since it never completes: a plan run is due."""

        found = transitive_prerequisites(task_ids, self.edges(self.tasks))
        unfinished = {task_id for task_id in found if self.tasks[task_id].status != "completed"}
        return topological_order(unfinished, self.edges(unfinished))


def _index(current: CurrentPlan, relation: str, *, reverse: bool = False) -> dict[str, tuple[str, ...]]:
    found: dict[str, set[str]] = {}
    for link in current.links:
        if link.type == relation:
            source, target = (link.target, link.source) if reverse else (link.source, link.target)
            found.setdefault(source, set()).add(target)
    return {key: tuple(sorted(values)) for key, values in found.items()}


async def load_plan(executor: Executor, project_id: str) -> LoadedPlan:
    return LoadedPlan(await read_current_plan(executor, project_id))


def executor_of(source: Any, database: str | None) -> Executor:
    """The executor of a ``GraphStore``, an executor itself or, deprecated, a Neo4j driver on ``database``."""

    return as_executor(source, database)


class PlanReader:
    """Project-scoped reads of the persisted plan."""

    def __init__(
        self, driver_or_executor: Any, ontology: Ontology, rules: PlanRules, *, database: str | None = None
    ) -> None:
        self._executor = executor_of(driver_or_executor, database)
        self._ontology = ontology
        self._rules = rules
        self._graph = GraphReader(self._executor, ontology, database=database)

    async def plan(self, project_id: str) -> PlanInfo | None:
        """The ``Plan`` node, or None before the first plan run."""

        return (await load_plan(self._executor, project_id)).current.plan

    async def areas(self, project_id: str, status: AreaStatus | None = None) -> list[PlanArea]:
        """Areas ordered by id, with members, task counts by status and story counts by progress."""

        loaded = await load_plan(self._executor, project_id)
        members: dict[str, list[str]] = {}
        for link in loaded.current.links:
            if link.type == _IN_AREA:
                members.setdefault(link.target, []).append(link.source)
        found = []
        for area in loaded.current.areas:
            if status is not None and area.status != status:
                continue
            story_counts: dict[str, int] = {}
            for story in loaded.current.stories:
                if story.area_id == area.area_id and story.status == "active":
                    progress = loaded.progress(story.story_id)
                    story_counts[progress] = story_counts.get(progress, 0) + 1
            found.append(
                PlanArea(
                    **area.model_dump(),
                    members=tuple(sorted(members.get(area.area_id, ()))),
                    task_counts=_counts(task.status for task in loaded.current.tasks if task.area_id == area.area_id),
                    story_counts=story_counts,
                )
            )
        return found

    async def stories(
        self,
        project_id: str,
        *,
        area_id: str | None = None,
        status: StoryStatus | StoryProgress | None = None,
        release: str | None = None,
    ) -> list[PlanStory]:
        """Stories ordered by id; ``status`` is a stored status or a derived progress (None: active stories)."""

        loaded = await load_plan(self._executor, project_id)
        found = []
        for story in loaded.current.stories:
            view = story_view(loaded, story.story_id)
            if view is None or (area_id is not None and story.area_id != area_id):
                continue
            if release is not None and story.release != release:
                continue
            if status is None and story.status != "active":
                continue
            if status in _STORY_STATUSES and story.status != status:
                continue
            if (
                status is not None
                and status not in _STORY_STATUSES
                and (story.status != "active" or view.progress != status)
            ):
                continue
            found.append(view)
        return found

    async def story(self, project_id: str, story_id: str) -> StoryDetail | None:
        """A story with its anchor, members (uncovered and not-buildable ones included), tasks in
        topological order, and the stories it waits on and that wait on it."""

        loaded = await load_plan(self._executor, project_id)
        view = story_view(loaded, story_id)
        if view is None:
            return None
        nodes = {node.spec_id: node for node in await self._graph.nodes(project_id)}
        aliases = spec_aliases(nodes.values())
        member_ids = sorted(
            link.source
            for link in loaded.current.links
            if link.type == _IN_AREA and dict(link.properties).get("story") == story_id
        )
        not_buildable = {
            str(entry.get("spec_id")): entry.get("reason")
            for area in loaded.current.areas
            for entry in area.not_buildable
        }
        live = [task for task in loaded.current.tasks if task.status in OPEN or task.status == "completed"]
        members = []
        for spec_id in member_ids:
            node = nodes.get(spec_id)
            implementing = tuple(
                task.task_id for task in live if spec_id in {resolve_spec_id(item, aliases) for item in task.implements}
            )
            members.append(
                StoryMember(
                    spec_id=spec_id,
                    label=node.label if node else None,
                    name=node.name if node else None,
                    task_ids=implementing,
                    not_buildable_reason=cast("NotBuildableReason | None", not_buildable.get(spec_id)),
                    covered=bool(implementing) or spec_id in not_buildable,
                )
            )
        tasks = [task for task in loaded.story_tasks(story_id) if task.status not in CLOSED]
        order = topological_order([task.task_id for task in tasks], loaded.edges(task.task_id for task in tasks))
        anchor = nodes.get(resolve_spec_id(view.anchor, aliases)) if view.anchor else None
        waits_on, waited_by = _story_neighbours(loaded, story_id)
        return StoryDetail(
            story=view,
            anchor=_node_summary(anchor) if anchor is not None else None,
            members=tuple(members),
            tasks=tuple(loaded.plan_task(loaded.tasks[task_id]) for task_id in order),
            waits_on=waits_on,
            waited_by=waited_by,
        )

    async def story_work(self, project_id: str, story_id: str) -> StoryWork | None:
        """The story's open tasks plus their unfinished transitive prerequisites, in topological order."""

        loaded = await load_plan(self._executor, project_id)
        if story_view(loaded, story_id) is None:
            return None
        return story_work(loaded, story_id)

    async def plan_dag(self, project_id: str, output: Literal["json", "mermaid"] = "json") -> PlanDag | str:
        """Open and completed tasks with their waits, grouped by story; ``output="mermaid"`` gives Mermaid
        flowchart text instead, one subgraph per story."""

        loaded = await load_plan(self._executor, project_id)
        live = [task for task in loaded.current.tasks if task.status not in CLOSED]
        ids = [task.task_id for task in live]
        edges = sorted(loaded.edges(ids))
        ordered = topological_order(ids, edges)
        # A wait cycle (a broken plan) leaves its tasks out of the order; they are listed after it, not lost.
        order = (*ordered, *sorted(set(ids) - set(ordered)))
        by_story: dict[str, list[str]] = {}
        for task_id in order:
            by_story.setdefault(loaded.tasks[task_id].story_id, []).append(task_id)
        if output == "mermaid":
            return to_mermaid(by_story, edges)
        story_edges = sorted(
            {
                (loaded.tasks[source].story_id, loaded.tasks[target].story_id)
                for source, target in edges
                if loaded.tasks[source].story_id != loaded.tasks[target].story_id
            }
        )
        return PlanDag(
            tasks=tuple(
                DagTask(
                    task_id=task_id,
                    story_id=loaded.tasks[task_id].story_id,
                    area_id=loaded.tasks[task_id].area_id,
                    status=loaded.tasks[task_id].status,
                    phase=loaded.tasks[task_id].phase,
                )
                for task_id in order
            ),
            edges=tuple(edges),
            stories=tuple(
                DagStory(
                    story_id=story_id,
                    area_id=loaded.tasks[task_ids[0]].area_id,
                    progress=loaded.progress(story_id),
                    task_ids=tuple(task_ids),
                )
                for story_id, task_ids in sorted(by_story.items())
            ),
            story_edges=tuple(story_edges),
            story_groups=strongly_connected(by_story, story_edges),
        )

    async def tasks(
        self,
        project_id: str,
        *,
        status: TaskStatus | None = None,
        area_id: str | None = None,
        story_id: str | None = None,
        phase: Phase | None = None,
    ) -> list[PlanTask]:
        """Tasks ordered by id."""

        loaded = await load_plan(self._executor, project_id)
        return [
            loaded.plan_task(task)
            for task in loaded.current.tasks
            if (status is None or task.status == status)
            and (area_id is None or task.area_id == area_id)
            and (story_id is None or task.story_id == story_id)
            and (phase is None or task.phase == phase)
        ]

    async def task(self, project_id: str, task_id: str) -> PlanTask | None:
        """One task with its prerequisites, dependants, predecessor and successor revision."""

        loaded = await load_plan(self._executor, project_id)
        task = loaded.tasks.get(task_id)
        return loaded.plan_task(task) if task is not None else None

    async def tasks_for(
        self, project_id: str, spec_ids: Sequence[str], *, include_closed: bool = False
    ) -> list[PlanTask]:
        """Tasks that implement any of the given spec nodes (merged ids resolved)."""

        loaded = await load_plan(self._executor, project_id)
        aliases = spec_aliases(await self._graph.nodes(project_id))
        return tasks_implementing(loaded, spec_ids, aliases, include_closed=include_closed)

    async def spec_graph(self, project_id: str) -> tuple[list[GraphNode], list[GraphEdge], dict[str, str]]:
        """The spec nodes, domain edges and alias map of the project, read through ``GraphReader``."""

        nodes = await self._graph.nodes(project_id)
        return nodes, await self._graph.edges(project_id), spec_aliases(nodes)


def tasks_implementing(
    loaded: LoadedPlan, spec_ids: Iterable[str], aliases: dict[str, str], *, include_closed: bool = False
) -> list[PlanTask]:
    """The tasks that implement any of ``spec_ids``, ids resolved through ``aliases`` (see ``spec_aliases``)."""

    wanted = {resolve_spec_id(spec_id, aliases) for spec_id in spec_ids}
    return [
        loaded.plan_task(task)
        for task in loaded.current.tasks
        if (include_closed or task.status not in CLOSED)
        and wanted & {resolve_spec_id(spec_id, aliases) for spec_id in task.implements}
    ]


def story_work(loaded: LoadedPlan, story_id: str) -> StoryWork:
    """The work of one story (see ``PlanReader.story_work``)."""

    open_tasks = [task.task_id for task in loaded.story_tasks(story_id) if task.status in OPEN]
    ids = set(open_tasks) | set(loaded.unfinished_prerequisites(open_tasks))
    ready = loaded.ready()
    return StoryWork(
        story_id=story_id,
        entries=tuple(
            WorkEntry(
                task_id=task_id,
                story_id=loaded.tasks[task_id].story_id,
                area_id=loaded.tasks[task_id].area_id,
                status=loaded.tasks[task_id].status,
                ready=task_id in ready,
                waits_for=loaded.waits.get(task_id, ()),
                claimed_by=loaded.tasks[task_id].claimed_by,
                claimed_at=loaded.tasks[task_id].claimed_at,
            )
            for task_id in topological_order(ids, loaded.edges(ids))
        ),
    )


def story_view(loaded: LoadedPlan, story_id: str) -> PlanStory | None:
    """A story with its derived progress and task counts."""

    story = next((item for item in loaded.current.stories if item.story_id == story_id), None)
    if story is None:
        return None
    return PlanStory(
        **story.model_dump(),
        progress=loaded.progress(story_id),
        task_counts=_counts(task.status for task in loaded.story_tasks(story_id)),
    )


def _story_neighbours(loaded: LoadedPlan, story_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    waits_on: set[str] = set()
    waited_by: set[str] = set()
    for source, target in loaded.edges(task.task_id for task in loaded.current.tasks if task.status not in CLOSED):
        source_story, target_story = loaded.tasks[source].story_id, loaded.tasks[target].story_id
        if source_story == story_id and target_story != story_id:
            waits_on.add(target_story)
        if target_story == story_id and source_story != story_id:
            waited_by.add(source_story)
    return tuple(sorted(waits_on)), tuple(sorted(waited_by))


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def spec_aliases(nodes: Iterable[GraphNode]) -> dict[str, str]:
    return {alias: node.spec_id for node in nodes for alias in node.spec_id_aliases if alias != node.spec_id}


def _node_summary(node: GraphNode) -> dict[str, Any]:
    return {"spec_id": node.spec_id, "label": node.label, "name": node.name, "attributes": dict(node.attributes)}
