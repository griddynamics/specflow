"""Local files batches: start, add one file, end (INT-SRC-0004, FR-SRC-0020/0021/0022).

A batch is a ``connections`` row of kind ``upload`` (DATA-SRC-0003); each file is a
``connection_items`` row and ``documents.source_id`` is the one holder of the content.

Same rules as ``db.sources``: every function runs inside the caller's session and
transaction, flushes and never commits, and nothing here logs (names of files live in
the database only). Typed errors tell the router what to answer; a database error that
is a deadlock or a serialization failure becomes ``LockConflictError`` (the router retries
the call once, R11) and every other database error is left to the caller.

Lock order (SPECS section 5), the same in ``db.removal``:
``projects`` row ``FOR NO KEY UPDATE`` (R06) -> connection rows -> document rows.
``FOR NO KEY UPDATE`` still lets the foreign-key inserts of the project run. Every write
path (start, add, legacy batch, removal) takes the project lock first, so writes of one
project run one after another and the order cannot cycle; ``end_batch`` locks only its
own batch row and never asks for another lock afterwards.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import DateTime, and_, cast, delete, exists, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from db import repositories as repo
from db.models import Connection, ConnectionItem, Document
from db.source_files import UPLOAD_OPEN_FOR
from db.source_repair import LOCK_PROJECT, adopt_unheld
from db.sources import UPLOAD_STALE_AFTER, DocumentFile

BATCH_KIND: Final = "upload"
BATCH_NAME: Final = "Local files"
MAX_EXPECTED_FILES: Final = 10_000
LOCK_TIMEOUT: Final = "20s"  # the wait for the project lock of start, add and legacy batch
_LOCK_NOT_AVAILABLE: Final = "55P03"
_RETRYABLE_SQLSTATES: Final = frozenset({"40P01", "40001"})  # deadlock, serialization
_DOCUMENT_LOOPS: Final = 4

Outcome = Literal["added", "already_in_project", "read_again", "duplicate"]


class SourceNotFoundError(Exception):
    """No such source in this project (another project's id counts as absent)."""


class BatchEndedError(Exception):
    """The batch is ended, closed by the 15-minute rule, or not a Local files batch."""


class LockTimeoutError(Exception):
    """A lock wait went past ``lock_timeout``; the router answers 503 (try again) and rolls back."""


class LockConflictError(Exception):
    """A deadlock or a serialization failure; the transaction is aborted. Retry once."""


@dataclass(frozen=True)
class AddOutcome:
    """The result of one ``add_batch_file`` (SPECS 4.4).

    ``reparse_document_id`` is the document the router starts a parse for after the
    commit (``added`` and ``read_again``), else ``None``. ``discard_upload`` is true when
    the object the router just streamed is not referenced by any row (every outcome but
    ``added``) and must be deleted. ``holder`` is the source that holds the content
    (only for ``already_in_project``).
    """

    outcome: Outcome
    item: ConnectionItem | None
    document: Document | None
    holder: Connection | None
    reparse_document_id: uuid.UUID | None

    @property
    def discard_upload(self) -> bool:
        return self.outcome != "added"


def sqlstate(exc: DBAPIError) -> str | None:
    """The Postgres error code of a database error, when the driver gives one."""

    orig = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None


def timeout_from(exc: DBAPIError) -> LockTimeoutError | None:
    """``LockTimeoutError`` for a lock wait over ``lock_timeout`` (55P03), else ``None``."""

    if sqlstate(exc) == _LOCK_NOT_AVAILABLE:
        return LockTimeoutError("lock wait over the limit")
    return None


async def _bound_lock_wait(session: AsyncSession) -> None:
    """``SET LOCAL lock_timeout``: a write queued behind a long removal fails with
    ``LockTimeoutError`` (503) instead of holding a connection and a request for ever."""

    await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))


def conflict_from(exc: DBAPIError) -> LockConflictError | None:
    """``LockConflictError`` for a deadlock or serialization failure, else ``None``."""

    if sqlstate(exc) in _RETRYABLE_SQLSTATES:
        return LockConflictError("deadlock or serialization failure; retry once")
    return None


