"""``ImportRunner``: runs the importers of the API process (docs/mission-control/integrations-api.md "Importer seam").

An import is a ``connections`` row of a kind in ``IMPORT_KINDS`` (a source) and one asyncio task. The task runs while
one of ``MAX_CONCURRENT_IMPORTS`` slots is free: the source goes ``importing``, the importer reads and writes through
its ``ImportWriter``, and the source ends ``done`` or ``failed`` (a closed reason: the importer's
``ImportFailedError``, else ``unknown_error``). No Temporal workflow is involved: the Atlassian connections live in
the ``api`` layer, which a worker may not import.

A restart ends the tasks, so at startup (``start_recovery``) every source of those kinds still ``listing`` or
``importing`` that was last touched before this process started is marked ``failed`` and added again by the member
(the import is a snapshot, not a sync). The start instant is read once from the database clock and compared with the
database's ``updated_at``, so no clock of this process or of a dead one matters; a source added since, by a request
racing the recovery, is never touched. One API replica is assumed (``Recreate``, as for the Slack bindings): a second
replica would fail the imports of the first at its own start only when they began before it did. A source
removed while its task runs stops the task at its next write. Events carry ids, the kind and codes only; the text of
an importer's error never reaches a log.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Final, Protocol

from api.contained import Contained
from api.imports.model import (
    IMPORT_KINDS,
    ImportFailedError,
    ImportJob,
    ImportRunnerClosedError,
    NotImportedKindError,
    SourceGoneError,
)
from api.imports.writer import ImportWriter
from db import imports as imports_db
from db import sources as sources_db
from logger.logger import INFO, WARNING, get_logger, log_event
from storage.base import ObjectStore

logger = get_logger("api.imports")

MAX_CONCURRENT_IMPORTS: Final = 2
UNKNOWN_REASON: Final = imports_db.INTERRUPTED_REASON


class Importer(Protocol):
    """What an importer is: it reads from its app as ``job.user_id`` and stores each item through ``writer``."""

    async def __call__(self, job: ImportJob, writer: ImportWriter) -> None: ...


class ImportRunner:
    """Starts, bounds and stops the imports of this process; held on ``Runtime.imports``."""

    def __init__(
        self,
        *,
        transaction: Callable[[], AbstractAsyncContextManager[Any]],
        store: ObjectStore,
        max_concurrent: int = MAX_CONCURRENT_IMPORTS,
    ) -> None:
        self._transaction = transaction
        self._store = store
        self._slots = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._recovery: asyncio.Task[None] | None = None
        self._closed = False

    async def add_source(
        self, project_id: uuid.UUID, user_id: uuid.UUID, *, kind: str, name: str, link: str
    ) -> uuid.UUID:
        """Create the source of an import (``listing``); ``external_id`` is a new uuid, as a Local files batch has."""

        if kind not in IMPORT_KINDS:
            raise NotImportedKindError
        async with self._transaction() as session:
            source, _ = await sources_db.create_connection(
                session,
                project_id=project_id,
                kind=kind,
                external_id=str(uuid.uuid4()),
                link=link,
                name=name,
                connected_by=user_id,
            )
        return source.id

    def start(self, job: ImportJob, importer: Importer) -> None:
        """Run ``importer`` for the source in the background, one task per source."""

        if self._closed:
            raise ImportRunnerClosedError
        if job.source_id in self._tasks:
            return
        writer = ImportWriter(job, transaction=self._transaction, store=self._store)
        task = asyncio.create_task(self._run(job, importer, writer), name=f"import-{job.source_id}")
        self._tasks[job.source_id] = task
        task.add_done_callback(lambda _done: self._tasks.pop(job.source_id, None))

    def running(self) -> int:
        """How many imports have a task (queued for a slot or running)."""

        return len(self._tasks)

    async def start_recovery(self) -> None:
        """Take this process's start instant from the database clock (before anything is served), then end, in the
        background, the sources a stopped process left ``listing`` or ``importing``: those not touched since it."""

        with Contained() as read:
            async with self._transaction() as session:
                started = await imports_db.database_now(session)
        if read.failed:
            log_event(logger, WARNING, "api.imports.recovery_failed", error_type=read.error_type)
            return
        self._recovery = asyncio.create_task(self._recover(started), name="import-recovery")

    async def aclose(self) -> None:
        """Cancel the tasks (the sources they leave are ended by the next start's recovery)."""

        self._closed = True
        tasks = [*self._tasks.values(), *([self._recovery] if self._recovery is not None else [])]
        tasks = [task for task in tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job: ImportJob, importer: Importer, writer: ImportWriter) -> None:
        async with self._slots:
            if not await self._set_state(job, "importing"):
                return
            log_event(logger, INFO, "api.imports.started", project_id=str(job.project_id), kind=job.kind)
            reason: str | None = None
            error_type: str | None = None
            try:
                with Contained(passing=(ImportFailedError, SourceGoneError)) as ran:
                    await importer(job, writer)
            except SourceGoneError:  # removed meanwhile: nothing is left to end
                log_event(logger, INFO, "api.imports.stopped", project_id=str(job.project_id), kind=job.kind)
                return
            except ImportFailedError as error:
                reason = error.reason
            else:
                if ran.failed:  # an error the importer did not name: a closed reason, the class only
                    reason, error_type = UNKNOWN_REASON, ran.error_type
            with Contained() as settled:  # the source is ended; a failure here is left to the next start's recovery
                if reason is None:
                    await self._finish(job)
                else:
                    await self._fail(job, reason, error_type)
            if settled.failed:
                log_event(
                    logger,
                    WARNING,
                    "api.imports.settle_failed",
                    project_id=str(job.project_id),
                    error_type=settled.error_type,
                )

    async def _finish(self, job: ImportJob) -> None:
        if await self._set_state(job, "done"):
            log_event(logger, INFO, "api.imports.done", project_id=str(job.project_id), kind=job.kind)

    async def _fail(self, job: ImportJob, reason: str, error_type: str | None) -> None:
        async with self._transaction() as session:
            await sources_db.fail_unfinished_items(session, job.source_id)
            await sources_db.set_connection_state(session, job.source_id, "failed", failure_reason=reason)
        log_event(
            logger,
            WARNING,
            "api.imports.failed",
            project_id=str(job.project_id),
            kind=job.kind,
            reason=reason,
            error_type=error_type,
        )

    async def _set_state(self, job: ImportJob, state: str) -> bool:
        async with self._transaction() as session:
            return await sources_db.set_connection_state(session, job.source_id, state)

    async def _recover(self, before: datetime) -> None:
        with Contained() as recovered:
            async with self._transaction() as session:
                ended = await imports_db.fail_interrupted(session, IMPORT_KINDS, before=before)
            if ended:
                log_event(logger, WARNING, "api.imports.interrupted", sources=len(ended))
        if recovered.failed:
            log_event(logger, WARNING, "api.imports.recovery_failed", error_type=recovered.error_type)
