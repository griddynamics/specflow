"""Application-role data access for sources: connections and their listed files.

Same rules as ``db.repositories``: every function runs inside the
caller's session and transaction, flushes and never commits. Names of files
live in the database only; nothing here logs.

Requirements: INT-GDUI-0003 (one source per project and folder),
INT-GDUI-0004 (progress), DATA-GDUI-0001 (categories), DATA-SRC-0002 (holder,
``source_id``), INT-SRC-0001 (a Local files batch is a source of kind ``upload``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Select,
    String,
    and_,
    case,
    delete,
    exists,
    func,
    literal,
    null,
    or_,
    select,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from db.models import (
    CONNECTION_FAILURE_REASONS,
    CONNECTION_KINDS,
    CONNECTION_STATES,
    DOCUMENT_STATUSES,
    ITEM_ERROR_REASONS,
    ITEM_ERROR_STEPS,
    Connection,
    ConnectionItem,
    Document,
)
from drive.categories import categorize
from drive.model import DriveItem

# An upload still ``processing`` after the parse activity's execution timeout
# is finished for progress (the parse can no longer report back).
UPLOAD_STALE_AFTER: Final = timedelta(minutes=60)
# A Drive import has this long as a whole (workflow execution timeout). A hard
# timeout runs no code, so a connection older than this and still
# listing/importing reads as failed (read time only, nothing is written).
DRIVE_IMPORT_EXECUTION_TIMEOUT: Final = timedelta(hours=12)

CONNECTION_ORIGIN: Final = "connection"
STALE_FAILURE_REASON: Final = "unknown_error"
FINAL_ITEM_STATES: Final = frozenset({"processed", "stored", "skipped", "error", "duplicate"})
_INSERT_CHUNK: Final = 500  # rows per INSERT; 13 columns stay far below the bind limit


def _checked(value: str | None, allowed: Sequence[str], what: str) -> str | None:
    if value is not None and value not in allowed:
        raise ValueError(f"unknown {what}; expected one of {sorted(allowed)}")
    return value


# Connections --------------------------------------------------------------


async def create_connection(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    kind: str,
    external_id: str,
    link: str,
    name: str,
    connected_by: uuid.UUID,
) -> tuple[Connection, bool]:
    """Insert a source in state ``listing``; ``(row, created)``.

    One row per (project, kind, folder) at most (INT-GDUI-0003.AC2): the
    second of two concurrent calls waits on the unique index, then returns the
    first one's row with ``created=False``.
    """

    _checked(kind, CONNECTION_KINDS, "connection kind")
    values = {
        "id": uuid.uuid4(),
        "project_id": project_id,
        "kind": kind,
        "external_id": external_id,
        "link": link,
        "name": name,
        "connected_by": connected_by,
    }
    insert = (
        pg_insert(Connection)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_connections_project_kind_external")
        .returning(Connection)
    )
    # A row deleted between the conflict and the read (start-failure
    # compensation) makes the second pass insert again.
    for _ in range(3):
        created = (await session.scalars(insert)).one_or_none()
        if created is not None:
            return created, True
        existing = await find_connection(session, project_id, kind, external_id)
        if existing is not None:
            return existing, False
    raise RuntimeError("connection create did not settle")


async def get_connection(session: AsyncSession, project_id: uuid.UUID, connection_id: uuid.UUID) -> Connection | None:
    return await session.scalar(
        select(Connection).where(Connection.project_id == project_id, Connection.id == connection_id)
    )


async def list_connections(session: AsyncSession, project_id: uuid.UUID, kind: str | None = None) -> list[Connection]:
    """The project's sources, oldest first; ``kind`` limits them to one kind."""

    statement = (
        select(Connection).where(Connection.project_id == project_id).order_by(Connection.created_at, Connection.id)
    )
    if kind is not None:
        statement = statement.where(Connection.kind == _checked(kind, CONNECTION_KINDS, "connection kind"))
    return list(await session.scalars(statement))


async def lock_importing_connection(
    session: AsyncSession, project_id: uuid.UUID, connection_id: uuid.UUID
) -> Connection | None:
    """Lock the source ``FOR SHARE``; ``None`` unless it is ``importing``.

    A removal takes the same row ``FOR UPDATE``, so a worker that holds this lock
    finishes its write before the removal proceeds, and a worker that comes
    later sees the source gone (SPECS 3.4, 5).
    """

    return await session.scalar(
        select(Connection)
        .where(
            Connection.project_id == project_id,
            Connection.id == connection_id,
            Connection.state == "importing",
        )
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )


