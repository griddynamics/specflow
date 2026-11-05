"""In-process plan runs of the API: one background task per project (no Temporal yet), as the graph build.

Wired like the graph build runner: file stores under ``GRAPH_STATE_DIR`` (the plan store shares the root with
the build store), the runtime's graph store, ``load_ontology()`` (the plan ontology is merged in), the shipped
plan rules, one ``Gateway`` per run and ``ensure_plan_schema`` once per process, then ``run_plan``.

A run plans from the project's latest finished build. The first plan of a project is a ``first`` run; a plan whose
``last_build_id`` is that build is an ``update`` run with no change reports (the plan reports it unchanged);
a newer build, or a ``replan`` request, re-plans every area from the current graph (the build store keeps no
build chain, so the host passes no change reports: ``docs/graph/plan/api.md`` "Change reports"). A project's
unfinished run (interrupted by a shutdown) is resumed by the next start. ``status`` combines the running task
with the plan store, so it survives a restart.

Logs carry ids, counts, step names and error codes only (``api.plan.*``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from api.contained import Contained
from api.plan.state import NoGraphBuildError, PlanState
from api.schemas import (
    PlanAreaOut,
    PlanContent,
    PlanDoneWhenOut,
    PlanFeatureOut,
    PlanNotBuildableOut,
    PlanPreview,
    PlanQualityOut,
    PlanReviewIssueOut,
    PlanSpecRef,
    PlanStoryOut,
    PlanTaskOut,
)
from graph.build import BuildStore, open_file_stores
from graph.core import Gateway, GraphError, LLMConfig, Ontology, load_ontology
from graph.core.reader import GraphNode, GraphReader
from graph.core.store import GraphStore, as_executor
from graph.plan import (
    STEPS,
    PlanContext,
    PlanEstimate,
    PlanReader,
    PlanResult,
    PlanRules,
    PlanStore,
    PlanTask,
    ensure_plan_schema,
    estimate_plan,
    load_plan_rules,
    open_plan_store,
    run_plan,
)
from graph.plan.models import PlanRunRecord, PlanStepName
from logger.logger import ERROR, INFO, WARNING, get_logger, log_event
from settings import GraphBuildSettings

logger = get_logger("api.plan.runner")

MAX_CONCURRENT_RUNS: Final = 1  # projects planning at once in this process; the rest wait
INTERRUPTED: Final = "interrupted"  # a stored run left running by a stopped process
UNEXPECTED_ERROR: Final = "unexpected_error"
FEATURE_STORY: Final = "feature"  # the story kind of the plan rules whose anchors the preview lists
COMPLETED: Final = "completed"
PENDING: Final = "pending"


class RunnerClosedError(RuntimeError):
    """A plan run was requested after the runner was closed."""


PlanFunction = Callable[[PlanContext], Awaitable[PlanResult]]
EstimateFunction = Callable[[PlanContext], Awaitable[PlanEstimate]]


@dataclass(frozen=True)
class PlanReads:
    """The graph reads of the runner; production reads the graph store, tests pass stubs."""

    plan_reader: Any  # PlanReader: plan, areas, stories, tasks
    graph_reader: Any  # GraphReader: nodes


ReadsFactory = Callable[[GraphStore, Ontology, PlanRules], PlanReads]


def graph_reads(store: GraphStore, ontology: Ontology, rules: PlanRules) -> PlanReads:
    return PlanReads(PlanReader(store, ontology, rules), GraphReader(as_executor(store), ontology))


@dataclass(frozen=True)
class RunnerDependencies:
    """What a runner plans with; the defaults are production's, tests swap in stubs."""

    plan: PlanFunction = run_plan
    estimate: EstimateFunction = estimate_plan
    plans: PlanStore | None = None  # opened lazily under ``GRAPH_STATE_DIR`` when ``None``
    builds: BuildStore | None = None  # opened lazily under ``GRAPH_STATE_DIR`` when ``None``
    ontology: Ontology | None = None  # ``load_ontology()`` on first use when ``None``
    rules: PlanRules | None = None  # the shipped plan rules on first use when ``None``
    ensure: Callable[..., Awaitable[Any]] = ensure_plan_schema
    gateway_factory: Callable[[LLMConfig], Any] = Gateway
    reads: ReadsFactory = graph_reads


