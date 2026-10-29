"""``DriveImportWorkflow``: one run imports one Drive source (architecture A3).

Flow: list every level (files become ``pending`` rows, connection ``importing``)
-> batches of pending files -> per file one activity (row-only files finish in the
batch read; PDFs then run the child ``ParseDocumentWorkflow``) -> connection
``done``. A file that fails ends as ``error`` and never fails the run. A whole-
import failure (setup error, the soft deadline, any non-cancel workflow failure)
runs ``fail_import``: connection ``failed``, open files ``error``/``import_failed``
(INT-GDUI-0004.AC4).

Sandbox rules as ``ParseDocumentWorkflow``: only ``temporalio`` and the pydantic
models are imported; activities are scheduled by name. History carries ids and
fixed codes only (never file names).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    ChildWorkflowError,
    WorkflowAlreadyStartedError,
)

with workflow.unsafe.imports_passed_through():
    from temporal.documents.models import (
        ParseDocumentInput,
        ParseDocumentResult,
    )
    from temporal.documents.workflows import (
        EXECUTION_TIMEOUT as PARSE_EXECUTION_TIMEOUT,
    )
    from temporal.documents.workflows import (
        WORKFLOW_NAME as PARSE_WORKFLOW_NAME,
    )
    from temporal.ids import parse_workflow_id
    from temporal.sources.models import (
        BatchItem,
        BatchRequest,
        BatchResult,
        CompleteInput,
        DriveImportInput,
        FailImportInput,
        FinishItemInput,
        ImportEnd,
        ImportItemInput,
        ImportItemResult,
        ListResult,
        WaitDocumentInput,
        WaitDocumentResult,
    )

WORKFLOW_NAME: Final = "DriveImportWorkflow"
LIST_ACTIVITY: Final = "drive_list"
BATCH_ACTIVITY: Final = "drive_next_batch"
ITEM_ACTIVITY: Final = "drive_import_item"
FINISH_ACTIVITY: Final = "drive_finish_item"
COMPLETE_ACTIVITY: Final = "drive_complete"
FAIL_ACTIVITY: Final = "drive_fail_import"
WAIT_ACTIVITY: Final = "drive_wait_document"
FAILURE_TYPE: Final = "DriveImportFailed"

#: Workflow soft deadline; the start option (12 h, ``db.sources.DRIVE_IMPORT_EXECUTION_TIMEOUT``) is the hard one.
SOFT_DEADLINE: Final = timedelta(hours=11)
BATCH_SIZE: Final = 50
#: Files downloading/converting at once per import (the worker's Drive pool has 4 threads).
ITEM_CONCURRENCY: Final = 4
#: Child parses in flight per import (HANDOFF "parse head-of-line"; owner decision pending).
PARSE_CONCURRENCY: Final = 2
#: Activity attempts for one file; the last attempt records the error itself.
ITEM_MAX_ATTEMPTS: Final = 3

LIST_START_TO_CLOSE: Final = timedelta(minutes=20)
ITEM_START_TO_CLOSE: Final = timedelta(minutes=30)
ITEM_HEARTBEAT_TIMEOUT: Final = timedelta(seconds=60)
#: A shared document is parsed within the parse workflow's own execution limit (60 min) plus margin.
WAIT_START_TO_CLOSE: Final = timedelta(minutes=65)
DB_START_TO_CLOSE: Final = timedelta(minutes=2)

LIST_RETRY: Final = RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=3)
ITEM_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_attempts=ITEM_MAX_ATTEMPTS,
)
DB_RETRY: Final = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5)


class DeadlineExceededError(Exception):
    """The soft deadline passed: the whole import fails (``fail_import``)."""


def _is_cancel(error: BaseException) -> bool:
    return isinstance(error, asyncio.CancelledError) or (
        isinstance(error, ActivityError | ChildWorkflowError) and isinstance(error.cause, CancelledError)
    )


@workflow.defn(name=WORKFLOW_NAME)
class DriveImportWorkflow:
    @workflow.run
    async def run(self, data: DriveImportInput) -> ImportEnd:
        if data.started_epoch is None:
            data = data.model_copy(update={"started_epoch": workflow.now().timestamp()})
        self._item_slots = asyncio.Semaphore(ITEM_CONCURRENCY)
        self._parse_slots = asyncio.Semaphore(PARSE_CONCURRENCY)
        self._deadline = datetime.fromtimestamp((data.started_epoch or 0.0) + SOFT_DEADLINE.total_seconds(), tz=UTC)
        try:
            return await self._import(data)
        except Exception as error:
            if _is_cancel(error) or isinstance(error, workflow.ContinueAsNewError):
                raise
            await self._fail_import(data)
            raise ApplicationError("drive_import_failed", type=FAILURE_TYPE, non_retryable=True) from None

    async def _import(self, data: DriveImportInput) -> ImportEnd:
        if not data.listed:
            listed = await workflow.execute_activity(
                LIST_ACTIVITY,
                data,
                result_type=ListResult,
                start_to_close_timeout=LIST_START_TO_CLOSE,
                heartbeat_timeout=ITEM_HEARTBEAT_TIMEOUT,
                retry_policy=LIST_RETRY,
            )
            if listed.outcome != "listed":
                return ImportEnd(state=listed.outcome)
            data = data.model_copy(update={"listed": True})
        request = BatchRequest(project_id=data.project_id, connection_id=data.connection_id)
        batches = 0
        while True:
            self._check_deadline()
            batch = await workflow.execute_activity(
                BATCH_ACTIVITY,
                request,
                result_type=BatchResult,
                start_to_close_timeout=DB_START_TO_CLOSE,
                retry_policy=DB_RETRY,
            )
            if batch.stop:
                return ImportEnd(state="stop")
            if batch.pending == 0:
                break
            results = await asyncio.gather(
                *(self._file(data, item) for item in batch.items),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            batches += 1
            if workflow.info().is_continue_as_new_suggested() or (
                data.max_batches_per_run and batches >= data.max_batches_per_run
            ):
                workflow.continue_as_new(data)
        await workflow.execute_activity(
            COMPLETE_ACTIVITY,
            CompleteInput(
                project_id=data.project_id,
                connection_id=data.connection_id,
                started_epoch=data.started_epoch,
            ),
            start_to_close_timeout=DB_START_TO_CLOSE,
            retry_policy=DB_RETRY,
        )
        return ImportEnd(state="done")

    def _check_deadline(self) -> None:
        """Checked before every batch and every file, so ``fail_import`` runs before the hard timeout."""

        if workflow.now() > self._deadline:
            raise DeadlineExceededError

    # -- one file ------------------------------------------------------------
    async def _file(self, data: DriveImportInput, item: BatchItem) -> None:
        job = ImportItemInput(
            project_id=data.project_id,
            connection_id=data.connection_id,
            item_id=item.item_id,
            kind=item.kind,
        )
        try:
            async with self._item_slots:
                self._check_deadline()
                result = await workflow.execute_activity(
                    ITEM_ACTIVITY,
                    job,
                    result_type=ImportItemResult,
                    start_to_close_timeout=ITEM_START_TO_CLOSE,
                    heartbeat_timeout=ITEM_HEARTBEAT_TIMEOUT,
                    retry_policy=ITEM_RETRY,
                )
        except ActivityError as error:
            if _is_cancel(error):
                raise
            # A timeout or a lost worker on every attempt: the file ends in error.
            step = "read" if item.kind == "google" else "download"
            await self._finish(job, "error", reason="unknown_error", step=step)
            return
        if result.action == "done":
            return
        document_id = result.document_id
        if result.action == "parse":
            try:
                async with self._parse_slots:
                    self._check_deadline()
                    await workflow.execute_child_workflow(
                        PARSE_WORKFLOW_NAME,
                        ParseDocumentInput(project_id=data.project_id, document_id=document_id),
                        id=parse_workflow_id(document_id, prefix=data.id_prefix),
                        result_type=ParseDocumentResult,
                        execution_timeout=PARSE_EXECUTION_TIMEOUT,
                    )
            except WorkflowAlreadyStartedError:
                # The parse of this document already runs: wait for its final status.
                await self._wait_and_finish(data, job, document_id)
                return
            except DeadlineExceededError:
                raise
            except Exception as error:
                if _is_cancel(error):
                    raise
                # A failed child marked the document itself; after a timeout or a
                # terminate it may still be ``processing``: the finish activity
                # then marks the document failed too.
                await self._finish(
                    job,
                    "error",
                    document_id=document_id,
                    reason="parse_failed",
                    step="parse",
                )
                return
            await self._finish(job, "processed", document_id=document_id)
            return
        await self._wait_and_finish(data, job, document_id)

    async def _wait_and_finish(self, data: DriveImportInput, job: ImportItemInput, document_id: UUID) -> None:
        """The file shares a document another flow still parses: pending until it is final."""

        try:
            waited = await workflow.execute_activity(
                WAIT_ACTIVITY,
                WaitDocumentInput(project_id=data.project_id, document_id=document_id),
                result_type=WaitDocumentResult,
                start_to_close_timeout=WAIT_START_TO_CLOSE,
                heartbeat_timeout=ITEM_HEARTBEAT_TIMEOUT,
                retry_policy=DB_RETRY,
            )
        except ActivityError as error:
            if _is_cancel(error):
                raise
            await self._finish(job, "error", reason="parse_failed", step="parse")
            return
        if waited.status == "parsed":
            await self._finish(job, "processed", document_id=document_id)
        else:
            await self._finish(
                job,
                "error",
                document_id=document_id,
                reason="parse_failed",
                step="parse",
            )

    async def _finish(
        self,
        job: ImportItemInput,
        state: Literal["processed", "error"],
        *,
        document_id: UUID | None = None,
        reason: str | None = None,
        step: str | None = None,
    ) -> None:
        await workflow.execute_activity(
            FINISH_ACTIVITY,
            FinishItemInput(
                project_id=job.project_id,
                connection_id=job.connection_id,
                item_id=job.item_id,
                state=state,
                document_id=document_id,
                reason=reason,
                step=step,
            ),
            start_to_close_timeout=DB_START_TO_CLOSE,
            retry_policy=DB_RETRY,
        )

    async def _fail_import(self, data: DriveImportInput) -> None:
        try:
            await workflow.execute_activity(
                FAIL_ACTIVITY,
                FailImportInput(project_id=data.project_id, connection_id=data.connection_id),
                start_to_close_timeout=DB_START_TO_CLOSE,
                retry_policy=DB_RETRY,
            )
        except ActivityError as error:
            if _is_cancel(error):
                raise
            # The connection reads failed from the clock after the execution timeout.
