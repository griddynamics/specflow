"""The process-wide resources of the API, built once and closed on shutdown."""

from __future__ import annotations

import asyncio
import functools
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from api.errors import ApiError, database_unavailable, graph_unavailable, required
from api.gap.notice import GapNotice
from api.gap.runner import GapRunner
from api.graph_build.runner import GraphBuildRunner
from api.imports import ImportRunner
from api.jira.connections import AtlassianConnections
from api.jira.runner import JiraSyncRunner
from api.plan.runner import GraphPlanRunner
from api.plan_destinations import PlanDestinations
from db import session as db_session
from db.github import connection_store
from db.models import Project
from db.session import is_database_unreachable
from github.connections import GitHubConnections
from graph.core import InfrastructureUnavailableError
from graph.gap import GapSettings
from graph.stores import build_store
from health import (
    Readiness,
    database_probe,
    graph_probe,
    storage_probe,
    temporal_probe,
)
from jira.http import HttpJiraGateway
from logger.correlation import run_in_context
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import GRAPH_BUILD_SWITCHES, AtlassianOAuthSettings, GraphBuildSettings, JiraModeSettings, RoleSettings
from storage import factory as storage_factory
from temporal import client as temporal_client

logger = get_logger("api.runtime")

DRIVE_POOL_SIZE: Final = 8  # threads of the API's own Drive pool
DRIVE_IDENTITY_POOL_SIZE: Final = 2  # identity reads never queue behind folder calls
DRIVE_TEST_CONCURRENCY: Final = 4  # Drive calls in flight per process (B4)


def agent_error(error: Exception) -> ApiError | None:
    """The API error of an infrastructure failure, else ``None`` (the generic 500 handler answers a bug).

    A graph outage is checked before ``is_database_unreachable``, which counts any ``OSError`` as a database outage.
    """

    if isinstance(error, InfrastructureUnavailableError):
        return graph_unavailable()
    if is_database_unreachable(error):
        return database_unavailable()
    return None


class DriveTimeoutError(Exception):
    """The time budget of a Drive call ran out (waiting for a slot counts)."""