async def find_connection(
    session: AsyncSession, project_id: uuid.UUID, kind: str, external_id: str
) -> Connection | None:
    return await session.scalar(
        select(Connection).where(
            Connection.project_id == project_id,
            Connection.kind == kind,
            Connection.external_id == external_id,
        )
    )


async def delete_connection_if_empty(session: AsyncSession, project_id: uuid.UUID, connection_id: uuid.UUID) -> bool:
    """Delete a source that has no listed files (start-failure compensation).

    ``False`` when it is missing or already has items: a started import is never
    deleted from under its worker.
    """

    has_items = exists().where(ConnectionItem.connection_id == Connection.id)
    deleted = await session.scalars(
        delete(Connection)
        .where(
            Connection.project_id == project_id,
            Connection.id == connection_id,
            ~has_items,
        )
        .returning(Connection.id)
        .execution_options(synchronize_session=False)
    )
    return deleted.one_or_none() is not None


async def set_connection_state(
    session: AsyncSession,
    connection_id: uuid.UUID,
    state: str,
    *,
    failure_reason: str | None = None,
) -> bool:
    """Set the state; ``False`` when the connection does not exist.

    ``failed`` needs a reason of the closed connection set (never the file-level
    ``import_failed``); any other state clears it. ``done`` stamps
    ``last_synced_at``.
    """

    _checked(state, CONNECTION_STATES, "connection state")
    _checked(failure_reason, CONNECTION_FAILURE_REASONS, "failure reason")
    if (state == "failed") != (failure_reason is not None):
        raise ValueError("a failure reason goes with state 'failed' and only with it")
    values: dict[str, object] = {
        "state": state,
        "failure_reason": failure_reason,
        "updated_at": func.now(),
    }
    if state == "done":
        values["last_synced_at"] = func.now()
    changed = await session.scalars(
        update(Connection)
        .where(Connection.id == connection_id)
        .values(**values)
        .returning(Connection.id)
        .execution_options(synchronize_session=False)
    )
    return changed.one_or_none() is not None


# Items --------------------------------------------------------------------


def _aware(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Drive modifiedTime must carry a time zone")
    return parsed


def _item_values(connection_id: uuid.UUID, project_id: uuid.UUID, item: DriveItem) -> dict[str, object]:
    category, processing_class = categorize(item.mime_type)
    return {
        "id": uuid.uuid4(),
        "connection_id": connection_id,
        "project_id": project_id,
        "external_id": item.id,
        "name": item.name,
        "mime_type": item.mime_type,
        "category": category.value,
        "processing_class": processing_class.value,
        "size_bytes": item.size,
        "modified_time": _aware(item.modified_time),
        "drive_version": item.version,
        "md5": item.md5,
    }


async def upsert_items(
    session: AsyncSession,
    connection_id: uuid.UUID,
    project_id: uuid.UUID,
    items: Sequence[DriveItem],
) -> int:
    """Add the listed files as ``pending`` rows; the number of rows added.

    Categories come from the mime type only (DATA-GDUI-0001.AC5). A file already
    stored for the connection is left as it is, so a re-run of the listing never
    resets progress.
    """

    added = 0
    for start in range(0, len(items), _INSERT_CHUNK):
        rows = [_item_values(connection_id, project_id, item) for item in items[start : start + _INSERT_CHUNK]]
        inserted = await session.scalars(
            pg_insert(ConnectionItem)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_connection_items_connection_external")
            .returning(ConnectionItem.id)
        )
        added += len(inserted.all())
    return added


async def next_pending_items(session: AsyncSession, connection_id: uuid.UUID, limit: int) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(ConnectionItem.id)
        .where(
            ConnectionItem.connection_id == connection_id,
            ConnectionItem.state == "pending",
        )
        .order_by(ConnectionItem.id)
        .limit(limit)
    )
    return list(rows)


async def get_item(session: AsyncSession, item_id: uuid.UUID) -> ConnectionItem | None:
    return await session.get(ConnectionItem, item_id)


async def claim_item(
    session: AsyncSession,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
    item_id: uuid.UUID,
) -> ConnectionItem | None:
    """Start one file: ``pending|processing`` -> ``processing``; the row, or ``None``.

    ``None`` when the source is gone or no longer ``importing`` (removed, failed)
    or the file already has a final state. A retry of the activity claims a
    ``processing`` file again. The source row stays locked ``FOR SHARE`` until the
    transaction ends.
    """

    if await lock_importing_connection(session, project_id, connection_id) is None:
        return None
    return await session.scalar(
        update(ConnectionItem)
        .where(
            ConnectionItem.id == item_id,
            ConnectionItem.connection_id == connection_id,
            ConnectionItem.project_id == project_id,
            ConnectionItem.state.in_(("pending", "processing")),
        )
        .values(state="processing", updated_at=func.now())
        .returning(ConnectionItem)
        .execution_options(populate_existing=True, synchronize_session=False)
    )


