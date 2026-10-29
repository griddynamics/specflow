"""The worker role: one ``Worker`` per polled queue on one plugin client.

``run(settings)`` is the work-module contract of ``specflow worker``. SIGTERM
(or SIGINT) stops the process in this order: graceful worker shutdown (up to
130 s for running activities), then tracing flush and shutdown. The process
entrypoint (``cli.main``) calls ``logger.close()`` exactly once, after
``run`` returned; this module never closes the logger.
"""

from __future__ import annotations

import asyncio
import inspect
import signal
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import timedelta
from typing import Any, Final

from temporalio.client import Client
from temporalio.worker import Worker

from agent_runtime.tracing import TracingHandle
from logger.logger import ERROR, INFO, get_logger, log_event
from settings import RoleSettings
from temporal import diagnostics, health_server
from temporal.client import open_connection
from temporal.containment import Contain
from temporal.queues import polled_queues
from temporal.registry import Registration, load_registration

logger = get_logger("temporal.worker")

GRACEFUL_SHUTDOWN: Final = timedelta(seconds=130)
#: One thread per executor: a long sync activity must not block another queue.
SINGLE_THREAD: Final = 1
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1


def has_sync_activities(registration: Registration) -> bool:
    """True when any of the queue's own activities is a plain ``def``.

    No shipped registration has one today (every activity is ``async def``);
    the executor path below stays as a tested seam for a future sync activity.
    """

    return any(not inspect.iscoroutinefunction(fn) for fn in registration.activities)


def build_executors(
    registrations: dict[str, Registration],
) -> dict[str, ThreadPoolExecutor | None]:
    """One 1-thread executor per queue that has sync activities, else ``None``.

    Never shared between queues: a long sync ``parse_document`` must not block
    the other queue. ``max_concurrent_activities`` stays unset on purpose: the
    async activities of a queue do not use the executor, so capping the slots
    at the thread count would throttle them for nothing.
    """

    return {
        queue: ThreadPoolExecutor(max_workers=SINGLE_THREAD, thread_name_prefix=f"activity-{queue}")
        if has_sync_activities(registration)
        else None
        for queue, registration in registrations.items()
    }


def build_workers(
    client: Client,
    settings: RoleSettings,
    registrations: dict[str, Registration],
    executors: dict[str, ThreadPoolExecutor | None],
    *,
    worker_factory: Callable[..., Any] | None = None,
) -> list[Any]:
    """One worker per polled queue; ``registrations`` and ``executors`` are keyed by queue."""

    if settings.temporal is None:
        raise ValueError("The worker role needs Temporal settings")
    factory = worker_factory or Worker
    workers = []
    for queue, task_queue in polled_queues(settings.temporal):
        options: dict[str, Any] = {}
        if executors.get(queue) is not None:
            options["activity_executor"] = executors[queue]
        workers.append(
            factory(
                client,
                task_queue=task_queue,
                workflows=list(registrations[queue].workflows),
                activities=list(registrations[queue].activities),
                graceful_shutdown_timeout=GRACEFUL_SHUTDOWN,
                **options,
            )
        )
    return workers


async def shutdown_in_order(
    workers: Sequence[Any],
    tasks: Sequence[asyncio.Task[Any]],
    tracing: TracingHandle,
    *,
    registrations: Sequence[Registration] = (),
) -> None:
    """Graceful worker shutdown, registrations' ``close``, then tracing flush + shutdown."""

    await asyncio.gather(*(worker.shutdown() for worker in workers), return_exceptions=True)
    await asyncio.gather(*tasks, return_exceptions=True)
    log_event(logger, INFO, "worker.stopped")
    for registration in registrations:
        if registration.close is not None:
            with suppress(Exception):
                await registration.close()
    await asyncio.to_thread(tracing.shutdown)


async def _wait_for_stop(stop: asyncio.Event, tasks: Sequence[asyncio.Task[Any]]) -> None:
    """Return when a signal arrives or any worker task ends."""

    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait([stopper, *tasks], return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopper.cancel()


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, stop.set)


async def run(settings: RoleSettings) -> int:
    """Poll the configured queues until SIGTERM; returns the process exit code."""

    temporal = settings.temporal
    if temporal is None:
        raise ValueError("The worker role needs Temporal settings")
    queues = polled_queues(temporal)
    registrations: dict[str, Registration] = {}
    executors: dict[str, ThreadPoolExecutor | None] = {}
    handle = TracingHandle(enabled=False)
    workers: list[Any] = []
    tasks: list[asyncio.Task[Any]] = []
    stop = asyncio.Event()
    _install_signal_handlers(stop)  # before connecting: a signal then still runs the cleanup
    # Liveness for the platform probe; up until the drain below has finished.
    health = health_server.start(settings.app.worker_health_port)
    # TEMPORARY (worker-parse freeze investigation): after the signal handlers,
    # so the faulthandler SIGTERM dump chains to them. Revert with its commit.
    diag = diagnostics.start()
    code = EXIT_OK
    try:
        with Contain() as contained:
            # Inside the contained block: a registration that fails to load still runs the
            # cleanup of the ones loaded before it, logs the failure, exits 1.
            for queue, _ in queues:
                registrations[queue] = load_registration(queue, settings)
            executors = build_executors(registrations)
            connecting = asyncio.create_task(open_connection(settings))
            await _wait_for_stop(stop, [connecting])
            if not connecting.done():  # stopped while connecting
                connecting.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await connecting
                return code
            connection = connecting.result()
            handle = connection.tracing
            workers = build_workers(connection.client, settings, registrations, executors)
            tasks = [asyncio.create_task(worker.run()) for worker in workers]
            log_event(
                logger,
                INFO,
                "worker.started",
                queues=[task_queue for _, task_queue in queues],
                namespace=temporal.namespace,
            )
            await _wait_for_stop(stop, tasks)
            if not stop.is_set():
                code = EXIT_FAILED
                failed = next(
                    (t for t in tasks if t.done() and not t.cancelled() and t.exception()),
                    None,
                )
                log_event(
                    logger,
                    ERROR,
                    "worker.run.failed",
                    exc_info=failed.exception() if failed is not None else None,
                )
        if contained.error is not None:
            # Logged before the logger closes below; the type only, never the message.
            code = EXIT_FAILED
            log_event(
                logger,
                ERROR,
                "worker.run.failed",
                reason="startup_failed",
                error_type=contained.error_type,
            )
    finally:
        await shutdown_in_order(workers, tasks, handle, registrations=list(registrations.values()))
        for executor in executors.values():
            if executor is not None:
                executor.shutdown(wait=False)
        if health is not None:
            health.close()
        if diag is not None:
            diag.stop()
    return code
