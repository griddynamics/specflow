"""Sources: the Drive identity, folder test, save and the project's sources status.

Requirements: INT-GDUI-0001 (identity), INT-GDUI-0002 (test), INT-GDUI-0003
(save), INT-GDUI-0004 (status), NFR-REL-0001/0002 (time limits), NFR-MAIN-0001
(logs), FR-GDUI-0012 (stable reason codes; no Google text ever leaves here).

Drive calls are blocking: they run on the Runtime's own pool behind a
semaphore, and the server limits (test 55 s, save 25 s) sit inside the web
app's 60 s / 30 s so the server's ``too_slow`` is what the user sees.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any, Final

from fastapi import APIRouter
from pydantic import TypeAdapter

from api import source_views, workflows
from api.contained import Contained
from api.deps import RuntimeDep, UserDep
from api.errors import ApiError, required
from api.routers.documents import require_project
from api.runtime import DriveTimeoutError, Runtime
from api.schemas import (
    ConnectionStateName,
    DriveCategoryOut,
    DriveFolderOut,
    DriveIdentityOut,
    DriveLinkRequest,
    DriveTestFailed,
    DriveTestOk,
    ProgressOut,
    SourceCreated,
    SourceOut,
    SourcesStatusOut,
    SourcesStatusV2Out,
    UploadsStatusOut,
)
from db import source_files
from db import sources as repo
from db.models import Connection
from drive.gateway import DriveError
from drive.links import canonical_link, parse_folder_link
from drive.model import ordered, summarize
from drive.reasons import Reason
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.routers.sources")
router = APIRouter(tags=["sources"])

KIND: Final = "gdrive"
TEST_LIMIT_SECONDS: Final = 55.0  # NFR-REL-0001: the web app gives up at 60 s
SAVE_LIMIT_SECONDS: Final = 25.0  # NFR-REL-0002: the web app gives up at 30 s
IDENTITY_LIMIT_SECONDS: Final = 10.0

_CONNECTION_STATE: TypeAdapter[ConnectionStateName] = TypeAdapter(ConnectionStateName)

# Reason -> (HTTP status, fixed message). Clients key on the envelope code.
REASON_RESPONSES: Final[dict[Reason, tuple[int, str]]] = {
    Reason.INVALID_LINK: (422, "The link is not a Drive folder link."),
    Reason.NOT_A_FOLDER: (422, "The link does not point to a folder."),
    Reason.NO_ACCESS: (409, "The folder is not shared with the Drive account."),
    Reason.ALREADY_CONNECTED: (409, "The folder is already connected."),
    Reason.DRIVE_UNAVAILABLE: (503, "Google Drive is unavailable. Try again later."),
    Reason.SETUP_ERROR: (503, "Google Drive access is not set up."),
    Reason.TOO_SLOW: (504, "Google Drive took too long to answer."),
    Reason.UNKNOWN_ERROR: (502, "The Drive request failed."),
}
_LOUD: Final = frozenset({Reason.SETUP_ERROR, Reason.UNKNOWN_ERROR})


def reason_error(reason: Reason) -> ApiError:
    status, message = REASON_RESPONSES[reason]
    return ApiError(status, reason.value, message)


async def drive_call[T](runtime: Runtime, function: Callable[..., T], *args: Any, by: float, **kwargs: Any) -> T:
    """One Drive call as a result or a ``DriveError``: nothing Google-worded gets out."""

    try:
        return await runtime.drive_call(function, *args, by=by, **kwargs)
    except DriveError:
        raise
    except DriveTimeoutError:
        raise DriveError(Reason.TOO_SLOW) from None
    except Exception as error:
        log_event(
            logger,
            WARNING,
            "api.drive.call_failed",
            error_type=type(error).__name__,
        )
        raise DriveError(Reason.UNKNOWN_ERROR) from error


def _gateway(runtime: Runtime) -> Any:
    if runtime.drive is None:
        raise DriveError(Reason.SETUP_ERROR)
    return runtime.drive


def _millis(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def _folder_of(runtime: Runtime, pid: uuid.UUID, link: str) -> str:
    """Link -> folder id; the check order is: link, already connected (no Google call)."""

    folder_id = parse_folder_link(link)
    if folder_id is None:
        raise DriveError(Reason.INVALID_LINK)
    async with runtime.transaction() as session:
        existing = await repo.find_connection(session, pid, KIND, folder_id)
    if existing is not None:
        raise DriveError(Reason.ALREADY_CONNECTED)
    return folder_id


def _log_level(reason: Reason | None) -> int:
    return WARNING if reason in _LOUD else INFO


# Identity -------------------------------------------------------------------


@router.get("/drive/identity", response_model=DriveIdentityOut)
async def drive_identity(runtime: RuntimeDep, user: UserDep) -> DriveIdentityOut:
    """The email the Drive account has (INT-GDUI-0001); 503 + reason code when unreadable."""

    try:
        drive = _gateway(runtime)
        email = await drive_call(
            runtime,
            drive.identity,
            by=time.monotonic() + IDENTITY_LIMIT_SECONDS,
            identity=True,
        )
    except DriveError as error:
        log_event(
            logger,
            WARNING,
            "api.drive.identity",
            user_id=str(user.id),
            result="failed",
            reason=error.reason.value,
        )
        message = REASON_RESPONSES[error.reason][1]
        raise ApiError(503, error.reason.value, message) from None
    return DriveIdentityOut(email=email)


# Test -----------------------------------------------------------------------


@router.post(
    "/projects/{pid}/sources/gdrive/test",
    response_model=DriveTestOk | DriveTestFailed,
)
async def test_drive_folder(
    pid: uuid.UUID, body: DriveLinkRequest, runtime: RuntimeDep, user: UserDep
) -> DriveTestOk | DriveTestFailed:
    """INT-GDUI-0002: look at the folder, count its files, create nothing."""

    await require_project(runtime, user.id, pid)
    started = time.monotonic()
    by = started + TEST_LIMIT_SECONDS
    folder_id: str | None = None
    result: DriveTestOk | None = None
    reason: Reason | None = None
    failure: str | None = None
    counts: dict[str, int] = {}
    folders = files = 0
    with Contained(passing=(ApiError,)) as test:  # an ApiError is its own answer
        try:
            folder_id = await _folder_of(runtime, pid, body.link)
            drive = _gateway(runtime)
            folder = await drive_call(runtime, drive.check_folder, folder_id, by=by)
            listing = await drive_call(runtime, drive.list_all, folder_id, by=by, deadline=by)
            rows = ordered(summarize(listing))
            folders, files = listing.folder_count, len(listing.items)
            counts = {row.category.value: row.count for row in rows}
            result = DriveTestOk(
                folder=DriveFolderOut(id=folder.id, name=folder.name),
                folder_count=folders,
                file_count=files,
                categories=[
                    DriveCategoryOut(
                        category=row.category.value,
                        processing_class=row.processing_class.value,
                        count=row.count,
                    )
                    for row in rows
                ],
            )
        except DriveError as error:
            reason = error.reason
    if test.failed:
        reason, failure = Reason.UNKNOWN_ERROR, test.error_type
    log_event(
        logger,
        _log_level(reason),
        "api.drive.tested",
        user_id=str(user.id),
        project_id=str(pid),
        folder_id=folder_id,
        result="ok" if result else ("error" if failure else "failed"),
        reason=reason.value if reason else None,
        error_type=failure,
        folder_count=folders,
        file_count=files,
        counts=counts,
        duration_ms=_millis(started),
    )
    if result is not None:
        return result
    return DriveTestFailed(reason=required(reason, "a failed test has a reason").value)


# Save -----------------------------------------------------------------------


def source_out(
    row: Connection,
    *,
    state: str | None = None,
    failure_reason: str | None = None,
    progress: repo.Progress | None = None,
) -> SourceOut:
    shown = progress or repo.Progress(listing_finished=False, total=0, finished=0)
    return SourceOut(
        id=row.id,
        kind=KIND,
        folder_id=row.external_id,
        link=required(row.link, "a Drive source has a link"),
        name=row.name,
        state=_CONNECTION_STATE.validate_python(state or row.state),
        failure_reason=failure_reason if state else row.failure_reason,
        connected_by=row.connected_by,
        created_at=row.created_at,
        last_synced_at=row.last_synced_at,
        progress=ProgressOut(
            listing_finished=shown.listing_finished,
            total=shown.total,
            finished=shown.finished,
        ),
    )


async def _compensate(runtime: Runtime, pid: uuid.UUID, connection_id: uuid.UUID) -> None:
    """The import never started: remove the source, else it would lock Build knowledge for ever.

    If the delete itself fails, the source is marked ``failed`` / ``unknown_error``
    so it never stays ``listing`` without a worker; if that fails too, only the
    ids are logged.
    """

    with Contained() as delete:
        async with runtime.transaction() as session:
            await repo.delete_connection_if_empty(session, pid, connection_id)
    if not delete.failed:
        return
    with Contained() as mark:
        async with runtime.transaction() as session:
            await repo.set_connection_state(
                session,
                connection_id,
                "failed",
                failure_reason=Reason.UNKNOWN_ERROR.value,
            )
    if mark.failed:
        log_event(
            logger,
            WARNING,
            "api.drive.compensation_failed",
            project_id=str(pid),
            connection_id=str(connection_id),
            error_type=mark.error_type,
            delete_error_type=delete.error_type,
        )


async def _create_source(
    runtime: Runtime, pid: uuid.UUID, user_id: uuid.UUID, folder_id: str, name: str
) -> SourceCreated:
    """Create the source of a folder once; a concurrent save that won is ``already_connected``."""

    created: SourceCreated | None = None
    async with runtime.transaction() as session:
        row, new = await repo.create_connection(
            session,
            project_id=pid,
            kind=KIND,
            external_id=folder_id,
            link=canonical_link(folder_id),
            name=name,
            connected_by=user_id,
        )
        if new:
            created = SourceCreated(source=source_out(row))
    if created is None:  # a concurrent save won: exactly one source per folder
        raise DriveError(Reason.ALREADY_CONNECTED)
    return created


@router.post("/projects/{pid}/sources/gdrive", status_code=201, response_model=SourceCreated)
async def save_drive_folder(
    pid: uuid.UUID, body: DriveLinkRequest, runtime: RuntimeDep, user: UserDep
) -> SourceCreated:
    """INT-GDUI-0003: re-check, create the source once, start the import."""

    await require_project(runtime, user.id, pid)
    started = time.monotonic()
    by = started + SAVE_LIMIT_SECONDS
    folder_id: str | None = None
    connection_id: uuid.UUID | None = None
    reason: Reason | None = None
    created: SourceCreated | None = None
    failure: str | None = None
    with Contained(passing=(ApiError,)) as save:  # e.g. database_unavailable: its own answer
        try:
            folder_id = await _folder_of(runtime, pid, body.link)
            drive = _gateway(runtime)
            folder = await drive_call(runtime, drive.check_folder, folder_id, by=by)
            created = await _create_source(runtime, pid, user.id, folder_id, folder.name)
            connection_id = created.source.id
            try:
                await workflows.start_drive_import(runtime, pid, connection_id)
            except Exception as error:
                log_event(
                    logger,
                    WARNING,
                    "api.drive.import_start_failed",
                    project_id=str(pid),
                    connection_id=str(connection_id),
                    error_type=type(error).__name__,
                )
                await _compensate(runtime, pid, connection_id)
                created = None
                raise DriveError(Reason.UNKNOWN_ERROR) from error
        except DriveError as error:
            reason = error.reason
    if save.failed:
        reason, created, failure = Reason.UNKNOWN_ERROR, None, save.error_type
    log_event(
        logger,
        _log_level(reason),
        "api.drive.saved",
        user_id=str(user.id),
        project_id=str(pid),
        folder_id=folder_id,
        connection_id=str(connection_id) if created else None,
        result="ok" if created else ("error" if failure else "failed"),
        reason=reason.value if reason else None,
        error_type=failure,
        duration_ms=_millis(started),
    )
    if created is None:
        raise reason_error(reason or Reason.UNKNOWN_ERROR)
    return created


# Status ---------------------------------------------------------------------


@router.get("/projects/{pid}/sources", response_model=SourcesStatusV2Out | SourcesStatusOut)
async def sources_status(
    pid: uuid.UUID, runtime: RuntimeDep, user: UserDep, batches: str | None = None
) -> SourcesStatusV2Out | SourcesStatusOut:
    """INT-GDUI-0004: per source and for the project; uploads are counted too (K4).

    ``?batches=1`` (INT-SRC-0001) answers the unified cards instead; without the flag
    the body is the legacy shape, for old tabs and pods during a rolling deploy (SPECS 9).
    """

    await require_project(runtime, user.id, pid)
    if batches == "1":
        async with runtime.transaction() as session:
            summaries = await source_files.source_summaries(session, pid)
            processed = await repo.count_processed_documents(session, pid)
            removed = await source_views.removed_files(session, pid)
        return source_views.status_v2(summaries, processed, removed)
    async with runtime.transaction() as session:
        progress = await repo.sources_progress(session, pid)
        rows = await repo.list_connections(session, pid, KIND)  # Drive only
        processed = await repo.count_processed_documents(session, pid)
    by_id = {item.connection_id: item for item in progress.connections}
    sources = []
    for row in rows:
        item = by_id.get(row.id)  # None: created between the two reads
        sources.append(
            source_out(
                row,
                state=item.state if item else row.state,
                failure_reason=item.failure_reason if item else row.failure_reason,
                progress=item.progress if item else None,
            )
        )
    uploads = progress.uploads
    return SourcesStatusOut(
        progress=_progress(progress.progress),
        sources=sources,
        processed_documents=processed,
        uploads=(
            UploadsStatusOut(
                progress=_progress(uploads.progress),
                documents=uploads.documents,
                failed=uploads.failed,
            )
            if uploads
            else None
        ),
    )


def _progress(value: repo.Progress) -> ProgressOut:
    return ProgressOut(
        listing_finished=value.listing_finished,
        total=value.total,
        finished=value.finished,
    )