async def finish_item(
    session: AsyncSession,
    item_id: uuid.UUID,
    state: str,
    *,
    document_id: uuid.UUID | None = None,
    error_reason: str | None = None,
    error_step: str | None = None,
) -> bool:
    """End one file: a final state, only from ``pending`` or ``processing``.

    ``duplicate`` links the document another source already holds. ``False`` when
    the file already has a final state (a retry after success, or the import
    already failed): the first final state stays.
    """

    if state not in FINAL_ITEM_STATES:
        raise ValueError(f"not a final file state; expected one of {sorted(FINAL_ITEM_STATES)}")
    _checked(error_reason, ITEM_ERROR_REASONS, "file error reason")
    _checked(error_step, ITEM_ERROR_STEPS, "file error step")
    if (state == "error") != (error_reason is not None):
        raise ValueError("an error reason goes with state 'error' and only with it")
    if error_step is not None and state != "error":
        raise ValueError("an error step goes with state 'error' only")
    if state == "duplicate" and document_id is None:
        raise ValueError("a duplicate file needs the document it links")
    changed = await session.scalars(
        update(ConnectionItem)
        .where(
            ConnectionItem.id == item_id,
            ConnectionItem.state.in_(("pending", "processing")),
        )
        .values(
            state=state,
            document_id=document_id,
            error_reason=error_reason,
            error_step=error_step,
            updated_at=func.now(),
        )
        .returning(ConnectionItem.id)
        .execution_options(synchronize_session=False)
    )
    return changed.one_or_none() is not None


async def fail_unfinished_items(session: AsyncSession, connection_id: uuid.UUID, step: str | None = None) -> int:
    """End every ``pending`` or ``processing`` file as ``error`` / ``import_failed``
    (INT-GDUI-0004.AC4).

    Returns the number of files ended. ``import_failed`` is a file reason only;
    the connection's own reason is set with ``set_connection_state``.
    """

    _checked(step, ITEM_ERROR_STEPS, "file error step")
    changed = await session.scalars(
        update(ConnectionItem)
        .where(
            ConnectionItem.connection_id == connection_id,
            ConnectionItem.state.in_(("pending", "processing")),
        )
        .values(
            state="error",
            error_reason="import_failed",
            error_step=step,
            updated_at=func.now(),
        )
        .returning(ConnectionItem.id)
        .execution_options(synchronize_session=False)
    )
    return len(changed.all())


# Documents of a connection --------------------------------------------------


@dataclass(frozen=True)
class DocumentFile:
    """The stored original of a new document: its id, content, name, size and object key."""

    document_id: uuid.UUID
    content_hash: str
    filename: str  # the ``original_filename`` column
    size_bytes: int
    original_key: str


async def insert_connection_document(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    file: DocumentFile,
    media_type: str,
    markdown: str | None = None,
    parser: str | None = None,
    status: str = "uploaded",
    source_id: uuid.UUID | None = None,
) -> tuple[Document, bool]:
    """Insert a document with ``origin='connection'``; ``(row, created)``.

    ``source_id`` is the holder (DATA-SRC-0002): the source that brings the content.

    A project that already holds the same content hash (an upload, another
    connection, or this very row on a retry) keeps its row: that one is returned
    with ``created=False`` and nothing is written. No uploader is recorded.
    """

    _checked(status, DOCUMENT_STATUSES, "document status")
    created = (
        await session.scalars(
            pg_insert(Document)
            .values(
                id=file.document_id,
                project_id=project_id,
                content_hash=file.content_hash,
                original_filename=file.filename,
                media_type=media_type,
                size_bytes=file.size_bytes,
                origin=CONNECTION_ORIGIN,
                original_key=file.original_key,
                markdown=markdown,
                parser=parser,
                status=status,
                source_id=source_id,
            )
            .on_conflict_do_nothing()  # the content-hash unique index or the primary key
            .returning(Document)
        )
    ).one_or_none()
    if created is not None:
        return created, True
    existing = await session.scalar(
        select(Document).where(Document.project_id == project_id, Document.content_hash == file.content_hash)
    )
    if existing is None:
        existing = await session.scalar(
            select(Document).where(Document.project_id == project_id, Document.id == file.document_id)
        )
    if existing is None:
        raise RuntimeError("document insert conflicted with a row outside the project")
    return existing, False


