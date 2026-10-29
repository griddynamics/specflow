"""In-process graph builds of the API: one background task per project (no Temporal yet).

Wired like ``examples/graph_build.py``: file stores under ``GRAPH_STATE_DIR`` with the Postgres
corpus, the runtime's Neo4j driver, ``load_ontology()``, one ``Gateway`` per build, ``ensure_schema``
once per process, then ``run_build``. A project's unfinished build (interrupted by a shutdown, or
failed) is resumed by the next start; a new build retries the follow-up lineages of the latest
finished one. ``status`` combines the running task with the build store, so it survives a restart.

Logs carry ids, counts and error codes only (``api.graph_build.*``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from api.contained import Contained
from api.graph_build.corpus import PostgresCorpusSource
from api.graph_build.state import BuildState
from graph.build import BuildContext, BuildResult, BuildStore, FileStores, open_file_stores, run_build
from graph.core import Gateway, GraphError, LLMConfig, Ontology, ensure_schema, load_ontology
from logger.logger import ERROR, INFO, WARNING, get_logger, log_event
from settings import GraphBuildSettings

logger = get_logger("api.graph_build.runner")

MAX_CONCURRENT_BUILDS: Final = 1  # projects building at once in this process; the rest wait
INTERRUPTED: Final = "interrupted"  # a stored build left running by a stopped process
UNEXPECTED_ERROR: Final = "unexpected_error"


class RunnerClosedError(RuntimeError):
    """A build was requested after the runner was closed."""


BuildFunction = Callable[[BuildContext], Awaitable[BuildResult]]


@dataclass(frozen=True)
class RunnerDependencies:
    """What a runner builds with; the defaults are production's, tests swap in stubs."""

    build: BuildFunction = run_build
    stores: FileStores | None = None  # opened lazily under ``GRAPH_STATE_DIR`` when ``None``
    ontology: Ontology | None = None  # ``load_ontology()`` on first use when ``None``
    ensure: Callable[..., Awaitable[Any]] = ensure_schema
    gateway_factory: Callable[[LLMConfig], Any] = Gateway


@dataclass
class _Run:
    started_at: datetime
    resumed_build_id: str | None = None
    task: asyncio.Task[None] = field(init=False)


@dataclass
class _Failure:
    """A failure the build store may not show (before ``run_build`` stored a result)."""

    started_at: datetime
    finished_at: datetime
    error_code: str
    build_id: str | None = None


@dataclass
class _Lazy:
    ontology: Ontology | None = None
    stores: FileStores | None = None
    schema_ready: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def error_code(error: BaseException) -> str:
    return error.code if isinstance(error, GraphError) else UNEXPECTED_ERROR


def spec_change_counts(result: BuildResult) -> dict[str, int]:
    return {name: len(values) for name, values in result.spec_changes.model_dump().items() if values}


