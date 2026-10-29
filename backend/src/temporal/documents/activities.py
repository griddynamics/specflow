"""Parse-queue activities (SPECS section 11).

Design: both activities are ``async def`` so the async database and storage
layers run on the worker's event loop and no per-queue sync executor is needed.
Only the CPU-bound ``pdf_file_to_markdown`` runs in a dedicated single-thread
executor owned by the registration; a semaphore keeps one download+parse at a
time per worker. A background task heartbeats while the parse runs.

Cancellation and shutdown (accepted for the hello-world, F2):

- A running parse cannot be interrupted: the CPU work is a plain thread. A
  workflow cancel is delivered to the activity only with the next heartbeat the
  SDK actually sends to the server. The SDK throttles those to about 0.8 x the
  heartbeat timeout (60 s) = about 48 s, so a cancel can wait up to ~48 s, and
  a parse that ends sooner just finishes: the row becomes ``parsed`` while the
  workflow shows CANCELED. The data is correct; only the two statuses differ.
- On SIGTERM the worker stops polling and waits for running activities up to
  its grace period (130 s). A parse still running after that cannot be stopped,
  and the interpreter joins the executor thread at exit, so the process lives
  until the parse ends (up to the parse length). Kubernetes then SIGKILLs it at
  ``terminationGracePeriodSeconds`` (set it above 130 s plus a margin); the
  attempt is retried on another worker after the 60 s heartbeat timeout.
- While a document waits for the single parse slot it keeps heartbeating.

Error messages that reach workflow history are fixed codes and types only
(AC-8.4): never a filename, path, markdown or exception text.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import Executor
from contextlib import suppress
from pathlib import Path
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError, ApplicationErrorCategory

from db import repositories
from file_parsers.pdf.to_markdown import (
    ParsedMarkdown,
    PdfParseError,
    pdf_file_to_markdown,
)
from logger.logger import ERROR, INFO, WARNING, get_logger, log_event
from storage import ObjectNotFoundError, ObjectStore
from storage.keys import document_original
from temporal import diagnostics  # TEMPORARY (freeze investigation)
from temporal.containment import Contain
from temporal.documents.models import (
    MarkFailedInput,
    ParseDocumentInput,
    ParseDocumentResult,
)
from temporal.documents.workflows import MARK_FAILED_ACTIVITY, PARSE_ACTIVITY

logger = get_logger("temporal.documents.activities")

HEARTBEAT_INTERVAL_SECONDS: Final = 20.0
PARSED: Final = "parsed"


def _fixed(code: str, error_type: str, *, retryable: bool = False, expected: bool = False) -> ApplicationError:
    """An error whose message is only the fixed code (no chained cause).

    ``expected`` marks a domain failure the user caused (corrupt PDF, missing
    object or row): the Temporal SDK then logs it at DEBUG instead of WARNING
    with a traceback, so ERROR/WARNING stay for real problems.
    """

    return ApplicationError(
        code,
        type=error_type,
        non_retryable=not retryable,
        category=(ApplicationErrorCategory.BENIGN if expected else ApplicationErrorCategory.UNSPECIFIED),
    )


def _size_or_none(path: Path) -> int | None:
    return path.stat().st_size if path.exists() else None


class ParseActivities:
    """Activities bound to one session factory, object store and CPU executor."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        executor: Executor,
        *,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        parse: Callable[[Path], ParsedMarkdown] = pdf_file_to_markdown,
    ) -> None:
        self._sessions = session_factory
        self._store = store
        self._executor = executor
        self._heartbeat_interval = heartbeat_interval
        self._parse = parse
        self._slot = asyncio.Semaphore(1)

    @activity.defn(name=PARSE_ACTIVITY)
    async def parse_document(self, data: ParseDocumentInput) -> ParseDocumentResult:
        """Original from storage -> markdown -> ``parsed``. Idempotent."""

        # TEMPORARY (freeze investigation): attempt, stages, previous attempt.
        info = activity.info()
        diagnostics.set_activity(
            activity=PARSE_ACTIVITY,
            document_id=str(data.document_id),
            attempt=info.attempt,
            stage="load",
        )
        if info.attempt > 1:
            previous = info.heartbeat_details[-1] if info.heartbeat_details else None
            log_event(
                logger,
                WARNING,
                "document.parse.previous_attempt",
                document_id=str(data.document_id),
                attempt=info.attempt,
                previous_heartbeat=previous,
            )
        try:
            with Contain(ApplicationError) as contained:
                return await self._parse_document(data)
            # Retryable by default; the history message must stay free of data.
            # TEMPORARY: the traceback goes to stdout logs only (exc_info).
            log_event(
                logger,
                ERROR,
                "document.parse.attempt_failed",
                document_id=str(data.document_id),
                error_type=contained.error_type,
                exc_info=contained.error,
            )
            raise _fixed("internal_error", "ParseIoError", retryable=True) from None
        finally:
            diagnostics.clear_activity()

    async def _parse_document(self, data: ParseDocumentInput) -> ParseDocumentResult:
        async with self._sessions() as session:
            row = await repositories.get_document(session, data.project_id, data.document_id)
        if row is None:
            raise _fixed("document_not_found", "DocumentNotFound", expected=True)
        key = document_original(str(data.project_id), str(data.document_id))
        # The heartbeat starts BEFORE waiting for the slot: a second document
        # queued behind a long parse must not hit the 60 s heartbeat timeout.
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            with tempfile.TemporaryDirectory(prefix="specflow-parse-") as tmp:
                path = Path(tmp) / "original.pdf"
                diagnostics.set_stage("wait_slot")
                async with self._slot:
                    parsed = await self._download_and_parse(key, path)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        markdown = parsed.markdown.replace("\x00", "")  # Postgres text cannot hold NUL
        diagnostics.set_stage("store")
        await self._store_result(data, markdown, parsed.parser)
        log_event(
            logger,
            INFO,
            "document.parse.completed",
            project_id=str(data.project_id),
            document_id=str(data.document_id),
            markdown_chars=len(markdown),
            # TEMPORARY (freeze investigation)
            attempt=activity.info().attempt,
            duration_s=diagnostics.activity_view().get("elapsed_s"),
            rss_mb=diagnostics.rss_mb(),
        )
        return ParseDocumentResult(document_id=data.document_id, status=PARSED, markdown_chars=len(markdown))

    async def _download_and_parse(self, key: str, path: Path) -> ParsedMarkdown:
        diagnostics.set_stage("download")
        try:
            await self._store.download_to_path(key, path)
        except ObjectNotFoundError:
            raise _fixed("original_not_found", "OriginalNotFound", expected=True) from None
        # TEMPORARY (freeze investigation): the last line before a silent death.
        size_bytes = await asyncio.to_thread(_size_or_none, path)
        log_event(
            logger,
            INFO,
            "document.parse.started",
            document_id=diagnostics.activity_view().get("document_id"),
            attempt=activity.info().attempt,
            size_bytes=size_bytes,
            rss_mb=diagnostics.rss_mb(),
        )
        diagnostics.set_stage("parse")
        started = time.monotonic()
        try:
            return await asyncio.get_running_loop().run_in_executor(self._executor, self._parse, path)
        except PdfParseError as error:
            # TEMPORARY: the cause (type and traceback) to stdout logs only.
            log_event(
                logger,
                WARNING,
                "document.parse.pdf_error",
                cause_type=type(error.__cause__).__name__ if error.__cause__ else None,
                parse_s=round(time.monotonic() - started, 1),
                exc_info=error,
            )
            raise _fixed("pdf_parse_failed", "PdfParseError", expected=True) from None

    async def _heartbeat(self) -> None:
        while True:
            # TEMPORARY (freeze investigation): numbers only, visible in the
            # Temporal UI and handed to the next attempt after a timeout.
            view = diagnostics.activity_view()
            activity.heartbeat(
                {
                    "stage": view.get("stage"),
                    "elapsed_s": view.get("elapsed_s"),
                    "stage_elapsed_s": view.get("stage_elapsed_s"),
                    "rss_mb": diagnostics.rss_mb(),
                    "loop_lag_ms": diagnostics.loop_lag_ms(),
                }
            )
            await asyncio.sleep(self._heartbeat_interval)

    async def _store_result(self, data: ParseDocumentInput, markdown: str, parser: str) -> None:
        async with self._sessions() as session, session.begin():
            found = await repositories.mark_parsed(session, data.project_id, data.document_id, markdown, parser)
        if not found:
            raise _fixed("document_not_found", "DocumentNotFound", expected=True)

    @activity.defn(name=MARK_FAILED_ACTIVITY)
    async def mark_document_failed(self, data: MarkFailedInput) -> None:
        """Set ``failed`` with a fixed code; a vanished row counts as done."""

        try:
            with Contain(ValueError) as contained:
                async with self._sessions() as session, session.begin():
                    await repositories.mark_failed(session, data.project_id, data.document_id, data.error_code)
        except ValueError:
            raise _fixed("invalid_error_code", "InvalidErrorCode") from None
        if contained.error is not None:
            log_event(
                logger,
                ERROR,
                "document.parse.mark_failed_error",
                document_id=str(data.document_id),
                error_type=contained.error_type,
            )
            raise _fixed("internal_error", "MarkFailedError", retryable=True) from None
        # Expected domain failures are WARNING; only internal_error is ERROR.
        log_event(
            logger,
            ERROR if data.error_code == "internal_error" else WARNING,
            "document.parse.failed",
            project_id=str(data.project_id),
            document_id=str(data.document_id),
            error_code=data.error_code,
        )
