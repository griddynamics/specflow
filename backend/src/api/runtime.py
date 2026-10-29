"""The process-wide resources of the API, built once and closed on shutdown."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from api.errors import database_unavailable, required
from api.graph_build.runner import GraphBuildRunner
from db import session as db_session
from graph import core as graph_core
from health import (
    Readiness,
    database_probe,
    neo4j_probe,
    storage_probe,
    temporal_probe,
)
from logger.correlation import run_in_context
from logger.logger import WARNING, get_logger, log_event
from settings import RoleSettings
from storage import factory as storage_factory
from temporal import client as temporal_client

logger = get_logger("api.runtime")

DRIVE_POOL_SIZE: Final = 8  # threads of the API's own Drive pool
DRIVE_IDENTITY_POOL_SIZE: Final = 2  # identity reads never queue behind folder calls
DRIVE_TEST_CONCURRENCY: Final = 4  # Drive calls in flight per process (B4)


class DriveTimeoutError(Exception):
    """The time budget of a Drive call ran out (waiting for a slot counts)."""


_ASYNCPG_CONNECTION_ERRORS: Final = frozenset({"PostgresConnectionError", "InterfaceError"})


def _is_asyncpg_connection_error(error: BaseException) -> bool:
    """``isinstance`` of ``asyncpg.PostgresConnectionError | asyncpg.InterfaceError`` by class name.

    asyncpg ships no type information, so its classes are recognised along the error's MRO.
    """

    return any(
        cls.__module__.split(".", 1)[0] == "asyncpg" and cls.__name__ in _ASYNCPG_CONNECTION_ERRORS
        for cls in type(error).__mro__
    )


def is_database_unreachable(error: BaseException) -> bool:
    """True only for connectivity failures; a programming error stays a bug (500)."""

    if isinstance(error, OperationalError | InterfaceError | OSError):
        return True  # OSError covers ConnectionError, timeouts, DNS failures
    if isinstance(error, DBAPIError):
        return error.connection_invalidated
    return _is_asyncpg_connection_error(error)


@dataclass
class Runtime:
    """Everything a request handler needs; tests build it from fakes."""

    settings: RoleSettings
    session_factory: Any
    store: Any
    temporal: Any
    driver: Any
    readiness: Readiness
    id_prefix: str = ""  # tier-2 tests prefix workflow ids and task queues
    drive: Any = None  # drive.gateway.DriveGateway; None = Drive unavailable
    drive_pool: ThreadPoolExecutor | None = None
    identity_pool: ThreadPoolExecutor | None = None
    drive_slots: asyncio.Semaphore | None = None
    # api.graph_build.runner.GraphBuildRunner; None = graph build not configured (503)
    graph_builds: Any = None
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
    """Engine, ONE storage client, the Temporal client, the Neo4j driver and,
    when configured, the graph build runner.

    A Temporal that cannot be reached fails the start (the container restarts);
    the database, storage and Neo4j are lazy and show up in ``/readyz``.
    """

    storage = required(settings.storage, "the api role requires storage settings")
    required(settings.temporal, "the api role requires temporal settings")
    neo4j = required(settings.neo4j, "the api role requires neo4j settings")
    async with AsyncExitStack() as stack:
        engine = db_session.build_engine(settings.database)
        stack.push_async_callback(engine.dispose)
        store = storage_factory.build_object_store(storage, app_env=settings.app.app_env)
        stack.push_async_callback(store.close)
        driver = graph_core.connect(neo4j.to_graph_config())
        stack.push_async_callback(driver.close)
        connection = await temporal_client.open_connection(settings)
        stack.push_async_callback(asyncio.to_thread, connection.tracing.shutdown)
        readiness = Readiness(
            {
                "database": database_probe(engine),
                "temporal": temporal_probe(connection.client),
                "storage": storage_probe(store),
                "neo4j": neo4j_probe(driver, neo4j.database),
            }
        )
        session_factory = db_session.build_session_factory(engine)
        graph_builds = None
        if settings.graph_build is not None:
            graph_builds = GraphBuildRunner(
                settings.graph_build,
                driver=driver,
                session_factory=session_factory,
                database=neo4j.database,
            )
            # Pushed last, so it runs first: builds stop before the driver and engine close.
            stack.push_async_callback(graph_builds.aclose)
        runtime = Runtime(
            settings=settings,
            session_factory=session_factory,
            store=store,
            temporal=connection.client,
            driver=driver,
            readiness=readiness,
            graph_builds=graph_builds,
        )
        runtime.cleanups.append(stack.pop_all().aclose)
    return runtime
