"""Worker registration of the ``parse`` queue (``temporal.registry`` contract).

One app-role engine, one object store (one ``storage.Client``), one
single-thread parse executor and one ``drive-io`` pool (blocking Drive calls) per
process; ``close`` releases them. The Drive import (``DriveImportWorkflow``,
architecture A3) and ``SourceCleanupWorkflow`` (sources-ux) run on this same
``parse`` queue.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.ext.asyncio import AsyncEngine

from db.session import build_engine, build_session_factory
from drive.gateway import DriveGateway
from drive.select import build_drive_gateway
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import RoleSettings
from storage import ObjectStore
from storage import factory as storage_factory
from temporal.cleanup.activities import SourceCleanupActivities
from temporal.cleanup.workflows import SourceCleanupWorkflow
from temporal.containment import Contain
from temporal.documents.activities import ParseActivities
from temporal.documents.workflows import ParseDocumentWorkflow
from temporal.registry import Registration
from temporal.sources.activities import DriveImportActivities
from temporal.sources.workflows import DriveImportWorkflow

logger = get_logger("temporal.documents.registration")

PARSE_THREAD_PREFIX = "parse-cpu"
DRIVE_THREAD_PREFIX = "drive-io"
DRIVE_THREADS = 4
#: One thread per executor that must not run two sync activities at once.
SINGLE_THREAD = 1
IDENTITY_TIMEOUT_SECONDS = 5.0


def build_registration(
    engine: AsyncEngine,
    store: ObjectStore,
    *,
    executor: ThreadPoolExecutor | None = None,
    drive: DriveGateway | None = None,
    drive_executor: ThreadPoolExecutor | None = None,
) -> Registration:
    """The registration over ready parts (tests pass fakes).

    Without ``drive`` the import activities answer ``setup_error`` (no gateway).
    """

    owned = executor or ThreadPoolExecutor(max_workers=SINGLE_THREAD, thread_name_prefix=PARSE_THREAD_PREFIX)
    drive_owned = drive_executor or ThreadPoolExecutor(
        max_workers=DRIVE_THREADS, thread_name_prefix=DRIVE_THREAD_PREFIX
    )
    sessions = build_session_factory(engine)
    activities = ParseActivities(sessions, store, owned)
    drive_activities = DriveImportActivities(sessions, store, drive, drive_owned)
    cleanup_activities = SourceCleanupActivities(sessions, store)

    closed = False

    async def close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            await engine.dispose()
        finally:
            owned.shutdown(wait=False)
            drive_owned.shutdown(wait=False)
            store_close = getattr(store, "close", None)  # the in-memory store has none
            if store_close is not None:
                await store_close()

    return Registration(
        workflows=[ParseDocumentWorkflow, DriveImportWorkflow, SourceCleanupWorkflow],
        activities=[
            activities.parse_document,
            activities.mark_document_failed,
            *drive_activities.activities,
            *cleanup_activities.activities,
        ],
        close=close,
    )


def log_gateway_selected(drive: DriveGateway, *, role: str) -> None:
    """``drive.gateway.selected`` once per worker process (NFR-PORT-0002, NFR-MAIN-0001).

    The email is best effort: an unreadable identity is logged, never fatal.
    """

    email: str | None = None
    probe = ThreadPoolExecutor(max_workers=SINGLE_THREAD, thread_name_prefix="drive-identity")
    try:
        with Contain():  # DriveError, timeout, anything: the worker still starts, email stays None
            email = probe.submit(drive.identity).result(timeout=IDENTITY_TIMEOUT_SECONDS)
    finally:
        probe.shutdown(wait=False)
    log_event(
        logger,
        INFO if email else WARNING,
        "drive.gateway.selected",
        mode=drive.mode,
        email=email,
        role=role,
    )


def register(settings: RoleSettings) -> Registration:
    """Lazy engine (no connection yet), the GCS store and the Drive gateway, built once per process."""

    if settings.storage is None:
        raise ValueError(f"Role {settings.role!r} has no storage settings")
    drive = build_drive_gateway(settings.app.app_env, role="worker")
    log_gateway_selected(drive, role="worker")
    return build_registration(
        build_engine(settings.database),
        storage_factory.build_object_store(settings.storage, app_env=settings.app.app_env),
        drive=drive,
    )