# Progress -------------------------------------------------------------------


@dataclass(frozen=True)
class Progress:
    """INT-GDUI-0004: listing state, total and finished files."""

    listing_finished: bool
    total: int
    finished: int


@dataclass(frozen=True)
class ConnectionProgress:
    """One source: the state to report (stale imports read as failed) and its numbers."""

    connection_id: uuid.UUID
    state: str
    failure_reason: str | None
    progress: Progress


@dataclass(frozen=True)
class UploadsProgress:
    progress: Progress
    documents: int
    failed: int


@dataclass(frozen=True)
class SourcesProgress:
    progress: Progress  # the whole project: AND of listing_finished, sums of the rest
    connections: tuple[ConnectionProgress, ...]
    uploads: UploadsProgress | None  # None when the project has no uploaded document


def _sources_progress_statement(project_id: uuid.UUID) -> Select[Any]:
    """One statement; one row per connection plus one row for the uploads."""

    uuid_type = PG_UUID(as_uuid=True)
    # Drive sources only: an upload batch's state is never read (SPECS 3.3).
    stale = and_(
        Connection.state.in_(("listing", "importing")),
        Connection.created_at < func.now() - DRIVE_IMPORT_EXECUTION_TIMEOUT,
    )
    items = (
        select(
            ConnectionItem.connection_id.label("connection_id"),
            func.count().label("total"),
            func.count().filter(ConnectionItem.state.in_(sorted(FINAL_ITEM_STATES))).label("finished"),
        )
        .where(ConnectionItem.project_id == project_id)
        .group_by(ConnectionItem.connection_id)
        .cte("item_counts")
    )
    effective_state = case((stale, "failed"), else_=Connection.state)
    effective_reason = case((stale, STALE_FAILURE_REASON), else_=Connection.failure_reason)
    total = func.coalesce(items.c.total, 0)
    # A failed import ends every file (INT-GDUI-0004.AC4), also when the failure
    # is only read from the clock.
    finished = case((effective_state == "failed", total), else_=func.coalesce(items.c.finished, 0))
    connections = (
        select(
            literal("connection").label("row_kind"),
            Connection.id.label("connection_id"),
            effective_state.label("state"),
            effective_reason.label("failure_reason"),
            total.cast(BigInteger).label("total"),
            finished.cast(BigInteger).label("finished"),
            literal(0).cast(BigInteger).label("failed"),
            Connection.created_at.label("created_at"),
        )
        .select_from(Connection)
        .outerjoin(items, items.c.connection_id == Connection.id)
        .where(Connection.project_id == project_id, Connection.kind == "gdrive")
    )
    # An upload that is still 'uploaded' (not parsed yet) is not finished
    # (FR-GDUI-0021: Build knowledge waits for processed files).
    upload_open = Document.status.in_(("processing", "uploaded"))
    upload_stale = and_(
        upload_open,
        Document.updated_at < func.now() - UPLOAD_STALE_AFTER,
    )
    uploads = select(
        literal("uploads").label("row_kind"),
        null().cast(uuid_type).label("connection_id"),
        null().cast(String).label("state"),
        null().cast(String).label("failure_reason"),
        func.count().cast(BigInteger).label("total"),
        func.count().filter(or_(~upload_open, upload_stale)).cast(BigInteger).label("finished"),
        func.count().filter(or_(Document.status == "failed", upload_stale)).cast(BigInteger).label("failed"),
        null().cast(Connection.created_at.type).label("created_at"),
    ).where(Document.project_id == project_id, Document.origin == "upload")
    combined = union_all(connections, uploads).subquery("progress_rows")
    return select(combined).order_by(combined.c.created_at, combined.c.connection_id)


# A document counts as processed once its text is read (the parse finished).
PROCESSED_DOCUMENT_STATUSES: Final = ("parsed", "ingested", "partially_ingested")