def decide_outcome(
    *,
    holder_is_batch: bool,
    holder_kind: str | None,
    doc_status: str,
    doc_stale: bool,
) -> Outcome:
    """The outcome for content the project already holds (SPECS 4.4 rule 4). Pure.

    ``duplicate``: this batch holds it. ``read_again``: held by a Local files batch and
    shown as Failed (failed, or ``processing``/``uploaded`` past ``UPLOAD_STALE_AFTER``,
    R10): the file moves here and is read again. Anything else, including a failed file
    of a Drive source (SD6) and content with no holder, is ``already_in_project``.
    """

    if holder_is_batch:
        return "duplicate"
    failed = doc_status == "failed" or (doc_status in ("processing", "uploaded") and doc_stale)
    if holder_kind == BATCH_KIND and failed:
        return "read_again"
    return "already_in_project"


# Queries ----------------------------------------------------------------------------


async def _lock_connection(
    session: AsyncSession,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    share: bool = False,
) -> Connection | None:
    """The source row, locked (``FOR UPDATE``, or ``FOR SHARE``); ``None`` when absent."""

    return await session.scalar(
        select(Connection)
        .where(Connection.project_id == project_id, Connection.id == connection_id)
        .with_for_update(read=share)
        .execution_options(populate_existing=True)
    )


async def _is_open(session: AsyncSession, batch_id: uuid.UUID) -> bool:
    """INT-SRC-0004: not ended and touched within ``UPLOAD_OPEN_FOR``."""

    last_item = (
        select(func.max(ConnectionItem.created_at))
        .where(ConnectionItem.connection_id == Connection.id)
        .scalar_subquery()
    )
    touched = func.greatest(Connection.created_at, func.coalesce(last_item, Connection.created_at))
    opened = and_(
        Connection.kind == BATCH_KIND,
        Connection.ended_at.is_(None),
        touched > func.now() - UPLOAD_OPEN_FOR,
    )
    return bool(await session.scalar(select(opened).where(Connection.id == batch_id)))


async def _item_count(session: AsyncSession, connection_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(ConnectionItem).where(ConnectionItem.connection_id == connection_id)
        )
        or 0
    )


async def _holds_documents(session: AsyncSession, connection_id: uuid.UUID) -> bool:
    return bool(await session.scalar(select(exists().where(Document.source_id == connection_id))))


# Start and end -----------------------------------------------------------------------


async def start_batch(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    expected_files: int,
) -> Connection:
    """Open a new Local files batch (FR-SRC-0020, FR-SRC-0021).

    Adopts the project's unheld documents (so old uploads have their day batch), deletes
    the project's hidden batches (upload kind only: a Drive source with no files is
    never touched, R02), and numbers the batch: ``1 + max(name_suffix)`` of the batches
    started in the same minute, under the ``projects`` row lock (R06).
    """

    if not 1 <= expected_files <= MAX_EXPECTED_FILES:
        raise ValueError(f"expected_files must be 1..{MAX_EXPECTED_FILES}")
    try:
        return await _start(session, project_id, user_id, expected_files)
    except DBAPIError as exc:
        raise (timeout_from(exc) or conflict_from(exc) or exc) from exc


async def _start(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    expected_files: int,
) -> Connection:
    await _bound_lock_wait(session)
    await session.execute(text(LOCK_PROJECT), {"pid": project_id})
    await adopt_unheld(session, project_id)
    await _delete_hidden_batches(session, project_id)

    minute = func.date_trunc("minute", func.now())
    taken = await session.scalar(
        select(func.coalesce(func.max(Connection.name_suffix), 0)).where(
            Connection.project_id == project_id,
            Connection.kind == BATCH_KIND,
            func.date_trunc("minute", Connection.created_at) == minute,
        )
    )
    batch_id = uuid.uuid4()
    batch = Connection(
        id=batch_id,
        project_id=project_id,
        kind=BATCH_KIND,
        external_id=str(batch_id),
        link=None,
        name=BATCH_NAME,
        state="importing",  # never read for an upload (R03)
        connected_by=user_id,
        expected_files=expected_files,
        name_suffix=int(taken or 0) + 1,
    )
    session.add(batch)
    await session.flush()
    await session.refresh(batch)
    return batch


