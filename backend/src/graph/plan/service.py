"""Plan reads for the callers of the plan: the task lists, a feature's tasks, the path to a task and a task's brief.

Each call reads the persisted plan once (``load_plan``) and answers from it with the pure functions below, so a host
(the MCP role, later the REST app) only checks access, pages and renders. A project without a plan, an unknown
feature and an unknown, superseded or retired task raise ``PlanReadError`` with ``no_plan``, ``feature_not_found``
or ``task_not_found``. Superseded and retired tasks are history: never listed, never on a path, never briefed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Final, Protocol

from graph.core.ontology import Ontology
from graph.core.reader import GraphNode, GraphReader
from graph.plan.brief import task_brief
from graph.plan.dag import topological_order, transitive_prerequisites
from graph.plan.errors import PlanReadError, TaskStateError
from graph.plan.invariants import CLOSED
from graph.plan.models import (
    AreaRef,
    FeatureRef,
    FeatureTasks,
    PlanTask,
    StoryRef,
    TaskBrief,
    TaskItem,
    TaskPath,
)
from graph.plan.names import TaskStatus
from graph.plan.reader import LoadedPlan, PlanReader, executor_of, load_plan, spec_aliases, tasks_implementing
from graph.plan.rules import PlanRules

FEATURE_TYPE: Final = "feature"  # the node type whose tasks ``feature_tasks`` lists
NO_PLAN: Final = "no_plan"
FEATURE_NOT_FOUND: Final = "feature_not_found"
TASK_NOT_FOUND: Final = "task_not_found"
_NO_TASK_CODES: Final = frozenset({"unknown_task", "wrong_state"})


class SpecGraphReads(Protocol):
    """The ``GraphReader`` methods the service uses."""

    async def node(self, project_id: str, spec_id: str) -> GraphNode | None: ...

    async def nodes(self, project_id: str) -> list[GraphNode]: ...

    async def scope(self, project_id: str, spec_id: str) -> list[GraphNode]: ...


PlanLoader = Callable[[str], Awaitable[LoadedPlan]]


class PlanService:
    """Project-scoped plan reads over one graph store; tests pass ``graph`` and ``load`` instead of a store."""

    def __init__(
        self,
        store_or_executor: Any,
        ontology: Ontology,
        rules: PlanRules,
        *,
        graph: SpecGraphReads | None = None,
        load: PlanLoader | None = None,
    ) -> None:
        executor = executor_of(store_or_executor, None)
        self._reader = PlanReader(executor, ontology, rules)
        self._graph: SpecGraphReads = graph or GraphReader(executor, ontology)
        self._load: PlanLoader = load or (lambda project_id: load_plan(executor, project_id))
        self._rules = rules
        feature = ontology.node_types.get(FEATURE_TYPE)
        self._feature_label = feature.label if feature is not None else None

    async def list_tasks(
        self,
        project_id: str,
        *,
        ready_only: bool = True,
        status: TaskStatus | None = None,
        area_id: str | None = None,
        story_id: str | None = None,
    ) -> tuple[TaskItem, ...]:
        """See ``list_tasks``."""

        loaded = await self._plan(project_id)
        return list_tasks(loaded, ready_only=ready_only, status=status, area_id=area_id, story_id=story_id)

    async def feature_tasks(self, project_id: str, feature_id: str) -> FeatureTasks:
        """See ``feature_tasks``; ``feature_id`` is the spec id or an alias of a node of the feature type."""

        loaded = await self._plan(project_id)
        node = await self._graph.node(project_id, feature_id)
        if node is None or node.label != self._feature_label:
            raise PlanReadError(FEATURE_NOT_FOUND)
        scope = await self._graph.scope(project_id, node.spec_id)
        aliases = spec_aliases(await self._graph.nodes(project_id))
        return feature_tasks(loaded, node, [item.spec_id for item in scope], aliases, asked=feature_id)

    async def task_path(self, project_id: str, task_id: str) -> TaskPath:
        """See ``task_path``."""

        return task_path(await self._plan(project_id), task_id)

    async def task_brief(self, project_id: str, task_id: str, *, neighbours: int = 30) -> TaskBrief:
        """The brief of one open or completed task (no model call)."""

        loaded = await self._plan(project_id)
        try:
            return await task_brief(self._reader, loaded, project_id, task_id, rules=self._rules, neighbours=neighbours)
        except TaskStateError as error:
            if error.code in _NO_TASK_CODES:
                raise PlanReadError(TASK_NOT_FOUND) from None
            raise

    async def _plan(self, project_id: str) -> LoadedPlan:
        loaded = await self._load(project_id)
        if loaded.current.plan is None:
            raise PlanReadError(NO_PLAN)
        return loaded


def list_tasks(
    loaded: LoadedPlan,
    *,
    ready_only: bool = True,
    status: str | None = None,
    area_id: str | None = None,
    story_id: str | None = None,
) -> tuple[TaskItem, ...]:
    """The open and completed tasks after the filters, ordered by ``task_id``; ``ready_only`` keeps the tasks that can
    start now (lifecycle.md, Ready tasks)."""

    ready = loaded.ready()
    return task_items(
        loaded,
        (
            loaded.plan_task(task)
            for task in loaded.current.tasks
            if task.status not in CLOSED
            and (status is None or task.status == status)
            and (area_id is None or task.area_id == area_id)
            and (story_id is None or task.story_id == story_id)
            and (not ready_only or task.task_id in ready)
        ),
    )


def feature_tasks(
    loaded: LoadedPlan, feature: GraphNode, scope: Iterable[str], aliases: dict[str, str], *, asked: str | None = None
) -> FeatureTasks:
    """The tasks of the active stories ``feature`` anchors and the tasks that implement it or a node of its ``scope``
    (merged ids resolved through ``aliases``), ordered by ``task_id``; ``asked`` is the id the caller used."""

    found = {task.task_id: task for task in tasks_implementing(loaded, [feature.spec_id, *scope], aliases)}
    anchors = {feature.spec_id, *feature.spec_id_aliases, *([asked] if asked else [])}
    for story in loaded.current.stories:
        if story.status == "active" and story.anchor in anchors:
            found.update((task.task_id, loaded.plan_task(task)) for task in loaded.story_tasks(story.story_id))
    return FeatureTasks(
        feature=FeatureRef(spec_id=feature.spec_id, name=feature.name),
        items=task_items(loaded, (task for task in found.values() if task.status not in CLOSED)),
    )


def task_path(loaded: LoadedPlan, task_id: str) -> TaskPath:
    """Every transitive prerequisite of the task in build order (prerequisites first, the lowest id first among equals),
    then the task."""

    target = loaded.tasks.get(task_id)
    if target is None or target.status in CLOSED:
        raise PlanReadError(TASK_NOT_FOUND)
    edges = loaded.edges(loaded.tasks)
    before = transitive_prerequisites([task_id], edges) - {task_id}
    order = [*topological_order(before, edges), task_id]
    items = task_items(loaded, (loaded.plan_task(loaded.tasks[item]) for item in order), ordered=True)
    completed = sum(1 for item in before if loaded.tasks[item].status == "completed")
    return TaskPath(task_id=task_id, items=items, completed=completed)


def task_items(loaded: LoadedPlan, tasks: Iterable[PlanTask], *, ordered: bool = False) -> tuple[TaskItem, ...]:
    """The list items of ``tasks`` with the titles of their areas and active stories, sorted by ``task_id`` unless
    ``ordered`` keeps the given order."""

    ready = loaded.ready()
    areas = {area.area_id: area.title for area in loaded.current.areas}
    stories = {story.story_id: story.title for story in loaded.current.stories if story.status == "active"}
    listed = list(tasks) if ordered else sorted(tasks, key=lambda task: task.task_id)
    return tuple(
        TaskItem(
            task_id=task.task_id,
            title=task.title,
            phase=task.phase,
            status=task.status,
            complexity=task.complexity,
            area=AreaRef(area_id=task.area_id, title=areas.get(task.area_id)),
            story=StoryRef(story_id=task.story_id, title=stories.get(task.story_id)),
            ready=task.task_id in ready,
            waits_for=task.waits_for,
            planned_by=task.planned_by,
        )
        for task in listed
    )
