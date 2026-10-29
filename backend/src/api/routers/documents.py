"""Documents: raw-body PDF upload, list/get, parse and summarize (SPECS section 9)."""

from __future__ import annotations

import unicodedata
import uuid
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from api import workflows
from api.batch_files import add_streamed_file, start_parse_or_mark_failed
from api.deps import RuntimeDep, UserDep
from api.errors import (
    ApiError,
    ApiInvariantError,
    document_not_found,
    model_not_allowed,
    project_not_found,
)
from api.routers.projects import LimitQuery, invalid_cursor
from api.runtime import Runtime
from api.schemas import (
    DocumentDetailOut,
    DocumentOut,
    Page,
    ParseStarted,
    SummarizeRequest,
    SummarizeStarted,
    UploadOut,
)
from api.uploads import is_pdf_content_type
from db import repositories as repo
from db.batches import AddOutcome
from db.models import Document
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.routers.documents")
router = APIRouter(prefix="/projects/{pid}/documents", tags=["documents"])

MARKDOWN_CAP = 1_000_000
FILENAME_MAX = 255
# Statuses from which "parse" is a conflict: the document was already parsed.
PARSED_STATUSES = frozenset({"parsed", "ingested", "partially_ingested"})

# Control, format (bidi overrides, zero-width), line and paragraph separators.
FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

# Batch outcome (SPECS 4.4) -> the legacy ``UploadOut`` outcome (SPECS 4.5).
LEGACY_OUTCOMES = {
    "added": "created",
    "read_again": "retried",
    "already_in_project": "duplicate",
    "duplicate": "duplicate",
}

FilenameQuery = Annotated[str | None, Query(max_length=FILENAME_MAX * 4)]


async def require_project(runtime: Runtime, user_id: uuid.UUID, pid: uuid.UUID) -> None:
    async with runtime.transaction() as session:
        if await repo.get_member_project(session, user_id, pid) is None:
            raise project_not_found()


def clean_filename(raw: str | None) -> str:
    """The base name of the submitted filename, else ``missing_filename`` / 422."""

    path = (raw or "").replace("\\", "/")
    name = PurePosixPath(path).name.strip()
    if path.rsplit("/", 1)[-1].strip() == ".":
        name = "."  # PurePosixPath drops a trailing "."; "." is never a filename
    if not name:
        raise ApiError(400, "missing_filename", "The filename query parameter is required.")
    if (
        len(name) > FILENAME_MAX
        or name in {".", ".."}
        or any(unicodedata.category(char) in FORBIDDEN_CATEGORIES for char in name)
    ):
        raise ApiError(
            422,
            "validation_failed",
            "The request is invalid.",
            details={"errors": [{"loc": ["query", "filename"], "type": "invalid_filename"}]},
        )
    return name


@router.post("", response_model=UploadOut, response_model_exclude_none=True)
async def upload_document(
    pid: uuid.UUID,
    request: Request,
    runtime: RuntimeDep,
    user: UserDep,
    filename: FilenameQuery = None,
) -> JSONResponse:
    await require_project(runtime, user.id, pid)
    name = clean_filename(filename)
    if not is_pdf_content_type(request.headers.get("content-type")):
        raise ApiError(415, "unsupported_format", "The body must be a PDF sent as application/pdf.")

    def build(outcome: AddOutcome) -> DocumentOut:
        if outcome.document is None:  # unreachable: every upload item has a document
            raise ApiInvariantError("a batch file without a document")
        return DocumentOut.model_validate(outcome.document, from_attributes=True)

    # SPECS 4.5: the legacy endpoint fills today's per-UTC-day batch.
    added = await add_streamed_file(
        runtime,
        request,
        user_id=user.id,
        project_id=pid,
        filename=name,
        batch_id=None,
        build=build,
    )
    document = added.built
    started = added.parse_started
    outcome = LEGACY_OUTCOMES[added.outcome]
    if added.outcome == "duplicate" and document.status == "failed":
        # Old behaviour kept for old tabs: a re-upload of a failed document of today's
        # batch parses it again (a batch add alone answers "duplicate"). The
        # conditional UPDATE lets one of two racing requests win.
        async with runtime.transaction() as session:
            if await repo.mark_processing(session, pid, document.id):
                row = await repo.get_document_state(session, pid, document.id)
                document = DocumentOut.model_validate(row, from_attributes=True)
                outcome = "retried"
        if outcome == "retried":
            started = await start_parse_or_mark_failed(runtime, user.id, pid, document.id)
    if started is False:
        document = document.model_copy(update={"status": "failed", "error": "workflow_start_failed"})
    created = outcome == "created"
    body = UploadOut(document=document, outcome=None if created else outcome)
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=201 if created else 200,
    )


