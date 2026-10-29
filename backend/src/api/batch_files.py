"""The shared upload path of the batch ``add`` route and the legacy upload (SPECS 4.4, 4.5).

Stream the body to a new key, add the file to a batch in one transaction (retried once
on ``LockConflictError``, R11), then after the commit: delete the streamed object when no row
uses it, start the parse, log. Nothing here logs a file name.

Requirements: INT-SRC-0004, FR-SRC-0020, NFR-MAIN-0002, NFR-REL-0003.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy.exc import DBAPIError

from api import workflows
from api.contained import Contained
from api.errors import (
    ApiError,
    batch_ended,
    database_unavailable,
    source_not_found,
)
from api.runtime import Runtime
from api.uploads import stream_pdf
from db import batches
from db import repositories as repo
from db import source_files as files
from db.batches import AddOutcome, BatchEndedError, LockConflictError, LockTimeoutError, SourceNotFoundError
from db.removal import RevisionChainError
from db.sources import DocumentFile
from logger.logger import INFO, WARNING, get_logger, log_event
from storage import keys

logger = get_logger("api.batch_files")

_FOREIGN_KEY_VIOLATION = "23503"


def translate(error: Exception) -> ApiError:
    """The API answer for an error of a batch write; total (anything unknown is a 500)."""

    if isinstance(error, SourceNotFoundError):
        return source_not_found()
    if isinstance(error, BatchEndedError):
        return batch_ended()
    if isinstance(error, LockTimeoutError | LockConflictError):
        return database_unavailable()  # a queued writer gave up: try again
    return ApiError(500, "internal_error", "An internal error occurred.")


def typed_database_error(error: DBAPIError) -> Exception:
    """Type a database error that surfaced at COMMIT like the ones inside the transaction.

    Lock timeout, deadlock and serialization failure map as in ``db.batches``; a foreign
    key violation is a concurrent write (``LockConflictError``, retried once) or, for the
    revision chain, ``RevisionChainError`` (never retried). Anything else stays as it is.
    """

    mapped = batches.timeout_from(error) or batches.conflict_from(error)
    if mapped is not None:
        return mapped
    if batches.sqlstate(error) == _FOREIGN_KEY_VIOLATION:
        if "previous_revision" in str(error.orig):
            return RevisionChainError("a document is the previous revision of another")
        return LockConflictError("a concurrent write referenced a deleted row")
    return error


async def _typed[T](attempt: Callable[[], Awaitable[T]]) -> T:
    try:
        return await attempt()
    except DBAPIError as error:  # raised by the COMMIT when it leaves the transaction
        typed = typed_database_error(error)
        if typed is error:
            raise
        raise typed from error


async def retry_once[T](attempt: Callable[[], Awaitable[T]]) -> T:
    """Run a whole transaction; one more time after a deadlock (``LockConflictError``, R11)."""

    try:
        return await _typed(attempt)
    except LockConflictError:
        return await _typed(attempt)


async def check_batch_accepts(runtime: Runtime, project_id: uuid.UUID, batch_id: uuid.UUID) -> None:
    """Answer 404 / 409 BEFORE the body is streamed (SPECS 4.4 step 2, review m4).

    A cheap read of the source card: unknown, another project's or hidden is
    ``SourceNotFoundError``; not an upload batch or not open is ``BatchEndedError``. The
    transaction of the add checks again under the lock, so a race still ends right.
    """

    async with runtime.transaction() as session:
        summary = await files.source_summary(session, project_id, batch_id)
    if summary is None:
        raise source_not_found()
    if summary.kind != batches.BATCH_KIND or not summary.open:
        raise batch_ended()


@dataclass(frozen=True)
class Added[T]:
    outcome: str
    built: T
    source_id: uuid.UUID
    document_id: uuid.UUID | None
    parse_started: bool | None  # None: no parse was due


async def start_parse_or_mark_failed(
    runtime: Runtime,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
) -> bool:
    """K5: start the parse of a new, moved or re-read document; a failed start marks it failed.

    The upload (or removal) itself succeeded, so the caller keeps its answer; the user
    can start the parse again with ``POST .../parse`` or a new upload.
    """

    workflow_id: str | None = None
    with Contained() as start:
        workflow_id = await workflows.start_parse(runtime, project_id, document_id)
    if start.failed:
        log_event(
            logger,
            WARNING,
            "api.document.parse_start_failed",
            project_id=str(project_id),
            document_id=str(document_id),
            error_type=start.error_type,
        )
        with Contained() as mark:  # the upload stays stored
            async with runtime.transaction() as session:
                await repo.mark_failed(session, project_id, document_id, "workflow_start_failed")
        if mark.failed:
            log_event(
                logger,
                WARNING,
                "api.document.mark_failed_failed",
                project_id=str(project_id),
                document_id=str(document_id),
                error_type=mark.error_type,
            )
        return False
    log_event(
        logger,
        INFO,
        "api.document.parse_started",
        user_id=str(user_id) if user_id else None,
        project_id=str(project_id),
        document_id=str(document_id),
        workflow_id=workflow_id,
    )
    return True


async def add_streamed_file[T](
    runtime: Runtime,
    request: Request,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    filename: str,
    batch_id: uuid.UUID | None,
    build: Callable[[AddOutcome], T],
) -> Added[T]:
    """Add the request body (a PDF) to ``batch_id``; ``None`` = today's legacy day batch.

    ``build`` turns the outcome into the caller's answer while the transaction is open
    (the rows are attached then). The streamed object is deleted unless a row uses it;
    ``document.original_key`` is never deleted (a moved or linked document keeps its
    own object).
    """

    if batch_id is not None:
        await check_batch_accepts(runtime, project_id, batch_id)
    document_id = uuid.uuid4()
    key = keys.document_original(str(project_id), str(document_id))
    uploaded = await stream_pdf(request, runtime.store, key)
    legacy = batch_id is None

    async def attempt() -> tuple[Added[T], bool, uuid.UUID | None]:
        async with runtime.transaction() as session:
            target = batch_id
            if target is None:
                day = await batches.legacy_day_batch(session, project_id, user_id, datetime.now(UTC))
                target = day.id
            outcome = await batches.add_batch_file(
                session,
                project_id,
                target,
                file=DocumentFile(
                    document_id=document_id,
                    content_hash=uploaded.content_hash,
                    filename=filename,
                    size_bytes=uploaded.size,
                    original_key=key,
                ),
                user_id=user_id,
                allow_ended=legacy,
            )
            document = outcome.document
            owns_key = document is not None and document.original_key == key
            added = Added(
                outcome=outcome.outcome,
                built=build(outcome),
                source_id=target,
                document_id=document.id if document is not None else None,
                parse_started=None,
            )
            discard = outcome.discard_upload and not owns_key
            return added, discard, outcome.reparse_document_id

    try:
        added, discard, reparse_id = await retry_once(attempt)
    except (SourceNotFoundError, BatchEndedError, LockTimeoutError, LockConflictError) as error:
        await _delete_quietly(runtime, key)
        raise translate(error) from None
    except BaseException:
        with suppress(Exception):
            await asyncio.shield(runtime.store.delete(key))
        raise
    if discard:
        await _delete_quietly(runtime, key)  # C20: the new object is not needed
    log_event(
        logger,
        INFO,
        "api.document.uploaded",
        user_id=str(user_id),
        project_id=str(project_id),
        source_id=str(added.source_id),
        document_id=str(added.document_id) if added.document_id else None,
        size_bytes=uploaded.size,
        outcome=added.outcome,
    )
    if reparse_id is None:
        return added
    started = await start_parse_or_mark_failed(runtime, user_id, project_id, reparse_id)
    return Added(
        outcome=added.outcome,
        built=added.built,
        source_id=added.source_id,
        document_id=added.document_id,
        parse_started=started,
    )


async def _delete_quietly(runtime: Runtime, key: str) -> None:
    with Contained() as delete:  # an unreferenced object is harmless; the answer stands
        await asyncio.shield(runtime.store.delete(key))
    if delete.failed:
        log_event(
            logger,
            WARNING,
            "api.document.discard_failed",
            error_type=delete.error_type,
        )
