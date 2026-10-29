"""``DELETE /projects/{pid}/sources/{sid}``: remove a source (INT-SRC-0003, SPECS 5).

Requirements: FR-SRC-0016/0017, NFR-REL-0003, NFR-PERF-0003, NFR-SEC-0002,
NFR-MAIN-0002.

One transaction deletes the rows and starts the cleanup workflows (before the commit,
so a failed start rolls everything back). After the commit, in this order [R09]:
terminate the Drive import, start the re-reads (16 at once), delete the objects inline
(re-reads and deletes share one 20 s budget, the sweeps take what is left [R08]),
answer 204. The cleanup start runs under the project lock, so it stops after 10 s. Nothing after the commit can
change the answer. Logs: ids, counts and codes, never a name.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Final

from fastapi import APIRouter, Response

from api import workflows
from api.batch_files import retry_once, start_parse_or_mark_failed
from api.contained import Contained
from api.deps import RuntimeDep, UserDep
from api.errors import ApiError, removal_unavailable, source_not_found
from api.routers.documents import require_project
from api.runtime import Runtime
from db import removal
from db.batches import LockConflictError, LockTimeoutError, SourceNotFoundError
from db.removal import Removal, RevisionChainError
from logger.logger import INFO, WARNING, get_logger, log_event
from storage.base import ObjectStore

logger = get_logger("api.routers.source_removal")
router = APIRouter(tags=["sources"])

DELETE_BUDGET_SECONDS: Final = 20.0  # R08: the inline object deletes stop here
DELETE_CONCURRENCY: Final = 16
REPARSE_CONCURRENCY: Final = 16  # re-read starts at once; they share the 20 s budget
CLEANUP_START_TIMEOUT: Final = 10.0  # the start runs under the project lock (review m1)
LOGGED_IDS: Final = 20


class CleanupStartError(Exception):
    """A cleanup workflow could not be started: the removal is rolled back."""


def _millis(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def run_bounded[T](
    items: Iterable[T],
    work: Callable[[T], Awaitable[object]],
    *,
    budget: float,
    concurrency: int,
) -> list[T]:
    """``work(item)`` for every item, ``concurrency`` at once, stopping at ``budget`` seconds.

    Returns the items that did NOT complete (raised, or still waiting at the budget).
    """

    items = list(items)
    slots = asyncio.Semaphore(concurrency)

    async def one(item: T) -> None:
        async with slots:
            await work(item)

    tasks = [asyncio.ensure_future(one(item)) for item in items]
    if not tasks:
        return []
    try:
        _, pending = await asyncio.wait(tasks, timeout=budget)
    except BaseException:
        for task in tasks:
            task.cancel()
        raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return [item for item, task in zip(items, tasks, strict=True) if task.cancelled() or task.exception() is not None]


async def delete_objects(store: ObjectStore, object_keys: Iterable[str], *, budget: float, concurrency: int) -> int:
    """Delete the objects with ``concurrency`` calls at once, stop at ``budget`` seconds.

    Returns how many were NOT deleted (failed, or still waiting at the budget).
    """

    left = await run_bounded(dict.fromkeys(object_keys), store.delete, budget=budget, concurrency=concurrency)
    return len(left)


async def _attempt(runtime: Runtime, pid: uuid.UUID, sid: uuid.UUID) -> Removal:
    """One removal transaction: rows first, then the cleanup workflows, then the commit."""

    removal_id = uuid.uuid4()
    async with runtime.transaction() as session:
        result = await removal.remove_source(session, pid, sid)
        try:
            await asyncio.wait_for(
                workflows.start_cleanups(runtime, pid, sid, result.sweep_document_ids, removal_id),
                CLEANUP_START_TIMEOUT,
            )
        except Exception as error:  # a timeout too: the row lock is not held for ever
            raise CleanupStartError(type(error).__name__) from error
        return result


async def _after_commit(runtime: Runtime, user_id: uuid.UUID, pid: uuid.UUID, sid: uuid.UUID, done: Removal) -> None:
    """Terminate, re-read, delete objects (R09). Each step is isolated: the answer is 204."""

    if done.kind == "gdrive":
        await workflows.terminate_drive_import(runtime, sid)  # never raises
    began = time.monotonic()

    async def reparse(document_id: uuid.UUID) -> None:
        with Contained() as start:  # the sweeps and the next read are unaffected
            await start_parse_or_mark_failed(runtime, user_id, pid, document_id)
        if start.failed:
            log_event(
                logger,
                WARNING,
                "api.source.reparse_failed",
                project_id=str(pid),
                source_id=str(sid),
                document_id=str(document_id),
                error_type=start.error_type,
            )

    waiting = await run_bounded(
        done.reparse,
        reparse,
        budget=DELETE_BUDGET_SECONDS,
        concurrency=REPARSE_CONCURRENCY,
    )
    if waiting:
        log_event(
            logger,
            WARNING,
            "api.source.reparse_left",
            project_id=str(pid),
            source_id=str(sid),
            failed_count=len(waiting),
            document_ids=[str(i) for i in waiting[:LOGGED_IDS]],
        )
    left_budget = max(DELETE_BUDGET_SECONDS - (time.monotonic() - began), 0.0)
    left = len(done.object_keys)  # kept when the deletes fail as a whole
    with Contained() as delete:
        left = await delete_objects(
            runtime.store,
            done.object_keys,
            budget=left_budget,
            concurrency=DELETE_CONCURRENCY,
        )
    if delete.failed:
        log_event(
            logger,
            WARNING,
            "api.source.delete_failed",
            project_id=str(pid),
            source_id=str(sid),
            error_type=delete.error_type,
        )
    if left:
        log_event(
            logger,
            WARNING,
            "api.source.objects_left",
            project_id=str(pid),
            source_id=str(sid),
            failed_count=left,
        )


@router.delete("/projects/{pid}/sources/{sid}", status_code=204)
async def remove_source(pid: uuid.UUID, sid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> Response:
    await require_project(runtime, user.id, pid)
    started = time.monotonic()
    ids: dict[str, Any] = {"user_id": str(user.id), "project_id": str(pid), "source_id": str(sid)}
    log_event(logger, INFO, "api.source.remove_started", **ids)

    def failed(outcome: str) -> None:
        log_event(
            logger,
            WARNING,
            "api.source.remove_failed",
            **ids,
            duration_ms=_millis(started),
            outcome=outcome,
        )

    try:
        # a deadlock or an FK error from a writer outside the lock order: once more
        done = await retry_once(lambda: _attempt(runtime, pid, sid))
    except SourceNotFoundError:
        failed("not_found")
        raise source_not_found() from None
    except LockTimeoutError:
        failed("lock_timeout")
        raise removal_unavailable() from None
    except CleanupStartError:
        failed("cleanup_start_failed")
        raise removal_unavailable() from None
    except RevisionChainError:  # R14: never retried
        failed("revision_fk")
        raise removal_unavailable() from None
    except LockConflictError:  # the second conflict
        failed("error")
        raise removal_unavailable() from None
    except ApiError:  # database_unavailable: rolled back, its own answer
        failed("error")
        raise
    except Exception:
        failed("error")
        raise
    await _after_commit(runtime, user.id, pid, sid, done)
    log_event(
        logger,
        INFO,
        "api.source.removed",
        **ids,
        kind=done.kind,
        file_count=done.file_count,
        handed_over=len(done.handed_over),
        duration_ms=_millis(started),
        outcome="removed",
    )
    return Response(status_code=204)