@router.get("", response_model=Page[DocumentOut])
async def list_documents(
    pid: uuid.UUID,
    runtime: RuntimeDep,
    user: UserDep,
    limit: LimitQuery = 50,
    cursor: str | None = None,
) -> Page[DocumentOut]:
    await require_project(runtime, user.id, pid)
    try:
        async with runtime.transaction() as session:
            page = await repo.list_documents(session, pid, limit, cursor)
    except repo.InvalidCursorError:
        raise invalid_cursor() from None
    return Page[DocumentOut](
        items=[DocumentOut.model_validate(d, from_attributes=True) for d in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{did}", response_model=DocumentDetailOut)
async def get_document(pid: uuid.UUID, did: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> DocumentDetailOut:
    await require_project(runtime, user.id, pid)
    async with runtime.transaction() as session:
        row = await repo.get_document(session, pid, did)
    if row is None:
        raise document_not_found()
    markdown = row.markdown
    truncated = markdown is not None and len(markdown) > MARKDOWN_CAP
    base = DocumentOut.model_validate(row, from_attributes=True)
    return DocumentDetailOut(
        **base.model_dump(),
        markdown=markdown[:MARKDOWN_CAP] if markdown is not None else None,
        markdown_truncated=truncated,
        summarization=row.summarization,
        summarization_model=row.summarization_model,
        summarization_error=row.summarization_error,
        summarized_at=row.summarized_at,
    )


async def _load(runtime: Runtime, user_id: uuid.UUID, pid: uuid.UUID, did: uuid.UUID) -> Document:
    async with runtime.transaction() as session:
        if await repo.get_member_project(session, user_id, pid) is None:
            raise project_not_found()
        document = await repo.get_document_state(session, pid, did)  # no markdown
    if document is None:
        raise document_not_found()
    return document


def _start_failed(error: Exception, event: str, pid: uuid.UUID, did: uuid.UUID) -> ApiError:
    log_event(
        logger,
        WARNING,
        event,
        project_id=str(pid),
        document_id=str(did),
        error_type=type(error).__name__,
    )
    return ApiError(
        503,
        "workflow_start_failed",
        "The workflow could not be started. Try again later.",
    )


@router.post("/{did}/parse", status_code=202, response_model=ParseStarted)
async def parse_document(pid: uuid.UUID, did: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> ParseStarted:
    document = await _load(runtime, user.id, pid, did)
    if document.status in PARSED_STATUSES:
        raise ApiError(409, "already_parsed", "The document is already parsed.")
    async with runtime.transaction() as session:
        marked = await repo.mark_processing(session, pid, did)  # UPDATE first
    try:
        workflow_id = await workflows.start_parse(runtime, pid, did)
    except Exception as error:
        if marked:
            async with runtime.transaction() as session:
                await repo.mark_failed(session, pid, did, "workflow_start_failed")
        raise _start_failed(error, "api.document.parse_start_failed", pid, did) from error
    log_event(
        logger,
        INFO,
        "api.document.parse_started",
        user_id=str(user.id),
        project_id=str(pid),
        document_id=str(did),
        workflow_id=workflow_id,
    )
    return ParseStarted(workflow_id=workflow_id, status="processing")


@router.post("/{did}/summarize", status_code=202, response_model=SummarizeStarted)
async def summarize_document(
    pid: uuid.UUID,
    did: uuid.UUID,
    runtime: RuntimeDep,
    user: UserDep,
    body: SummarizeRequest | None = None,
) -> SummarizeStarted:
    document = await _load(runtime, user.id, pid, did)
    if body is not None and body.model is not None:
        run = runtime.settings.agent_run
        if run is None or body.model != run.model:  # the value is never echoed or logged
            raise model_not_allowed()
    if document.status != "parsed":
        raise ApiError(409, "document_not_parsed", "The document must be parsed first.")
    async with runtime.transaction() as session:
        marked = await repo.mark_summarizing(session, pid, did)  # UPDATE first
    try:
        workflow_id = await workflows.start_summarize(runtime, pid, did, user.id)
    except Exception as error:
        if marked:
            async with runtime.transaction() as session:
                await repo.mark_summarization_failed(session, pid, did, "workflow_start_failed")
        raise _start_failed(error, "api.summary.start_failed", pid, did) from error
    log_event(
        logger,
        INFO,
        "api.summary.started",
        user_id=str(user.id),
        project_id=str(pid),
        document_id=str(did),
        workflow_id=workflow_id,
    )
    return SummarizeStarted(workflow_id=workflow_id, summarization_status="running")
