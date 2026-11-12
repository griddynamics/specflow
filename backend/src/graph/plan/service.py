"""Plan reads for the callers of the plan: the task lists, a feature's tasks, the path to a task and a task's brief.

Each call reads the persisted plan once (``load_plan``) and answers from it with the pure functions below, so a host
(the MCP role, later the REST app) only checks access, pages and renders. A project without a plan, an unknown
feature and an unknown, superseded or retired task raise ``PlanReadError`` with ``no_plan``, ``feature_not_found``
or ``task_not_found``. Superseded and retired tasks are history: never listed, never on a path, never briefed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Protocol, Required, TypedDict, Unpack

from graph.core.ontology import Ontology
from graph.core.reader import GraphNode, GraphReader
from graph.core.trace import Basis, Direction, TraceResult
from graph.plan.block_guard import check_block
from graph.plan.brief import open_questions, task_brief
from graph.plan.brief_models import StoryBrief
from graph.plan.claims import StoryClaims
from graph.plan.dag import topological_order, transitive_prerequisites
from graph.plan.errors import PlanReadError, TaskStateError
from graph.plan.findings_port import FindingsReads, open_counts_of, open_findings_about
from graph.plan.invariants import CLOSED
from graph.plan.models import (
    FeatureRef,
    FeatureTasks,
    TaskBrief,
    TaskItem,
    TaskPath,
)
from graph.plan.names import BlockReason, TaskProgress, TaskStatus
from graph.plan.node_plan import NodePlan, node_plan, plan_states
from graph.plan.reader import (
    LoadedPlan,
    PlanReader,
    executor_of,
    load_plan,
    spec_aliases,
    tasks_implementing,
)
from graph.plan.rules import PlanRules
from graph.plan.story_brief import story_brief
from graph.plan.story_findings import StoryFindings, story_findings
from graph.plan.story_models import (
    AreaSummary,
    ClaimRow,
    StoryClaim,
    StoryCompletion,
    StoryPreview,
    StoryRelease,
    StoryRow,
    StoryTask,
)
from graph.plan.story_reads import area_summary, claim_list, story_list, story_preview
from graph.plan.task_items import task_items
from graph.plan.trace_models import PlanState, PlanTrace
from graph.plan.trace_overlay import (
    check_seeds,
    overlay,
    question_nodes,
    story_seeds,
    tasks_by_node,
)

FEATURE_TYPE: Final = "feature"  # the node type whose tasks ``feature_tasks`` lists
NO_PLAN: Final = "no_plan"
FEATURE_NOT_FOUND: Final = "feature_not_found"
TASK_NOT_FOUND: Final = "task_not_found"
_NO_TASK_CODES: Final = frozenset({"unknown_task", "wrong_state"})


class TraceOptions(TypedDict, total=False):
    """The options of ``trace`` (trace.md "Plan overlay"): ``direction`` is required, the rest has defaults."""

    direction: Required[Direction]
    depth: int
    basis: Basis
    relations: Sequence[str] | None
    include_owner: bool
    limit: int


TRACE_DEPTH: Final = 2
TRACE_BASIS: Final = "trusted"
TRACE_LIMIT: Final = 100


class SpecGraphReads(Protocol):
    """The ``GraphReader`` methods the service uses."""

    async def node(self, project_id: str, spec_id: str) -> GraphNode | None: ...

    async def nodes(self, project_id: str) -> list[GraphNode]: ...

    async def scope(self, project_id: str, spec_id: str) -> list[GraphNode]: ...

    async def trace(
        self,
        project_id: str,
        spec_ids: Sequence[str],
        *,
        direction: Direction,
        depth: int,
        basis: Basis,
        relations: Sequence[str] | None,
        include_owner: bool,
        limit: int,
    ) -> TraceResult: ...


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
        clock: Callable[[], datetime] | None = None,
        findings: FindingsReads | None = None,
    ) -> None:
        self._findings = findings
        executor = executor_of(store_or_executor, None)
        self._claims = StoryClaims(executor, clock=clock)
        self._graph: SpecGraphReads = graph or GraphReader(executor, ontology)
        self._reader = PlanReader(executor, ontology, rules, graph=self._graph)
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

    async def story_brief(self, project_id: str, story_id: str, *, neighbours: int = 30) -> StoryBrief:
        """The brief of one active story: its tasks' briefs merged, from one read, without a model call; the open
        findings about the story's nodes come from the findings port when the service has one."""

        loaded = await self._plan(project_id)
        brief = await story_brief(
            self._reader,
            loaded,
            project_id,
            story_id,
            rules=self._rules,
            neighbours=neighbours,
        )
        anchor = [brief.anchor["spec_id"]] if brief.anchor else []
        found = await open_findings_about(
            self._findings,
            project_id,
            [*anchor, *(item.spec_id for item in brief.implements)],
        )
        return brief.model_copy(update={"findings": found})

    async def trace(
        self,
        project_id: str,
        *,
        story_id: str | None = None,
        spec_ids: Sequence[str] | None = None,
        **options: Unpack[TraceOptions],
    ) -> PlanTrace:
        """The graph trace from a story's nodes (``story_id``) or from ``spec_ids`` with each node's tasks, plan state
        and open finding count. A project without a plan still traces (plan fields null); only ``story_id`` needs a
        plan (``no_plan``), and a retired or unknown story raises ``story_not_found``."""

        check_seeds(story_id, spec_ids)
        loaded = await self._load(project_id)
        nodes_list, edges, aliases = await self._reader.spec_graph(project_id)
        seeds, cut = story_seeds(loaded, story_id, aliases) if story_id is not None else (tuple(spec_ids or ()), False)
        result = await self._graph.trace(
            project_id,
            seeds,
            direction=options["direction"],
            depth=options.get("depth", TRACE_DEPTH),
            basis=options.get("basis", TRACE_BASIS),
            relations=options.get("relations"),
            include_owner=options.get("include_owner", True),
            limit=options.get("limit", TRACE_LIMIT),
        )
        shown = [
            *result.sources,
            *(owner.spec_id for owner in result.context),
            *(n.spec_id for n in result.nodes),
        ]
        counts = await open_counts_of(self._findings, project_id, shown)
        nodes = {node.spec_id: node for node in nodes_list}
        questions = open_questions(question_nodes(result, nodes), nodes, edges, self._rules)
        return overlay(
            result,
            loaded,
            tasks_by_node(loaded, aliases),
            counts,
            questions,
            traced_story=story_id,
            seeds_truncated=cut,
        )

    async def story_nodes(self, project_id: str, story_id: str) -> tuple[str, ...]:
        """The anchor and the nodes the story's open and completed tasks implement (current ids, at most 50): where a
        search near the story starts. ``no_plan`` and ``story_not_found`` as for ``trace``."""

        loaded = await self._load(project_id)
        _, _, aliases = await self._reader.spec_graph(project_id)
        return story_seeds(loaded, story_id, aliases)[0]

    async def node_plan(self, project_id: str, spec_id: str, aliases: Sequence[str] = ()) -> NodePlan | None:
        """The plan state and tasks of one node (``spec_id`` and its ``aliases``), or ``None`` for a project without a
        plan."""

        return node_plan(await self._load(project_id), {spec_id, *aliases})

    async def plan_states(self, project_id: str, nodes: Mapping[str, Sequence[str]]) -> dict[str, PlanState] | None:
        """The plan state of each node (``spec_id`` to its aliases) from one plan read, or ``None`` without a plan."""

        return plan_states(await self._load(project_id), nodes)

    async def area_summary(self, project_id: str) -> tuple[AreaSummary, ...]:
        """Per active area, the project area last: stories per state, holders of held stories, open tasks."""

        return area_summary(await self._plan(project_id))

    async def story_list(
        self,
        project_id: str,
        *,
        view: str = "open",
        caller: str,
        area_id: str | None = None,
        finding_ids: Collection[str] = (),
    ) -> tuple[StoryRow, ...]:
        """The active stories as rows of a view: ``open``, ``mine`` (held by ``caller``, or blocked by one of
        ``finding_ids``) or ``done``."""

        loaded = await self._plan(project_id)
        return story_list(loaded, view=view, caller=caller, area_id=area_id, finding_ids=finding_ids)

    async def claims(self, project_id: str) -> tuple[ClaimRow, ...]:
        """The claims board: story claims and tasks claimed on their own, oldest first; holders are user ids."""

        return claim_list(await self._plan(project_id))

    async def story_findings(self, project_id: str, story_ids: Sequence[str]) -> tuple[StoryFindings, ...]:
        """Per story: the open findings about it and the nodes its tasks implement (count, oldest blocking one); none
        without a findings port."""

        return await story_findings(await self._plan(project_id), self._findings, project_id, story_ids)

    async def story_preview(self, project_id: str, story_id: str) -> StoryPreview:
        """The story before a claim: state, holder, unit, open tasks, blockers and what it unblocks, and the open
        findings about the nodes of those tasks when the service has a findings port."""

        loaded = await self._plan(project_id)
        preview = story_preview(loaded, story_id)
        open_ids = {task.task_id for task in preview.tasks}
        ids = [spec_id for task in loaded.current.tasks if task.task_id in open_ids for spec_id in task.implements]
        found = await open_findings_about(self._findings, project_id, ids)
        return preview.model_copy(update={"findings": found})

    async def claim_story(
        self,
        project_id: str,
        story_id: str,
        *,
        claimed_by: str,
        agent_label: str | None = None,
        branch: str | None = None,
    ) -> StoryClaim:
        """Claim a story (its whole cycle group) under one token; the holder claiming again gets the same token."""

        return await self._claims.claim(
            project_id,
            story_id,
            claimed_by=claimed_by,
            agent_label=agent_label,
            branch=branch,
        )

    async def release_story(
        self,
        project_id: str,
        story_id: str,
        *,
        claimed_by: str | None = None,
        claim_token: str | None = None,
        force: bool = False,
        note: str | None = None,
        branch: str | None = None,
    ) -> StoryRelease:
        """End the claim ``claimed_by`` holds under ``claim_token``; ``force=True`` is a host operation that frees a
        forgotten claim (no caller or token)."""

        return await self._claims.release(
            project_id,
            story_id,
            claimed_by=claimed_by,
            claim_token=claim_token,
            force=force,
            note=note,
            branch=branch,
        )

    async def report_progress(
        self,
        project_id: str,
        task_id: str,
        *,
        claimed_by: str,
        claim_token: str,
        progress: TaskProgress,
        note: str | None = None,
    ) -> StoryTask:
        """Report ``progress`` of a claimed task of a story ``claimed_by`` holds; the status stays ``in_progress``."""

        return await self._claims.report_progress(
            project_id, task_id, claimed_by=claimed_by, claim_token=claim_token, progress=progress, note=note
        )

    async def complete_story(
        self,
        project_id: str,
        story_id: str,
        *,
        claim_token: str,
        completed_by: str,
        merge_commit: str,
        pr_url: str | None = None,
        done: Collection[str] | None = None,
    ) -> StoryCompletion:
        """Complete the claimed tasks in ``done`` (default all) with the merge commit; end the claim ``completed_by``
        holds."""

        result = await self._claims.complete(
            project_id,
            story_id,
            claim_token=claim_token,
            completed_by=completed_by,
            merge_commit=merge_commit,
            pr_url=pr_url,
            done=done,
        )
        changed = () if result.repeat else await self._spec_changed(project_id, result.implements)
        return StoryCompletion(
            completed=result.completed,
            released=result.released,
            now_available=result.now_available,
            spec_changed=changed,
            repeat=result.repeat,
        )

    async def check_block(
        self,
        project_id: str,
        story_id: str,
        *,
        claimed_by: str,
        claim_token: str,
        spec_ids: Sequence[str],
    ) -> None:
        """The guard of a blocking finding (a host call): ``claimed_by`` holds the story under ``claim_token`` and a
        node of ``spec_ids`` is implemented by one of its tasks; else ``TaskStateError("claim_mismatch")``."""

        loaded = await self._load(project_id)
        aliases = spec_aliases(await self._graph.nodes(project_id))
        check_block(loaded, aliases, story_id, claimed_by=claimed_by, claim_token=claim_token, spec_ids=spec_ids)

    async def block_story(
        self,
        project_id: str,
        story_id: str,
        *,
        claim_token: str,
        reason: BlockReason,
        finding_id: str,
    ) -> StoryRelease:
        """Block the claimed tasks for a blocking finding and end the claim (a host call)."""

        return await self._claims.block(
            project_id,
            story_id,
            claim_token=claim_token,
            reason=reason,
            finding_id=finding_id,
        )

    async def unblock_finding(self, project_id: str, finding_id: str) -> tuple[str, ...]:
        """Return the tasks still blocked by the finding to ``pending`` (a host call); their ids."""

        return await self._claims.unblock_finding(project_id, finding_id)

    async def _spec_changed(self, project_id: str, spec_ids: Iterable[str]) -> tuple[str, ...]:
        """The spec ids a build wrote after the plan's consumed builds (the task brief's ``spec_changed`` rule)."""

        plan = (await self._load(project_id)).current.plan
        consumed = set(plan.consumed_build_ids) if plan is not None else set()
        found: dict[str, None] = {}
        for spec_id in spec_ids:
            node = await self._graph.node(project_id, spec_id)
            if node is not None and node.last_build_id is not None and node.last_build_id not in consumed:
                found[node.spec_id] = None
        return tuple(found)

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