class GraphBuildRunner:
    """Starts, tracks and cancels the builds of this process; held on ``Runtime.graph_builds``."""

    def __init__(
        self,
        settings: GraphBuildSettings,
        *,
        driver: Any,
        session_factory: Callable[[], Any],
        database: str | None,
        dependencies: RunnerDependencies | None = None,
    ) -> None:
        deps = dependencies or RunnerDependencies()
        self._llm: LLMConfig = settings.to_llm_config()
        self._state_dir = Path(settings.state_dir)
        self._driver = driver
        self._database = database
        self._corpus = PostgresCorpusSource(session_factory)
        self._build = deps.build
        self._ensure = deps.ensure
        self._gateway_factory = deps.gateway_factory
        self._lazy = _Lazy(ontology=deps.ontology, stores=deps.stores)
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_BUILDS)
        self._runs: dict[str, _Run] = {}
        self._failures: dict[str, _Failure] = {}
        self._closed = False

    # Public ------------------------------------------------------------------

    def running(self, project_id: str) -> bool:
        run = self._runs.get(project_id)
        return run is not None and not run.task.done()

    async def start(self, project_id: str) -> BuildState:
        """Start the project's build, or return the one already running (a double click is safe)."""

        if self._closed:
            raise RunnerClosedError("the graph build runner is closed")
        if not self.running(project_id):
            run = _Run(started_at=datetime.now(UTC))
            run.task = asyncio.create_task(self._run(project_id, run), name=f"graph-build-{project_id}")
            self._runs[project_id] = run
            self._failures.pop(project_id, None)
        return await self.status(project_id)

    async def status(self, project_id: str) -> BuildState:
        """The running build, else the latest stored build (or a failure before one was stored)."""

        builds = self._stores().builds
        record = await builds.latest_build(project_id)
        run = self._runs.get(project_id)
        if run is not None and not run.task.done():
            if record is not None and (record.build_id == run.resumed_build_id or record.started_at >= run.started_at):
                return BuildState("running", record.build_id, record.mode, run.started_at)
            return BuildState("running", started_at=run.started_at)
        failure = self._failures.get(project_id)
        if failure is not None and (
            record is None or record.finished_at is None or record.finished_at < failure.started_at
        ):
            return BuildState(
                "failed",
                build_id=failure.build_id,
                started_at=failure.started_at,
                finished_at=failure.finished_at,
                error_code=failure.error_code,
            )
        if record is None:
            return BuildState("idle")
        if record.finished_at is None:
            return BuildState("failed", record.build_id, record.mode, record.started_at, error_code=INTERRUPTED)
        return await _stored_state(builds, project_id, record)

    async def aclose(self) -> None:
        """Cancel every running build; the next start resumes it (builds are resumable)."""

        self._closed = True
        tasks = [run.task for run in self._runs.values() if not run.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # The build task ----------------------------------------------------------

    def _stores(self) -> FileStores:
        if self._lazy.stores is None:
            self._lazy.stores = open_file_stores(self._state_dir)
        return self._lazy.stores

    async def _prepare(self) -> tuple[Ontology, FileStores]:
        async with self._lazy.lock:
            if self._lazy.ontology is None:
                self._lazy.ontology = await asyncio.to_thread(load_ontology)
            if not self._lazy.schema_ready:
                await self._ensure(self._driver, self._lazy.ontology, self._llm, self._database)
                self._lazy.schema_ready = True
        return self._lazy.ontology, self._stores()

    async def _run(self, project_id: str, run: _Run) -> None:
        with Contained() as build:  # a failed build is recorded and logged, never raised
            try:
                async with self._slots:
                    await self._run_one(project_id, run)
            except asyncio.CancelledError:
                log_event(logger, INFO, "api.graph_build.cancelled", project_id=project_id)
                raise
        if build.error is None:
            return
        error = build.error
        code = error_code(error)
        self._failures[project_id] = _Failure(
            started_at=run.started_at,
            finished_at=datetime.now(UTC),
            error_code=code,
            build_id=run.resumed_build_id,
        )
        unexpected = not isinstance(error, GraphError)
        log_event(
            logger,
            ERROR if unexpected else WARNING,
            "api.graph_build.failed",
            project_id=project_id,
            build_id=run.resumed_build_id,
            error_code=code,
            error_type=build.error_type,
            exc_info=error if unexpected else None,  # the traceback of an unexpected error only
        )

    async def _run_one(self, project_id: str, run: _Run) -> None:
        ontology, stores = await self._prepare()
        latest = await stores.builds.latest_build(project_id)
        resume = latest.build_id if latest is not None and not latest.finished else None
        run.resumed_build_id = resume
        retry = () if resume else await _follow_ups(stores.builds, project_id)
        log_event(
            logger,
            INFO,
            "api.graph_build.started",
            project_id=project_id,
            build_id=resume,
            resumed=resume is not None,
            retry_lineages=len(retry),
        )
        gateway = self._gateway_factory(self._llm)
        try:
            ctx = BuildContext(
                project_id=project_id,
                ontology=ontology,
                driver=self._driver,
                gateway=gateway,
                registry=stores.registry,
                corpus=self._corpus,
                extraction=stores.extraction,
                builds=stores.builds,
                retry_lineages=retry,
                build_id=resume,
                database=self._database,
            )
            result = await self._build(ctx)
        finally:
            await gateway.aclose()
        usage = gateway.usage().total()
        log_event(
            logger,
            INFO,
            "api.graph_build.finished",
            project_id=project_id,
            build_id=result.build_id,
            mode=result.mode,
            status=result.status,
            settled=result.settled,
            spec_changes=spec_change_counts(result),
            verification_ok=result.verification.ok if result.verification else None,
            skipped_fragments=len(result.skipped_fragments),
            llm_calls=usage.calls,
            duration_s=result.metrics.duration_s,
        )


async def _follow_ups(builds: BuildStore, project_id: str) -> tuple[str, ...]:
    """The follow-up lineages the latest finished build's verification asked for."""

    record = await builds.latest_build(project_id, finished_only=True)
    result = await builds.get_result(project_id, record.build_id) if record else None
    if result is None or result.verification is None:
        return ()
    return tuple(result.verification.follow_up_lineages)


async def _stored_state(builds: BuildStore, project_id: str, record: Any) -> BuildState:
    result = await builds.get_result(project_id, record.build_id)
    if result is None:
        return BuildState(record.status, record.build_id, record.mode, record.started_at, record.finished_at)
    return BuildState(
        status=result.status,
        build_id=result.build_id,
        mode=result.mode,
        started_at=result.started_at,
        finished_at=result.finished_at,
        settled=result.settled,
        spec_changes=spec_change_counts(result),
        verification_ok=result.verification.ok if result.verification else None,
        error_code=result.error_code,
    )