async def _delete_hidden_batches(session: AsyncSession, project_id: uuid.UUID) -> int:
    """Hidden = upload, not open, no item (and no document it holds). ``SKIP LOCKED``
    leaves a batch somebody is writing to."""

    has_item = exists().where(ConnectionItem.connection_id == Connection.id)
    holds = exists().where(Document.source_id == Connection.id)
    ids = (
        await session.scalars(
            select(Connection.id)
            .where(
                Connection.project_id == project_id,
                Connection.kind == BATCH_KIND,
                ~has_item,
                ~holds,
                or_(
                    Connection.ended_at.is_not(None),
                    Connection.created_at <= func.now() - UPLOAD_OPEN_FOR,
                ),
            )
            .with_for_update(skip_locked=True)
        )
    ).all()
    if ids:
        await session.execute(delete(Connection).where(Connection.id.in_(ids)))
    return len(ids)


async def end_batch(session: AsyncSession, project_id: uuid.UUID, batch_id: uuid.UUID) -> Connection | None:
    """End a batch (FR-SRC-0021). Idempotent.

    ``None``: the batch had no file and was deleted. An ended batch is returned as it is.
    Raises ``SourceNotFoundError`` for an absent source and for a Drive source.
    """

    try:
        batch = await _lock_connection(session, project_id, batch_id)
        if batch is None or batch.kind != BATCH_KIND:
            raise SourceNotFoundError
        if await _item_count(session, batch_id) == 0 and not await _holds_documents(session, batch_id):
            await session.execute(delete(Connection).where(Connection.id == batch_id))
            return None
        if batch.ended_at is None:
            statement = (
                update(Connection)
                .where(Connection.id == batch_id)
                .values(ended_at=func.now(), state="done", updated_at=func.now())
                .returning(Connection)
                .execution_options(synchronize_session=False, populate_existing=True)
            )
            batch = (await session.scalars(statement)).one()
    except DBAPIError as exc:
        raise (timeout_from(exc) or conflict_from(exc) or exc) from exc
    else:
        return batch