@dataclass
class _Run:
    started_at: datetime
    replan: bool
    task: asyncio.Task[None] = field(init=False)
    items: tuple[str, int, int] | None = None  # (step, done, total) of the long step that runs

    def report(self, step: str, done: int, total: int) -> None:
        self.items = (step, done, total)


@dataclass
class _Failure:
    """A failure the plan store may not show (before ``run_plan`` stored a run)."""

    started_at: datetime
    finished_at: datetime
    error_code: str


@dataclass
class _Lazy:
    ontology: Ontology | None = None
    rules: PlanRules | None = None
    plans: PlanStore | None = None
    builds: BuildStore | None = None
    reads: PlanReads | None = None
    schema_ready: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def error_code(error: BaseException) -> str:
    return error.code if isinstance(error, GraphError) else UNEXPECTED_ERROR


class GraphPlanRunner:
    """Starts, tracks and cancels the plan runs of this process; held on ``Runtime.plans``."""

    def __init__(
        self,
        settings: GraphBuildSettings,
        *,
        store: GraphStore,
        dependencies: RunnerDependencies | None = None,
    ) -> None:
        deps = dependencies or RunnerDependencies()
        self._llm: LLMConfig = settings.to_llm_config()
        self._state_dir = Path(settings.state_dir)
        self._store = store
        self._plan = deps.plan
        self._estimate = deps.estimate
        self._ensure = deps.ensure
        self._gateway_factory = deps.gateway_factory
        self._reads_factory = deps.reads
        self._lazy = _Lazy(ontology=deps.ontology, rules=deps.rules, plans=deps.plans, builds=deps.builds)
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        self._runs: dict[str, _Run] = {}
        self._failures: dict[str, _Failure] = {}
        self._closed = False

    # Public ------------------------------------------------------------------

    def running(self, project_id: str) -> bool:
        run = self._runs.get(project_id)
        return run is not None and not run.task.done()

    async def start(self, project_id: str, *, replan: bool = False) -> PlanState:
        """Start the project's plan run, or return the one already running (a double click is safe).

        Raises ``NoGraphBuildError`` when the project has no finished graph build.
        """

        if self._closed:
            raise RunnerClosedError("the plan runner is closed")
        if not self.running(project_id):
            if await self._builds().latest_build(project_id, finished_only=True) is None:
                raise NoGraphBuildError(project_id)
            run = _Run(started_at=datetime.now(UTC), replan=replan)
            run.task = asyncio.create_task(self._run(project_id, run), name=f"plan-{project_id}")
            self._runs[project_id] = run
            self._failures.pop(project_id, None)
        return await self.status(project_id)

    async def status(self, project_id: str) -> PlanState:
        """The running run, else the latest stored run (or a failure before one was stored)."""

        record = await self._plans().latest_run(project_id)
        run = self._runs.get(project_id)
        if run is not None and not run.task.done():
            has_plan = await self._has_plan(project_id)
            if record is not None and not record.finished:  # this run's record, or the run it resumes
                return _with_items(_record_state(record, has_plan=has_plan, status="running"), run.items)
            return PlanState(
                "running", has_plan=has_plan, replan=run.replan, steps_total=len(STEPS), started_at=run.started_at
            )
        has_plan = await self._has_plan(project_id)
        failure = self._failures.get(project_id)
        if failure is not None and (record is None or record.started_at < failure.started_at):
            return PlanState(
                "failed",
                has_plan=has_plan,
                steps_total=len(STEPS),
                started_at=failure.started_at,
                finished_at=failure.finished_at,
                error_code=failure.error_code,
            )
        if record is None:
            return PlanState("idle", has_plan=has_plan)
        if not record.finished:
            return _record_state(record, has_plan=has_plan, status="failed", error_code=INTERRUPTED)
        result = await self._plans().get_result(project_id, record.run_id)
        stored = PlanResult.model_validate(result) if result is not None else None
        return _record_state(
            record,
            has_plan=has_plan,
            status=record.status,
            finished_at=stored.finished_at if stored else None,
            unchanged=stored.unchanged if stored else None,
            error_code=record.failure,
        )

    async def content(self, project_id: str) -> PlanContent | None:
        """The persisted plan, or None when the project has none."""

        reads = await self._reads()
        info = await reads.plan_reader.plan(project_id)
        if info is None:
            return None
        areas = await reads.plan_reader.areas(project_id)
        stories = await reads.plan_reader.stories(project_id)
        tasks = await reads.plan_reader.tasks(project_id)
        nodes = await reads.graph_reader.nodes(project_id)
        rules = await self._rules()
        names = _node_names(nodes)
        quality, review = await self._run_quality(project_id, info.last_run_id, len(tasks))
        return PlanContent(
            plan_id=info.plan_id,
            last_build_id=info.last_build_id,
            updated_at=info.updated_at,
            areas=[
                PlanAreaOut(
                    area_id=area.area_id,
                    kind=area.kind,
                    title=area.title,
                    description=area.description,
                    status=area.status,
                    planned_by=area.planned_by,
                    not_buildable=[_not_buildable(entry, names) for entry in area.not_buildable],
                )
                for area in areas
            ],
            stories=[
                PlanStoryOut(
                    story_id=story.story_id,
                    area_id=story.area_id,
                    kind=story.kind,
                    title=story.title,
                    summary=story.summary,
                    progress=story.progress,
                    status=story.status,
                )
                for story in stories
            ],
            tasks=_task_outs(tasks, nodes, [phase.name for phase in rules.phases]),
            quality=quality,
            review=review,
        )

    async def _run_quality(
        self, project_id: str, run_id: str | None, tasks_planned: int
    ) -> tuple[PlanQualityOut | None, list[PlanReviewIssueOut]]:
        """The result summary and review findings of the run that last wrote the plan."""

        if run_id is None:
            return None, []
        store = self._plans()
        stored = await store.get_result(project_id, run_id)
        if stored is None:
            return None, []
        result = PlanResult.model_validate(stored)
        planning = result.planning
        quality = PlanQualityOut(
            run_id=result.run_id,
            status=result.status,
            finished_at=result.finished_at,
            fallbacks={
                name: counts.fallbacks
                for name, counts in (
                    ("describe", planning.describe),
                    ("project", planning.project),
                    ("tasks", planning.tasks),
                )
                if counts.fallbacks
            },
            tasks_planned=tasks_planned,
            review=dict(planning.review_issues),
            review_failed=planning.review_failed,
            unplaced=result.metrics.unplaced,
            oversized=result.metrics.oversized_task,
        )
        artifact = await store.get_artifact(project_id, run_id, "review")
        issues = artifact.get("issues", []) if artifact is not None else []
        review = [PlanReviewIssueOut.model_validate(issue) for issue in issues if isinstance(issue, dict)]
        return quality, review

    async def preview(self, project_id: str) -> PlanPreview:
        """The features a run would plan and the estimate of that run; no model call, nothing written."""

        latest = await self._builds().latest_build(project_id, finished_only=True)
        if latest is None:
            return PlanPreview()
        ontology, rules, _ = await self._prepare()
        reads = await self._reads()
        labels = sorted(
            {label for rule in rules.types.values() if rule.story == FEATURE_STORY for label in rule.labels}
        )
        nodes = await reads.graph_reader.nodes(project_id, labels=labels) if labels else []
        features = [PlanFeatureOut(spec_id=node.spec_id, name=node.name) for node in nodes]
        gateway = self._gateway_factory(self._llm)
        try:
            context = await self._context(project_id, latest.build_id, gateway, ontology, rules, replan=False)
            with Contained() as estimated:
                estimate = await self._estimate(context)
        finally:
            await gateway.aclose()
        if estimated.error is not None:
            log_event(
                logger,
                WARNING,
                "api.plan.estimate_failed",
                project_id=project_id,
                error_code=error_code(estimated.error),
                error_type=estimated.error_type,
            )
            return PlanPreview(build_id=latest.build_id, features=features)
        return PlanPreview(
            build_id=latest.build_id,
            mode=estimate.mode,
            unchanged=estimate.unchanged,
            calls=sum(step.calls for step in estimate.steps.values()),
            input_tokens=sum(step.input_tokens for step in estimate.steps.values()),
            features=features,
        )

    async def aclose(self) -> None:
        """Cancel every running run; the next start resumes it (plan runs are resumable)."""

        self._closed = True
        tasks = [run.task for run in self._runs.values() if not run.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # Lazy resources ------------------------------------------------------------

    def _plans(self) -> PlanStore:
        if self._lazy.plans is None:
            self._lazy.plans = open_plan_store(self._state_dir)
        return self._lazy.plans

    def _builds(self) -> BuildStore:
        if self._lazy.builds is None:
            self._lazy.builds = open_file_stores(self._state_dir).builds
        return self._lazy.builds

    async def _ontology(self) -> Ontology:
        if self._lazy.ontology is None:
            self._lazy.ontology = await asyncio.to_thread(load_ontology)
        return self._lazy.ontology

    async def _rules(self) -> PlanRules:
        if self._lazy.rules is None:
            ontology = await self._ontology()
            self._lazy.rules = await asyncio.to_thread(load_plan_rules, ontology=ontology)
        return self._lazy.rules

    async def _reads(self) -> PlanReads:
        if self._lazy.reads is None:
            ontology = await self._ontology()
            rules = await self._rules()
            self._lazy.reads = self._reads_factory(self._store, ontology, rules)
        return self._lazy.reads

    async def _prepare(self) -> tuple[Ontology, PlanRules, PlanStore]:
        async with self._lazy.lock:
            ontology = await self._ontology()
            rules = await self._rules()
            if not self._lazy.schema_ready:
                await self._ensure(self._store, ontology, self._llm)
                self._lazy.schema_ready = True
        return ontology, rules, self._plans()

    async def _has_plan(self, project_id: str) -> bool:
        reads = await self._reads()
        return await reads.plan_reader.plan(project_id) is not None

    # The plan task -------------------------------------------------------------

    async def _context(
        self,
        project_id: str,
        build_id: str,
        gateway: Any,
        ontology: Ontology,
        rules: PlanRules,
        *,
        replan: bool,
        run_id: str | None = None,
        on_progress: Callable[[PlanStepName, int, int], None] | None = None,
    ) -> PlanContext:
        """A context for the latest build: ``first`` without a plan, else an update with no change reports,
        which is a ``replan`` when the plan was made from an earlier build."""

        reads = await self._reads()
        info = await reads.plan_reader.plan(project_id)
        stale = info is not None and info.last_build_id != build_id
        return PlanContext(
            project_id=project_id,
            ontology=ontology,
            rules=rules,
            driver=self._store,  # PlanContext takes the store in its (historic) driver field
            gateway=gateway,
            store=self._plans(),
            build_id=build_id,
            replan=info is not None and (replan or stale),
            run_id=run_id,
            on_progress=on_progress,
        )

    async def _run(self, project_id: str, run: _Run) -> None:
        with Contained() as planned:  # a failed run is recorded and logged, never raised
            try:
                async with self._slots:
                    await self._run_one(project_id, run)
            except asyncio.CancelledError:
                log_event(logger, INFO, "api.plan.cancelled", project_id=project_id)
                raise
        if planned.error is None:
            return
        error = planned.error
        code = error_code(error)
        self._failures[project_id] = _Failure(started_at=run.started_at, finished_at=datetime.now(UTC), error_code=code)
        unexpected = not isinstance(error, GraphError)
        log_event(
            logger,
            ERROR if unexpected else WARNING,
            "api.plan.failed",
            project_id=project_id,
            error_code=code,
            error_fields=error.fields if isinstance(error, GraphError) else None,  # ids, counts, sizes
            error_type=planned.error_type,
            exc_info=error if unexpected else None,  # the traceback of an unexpected error only
        )

    async def _run_one(self, project_id: str, run: _Run) -> None:
        ontology, rules, plans = await self._prepare()
        latest_build = await self._builds().latest_build(project_id, finished_only=True)
        if latest_build is None:
            raise NoGraphBuildError(project_id)
        latest_run = await plans.latest_run(project_id)
        resume = latest_run.run_id if latest_run is not None and not latest_run.finished else None
        gateway = self._gateway_factory(self._llm)
        try:
            context = await self._context(
                project_id,
                latest_build.build_id,
                gateway,
                ontology,
                rules,
                replan=run.replan,
                run_id=resume,
                on_progress=run.report,
            )
            log_event(
                logger,
                INFO,
                "api.plan.started",
                project_id=project_id,
                build_id=latest_build.build_id,
                run_id=resume,
                resumed=resume is not None,
                replan=context.replan,
            )
            result = await self._plan(context)
        finally:
            await gateway.aclose()
        usage = gateway.usage().total()
        log_event(
            logger,
            INFO,
            "api.plan.finished",
            project_id=project_id,
            run_id=result.run_id,
            mode=result.mode,
            status=result.status,
            unchanged=result.unchanged,
            failure=result.failure,
            tasks_by_status=result.metrics.tasks_by_status,
            llm_calls=usage.calls,
        )


def _with_items(state: PlanState, items: tuple[str, int, int] | None) -> PlanState:
    """The state with the item counts of its running step, when the run reported them for that step."""

    if items is None or items[0] != state.step:
        return state
    return replace(state, step_items_done=items[1], step_items_total=items[2])


def _record_state(
    record: PlanRunRecord,
    *,
    has_plan: bool,
    status: Any,
    finished_at: datetime | None = None,
    unchanged: bool | None = None,
    error_code: str | None = None,
) -> PlanState:
    done = sum(1 for name in STEPS if record.progress.get(name) == "done")
    current = next((name for name in STEPS if record.progress.get(name) != "done"), STEPS[-1])
    return PlanState(
        status=status,
        has_plan=has_plan,
        run_id=record.run_id,
        mode=record.mode,
        replan=record.replan,
        step=current,
        steps_done=done,
        steps_total=len(STEPS),
        started_at=record.started_at,
        finished_at=finished_at,
        unchanged=unchanged,
        error_code=error_code,
    )


def _task_outs(tasks: Iterable[PlanTask], nodes: Iterable[GraphNode], phases: list[str]) -> list[PlanTaskOut]:
    """Tasks in phase order, then by area, story and id; ``ready`` = pending with every wait completed."""

    by_id: dict[str, GraphNode] = {}
    for node in nodes:
        by_id[node.spec_id] = node
        for alias in node.spec_id_aliases:
            by_id.setdefault(alias, node)
    listed = list(tasks)
    status = {task.task_id: task.status for task in listed}
    rank = {phase: index for index, phase in enumerate(phases)}
    ordered = sorted(listed, key=lambda t: (rank.get(t.phase, len(rank)), t.area_id, t.story_id, t.task_id))
    return [
        PlanTaskOut(
            task_id=task.task_id,
            area_id=task.area_id,
            story_id=task.story_id,
            phase=task.phase,
            title=task.title,
            description=task.description,
            done_when=[PlanDoneWhenOut(text=item.text, source=item.source) for item in task.done_when],
            complexity=task.complexity,
            status=task.status,
            implements=[_spec_ref(spec_id, by_id.get(spec_id)) for spec_id in task.implements],
            waits_for=list(task.waits_for),
            ready=task.status == PENDING and all(status.get(wait) == COMPLETED for wait in task.waits_for),
            revision=task.revision,
            commit=task.commit,
            block_reason=task.block_reason,
            has_open_questions=task.has_open_questions,
            planned_by=task.planned_by,
        )
        for task in ordered
    ]


def _node_names(nodes: Iterable[GraphNode]) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in nodes:
        names[node.spec_id] = node.name
        for alias in node.spec_id_aliases:
            names.setdefault(alias, node.name)
    return names


def _not_buildable(entry: dict[str, Any], names: dict[str, str]) -> PlanNotBuildableOut:
    spec_id = str(entry.get("spec_id", ""))
    note = entry.get("note")
    return PlanNotBuildableOut(
        spec_id=spec_id,
        name=names.get(spec_id),
        reason=str(entry.get("reason", "")),
        note=str(note) if note else None,
    )


def _spec_ref(spec_id: str, node: GraphNode | None) -> PlanSpecRef:
    if node is None:
        return PlanSpecRef(spec_id=spec_id)
    return PlanSpecRef(spec_id=spec_id, label=node.label, name=node.name)