async def count_processed_documents(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Documents of the project, from any source, whose parse finished.

    The project is Ready (Plan and Decisions unlock) when this is at least 1
    (owner decision 2026-10-19; FR-GDUI-0021 keeps Build knowledge separate).
    """

    count = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(
            Document.project_id == project_id,
            Document.status.in_(PROCESSED_DOCUMENT_STATUSES),
        )
    )
    return int(count or 0)


# Graph corpus -----------------------------------------------------------------

DRIVE_KIND: Final = "gdrive"


@dataclass(frozen=True)
class GraphCorpusRow:
    """One processed document with text, for the graph build's corpus.

    ``drive_connection_id``/``drive_external_id``/``drive_source_name``: a Drive
    file that links this ``origin='connection'`` document (``None`` otherwise);
    one document linked by several Drive files gives one row per file.
    ``holder_name``: the name of the source that holds the document.
    """

    document_id: uuid.UUID
    origin: str
    content_hash: str
    original_filename: str
    created_at: datetime
    source_id: uuid.UUID | None
    holder_name: str | None
    drive_connection_id: uuid.UUID | None
    drive_external_id: str | None
    drive_source_name: str | None
    markdown: str


def graph_corpus_statement(project_id: uuid.UUID) -> Select[Any]:
    """The rows of ``list_graph_corpus`` (separate so the SQL can be checked offline)."""

    drive_items = (
        select(
            ConnectionItem.document_id,
            ConnectionItem.connection_id,
            ConnectionItem.external_id,
            Connection.name.label("source_name"),
        )
        .join(Connection, Connection.id == ConnectionItem.connection_id)
        .where(
            ConnectionItem.project_id == project_id,
            ConnectionItem.document_id.is_not(None),
            Connection.kind == DRIVE_KIND,
        )
        .subquery("drive_items")
    )
    holder = aliased(Connection, name="holder")
    return (
        select(
            Document.id.label("document_id"),
            Document.origin,
            Document.content_hash,
            Document.original_filename,
            Document.created_at,
            Document.source_id,
            holder.name.label("holder_name"),
            drive_items.c.connection_id.label("drive_connection_id"),
            drive_items.c.external_id.label("drive_external_id"),
            drive_items.c.source_name.label("drive_source_name"),
            Document.markdown,
        )
        .outerjoin(holder, holder.id == Document.source_id)
        .outerjoin(
            drive_items,
            and_(
                drive_items.c.document_id == Document.id,
                Document.origin == CONNECTION_ORIGIN,
            ),
        )
        .where(
            Document.project_id == project_id,
            Document.status.in_(PROCESSED_DOCUMENT_STATUSES),
            Document.markdown.is_not(None),
            func.length(func.btrim(Document.markdown)) > 0,
        )
        .order_by(Document.created_at, Document.id)
    )


async def list_graph_corpus(session: AsyncSession, project_id: uuid.UUID) -> list[GraphCorpusRow]:
    """Processed documents of the project whose markdown is not blank, oldest first.

    The graph build turns them into its corpus (``api.graph_build.corpus``).
    """

    result = await session.execute(graph_corpus_statement(project_id))
    return [GraphCorpusRow(**row._asdict()) for row in result.all()]


async def sources_progress(session: AsyncSession, project_id: uuid.UUID) -> SourcesProgress:
    """INT-GDUI-0004 for a project and each of its sources, in one SQL statement.

    - Finished Drive file: a final state (``FINAL_ITEM_STATES``: not ``pending`` or
      ``processing``); a file in error is finished (AC2). Only Drive sources are
      listed here; the new status read (``db.source_files``) covers upload batches.
    - Listing is finished when the (effective) state is not ``listing`` (AC3); a
      failed import is listing-finished with every file finished (AC4).
    - A connection still ``listing``/``importing`` after
      ``DRIVE_IMPORT_EXECUTION_TIMEOUT`` reads as ``failed`` / ``unknown_error``.
    - Uploads (``origin='upload'``): finished unless ``processing`` or ``uploaded``
      for less than ``UPLOAD_STALE_AFTER``; an older one counts as failed too. The
      query uses only the ``project_id`` prefix of ``ix_documents_project_id_status``.
    """

    result = await session.execute(_sources_progress_statement(project_id))
    connections: list[ConnectionProgress] = []
    uploads: UploadsProgress | None = None
    for row in result.all():
        if row.row_kind == "connection":
            connections.append(
                ConnectionProgress(
                    connection_id=row.connection_id,
                    state=row.state,
                    failure_reason=row.failure_reason,
                    progress=Progress(row.state != "listing", row.total, row.finished),
                )
            )
        elif row.total:
            uploads = UploadsProgress(
                progress=Progress(listing_finished=True, total=row.total, finished=row.finished),
                documents=row.total,
                failed=row.failed,
            )
    parts = [c.progress for c in connections] + ([uploads.progress] if uploads else [])
    return SourcesProgress(
        progress=Progress(
            listing_finished=all(p.listing_finished for p in parts),
            total=sum(p.total for p in parts),
            finished=sum(p.finished for p in parts),
        ),
        connections=tuple(connections),
        uploads=uploads,
    )
