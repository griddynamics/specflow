"""In-process gap runs of the API: one background task per project (no Temporal yet), as the build and plan runners.

Wired like the plan runner (docs/graph/gap/api.md "Host runner"): the run artifact store under ``GRAPH_STATE_DIR``
(``open_gap_run_store``, beside the build and plan stores), the runtime's graph store, ``load_ontology()`` (the gap
ontology is merged in), ``load_gap_rules`` (``GAP_RULES_PATH`` or the shipped rules), the PostgreSQL ``GapRecords``
and ``Amendments`` on the application database, the ``LoopInvestigator`` over the run's gateway, the plan as
``PlanView`` (``api.gap.plan_view``), one ``Gateway`` per run and ``ensure_gap_schema`` once per process, then
``run_gap``.

Runs of one project never overlap: each project has a queue and one task that drains it; runs of different projects
wait for the process's run slot. Triggers:

- ``after_build``: a returned build queues an ``update`` run, when ``GAP_ENABLED`` and ``GAP_AUTO_RUN_AFTER_BUILD``
  are set and the build neither failed nor lists ``documents_incomplete`` in its settlement. A queued update that has
  not started is replaced by a newer one (its change reports start from the same gap run). It never raises into the
  build.
- ``start_full``: ``POST .../gap/runs`` queues a ``full`` run on the latest finished build (once: a full run queued or
  running is joined).
- ``wait_idle``: the plan runner waits until the project's queue is drained before it plans, so the order after a
  build is build, gap run, plan run (lifecycle.md "Plan link"); a failed gap run is logged and never stops the plan.

- ``start_site``: a gap a person raised (``POST .../gaps``) queues a ``site`` run on it at the head of the queue (never
  beside the running run), on the build of the project's latest finished gap run, so the next update run's change
  reports still start there.
- ``schedule_apply``: an answer moves the project's apply time to the end of its quiet period; when it passes with no
  newer answer, ``GapService.apply_now`` makes the pending amendments active and requests one update build through the
  build runner (``use_builds``; answers.md "Applying answers"). The time lives in this process: after a restart the
  pending answers wait for the next answer or "Apply now".

The ``GapService`` of the routes is built once with the runner's ports, the plan view, the build requests and one
``Gateway`` for the ``gap_edits`` drafts of ``preview_edits`` (closed with the runner).

Every run reads, as ``GapContext.changes``, the change reports of the builds after the build of the project's latest
finished gap run up to its own build (``api.graph_build.reports``, converted by ``api.gap.changes``); the first gap run
of a project has none, and a chain the build store cannot complete (or a latest gap run that failed) gives none and
logs ``api.gap.change_reports_missing`` with the reason. ``GapContext.build`` holds the dedup refusals and cycle cuts
of that build's result. A project's unfinished run (interrupted by a shutdown) is resumed first by the next run of the
project, with the mode and build of its record. ``status`` combines the running task with the run store, so it
survives a restart.

Logs carry ids, counts, step names and error codes only (``api.gap.*``), never a question, quote or document text
(NFR-GSEC-02).
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from api.contained import Contained
from api.gap.changes import build_facts, gap_change_reports
from api.gap.plan_view import PlanReaderView
from api.gap.records import PostgresAmendments, PostgresGapRecords
from api.gap.state import GapRunState
from api.graph_build.reports import reports_since
from api.plan.state import NoGraphBuildError
from graph.build import BuildStore, open_file_stores
from graph.core import GraphError, LLMConfig, Ontology, load_ontology
from graph.core.llm import Gateway
from graph.core.reader import GraphReader
from graph.core.store import GraphStore, as_executor
from graph.gap import (
    RUN_STEPS,
    BuildFacts,
    GapChangeReport,
    GapContext,
    GapRules,
    GapRunRecord,
    GapRunResult,
    GapRunStore,
    GapService,
    GapSettings,
    GapStateError,
    LoopInvestigator,
    NoiseReport,
    ensure_gap_schema,
    load_gap_rules,
    open_gap_run_store,
    run_gap,
)
from graph.plan.reader import PlanReader
from graph.plan.rules import load_plan_rules
from logger.logger import ERROR, INFO, WARNING, get_logger, log_event
from settings import GraphBuildSettings

logger = get_logger("api.gap.runner")

MAX_CONCURRENT_RUNS: Final = 1  # projects running a gap run at once in this process; the rest wait
INTERRUPTED: Final = "interrupted"  # a stored run left running by a stopped process
UNEXPECTED_ERROR: Final = "unexpected_error"
DOCUMENTS_INCOMPLETE: Final = "documents_incomplete"
FAILED: Final = "failed"  # the status of a failed build and of a failed gap run
RUN_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")  # names a folder of the run store: no separator

RequestMode = Literal["update", "full", "site"]
APPLY_ACTOR: Final = "gap-apply"  # the actor of an apply the quiet period triggers
Transaction = Callable[[], Any]  # ``Runtime.transaction``: an async context manager of one session
GapRunFunction = Callable[..., Awaitable[GapRunResult]]
PortsFactory = Callable[[Transaction], tuple[Any, Any]]
ReaderFactory = Callable[[GraphStore, Ontology], Any]
PlanViewFactory = Callable[[GraphStore, Ontology], Any]


class RunnerClosedError(RuntimeError):
    """A gap run was requested after the runner was closed."""


def postgres_ports(transaction: Transaction) -> tuple[PostgresGapRecords, PostgresAmendments]:
    """``GapRecords`` and ``Amendments`` on the application database."""

    return PostgresGapRecords(transaction), PostgresAmendments(transaction)


def graph_reader(store: GraphStore, ontology: Ontology) -> GraphReader:
    return GraphReader(as_executor(store), ontology)


def plan_view(store: GraphStore, ontology: Ontology) -> PlanReaderView:
    """The plan as ``PlanView`` (blocking: reads the shipped plan rules)."""

    executor = as_executor(store)
    rules = load_plan_rules(ontology=ontology)
    return PlanReaderView(PlanReader(executor, ontology, rules), GraphReader(executor, ontology))


@dataclass(frozen=True)
class GapRunnerDependencies:
    """What a runner runs with; the defaults are production's, tests swap in stubs."""

    run: GapRunFunction = run_gap
    runs: GapRunStore | None = None  # opened lazily under ``GRAPH_STATE_DIR`` when ``None``
    builds: BuildStore | None = None  # opened lazily under ``GRAPH_STATE_DIR`` when ``None``
    ontology: Ontology | None = None  # ``load_ontology()`` on first use when ``None``
    rules: GapRules | None = None  # ``load_gap_rules(GAP_RULES_PATH)`` on first use when ``None``
    ensure: Callable[..., Awaitable[Any]] = ensure_gap_schema
    gateway_factory: Callable[[LLMConfig], Any] = Gateway
    ports: PortsFactory = postgres_ports
    investigator: Callable[[Any], Any] = LoopInvestigator  # called with the run's gateway
    executor: Callable[[GraphStore], Any] = as_executor
    reader: ReaderFactory = graph_reader
    plan: PlanViewFactory = plan_view
    service: Callable[..., Any] = GapService