@dataclass
class Runtime:
    """Everything a request handler needs; tests build it from fakes."""

    settings: RoleSettings
    session_factory: Any
    store: Any
    temporal: Any
    graph_store: Any  # graph.core.store.GraphStore; /readyz, the graph build runner and the agent reads use it
    readiness: Readiness
    id_prefix: str = ""  # tier-2 tests prefix workflow ids and task queues
    drive: Any = None  # drive.gateway.DriveGateway; None = Drive unavailable
    drive_pool: ThreadPoolExecutor | None = None
    identity_pool: ThreadPoolExecutor | None = None
    drive_slots: asyncio.Semaphore | None = None
    # api.graph_build.runner.GraphBuildRunner; None = graph build not configured (503)
    graph_builds: Any = None
    # api.plan.runner.GraphPlanRunner; None = plan not configured (503); set with the graph build
    plans: Any = None
    # api.gap.runner.GapRunner; None = gap runs not configured (503); set with the graph build when GAP_ENABLED
    gaps: Any = None
    # api.jira.runner.JiraSyncRunner; None = Jira sync not configured (503); set when the ATLASSIAN_* variables are.
    # In oauth mode it holds the users' Atlassian connections (``jira.connections``).
    jira: Any = None
    # api.slack.app.SlackGaps (started by the app's lifespan, ``start_slack``); None = not configured or not connected
    slack: Any = None
    # github.connections.GitHubConnections; None = the GitHub integration is off (503); GITHUB_APP_CLIENT_ID sets it.
    github: Any = None
    # api.plan_destinations.PlanDestinations: the plan runner's one post-plan hook; Jira is the first destination.
    plan_destinations: Any = None
    # api.imports.ImportRunner: the importers that run in this process (Confluence, Jira epics)
    imports: Any = None
    # api.agent_reads.AgentReads, loaded on the first agent read; independent of the graph build switches
    agent_reads: Any = None
    cleanups: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """One session and one transaction (committed on success).

        A database that cannot be reached becomes ``503 database_unavailable``;
        the log carries the event and the exception class, never driver text.
        """

        try:
            async with self.session_factory() as session, session.begin():
                yield session
        except Exception as error:
            if not is_database_unreachable(error):
                raise
            log_event(
                logger,
                WARNING,
                "api.database.unavailable",
                error_type=type(error).__name__,
            )
            raise database_unavailable() from None

    def _drive_pools(self) -> tuple[ThreadPoolExecutor, ThreadPoolExecutor]:
        if self.drive_pool is None:
            self.drive_pool = ThreadPoolExecutor(DRIVE_POOL_SIZE, "drive-api")
            self.cleanups.append(functools.partial(asyncio.to_thread, self.drive_pool.shutdown, wait=False))
        if self.identity_pool is None:
            self.identity_pool = ThreadPoolExecutor(DRIVE_IDENTITY_POOL_SIZE, "drive-identity")
            self.cleanups.append(functools.partial(asyncio.to_thread, self.identity_pool.shutdown, wait=False))
        if self.drive_slots is None:
            self.drive_slots = asyncio.Semaphore(DRIVE_TEST_CONCURRENCY)
        return self.drive_pool, self.identity_pool

    async def drive_call[T](
        self,
        function: Callable[..., T],
        *args: Any,
        by: float,
        identity: bool = False,
        **kwargs: Any,
    ) -> T:
        """Run a blocking Drive call off the event loop (INT-GDUI-0002/0003).

        ``by`` is a ``time.monotonic()`` value; ``DriveTimeoutError`` is raised when it
        passes. Folder calls run on the ``drive-api`` pool behind a semaphore of
        ``DRIVE_TEST_CONCURRENCY``; waiting for a slot counts against ``by``. The
        slot is released by the thread's own done-callback, not when the caller
        gives up: a thread that cannot be cancelled keeps its slot until it ends,
        so at most 4 Drive threads are in flight. Identity reads (``identity=True``)
        use their own small pool and no slot, so stuck folder calls can never make
        ``/drive/identity`` or the startup read answer ``too_slow``.
        """

        api_pool, identity_pool = self._drive_pools()
        slots = required(self.drive_slots, "the Drive slots exist once the pools are built")
        remaining = by - time.monotonic()
        if remaining <= 0:
            raise DriveTimeoutError
        held = False
        try:
            if not identity:
                await asyncio.wait_for(slots.acquire(), remaining)
                held = True
            loop = asyncio.get_running_loop()
            call = run_in_context(function, *args, **kwargs)  # keeps request_id
            future = loop.run_in_executor(identity_pool if identity else api_pool, call)
        except TimeoutError:
            raise DriveTimeoutError from None
        except BaseException:
            if held:
                slots.release()
            raise

        def finished(done: asyncio.Future[Any]) -> None:
            if held:
                slots.release()
            if not done.cancelled():
                done.exception()  # retrieved: a result nobody awaits is not an error

        future.add_done_callback(finished)
        remaining = by - time.monotonic()
        try:
            return await asyncio.wait_for(asyncio.shield(future), max(remaining, 0.0))
        except TimeoutError:
            raise DriveTimeoutError from None

    async def aclose(self) -> None:
        for cleanup in reversed(self.cleanups):
            await asyncio.shield(cleanup())


RuntimeFactory = Callable[[RoleSettings], Awaitable[Runtime]]


async def build_runtime(settings: RoleSettings) -> Runtime:
    """Engine, ONE storage client, the Temporal client, the graph store and,
    when configured, the graph build runner.

    A Temporal that cannot be reached fails the start (the container restarts);
    the database, storage and graph store are lazy and show up in ``/readyz``.
    """

    storage = required(settings.storage, "the api role requires storage settings")
    required(settings.temporal, "the api role requires temporal settings")
    graph_settings = required(settings.graph_store, "the api role requires graph store settings")
    async with AsyncExitStack() as stack:
        engine = db_session.build_engine(settings.database)
        stack.push_async_callback(engine.dispose)
        store = storage_factory.build_object_store(storage, app_env=settings.app.app_env)
        stack.push_async_callback(store.close)
        graph_store = build_store(graph_settings.to_store_config())
        stack.push_async_callback(graph_store.close)
        connection = await temporal_client.open_connection(settings)
        stack.push_async_callback(asyncio.to_thread, connection.tracing.shutdown)
        readiness = Readiness(
            {
                "database": database_probe(engine),
                "temporal": temporal_probe(connection.client),
                "storage": storage_probe(store),
                "graph": graph_probe(graph_store),
            }
        )
        session_factory = db_session.build_session_factory(engine)
        runtime = Runtime(
            settings=settings,
            session_factory=session_factory,
            store=store,
            temporal=connection.client,
            graph_store=graph_store,
            readiness=readiness,
        )
        if settings.jira is not None:
            _add_jira(runtime, settings.jira, settings, stack)
        if settings.github is not None:
            log_event(logger, INFO, "api.github.mode", mode="on")
            runtime.github = GitHubConnections(settings.github, store=connection_store(runtime.transaction))
            stack.push_async_callback(runtime.github.aclose)
        runtime.imports = ImportRunner(transaction=runtime.transaction, store=store)
        stack.push_async_callback(runtime.imports.aclose)
        runtime.imports.start_recovery()
        if settings.graph_build is None:
            # Build knowledge answers 503 graph_build_unavailable until one of these is set (all three are needed).
            log_event(logger, WARNING, "api.graph_build.disabled", missing=list(GRAPH_BUILD_SWITCHES))
        else:
            _add_runners(runtime, settings.graph_build, settings.gap or GapSettings(), stack)
        runtime.cleanups.append(stack.pop_all().aclose)
    return runtime