async def legacy_day_batch(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID, now: datetime
) -> Connection:
    """Find or create today's ``legacy-YYYYMMDD`` batch (UTC day) and lock it.

    The only batch ``add_batch_file(allow_ended=True)`` may add to: the legacy upload
    endpoint serves old browser tabs (SPECS 4.5). Concurrent callers get one row.
    """

    when = now.astimezone(UTC)
    external_id = f"legacy-{when:%Y%m%d}"
    try:
        # projects first, like every write path: a batch row lock taken before it could
        # deadlock with a removal (R11)
        await _bound_lock_wait(session)
        await session.execute(text(LOCK_PROJECT), {"pid": project_id})
        for _ in range(3):
            found = await session.scalar(
                select(Connection)
                .where(
                    Connection.project_id == project_id,
                    Connection.kind == BATCH_KIND,
                    Connection.external_id == external_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if found is not None:
                return found
            stamp = cast(literal(when), DateTime(timezone=True))
            taken = await session.scalar(
                select(func.coalesce(func.max(Connection.name_suffix), 0)).where(
                    Connection.project_id == project_id,
                    Connection.kind == BATCH_KIND,
                    func.date_trunc("minute", Connection.created_at) == func.date_trunc("minute", stamp),
                )
            )
            insert = (
                pg_insert(Connection)
                .values(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    kind=BATCH_KIND,
                    external_id=external_id,
                    link=None,
                    name=BATCH_NAME,
                    state="done",
                    connected_by=user_id,
                    expected_files=None,
                    ended_at=when,
                    name_suffix=int(taken or 0) + 1,
                    created_at=when,
                    updated_at=when,
                )
                .on_conflict_do_nothing(constraint="uq_connections_project_kind_external")
                .returning(Connection)
                .execution_options(populate_existing=True)
            )
            created = (await session.scalars(insert)).one_or_none()
            if created is not None:
                return created
        raise RuntimeError("legacy day batch did not settle")
    except DBAPIError as exc:
        raise (timeout_from(exc) or conflict_from(exc) or exc) from exc


# Add one file ------------------------------------------------------------------------


def _new_item(
    *,
    batch_id: uuid.UUID,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    content_hash: str,
    filename: str,
    size_bytes: int,
    state: Literal["pending", "duplicate"],
) -> ConnectionItem:
    return ConnectionItem(
        id=uuid.uuid4(),
        connection_id=batch_id,
        project_id=project_id,
        external_id=content_hash,
        name=filename,
        mime_type=repo.PDF_MEDIA_TYPE,
        category="pdf",
        processing_class="processed",
        size_bytes=size_bytes,
        state=state,
        document_id=document_id,
    )


async def _read_document(
    session: AsyncSession,
    project_id: uuid.UUID,
    content_hash: str,
    *,
    lock: bool = False,
) -> tuple[Document, bool] | None:
    """The project's document with this content and whether it is stale (older than
    ``UPLOAD_STALE_AFTER``); the large columns stay deferred."""

    statement = (
        select(Document, Document.updated_at < func.now() - UPLOAD_STALE_AFTER)
        .where(Document.project_id == project_id, Document.content_hash == content_hash)
        .options(*(defer(getattr(Document, n)) for n in repo.LARGE_COLUMNS))
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update(of=Document)
    row = (await session.execute(statement)).one_or_none()
    return None if row is None else (row[0], bool(row[1]))


async def add_batch_file(
    session: AsyncSession,
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    *,
    file: DocumentFile,
    user_id: uuid.UUID | None,
    allow_ended: bool = False,
) -> AddOutcome:
    """Add one uploaded PDF to a batch in the caller's transaction (SPECS 4.4).

    The batch row lock (``FOR UPDATE``) fences the add against a removal of the batch.
    Raises ``SourceNotFoundError``, ``BatchEndedError`` (``allow_ended`` skips the open test, for
    the legacy batch) and ``LockConflictError``.
    """

    try:
        # projects first, as removal does (adopt_unheld takes it too), so the two orders agree
        await _bound_lock_wait(session)
        await session.execute(text(LOCK_PROJECT), {"pid": project_id})
        batch = await _lock_connection(session, project_id, batch_id)
        if batch is None:
            raise SourceNotFoundError
        if batch.kind != BATCH_KIND or (not allow_ended and not await _is_open(session, batch_id)):
            raise BatchEndedError
        fields = {
            "batch_id": batch_id,
            "project_id": project_id,
            "content_hash": file.content_hash,
            "filename": file.filename,
            "size_bytes": file.size_bytes,
        }
        same = await session.scalar(
            select(ConnectionItem).where(
                ConnectionItem.connection_id == batch_id,
                ConnectionItem.external_id == file.content_hash,
            )
        )
        if same is not None:  # the same content twice in one pick: a no-op
            return await _duplicate(session, project_id, same)
        for _ in range(_DOCUMENT_LOOPS):
            added = await _insert_new(session, file, user_id, fields)
            if added is not None:
                return added
            settled = await _existing_content(session, fields)
            if settled is not None:
                return settled
        raise LockConflictError("the content kept changing; retry once")
    except DBAPIError as exc:
        raise (timeout_from(exc) or conflict_from(exc) or exc) from exc


async def _duplicate(session: AsyncSession, project_id: uuid.UUID, item: ConnectionItem | None) -> AddOutcome:
    document = None
    if item is not None and item.document_id is not None:
        document = await repo.get_document_state(session, project_id, item.document_id)
    return AddOutcome("duplicate", item, document, None, None)


async def _insert_new(
    session: AsyncSession,
    file: DocumentFile,
    user_id: uuid.UUID | None,
    fields: dict[str, Any],
) -> AddOutcome | None:
    """The ``added`` outcome, or ``None`` when the project already holds the content."""

    try:
        document = await repo.insert_document(
            session,
            project_id=fields["project_id"],
            file=file,
            uploaded_by=user_id,
            source_id=fields["batch_id"],
        )
    except repo.DuplicateDocumentError:
        return None
    item = _new_item(document_id=document.id, state="pending", **fields)
    session.add(item)
    await session.flush()
    await repo.mark_processing(session, fields["project_id"], document.id)
    return AddOutcome("added", item, document, None, document.id)


async def _existing_content(session: AsyncSession, fields: dict[str, Any]) -> AddOutcome | None:
    """Settle content the project already holds; ``None`` = the row changed under us,
    the caller tries again (a holder removed meanwhile, a document deleted)."""

    project_id, batch_id = fields["project_id"], fields["batch_id"]
    read = await _read_adopting(session, project_id, fields["content_hash"])
    if read is None:
        return None
    document, stale = read
    holder_id = document.source_id
    if holder_id == batch_id:
        same = await session.scalar(
            select(ConnectionItem).where(
                ConnectionItem.connection_id == batch_id,
                ConnectionItem.document_id == document.id,
            )
        )
        return await _duplicate(session, project_id, same)
    if holder_id is None:  # e.g. a to-do answer: nobody holds it, nothing to move
        return await _link_duplicate(session, document, None, fields)
    return await _from_other_holder(session, document, holder_id, fields, stale=stale)


async def _read_adopting(
    session: AsyncSession, project_id: uuid.UUID, content_hash: str
) -> tuple[Document, bool] | None:
    """Read the project's document of this content; an orphan of an old pod is adopted first."""

    read = await _read_document(session, project_id, content_hash)
    if read is not None and read[0].source_id is None:
        await adopt_unheld(session, project_id)  # an orphan of an old pod (R05)
        read = await _read_document(session, project_id, content_hash)
    return read


async def _from_other_holder(
    session: AsyncSession,
    document: Document,
    holder_id: uuid.UUID,
    fields: dict[str, Any],
    *,
    stale: bool,
) -> AddOutcome | None:
    """The content is held by another connection: link, move or go round again (``None``)."""

    project_id = fields["project_id"]
    holder_kind = await session.scalar(select(Connection.kind).where(Connection.id == holder_id))
    wants = decide_outcome(
        holder_is_batch=False,
        holder_kind=holder_kind,
        doc_status=document.status,
        doc_stale=stale,
    )
    # connection rows before document rows (R11); a share lock fences a removal of the holder
    holder = await _lock_connection(session, project_id, holder_id, share=wants == "already_in_project")
    if holder is None:
        return None
    read = await _read_document(session, project_id, fields["content_hash"], lock=True)
    if read is None or read[0].source_id != holder_id:
        return None
    document, stale = read
    again = decide_outcome(
        holder_is_batch=False,
        holder_kind=holder.kind,
        doc_status=document.status,
        doc_stale=stale,
    )
    if again == "read_again" and wants != "read_again":
        return None  # the holder needs the stronger lock: go round once more
    if again == "already_in_project":
        return await _link_duplicate(session, document, holder, fields)
    return await _move_here(session, document, holder, fields)


async def _link_duplicate(
    session: AsyncSession,
    document: Document,
    holder: Connection | None,
    fields: dict[str, Any],
) -> AddOutcome:
    item = _new_item(document_id=document.id, state="duplicate", **fields)
    session.add(item)
    await session.flush()
    return AddOutcome("already_in_project", item, document, holder, None)


async def _move_here(
    session: AsyncSession,
    document: Document,
    old_holder: Connection,
    fields: dict[str, Any],
) -> AddOutcome:
    """``read_again``: the Failed file of an older batch moves into this batch."""

    project_id, batch_id = fields["project_id"], fields["batch_id"]
    # first: a stale document is only taken while its old updated_at still shows
    if not await repo.mark_processing(session, project_id, document.id, include_stale=True):
        raise RuntimeError("a locked document refused mark_processing")  # unreachable
    await session.execute(
        delete(ConnectionItem).where(
            ConnectionItem.connection_id == old_holder.id,
            ConnectionItem.document_id == document.id,
        )
    )
    document.source_id = batch_id
    item = _new_item(document_id=document.id, state="pending", **fields)
    session.add(item)
    await session.flush()
    if (
        not await _item_count(session, old_holder.id)
        and not await _holds_documents(session, old_holder.id)
        and not await _is_open(session, old_holder.id)
    ):
        await session.execute(delete(Connection).where(Connection.id == old_holder.id))
    return AddOutcome("read_again", item, document, None, document.id)