@dataclass(frozen=True)
class _Request:
    mode: RequestMode
    build_id: str | None = None  # ``None``: the latest finished build when the run starts
    site_ids: tuple[str, ...] = ()  # a ``site`` run: the gaps it investigates


@dataclass
class _Active:
    request: _Request
    started_at: datetime
    items: tuple[str, int, int] | None = None  # (step, done, total) of the step that runs

    def report(self, step: str, done: int, total: int) -> None:
        self.items = (step, done, total)


@dataclass
class _Failure:
    """A failure the run store may not show (before ``run_gap`` stored a run)."""

    started_at: datetime
    finished_at: datetime
    error_code: str


@dataclass
class _Project:
    queue: deque[_Request] = field(default_factory=deque)
    task: asyncio.Task[None] | None = None
    active: _Active | None = None
    failure: _Failure | None = None
    apply_at: datetime | None = None  # when the pending answers are applied (end of the quiet period)
    applier: asyncio.Task[None] | None = None


@dataclass
class _Lazy:
    ontology: Ontology | None = None
    rules: GapRules | None = None
    runs: GapRunStore | None = None
    builds: BuildStore | None = None
    plan: Any = None
    service: Any = None
    gateway: Any = None  # the service's gateway (``gap_edits``)
    schema_ready: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def error_code(error: BaseException) -> str:
    return error.code if isinstance(error, GraphError) else UNEXPECTED_ERROR