async def project_name(session_factory: Any, project_id: str) -> str | None:
    """The project's name for the gap notice; ``None`` when the project is gone."""

    async with session_factory() as session:
        project = await session.get(Project, uuid.UUID(project_id))
    return None if project is None else project.name


def _add_jira(runtime: Runtime, jira: JiraModeSettings, settings: RoleSettings, stack: AsyncExitStack) -> None:
    """The Jira sync runner: of the one Atlassian account (``token``) or of the users' connections (``oauth``, which
    wins when both are set). It does not need the graph build; ``public_url`` is the gap cards' rule (``PUBLIC_URL``
    of the MCP settings, none without them). Startup recovery runs in the background."""

    log_event(logger, INFO, "api.jira.mode", mode=jira.mode)
    if isinstance(jira, AtlassianOAuthSettings):
        gateway, connections = None, AtlassianConnections(jira, transaction=runtime.transaction)
    else:
        gateway, connections = HttpJiraGateway(jira.site_url, jira.email, jira.api_token), None
    runtime.jira = JiraSyncRunner(
        gateway,
        transaction=runtime.transaction,
        store=runtime.graph_store,
        public_url=settings.mcp.public_url if settings.mcp else None,
        site_url=jira.site_url,
        connections=connections,
    )
    stack.push_async_callback(runtime.jira.aclose)
    runtime.jira.start_recovery()


def _add_runners(runtime: Runtime, graph_build: GraphBuildSettings, gap: GapSettings, stack: AsyncExitStack) -> None:
    """The graph build, gap and plan runners of the runtime. The gap runner (``GAP_ENABLED``) hears of every returned
    build and the plan runner waits for its runs; its relational ports use the runtime's transactions, and it applies
    answers through update builds of the graph build runner (``BuildRequests``)."""

    if gap.enabled:
        slack = runtime.settings.slack_notice
        notice = None if slack is None else GapNotice(slack, functools.partial(project_name, runtime.session_factory))
        runtime.gaps = GapRunner(
            graph_build, gap, store=runtime.graph_store, transaction=runtime.transaction, notice=notice
        )
        stack.push_async_callback(runtime.gaps.aclose)
    else:
        log_event(logger, WARNING, "api.gap.disabled")
    runtime.graph_builds = GraphBuildRunner(
        graph_build,
        store=runtime.graph_store,
        session_factory=runtime.session_factory,
        gaps=runtime.gaps,
    )
    if runtime.gaps is not None:  # answers are applied by update builds of this runner
        runtime.gaps.use_builds(runtime.graph_builds)
        runtime.gaps.start_recovery()  # applies a restart left behind (their update builds)
    destinations = PlanDestinations()
    if runtime.jira is not None:
        destinations.register(runtime.jira)
    runtime.plan_destinations = destinations  # the plan runner's one hook: a destination is added here
    runtime.plans = GraphPlanRunner(graph_build, store=runtime.graph_store, gaps=runtime.gaps, jira=destinations)
    # Pushed last, so they run first: plan runs, builds and then gap runs stop before the store and engine close.
    stack.push_async_callback(runtime.graph_builds.aclose)
    stack.push_async_callback(runtime.plans.aclose)
