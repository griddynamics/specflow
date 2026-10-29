"""Activities of the Drive import (INT-GDUI-0004, FR-GDUI-0017/0018).

All ``async def``: the database and the object store run on the worker's event
loop; the blocking Drive calls (list, download, Docs/Slides/Sheets reads) and the
converters run on a registration-owned thread pool (``drive-io``).

Rules kept here:

- No writable disk: a file is streamed Drive -> GCS chunk by chunk (create-only
  write); nothing is spooled to a temp file.
- Idempotent per file: ``document_id = uuid5(connection_id, file_id)`` names the
  object and the row, so a retry reuses both. ``ObjectAlreadyExistsError`` on a
  stored-only file or a Google JSON is done; on a PDF the row decides (the hash
  is only known while streaming): no row -> delete the orphan object and stream
  again. A duplicate content hash links the file to the existing document and
  deletes the own object; held by another source it ends as ``duplicate``.
- A file is claimed (``processing``) before work. The document insert runs under
  a ``FOR SHARE`` lock of the importing source: a removed source finds no row to
  write and the own object is dropped (fence, FR-SRC-0017, NFR-REL-0003).
- A per-file failure ends that file as ``error`` (reason + step) after bounded
  retries and never fails the run. A deleted connection ends the run quietly
  (no writes); a connection marked ``failed`` is never reopened.
- Messages that reach workflow history are fixed codes only (never file names).

Logs (NFR-MAIN-0001, architecture B6) carry ids and fixed codes, never names,
links, tokens, content or Google text.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Executor
from contextlib import asynccontextmanager, suppress
from typing import Any, Final, TypeVar, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from db import repositories, sources
from db.models import Connection, ConnectionItem, Document
from drive.categories import Category, ProcessingClass
from drive.convert import to_markdown
from drive.gateway import DriveError, DriveGateway
from drive.model import summarize
from drive.reasons import FileErrorReason, Reason
from logger.logger import ERROR, INFO, WARNING, get_logger, log_event
from storage import ObjectAlreadyExistsError, ObjectStore, ObjectStoreError
from storage.keys import (
    document_original,
    document_original_bin,
    document_original_json,
)
from temporal.containment import Contain
from temporal.ids import drive_document_id
from temporal.sources import workflows as wf
from temporal.sources.models import (
    BatchItem,
    BatchRequest,
    BatchResult,
    CompleteInput,
    DriveImportInput,
    FailImportInput,
    FinishItemInput,
    ImportItemInput,
    ImportItemResult,
    ItemKind,
    ListResult,
    WaitDocumentInput,
    WaitDocumentResult,
)

logger = get_logger("temporal.sources.activities")

HEARTBEAT_INTERVAL_SECONDS: Final = 20.0
#: A download whose key already exists is retried once after the orphan object is deleted.
STREAM_ATTEMPTS: Final = 2
#: The listing of a whole folder tree must end inside this (real ``too_slow`` after it).
LIST_DEADLINE_SECONDS: Final = 15 * 60.0
#: Output cap of a converted Google file (UTF-8 bytes of the Markdown). A larger
#: file ends as ``convert_failed`` (step ``convert``); its JSON original stays stored.
MAX_MARKDOWN_BYTES: Final = 5 * 1024 * 1024
#: Cap of the raw Google API response kept as the original (the converter's input).
MAX_GOOGLE_JSON_BYTES: Final = 32 * 1024 * 1024
#: A file the import still owns: ``processing`` once claimed, ``pending`` for a history
#: or retry that never claimed it (SPECS 6.1).
OPEN_ITEM_STATES: Final = ("pending", "processing")
WAIT_POLL_SECONDS: Final = 5.0
WAIT_LIMIT_SECONDS: Final = 60 * 60.0  # the parse workflow's execution limit
CONVERT_PARSER: Final = "drive-convert"
GOOGLE_SOURCE_TYPES: Final = frozenset({Category.GOOGLE_DOCS, Category.GOOGLE_SLIDES, Category.GOOGLE_SHEETS})

T = TypeVar("T")


class _ItemError(Exception):
    """A file failure: fixed reason and step; ``transient`` earns retries."""

    def __init__(self, reason: FileErrorReason, step: str, *, transient: bool = False):
        super().__init__(f"{reason.value}:{step}")
        self.reason, self.step, self.transient = reason, step, transient


def _drive_failure(error: DriveError, step: str) -> _ItemError:
    if error.reason is Reason.NO_ACCESS:
        return _ItemError(FileErrorReason.NO_ACCESS, step)
    if error.reason is Reason.DRIVE_UNAVAILABLE:
        return _ItemError(FileErrorReason.DRIVE_UNAVAILABLE, step, transient=True)
    return _ItemError(FileErrorReason.UNKNOWN_ERROR, step)


def _encode_json(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _convert(mime_type: str, payload: Any, max_bytes: int) -> str:
    """Markdown without NUL; larger than ``max_bytes`` UTF-8 raises (-> ``convert_failed``)."""

    markdown = to_markdown(mime_type, payload).replace("\x00", "")
    if len(markdown.encode("utf-8")) > max_bytes:
        raise ValueError("markdown over the cap")
    return markdown


def _fixed_errors(function: Callable[..., Any]) -> Callable[..., Any]:
    """Any unexpected exception leaves as a fixed, retryable code (history rule).

    Database and Google errors can carry statement parameters or file names; the
    Temporal SDK logs and stores the message, so it must never get out.
    """

    @functools.wraps(function)
    async def guarded(self: Any, data: Any) -> Any:
        with Contain(ApplicationError) as contained:
            return await function(self, data)
        log_event(
            logger,
            ERROR,
            "drive.import.activity_failed",
            activity=function.__name__,
            error_type=contained.error_type,
        )
        raise ApplicationError("internal_error", type="DriveImportError") from None

    return guarded


class _NoDrive:
    """Stands in when a registration was built without a gateway: every call is a setup error."""

    mode = "fake"

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise DriveError(Reason.SETUP_ERROR)

        return refuse


class DriveImportActivities:
    """Activities bound to one session factory, object store, gateway and executor."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        drive: DriveGateway | None,
        executor: Executor,
        *,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        max_markdown_bytes: int = MAX_MARKDOWN_BYTES,
        wait_interval: float = WAIT_POLL_SECONDS,
    ) -> None:
        self._sessions = session_factory
        self._store = store
        self._drive: DriveGateway = drive or cast("DriveGateway", _NoDrive())
        self._executor = executor
        self._heartbeat_interval = heartbeat_interval
        self._max_markdown_bytes = max_markdown_bytes
        self._wait_interval = wait_interval

    @property
    def activities(self) -> list[Callable[..., Any]]:
        return [
            self.drive_list,
            self.drive_next_batch,
            self.drive_import_item,
            self.drive_finish_item,
            self.drive_wait_document,
            self.drive_complete,
            self.drive_fail_import,
        ]

    # -- helpers -------------------------------------------------------------
    async def _blocking(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, functools.partial(function, *args, **kwargs)
        )

    @asynccontextmanager
    async def _heartbeating(self) -> AsyncIterator[None]:
        async def beat() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(self._heartbeat_interval)

        task = asyncio.create_task(beat())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    @staticmethod
    async def _lock(session: AsyncSession, project_id: uuid.UUID, connection_id: uuid.UUID) -> Connection | None:
        """The connection row locked until commit (a delete or state change waits)."""

        return await session.scalar(
            select(Connection)
            .where(Connection.project_id == project_id, Connection.id == connection_id)
            .with_for_update()
        )

    # -- listing ---------------------------------------------------------------
    @activity.defn(name=wf.LIST_ACTIVITY)
    @_fixed_errors
    async def drive_list(self, data: DriveImportInput) -> ListResult:
        """All levels -> ``pending`` rows, connection ``importing``; a Drive error fails the import."""

        async with self._sessions() as session:
            connection = await sources.get_connection(session, data.project_id, data.connection_id)
        if connection is None or connection.state not in ("listing", "importing"):
            return ListResult(outcome="stop")  # deleted, or already failed/done
        started = time.monotonic()
        try:
            async with self._heartbeating():
                listing = await self._blocking(
                    self._drive.list_all,
                    connection.external_id,
                    deadline=started + LIST_DEADLINE_SECONDS,
                )
        except DriveError as error:
            await self._fail(data.project_id, data.connection_id, error.reason)
            return ListResult(outcome="failed")
        async with self._sessions() as session, session.begin():
            locked = await self._lock(session, data.project_id, data.connection_id)
            if locked is None or locked.state not in ("listing", "importing"):
                return ListResult(outcome="stop")
            await sources.upsert_items(session, data.connection_id, data.project_id, listing.items)
            await sources.set_connection_state(session, data.connection_id, "importing")
        classes: Counter[str] = Counter()
        for row in summarize(listing):
            classes[row.processing_class.value] += row.count
        log_event(
            logger,
            INFO,
            "drive.import.listed",
            project_id=str(data.project_id),
            connection_id=str(data.connection_id),
            folder_id=connection.external_id,
            folder_count=listing.folder_count,
            file_count=len(listing.items),
            counts=dict(classes),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return ListResult(outcome="listed", files=len(listing.items))

    # -- batches ---------------------------------------------------------------
    @activity.defn(name=wf.BATCH_ACTIVITY)
    @_fixed_errors
    async def drive_next_batch(self, data: BatchRequest) -> BatchResult:
        """The next pending files; row-only (skipped) ones are finished right here."""

        async with self._sessions() as session, session.begin():
            connection = await sources.get_connection(session, data.project_id, data.connection_id)
            if connection is None or connection.state != "importing":
                return BatchResult(stop=True)
            ids = await sources.next_pending_items(session, data.connection_id, wf.BATCH_SIZE)
            if not ids:
                return BatchResult()
            rows = (
                await session.scalars(
                    select(ConnectionItem).where(ConnectionItem.id.in_(ids)).order_by(ConnectionItem.id)
                )
            ).all()
            work: list[BatchItem] = []
            for row in rows:
                kind = self._kind(row)
                if row.processing_class == ProcessingClass.SKIPPED.value:
                    await sources.finish_item(session, row.id, "skipped")
                elif kind is None:
                    await sources.finish_item(
                        session,
                        row.id,
                        "error",
                        error_reason=FileErrorReason.UNKNOWN_ERROR.value,
                        error_step="read",
                    )
                    self._log_file_failed(row, FileErrorReason.UNKNOWN_ERROR.value, "read")
                else:
                    work.append(BatchItem(item_id=row.id, kind=kind))
            return BatchResult(pending=len(ids), items=work)

    @staticmethod
    def _kind(row: ConnectionItem) -> ItemKind | None:
        if row.processing_class == ProcessingClass.STORED_ONLY.value:
            return "stored"
        if row.processing_class != ProcessingClass.PROCESSED.value:
            return None
        if row.category == Category.PDF.value:
            return "pdf"
        if row.category in {c.value for c in GOOGLE_SOURCE_TYPES}:
            return "google"
        return None

    # -- one file --------------------------------------------------------------
    @activity.defn(name=wf.ITEM_ACTIVITY)
    @_fixed_errors
    async def drive_import_item(self, data: ImportItemInput) -> ImportItemResult:
        """Skipped files never get here. A failure is recorded here on the last attempt."""

        step = "read" if data.kind == "google" else "download"
        with Contain(ApplicationError) as contained:
            async with self._heartbeating():
                return await self._import_item(data)
        if contained.error is not None:  # always: the block returns unless it failed
            failure = self._classify(contained.error, step, data)
            if failure.transient and activity.info().attempt < wf.ITEM_MAX_ATTEMPTS:
                raise ApplicationError(str(failure), type="ItemTransient") from None
            await self._finish_error(data, failure.reason.value, failure.step)
        return ImportItemResult(action="done")

    @staticmethod
    def _classify(error: Exception, step: str, data: ImportItemInput) -> _ItemError:
        if isinstance(error, _ItemError):
            return error
        if isinstance(error, DriveError):
            return _drive_failure(error, step)
        if isinstance(error, ObjectStoreError):
            return _ItemError(FileErrorReason.STORE_FAILED, "store", transient=True)
        log_event(
            logger,
            ERROR,
            "drive.import.item_attempt_failed",
            connection_id=str(data.connection_id),
            item_id=str(data.item_id),
            error_type=type(error).__name__,
        )
        return _ItemError(FileErrorReason.UNKNOWN_ERROR, step, transient=True)

    async def _import_item(self, data: ImportItemInput) -> ImportItemResult:
        # The claim (FR-SRC-0017, INT-SRC-0002): the file becomes ``processing`` while the
        # source is ``importing``. A retry after a killed pod claims it again.
        async with self._sessions() as session, session.begin():
            item = await sources.claim_item(session, data.project_id, data.connection_id, data.item_id)
        if item is None:
            return ImportItemResult(action="done")  # quiet: removed, failed or final
        document_id = drive_document_id(data.connection_id, item.external_id)
        if data.kind == "pdf":
            return await self._import_pdf(data, item, document_id)
        if data.kind == "google":
            return await self._import_google(data, item, document_id)
        return await self._import_stored(data, item, document_id)

    async def _import_stored(
        self, data: ImportItemInput, item: ConnectionItem, document_id: uuid.UUID
    ) -> ImportItemResult:
        key = document_original_bin(str(data.project_id), str(document_id))
        await self._stream(item.external_id, key, "application/octet-stream")
        await self._finish(item, "stored")
        return ImportItemResult(action="done")

    async def _import_pdf(
        self, data: ImportItemInput, item: ConnectionItem, document_id: uuid.UUID
    ) -> ImportItemResult:
        async with self._sessions() as session:
            existing = await repositories.get_document_state(session, data.project_id, document_id)
        if existing is not None:  # the row and object are there
            return await self._existing_pdf(data, item, document_id, existing)
        key = document_original(str(data.project_id), str(document_id))
        digest, size = await self._stream(item.external_id, key, "application/pdf", replace_orphan=True)
        async with self._sessions() as session, session.begin():
            # The fence (NFR-REL-0003): the source row stays locked FOR SHARE until the
            # insert commits, so a removal waits for it or finds this row (SPECS 5, 6.1).
            if await sources.lock_importing_connection(session, data.project_id, data.connection_id):
                document, created = await sources.insert_connection_document(
                    session,
                    project_id=data.project_id,
                    file=sources.DocumentFile(
                        document_id=document_id,
                        content_hash=digest,
                        filename=item.name,
                        size_bytes=size,
                        original_key=key,
                    ),
                    media_type="application/pdf",
                    status="processing",
                    source_id=data.connection_id,
                )
            else:
                document, created = None, False
        if document is None:
            return await self._fenced(data, document_id, key)
        if created:
            return ImportItemResult(action="parse", document_id=document_id)
        if document.id != document_id:
            # The same bytes are already a document of the project: no second copy.
            await self._drop_own_object(data, document_id, key)
        elif document.source_id == data.connection_id:  # a retry raced its own insert
            return ImportItemResult(action="parse", document_id=document_id)
        return await self._link(item, document)

    async def _existing_pdf(
        self, data: ImportItemInput, item: ConnectionItem, document_id: uuid.UUID, existing: Document
    ) -> ImportItemResult:
        """The document row and its object are already there: finish, parse or link."""

        if existing.source_id == data.connection_id:  # a retry of this source
            if existing.status in sources.PROCESSED_DOCUMENT_STATUSES:
                await self._finish(item, "processed", document_id=document_id)
                return ImportItemResult(action="done")
            return ImportItemResult(action="parse", document_id=document_id)
        # Another source holds that row (a hand-over or the backfill): its object
        # is theirs, never streamed over or deleted here.
        return await self._link(item, existing)

    async def _fenced(self, data: ImportItemInput, document_id: uuid.UUID, key: str) -> ImportItemResult:
        """The source is gone: late writes are dropped (fence)."""

        await self._drop_own_object(data, document_id, key)
        return ImportItemResult(action="done")

    async def _drop_own_object(self, data: ImportItemInput, document_id: uuid.UUID, key: str) -> None:
        """Delete this file's own object, unless a document row owns that id (a
        handed-over document keeps its object, R01). Used by the fence and by every
        content-duplicate branch."""

        async with self._sessions() as session:
            row = await repositories.get_document_state(session, data.project_id, document_id)
        if row is None:
            await self._store.delete(key)

    async def _link(self, item: ConnectionItem, document: Any) -> ImportItemResult:
        """Link a file to an existing document.

        Another source holds it (or nobody: an orphan): the file is ``duplicate``
        ("Already in project") at once, whatever the document's status (SD6,
        FR-SRC-0021). The same source (a twin in this folder, SD9) is pending until
        that document is final (INT-GDUI-0004: a file still being parsed is not
        finished) and then shows the one stored file's state.
        """

        if document.source_id != item.connection_id:
            await self._finish(item, "duplicate", document_id=document.id)
        elif document.status in sources.PROCESSED_DOCUMENT_STATUSES:
            await self._finish(item, "processed", document_id=document.id)
        elif document.status == "failed":
            await self._finish(
                item,
                "error",
                error_reason=FileErrorReason.PARSE_FAILED.value,
                error_step="parse",
                document_id=document.id,
            )
        else:
            return ImportItemResult(action="wait", document_id=document.id)
        return ImportItemResult(action="done")

    async def _import_google(
        self, data: ImportItemInput, item: ConnectionItem, document_id: uuid.UUID
    ) -> ImportItemResult:
        try:
            payload = await self._blocking(self._drive.read_google, item.external_id, item.mime_type)
        except DriveError as error:
            raise _drive_failure(error, "read") from None
        raw = await self._blocking(_encode_json, payload)
        if len(raw) > MAX_GOOGLE_JSON_BYTES:
            raise _ItemError(FileErrorReason.CONVERT_FAILED, "convert")
        key = document_original_json(str(data.project_id), str(document_id))
        await self._put(key, raw, "application/json")
        with Contain() as contained:
            markdown = await self._blocking(_convert, item.mime_type, payload, self._max_markdown_bytes)
        if contained.error is not None:  # ConversionError, a malformed payload or oversize: fixed code only
            raise _ItemError(FileErrorReason.CONVERT_FAILED, "convert") from None
        async with self._sessions() as session, session.begin():
            # The fence, as in ``_import_pdf``.
            if await sources.lock_importing_connection(session, data.project_id, data.connection_id):
                document, created = await sources.insert_connection_document(
                    session,
                    project_id=data.project_id,
                    file=sources.DocumentFile(
                        document_id=document_id,
                        content_hash=hashlib.sha256(raw).hexdigest(),
                        filename=item.name,
                        size_bytes=len(raw),
                        original_key=key,
                    ),
                    media_type="application/json",
                    markdown=markdown,
                    parser=CONVERT_PARSER,
                    status="parsed",
                    source_id=data.connection_id,
                )
            else:
                document, created = None, False
        if document is None:
            return await self._fenced(data, document_id, key)
        if not created and document.id != document_id:
            # duplicate content: keep the first document only
            await self._drop_own_object(data, document_id, key)
            return await self._link(item, document)
        if not created and document.source_id != data.connection_id:
            # the own id, held by another source
            return await self._link(item, document)
        await self._finish(item, "processed", document_id=document.id)
        return ImportItemResult(action="done")

    # -- storage ---------------------------------------------------------------
    async def _put(self, key: str, data: bytes, content_type: str) -> None:
        """Create-only write of a small object; an existing one is done."""

        try:
            writer = await self._store.open_writer(key, content_type=content_type)
            try:
                await writer.write(data)
                await writer.close()
            except BaseException:
                await writer.abort()
                raise
        except ObjectAlreadyExistsError:
            return
        except ObjectStoreError:
            raise _ItemError(FileErrorReason.STORE_FAILED, "store", transient=True) from None

    async def _stream(
        self,
        file_id: str,
        key: str,
        content_type: str,
        *,
        replace_orphan: bool = False,
    ) -> tuple[str, int]:
        """Drive -> object, chunk by chunk, hashing on the way; ``(sha256, size)``.

        An existing object is done for stored-only files (``("", 0)``); for a
        PDF (``replace_orphan``) it is an orphan of an attempt that died before the
        row was written, so it is deleted and written again.
        """

        for attempt in range(1, STREAM_ATTEMPTS + 1):
            try:
                return await self._stream_once(file_id, key, content_type)
            except ObjectAlreadyExistsError:
                if not replace_orphan:
                    return "", 0
                if attempt == STREAM_ATTEMPTS:
                    raise _ItemError(FileErrorReason.STORE_FAILED, "store", transient=True) from None
                await self._store.delete(key)
        raise AssertionError("unreachable")

    async def _stream_once(self, file_id: str, key: str, content_type: str) -> tuple[str, int]:
        try:
            chunks = await self._blocking(self._drive.iter_download, file_id)
        except DriveError as error:
            raise _drive_failure(error, "download") from None
        writer = await self._store.open_writer(key, content_type=content_type)
        digest, size = hashlib.sha256(), 0
        iterator = iter(chunks)
        try:
            while True:
                try:
                    chunk = await self._blocking(next, iterator, None)
                except DriveError as error:
                    raise _drive_failure(error, "download") from None
                if chunk is None:
                    break
                digest.update(chunk)
                size += len(chunk)
                await writer.write(chunk)
            await writer.close()
        except ObjectAlreadyExistsError:
            raise
        except ObjectStoreError:
            await writer.abort()
            raise _ItemError(FileErrorReason.STORE_FAILED, "store", transient=True) from None
        except BaseException:
            await writer.abort()
            raise
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                with suppress(Exception):
                    close()
        return digest.hexdigest(), size

    # -- finishing ---------------------------------------------------------------
    async def _finish(
        self,
        item: ConnectionItem,
        state: str,
        *,
        document_id: uuid.UUID | None = None,
        error_reason: str | None = None,
        error_step: str | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            await sources.finish_item(
                session,
                item.id,
                state,
                document_id=document_id,
                error_reason=error_reason,
                error_step=error_step,
            )
        if state == "error":
            self._log_file_failed(item, error_reason, error_step)

    async def _finish_error(self, data: ImportItemInput, reason: str, step: str) -> None:
        async with self._sessions() as session:
            item = await sources.get_item(session, data.item_id)
        if item is not None and item.state in OPEN_ITEM_STATES:
            await self._finish(item, "error", error_reason=reason, error_step=step)

    @staticmethod
    def _log_file_failed(item: ConnectionItem, reason: str | None, step: str | None) -> None:
        log_event(
            logger,
            WARNING,
            "drive.import.file_failed",
            project_id=str(item.project_id),
            connection_id=str(item.connection_id),
            file_id=item.external_id,
            mime_type=item.mime_type,
            step=step,
            reason=reason,
        )

    @activity.defn(name=wf.FINISH_ACTIVITY)
    @_fixed_errors
    async def drive_finish_item(self, data: FinishItemInput) -> None:
        """The workflow ends a file after its child parse (or after a lost activity)."""

        async with self._sessions() as session:
            item = await sources.get_item(session, data.item_id)
        if item is None or item.state not in OPEN_ITEM_STATES:
            return
        try:
            await self._finish(
                item,
                data.state,
                document_id=data.document_id,
                error_reason=data.reason,
                error_step=data.step,
            )
        except ValueError:
            raise ApplicationError("invalid_finish", type="InvalidFinish") from None
        own = drive_document_id(item.connection_id, item.external_id)
        if data.reason == "parse_failed" and data.document_id == own:
            # A child that timed out or was terminated left the document ``processing``.
            async with self._sessions() as session, session.begin():
                row = await repositories.get_document_state(session, data.project_id, own)
                if row is not None and row.status == "processing":
                    await repositories.mark_failed(session, data.project_id, own, "internal_error")

    @activity.defn(name=wf.WAIT_ACTIVITY)
    @_fixed_errors
    async def drive_wait_document(self, data: WaitDocumentInput) -> WaitDocumentResult:
        """Poll a shared document until it is parsed or failed (at most the parse limit)."""

        deadline = time.monotonic() + WAIT_LIMIT_SECONDS
        async with self._heartbeating():
            while True:
                async with self._sessions() as session:
                    row = await repositories.get_document_state(session, data.project_id, data.document_id)
                if row is None or row.status == "failed":
                    return WaitDocumentResult(status="failed")
                if row.status in sources.PROCESSED_DOCUMENT_STATUSES:
                    return WaitDocumentResult(status="parsed")
                if time.monotonic() >= deadline:
                    return WaitDocumentResult(status="timeout")
                await asyncio.sleep(self._wait_interval)

    # -- end of the import -------------------------------------------------------
    @activity.defn(name=wf.COMPLETE_ACTIVITY)
    @_fixed_errors
    async def drive_complete(self, data: CompleteInput) -> None:
        async with self._sessions() as session, session.begin():
            connection = await self._lock(session, data.project_id, data.connection_id)
            if connection is None or connection.state != "importing":
                return  # deleted, or failed meanwhile: never reopened
            await sources.set_connection_state(session, data.connection_id, "done")
            rows = await session.execute(
                select(ConnectionItem.state, func.count())
                .where(ConnectionItem.connection_id == data.connection_id)
                .group_by(ConnectionItem.state)
            )
            counts = {state: int(n) for state, n in rows.all()}
        log_event(
            logger,
            INFO,
            "drive.import.finished",
            project_id=str(data.project_id),
            connection_id=str(data.connection_id),
            processed=counts.get("processed", 0),
            stored=counts.get("stored", 0),
            skipped=counts.get("skipped", 0),
            error=counts.get("error", 0),
            duration_ms=self._duration_ms(data.started_epoch),
        )

    @staticmethod
    def _duration_ms(started_epoch: float | None) -> int | None:
        if started_epoch is None:
            return None
        return max(0, round((time.time() - started_epoch) * 1000))

    @activity.defn(name=wf.FAIL_ACTIVITY)
    @_fixed_errors
    async def drive_fail_import(self, data: FailImportInput) -> None:
        try:
            reason = Reason(data.reason)
        except ValueError:
            reason = Reason.UNKNOWN_ERROR
        await self._fail(data.project_id, data.connection_id, reason)

    async def _fail(self, project_id: uuid.UUID, connection_id: uuid.UUID, reason: Reason) -> None:
        """Connection ``failed`` + reason, every open file ``error``/``import_failed`` (AC4)."""

        async with self._sessions() as session, session.begin():
            connection = await self._lock(session, project_id, connection_id)
            if connection is None or connection.state not in ("listing", "importing"):
                return  # deleted or already ended: no writes
            failed_items = await sources.fail_unfinished_items(session, connection_id)
            await sources.set_connection_state(session, connection_id, "failed", failure_reason=reason.value)
        log_event(
            logger,
            ERROR if reason is Reason.UNKNOWN_ERROR else WARNING,
            "drive.import.failed",
            project_id=str(project_id),
            connection_id=str(connection_id),
            reason=reason.value,
            failed_items=failed_items,
        )