class GapRunner:
    """Queues, runs and reports the gap runs of this process; held on ``Runtime.gaps``."""

    def __init__(
        self,
        settings: GraphBuildSettings,
        gap: GapSettings,
        *,
        store: GraphStore,
        transaction: Transaction,
        dependencies: GapRunnerDependencies | None = None,
    ) -> None:
        deps = dependencies or GapRunnerDependencies()
        self._llm: LLMConfig = settings.to_llm_config()
        self._state_dir = Path(settings.state_dir)
        self._gap = gap
        self._store = store
        self._transaction = transaction
        self._deps = deps
        self._lazy = _Lazy(ontology=deps.ontology, rules=deps.rules, runs=deps.runs, builds=deps.builds)
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
        self._projects: dict[str, _Project] = {}
        self._builds_port: Any = None  # ``BuildRequests``: the graph build runner (``use_builds``)
        self._closed = False

    def use_builds(self, builds: Any) -> None:
        """The ``BuildRequests`` port the service applies answers through (the graph build runner)."""

        self._builds_port = builds

    # Triggers ------------------------------------------------------------------

    async def after_build(self, project_id: str, build_id: str) -> None:
        """Queue the ``update`` run that follows a returned build; never raises (the build keeps its result)."""

        with Contained() as hooked:
            await self._after_build(project_id, build_id)
        if hooked.failed:
            log_event(
                logger,
                WARNING,
                "api.gap.after_build_failed",
                project_id=project_id,
                build_id=build_id,
                error_code=error_code(hooked.error) if hooked.error is not None else None,
                error_type=hooked.error_type,
            )

    async def start_full(self, project_id: str) -> GapRunState:
        """Queue a ``full`` run, or join the one queued or running (a double click is safe).

        Raises ``NoGraphBuildError`` when the project has no finished graph build.
        """

        if self._closed:
            raise RunnerClosedError("the gap runner is closed")
        if await self._builds().latest_build(project_id, finished_only=True) is None:
            raise NoGraphBuildError(project_id)
        project = self._project(project_id)
        joined = any(request.mode == "full" for request in project.queue) or (
            project.active is not None and project.active.request.mode == "full"
        )
        if not joined:
            self._enqueue(project_id, _Request("full"))
        return await self.status(project_id)

    async def start_site(self, project_id: str, gap_id: str) -> None:
        """Queue a ``site`` run on one gap at the head of the queue (a raised gap gets its options at once)."""

        if self._closed:
            raise RunnerClosedError("the gap runner is closed")
        self._enqueue(project_id, _Request("site", None, (gap_id,)), front=True)

    def schedule_apply(self, project_id: str, at: datetime) -> None:
        """Apply the project's pending answers at ``at`` (the end of the quiet period after its last answer); a later
        time replaces an earlier one."""

        if self._closed:
            return
        project = self._project(project_id)
        project.apply_at = at if project.apply_at is None else max(project.apply_at, at)
        if project.applier is None or project.applier.done():
            project.applier = asyncio.create_task(
                self._apply_when_quiet(project_id, project), name=f"gap-apply-{project_id}"
            )

    def applied(self, project_id: str) -> None:
        """The answers were applied now ("Apply now"): no apply is due any more."""

        project = self._projects.get(project_id)
        if project is not None:
            project.apply_at = None

    async def _apply_when_quiet(self, project_id: str, project: _Project) -> None:
        while project.apply_at is not None and not self._closed:
            wait = (project.apply_at - datetime.now(UTC)).total_seconds()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            project.apply_at = None
            with Contained() as applying:
                service = await self.service()
                result = await service.apply_now(project_id, by=APPLY_ACTOR)
                log_event(
                    logger, INFO, "api.gap.applied", project_id=project_id, gaps=len(result.gap_ids), build=result.build
                )
            if applying.failed:
                error = applying.error
                log_event(
                    logger,
                    INFO if isinstance(error, GapStateError) else WARNING,
                    "api.gap.apply_skipped" if isinstance(error, GapStateError) else "api.gap.apply_failed",
                    project_id=project_id,
                    error_code=error_code(error) if error is not None else None,
                    error_type=applying.error_type,
                )

    async def wait_idle(self, project_id: str) -> None:
        """Return once the project has no gap run queued or running (at once when it has none)."""

        project = self._projects.get(project_id)
        while project is not None and project.task is not None and not project.task.done():
            await asyncio.wait({project.task})

    # Reads ---------------------------------------------------------------------

    async def status(self, project_id: str) -> GapRunState:
        """The running run, else the latest stored run (or a failure before one was stored)."""

        runs = self._runs()
        record = await runs.latest_run(project_id)
        project = self._projects.get(project_id)
        queued = len(project.queue) if project is not None else 0
        active = project.active if project is not None else None
        if active is not None:
            if record is not None and not record.finished:  # this run's record, or the run it resumes
                return _with_items(_record_state(record, "running", queued=queued), active.items)
            return GapRunState(
                "running",
                mode=active.request.mode,
                build_id=active.request.build_id,
                steps_total=len(RUN_STEPS),
                queued=queued,
                started_at=active.started_at,
            )
        failure = project.failure if project is not None else None
        if failure is not None and (record is None or not record.finished or record.started_at < failure.started_at):
            return GapRunState(
                "failed",
                run_id=record.run_id if record is not None and not record.finished else None,
                steps_total=len(RUN_STEPS),
                queued=queued,
                started_at=failure.started_at,
                finished_at=failure.finished_at,
                error_code=failure.error_code,
            )
        stored = await self._stored_state(runs, project_id, record, queued=queued)
        return replace(stored, status="queued") if queued else stored

    async def noise(self, project_id: str, run_id: str) -> NoiseReport | None:
        """The noise report of a finished run of the project, or None when the project has no such run."""

        if RUN_ID.fullmatch(run_id) is None:
            return None
        result = await self._runs().get_result(project_id, run_id)
        return result.noise if result is not None else None

    async def service(self) -> GapService:
        """The project-independent ``GapService`` of the reads (built once)."""

        ontology, rules, _ = await self._prepare()
        if self._lazy.service is None:
            records, amendments = self._deps.ports(self._transaction)
            self._lazy.gateway = self._deps.gateway_factory(self._llm)
            self._lazy.service = self._deps.service(
                self._deps.executor(self._store),
                self._deps.reader(self._store, ontology),
                ontology,
                rules,
                records,
                amendments,
                gateway=self._lazy.gateway,
                builds=self._builds_port,
                settings=self._gap,
                plan=await self._plan_view(ontology),
            )
        service: GapService = self._lazy.service
        return service

    async def aclose(self) -> None:
        """Cancel every queued and running run; the next run of the project resumes it (gap runs are resumable)."""

        self._closed = True
        tasks = [
            task
            for project in self._projects.values()
            for task in (project.task, project.applier)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._lazy.gateway is not None:
            await self._lazy.gateway.aclose()

    # Queue ---------------------------------------------------------------------

    def _project(self, project_id: str) -> _Project:
        return self._projects.setdefault(project_id, _Project())

    def _enqueue(self, project_id: str, request: _Request, *, front: bool = False) -> None:
        """Queue ``request``; ``front`` puts it before the waiting runs (never beside the running one)."""

        project = self._project(project_id)
        if request.mode == "update":  # a waiting update reads the same chain start: the newer build covers it
            project.queue = deque(waiting for waiting in project.queue if waiting.mode != "update")
        if front:
            project.queue.appendleft(request)
        else:
            project.queue.append(request)
        log_event(
            logger,
            INFO,
            "api.gap.queued",
            project_id=project_id,
            mode=request.mode,
            build_id=request.build_id,
            queued=len(project.queue),
        )
        if project.task is None or project.task.done():
            project.task = asyncio.create_task(self._drain(project_id, project), name=f"gap-{project_id}")

    async def _after_build(self, project_id: str, build_id: str) -> None:
        reason = await self._skip_reason(project_id, build_id)
        if reason is not None:
            log_event(logger, INFO, "api.gap.skipped", project_id=project_id, build_id=build_id, reason=reason)
            return
        self._enqueue(project_id, _Request("update", build_id))

    async def _skip_reason(self, project_id: str, build_id: str) -> str | None:
        """Why the build gets no gap run, or None."""

        off = self._switched_off()
        if off is not None:
            return off
        result = await self._builds().get_result(project_id, build_id)
        if result is None:
            return None
        if result.status == FAILED:
            return "build_failed"
        if result.settlement is not None and DOCUMENTS_INCOMPLETE in result.settlement.reasons:
            return DOCUMENTS_INCOMPLETE
        return None

    def _switched_off(self) -> str | None:
        if self._closed:
            return "closed"
        if not self._gap.enabled:
            return "disabled"
        return None if self._gap.auto_run_after_build else "auto_run_off"

    async def _drain(self, project_id: str, project: _Project) -> None:
        while project.queue and not self._closed:
            await self._run(project_id, project, project.queue.popleft())

    # The run -------------------------------------------------------------------

    async def _run(self, project_id: str, project: _Project, request: _Request) -> None:
        active = _Active(request=request, started_at=datetime.now(UTC))
        project.active = active
        project.failure = None
        with Contained() as ran:  # a failed run is recorded and logged, never raised
            try:
                async with self._slots:
                    await self._run_one(project_id, active)
            except asyncio.CancelledError:
                log_event(logger, INFO, "api.gap.cancelled", project_id=project_id)
                raise
            finally:
                project.active = None
        if ran.error is None:
            return
        error = ran.error
        code = error_code(error)
        project.failure = _Failure(started_at=active.started_at, finished_at=datetime.now(UTC), error_code=code)
        unexpected = not isinstance(error, GraphError)
        log_event(
            logger,
            ERROR if unexpected else WARNING,
            "api.gap.failed",
            project_id=project_id,
            mode=request.mode,
            build_id=request.build_id,
            error_code=code,
            error_fields=error.fields if isinstance(error, GraphError) else None,  # ids, counts, codes
            error_type=ran.error_type,
            exc_info=error if unexpected else None,  # the traceback of an unexpected error only
        )

    async def _run_one(self, project_id: str, active: _Active) -> None:
        _, _, runs = await self._prepare()
        request = active.request
        latest = await runs.latest_run(project_id)
        if latest is not None and not latest.finished:
            # The interrupted run first, with its own mode and build, so its stored steps stay consistent.
            await self._execute(
                project_id, active, latest.mode, latest.build_id, run_id=latest.run_id, site_ids=latest.site_ids
            )
            if _covered(latest, request):
                return
        build_id = request.build_id
        if build_id is None and request.mode == "site":  # the build of the latest finished gap run, read now
            last = await self._runs().latest_run(project_id, finished_only=True)
            build_id = last.build_id if last is not None else None
        if build_id is None:
            finished = await self._builds().latest_build(project_id, finished_only=True)
            if finished is None:
                raise NoGraphBuildError(project_id)
            build_id = finished.build_id
        await self._execute(project_id, active, request.mode, build_id, site_ids=request.site_ids)

    async def _execute(
        self,
        project_id: str,
        active: _Active,
        mode: str,
        build_id: str | None,
        *,
        run_id: str | None = None,
        site_ids: tuple[str, ...] = (),
    ) -> None:
        ontology, rules, runs = await self._prepare()
        changes = await self._changes(project_id, build_id)
        facts = await self._facts(project_id, build_id)
        records, amendments = self._deps.ports(self._transaction)
        gateway = self._deps.gateway_factory(self._llm)
        try:
            context = GapContext(
                project_id=project_id,
                executor=self._deps.executor(self._store),
                reader=self._deps.reader(self._store, ontology),
                gateway=gateway,
                ontology=ontology,
                rules=rules,
                records=records,
                amendments=amendments,
                investigator=self._deps.investigator(gateway),
                settings=self._gap,
                changes=changes,
                build=facts,
                plan=await self._plan_view(ontology),
                store=runs,
                run_id=run_id,
                on_progress=active.report,
            )
            log_event(
                logger,
                INFO,
                "api.gap.started",
                project_id=project_id,
                mode=mode,
                build_id=build_id,
                run_id=run_id,
                resumed=run_id is not None,
                change_reports=len(changes),
                refused_pairs=len(facts.refused_pairs),
                cut_edges=len(facts.cut_edges),
            )
            result = await self._deps.run(context, mode=mode, site_ids=site_ids)
        finally:
            await gateway.aclose()
        log_event(
            logger,
            INFO,
            "api.gap.finished",
            project_id=project_id,
            run_id=result.run_id,
            mode=result.mode,
            build_id=result.build_id,
            status=result.status,
            counts=_numbers(result.counts),
            deferred_sites=len(result.deferred_sites),
            held_tasks=len(result.held_tasks),
            warnings=len(result.warnings),
            llm_calls=gateway.usage().total().calls,
        )

    async def _changes(self, project_id: str, build_id: str | None) -> tuple[GapChangeReport, ...]:
        """The gap-side change reports after the build of the latest finished gap run up to ``build_id``; none
        for the first gap run, and none (logged with the reason) when the chain is incomplete."""

        last = await self._runs().latest_run(project_id, finished_only=True)
        if last is None or last.build_id is None or build_id is None:
            return ()
        reason: str | None
        missing_build_id: str | None = None
        if last.status == FAILED:
            reason = "last_gap_run_failed"
        else:
            chain = await reports_since(self._builds(), project_id, last.build_id, build_id)
            if chain.complete:
                return gap_change_reports(chain.links)
            reason, missing_build_id = chain.reason, chain.missing_build_id
        log_event(
            logger,
            WARNING,
            "api.gap.change_reports_missing",
            project_id=project_id,
            build_id=build_id,
            last_build_id=last.build_id,
            last_run_id=last.run_id,
            reason=reason,
            missing_build_id=missing_build_id,
        )
        return ()

    async def _facts(self, project_id: str, build_id: str | None) -> BuildFacts:
        if build_id is None:
            return BuildFacts()
        result = await self._builds().get_result(project_id, build_id)
        return build_facts(result) if result is not None else BuildFacts(build_id=build_id)

    async def _stored_state(
        self, runs: GapRunStore, project_id: str, record: GapRunRecord | None, *, queued: int
    ) -> GapRunState:
        if record is None:
            return GapRunState("idle", queued=queued)
        if not record.finished:
            return _record_state(record, "failed", queued=queued, error_code=INTERRUPTED)
        result = await runs.get_result(project_id, record.run_id)
        if result is None:
            return _record_state(record, record.status, queued=queued)
        return replace(
            _record_state(record, result.status, queued=queued),
            counts=dict(result.counts),
            result=result,
        )

    # Lazy resources ------------------------------------------------------------

    def _runs(self) -> GapRunStore:
        if self._lazy.runs is None:
            self._lazy.runs = open_gap_run_store(self._state_dir)
        return self._lazy.runs

    def _builds(self) -> BuildStore:
        if self._lazy.builds is None:
            self._lazy.builds = open_file_stores(self._state_dir).builds
        return self._lazy.builds

    async def _plan_view(self, ontology: Ontology) -> Any:
        if self._lazy.plan is None:
            self._lazy.plan = await asyncio.to_thread(self._deps.plan, self._store, ontology)
        return self._lazy.plan

    async def _prepare(self) -> tuple[Ontology, GapRules, GapRunStore]:
        async with self._lazy.lock:
            if self._lazy.ontology is None:
                self._lazy.ontology = await asyncio.to_thread(load_ontology)
            ontology = self._lazy.ontology
            if self._lazy.rules is None:
                self._lazy.rules = await asyncio.to_thread(load_gap_rules, self._gap.rules_path, ontology=ontology)
            if not self._lazy.schema_ready:
                await self._deps.ensure(self._store, ontology, self._llm)
                self._lazy.schema_ready = True
        return ontology, self._lazy.rules, self._runs()


def _record_state(record: GapRunRecord, status: Any, *, queued: int = 0, error_code: str | None = None) -> GapRunState:
    done = sum(1 for name in RUN_STEPS if record.progress.get(name) == "done")
    current = next((name for name in RUN_STEPS if record.progress.get(name) != "done"), RUN_STEPS[-1])
    return GapRunState(
        status=status,
        run_id=record.run_id,
        mode=record.mode,
        build_id=record.build_id,
        step=current,
        steps_done=done,
        steps_total=len(RUN_STEPS),
        queued=queued,
        started_at=record.started_at,
        finished_at=record.finished_at,
        error_code=error_code,
    )


def _covered(resumed: GapRunRecord, request: _Request) -> bool:
    """Whether the resumed run already does what ``request`` asks: an update of the same build (a project's first
    update run is recorded as ``full``), or a full run."""

    if request.mode == "update":
        return resumed.build_id == request.build_id
    if request.mode == "site":
        return False
    return resumed.mode == "full"


def _with_items(state: GapRunState, items: tuple[str, int, int] | None) -> GapRunState:
    """The state with the item counts of its running step, when the run reported them for that step."""

    if items is None or items[0] != state.step:
        return state
    return replace(state, step_items_done=items[1], step_items_total=items[2])


def _numbers(counts: Mapping[str, Any]) -> dict[str, Any]:
    """The run metrics that are numbers or maps of numbers (seeds by detector); nothing else reaches a log."""

    kept: dict[str, Any] = {}
    for name, value in counts.items():
        if isinstance(value, int | float) and not isinstance(value, bool):
            kept[name] = value
        elif isinstance(value, Mapping) and all(isinstance(item, int | float) for item in value.values()):
            kept[name] = dict(value)
    return kept
