"""Source file list and Local files batches: start, add one file, end (SPECS 4.2, 4.4).

Requirements: INT-SRC-0002, INT-SRC-0004, FR-SRC-0020/0021/0022, NFR-SEC-0002,
NFR-MAIN-0002. Every route calls ``require_project`` first (a non-member gets
``project_not_found``); every read and write also filters by ``project_id``, so the id
of another project's source is "not found". Logs carry ids, counts and codes only.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse

from api.batch_files import add_streamed_file, retry_once, translate
from api.deps import RuntimeDep, UserDep
from api.errors import ApiError, ApiInvariantError, source_not_found
from api.routers.documents import FilenameQuery, clean_filename, require_project
from api.routers.projects import invalid_cursor
from api.schemas import (
    BatchFileOut,
    BatchSourceOut,
    BatchStartIn,
    FilePageOut,
)
from api.source_views import added_file, file_out, holder_out, summary_out
from api.uploads import is_pdf_content_type
from db import batches
from db import source_files as files
from db.batches import AddOutcome, BatchEndedError, LockConflictError, LockTimeoutError, SourceNotFoundError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.source_batches")
router = APIRouter(tags=["sources"])

PageLimit = Annotated[int, Query(ge=1, le=files.PAGE_LIMIT)]


def _millis(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# Files ----------------------------------------------------------------------


@router.get("/projects/{pid}/sources/{sid}/files", response_model=FilePageOut)
async def list_source_files(
    pid: uuid.UUID,
    sid: uuid.UUID,
    runtime: RuntimeDep,
    user: UserDep,
    *,
    limit: PageLimit = files.PAGE_LIMIT,
    cursor: str | None = None,
) -> FilePageOut:
    """INT-SRC-0002: the source header plus one keyset page of its files."""

    await require_project(runtime, user.id, pid)
    try:
        after = files.decode_file_cursor(cursor) if cursor else None
    except files.InvalidCursorError:
        raise invalid_cursor() from None
    async with runtime.transaction() as session:
        summary = await files.source_summary(session, pid, sid)
        if summary is None:  # absent, removed, another project's, or a hidden batch
            raise source_not_found()
        page = await files.file_page(session, pid, sid, after, limit)
        return FilePageOut(
            source=summary_out(summary),
            items=[file_out(row) for row in page.items],
            next_cursor=page.next_cursor,
        )


# Batches --------------------------------------------------------------------


@router.post("/projects/{pid}/sources/uploads", status_code=201, response_model=BatchSourceOut)
async def start_upload_batch(pid: uuid.UUID, body: BatchStartIn, runtime: RuntimeDep, user: UserDep) -> BatchSourceOut:
    """INT-SRC-0004: open a Local files batch for ``expected_files`` files."""

    await require_project(runtime, user.id, pid)

    async def attempt() -> BatchSourceOut:
        async with runtime.transaction() as session:
            batch = await batches.start_batch(session, pid, user.id, body.expected_files)
            summary = await files.source_summary(session, pid, batch.id)
            if summary is None:  # unreachable: a new batch is open, so it is shown
                raise ApiInvariantError("a new batch is not readable")
            return BatchSourceOut(source=summary_out(summary))

    try:
        created = await retry_once(attempt)
    except (LockTimeoutError, LockConflictError) as error:
        raise translate(error) from None
    log_event(
        logger,
        INFO,
        "api.batch.created",
        user_id=str(user.id),
        project_id=str(pid),
        source_id=str(created.source.id),
        kind="upload",
        file_count=body.expected_files,
        outcome="created",
    )
    return created


@router.post("/projects/{pid}/sources/{sid}/files", response_model=BatchFileOut)
async def add_batch_file(
    pid: uuid.UUID,
    sid: uuid.UUID,
    request: Request,
    runtime: RuntimeDep,
    user: UserDep,
    *,
    filename: FilenameQuery = None,
) -> JSONResponse:
    """INT-SRC-0004: stream one PDF into an open batch (201 added, 200 otherwise)."""

    await require_project(runtime, user.id, pid)
    name = clean_filename(filename)
    if not is_pdf_content_type(request.headers.get("content-type")):
        raise ApiError(415, "unsupported_format", "The body must be a PDF sent as application/pdf.")

    def build(outcome: AddOutcome) -> BatchFileOut:
        return BatchFileOut(
            outcome=outcome.outcome,
            file=added_file(outcome),
            holder=holder_out(outcome.holder) if outcome.outcome == "already_in_project" else None,
        )

    added = await add_streamed_file(
        runtime,
        request,
        user_id=user.id,
        project_id=pid,
        filename=name,
        batch_id=sid,
        build=build,
    )
    answer = added.built
    if added.parse_started is False and answer.file is not None:
        answer = answer.model_copy(
            update={"file": answer.file.model_copy(update={"state": "failed", "reason": "unknown"})}
        )
    return JSONResponse(
        answer.model_dump(mode="json"),
        status_code=201 if added.outcome == "added" else 200,
    )


@router.post(
    "/projects/{pid}/sources/{sid}/end",
    response_model=BatchSourceOut,
    responses={204: {"description": "The batch had no file and was deleted."}},
)
async def end_upload_batch(pid: uuid.UUID, sid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> Response:
    """INT-SRC-0004: close a batch. 200 with the source; 204 when it had no file (deleted)."""

    await require_project(runtime, user.id, pid)
    started = time.monotonic()

    async def attempt() -> BatchSourceOut | None:
        async with runtime.transaction() as session:
            batch = await batches.end_batch(session, pid, sid)
            if batch is None:
                return None
            summary = await files.source_summary(session, pid, sid)
            if summary is None:  # unreachable: an ended batch with files is shown
                raise ApiInvariantError("an ended batch is not readable")
            return BatchSourceOut(source=summary_out(summary))

    try:
        ended = await retry_once(attempt)
    except (SourceNotFoundError, BatchEndedError, LockTimeoutError, LockConflictError) as error:
        raise translate(error) from None
    log_event(
        logger,
        INFO,
        "api.batch.ended",
        user_id=str(user.id),
        project_id=str(pid),
        source_id=str(sid),
        file_count=ended.source.total if ended else 0,
        duration_ms=_millis(started),
        outcome="ended" if ended else "deleted_empty",
    )
    if ended is None:
        return Response(status_code=204)
    return JSONResponse(ended.model_dump(mode="json"))
