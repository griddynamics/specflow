"""Application-role data access of the importers that run in the API process (``api.imports``): the listed items of an
imported source, and the sources a restart interrupted. Same rules as ``db.sources``: every function runs inside the
caller's session and transaction, flushes and never commits; names of items live in the database only, nothing here
logs."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Connection, ConnectionItem, Document

INTERRUPTED_REASON = "unknown_error"  # a closed reason of ``connections.failure_reason``
ITEM_CATEGORY = "other_files"
ITEM_PROCESSING_CLASS = "processed"
DROPBOX_KIND = "dropbox"  # the only source kind of the re-add skip (``find_stored_document``)


class ItemDeclareError(Exception):
    """An item insert that neither added the row nor found it (a concurrent removal of the source)."""


async def declare_item(
    session: AsyncSession,
    project_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    external_id: str,
    name: str,
    mime_type: str,
    size_bytes: int | None = None,
    modified_time: datetime | None = None,
) -> uuid.UUID:
    """The id of the source's item ``external_id``: a new ``pending`` row, or the one it already has. ``size_bytes``
    and ``modified_time`` are what the provider listed (the Dropbox re-add skip, ``find_stored_document``)."""

    inserted: uuid.UUID | None = await session.scalar(
        pg_insert(ConnectionItem)
        .values(
            id=uuid.uuid4(),
            connection_id=connection_id,
            project_id=project_id,
            external_id=external_id,
            name=name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            modified_time=modified_time,
            category=ITEM_CATEGORY,
            processing_class=ITEM_PROCESSING_CLASS,
        )
        .on_conflict_do_nothing(constraint="uq_connection_items_connection_external")
        .returning(ConnectionItem.id)
    )
    if inserted is not None:
        return inserted
    existing: uuid.UUID | None = await session.scalar(
        select(ConnectionItem.id).where(
            ConnectionItem.connection_id == connection_id, ConnectionItem.external_id == external_id
        )
    )
    if existing is None:
        raise ItemDeclareError
    return existing


async def find_stored_document(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    external_id: str,
    modified_time: datetime,
    size_bytes: int,
) -> uuid.UUID | None:
    """The re-add skip of the Dropbox importer (decision DB12): the ``document_id`` of an item of this project (any
    source of kind ``dropbox``: an ``external_id`` is only an id inside its provider) with the same ``external_id``
    (``id:...``), ``modified_time`` (Dropbox ``server_modified``) and ``size_bytes`` that still links a document that
    exists; ``None`` when there is none (the file is downloaded). One
    query; the plain SHA-256 on insert stays the final dedup. A document whose parse failed is not reused."""

    found: uuid.UUID | None = await session.scalar(
        select(ConnectionItem.document_id)
        .join(Connection, Connection.id == ConnectionItem.connection_id)
        .join(Document, Document.id == ConnectionItem.document_id)
        .where(
            ConnectionItem.project_id == project_id,
            Connection.project_id == project_id,
            Connection.kind == DROPBOX_KIND,
            ConnectionItem.external_id == external_id,
            ConnectionItem.modified_time == modified_time,
            ConnectionItem.size_bytes == size_bytes,
            ConnectionItem.state.in_(("processed", "duplicate")),
            Document.project_id == project_id,
            Document.status != "failed",
        )
        .order_by(ConnectionItem.updated_at.desc())
        .limit(1)
    )
    return found


async def database_now(session: AsyncSession) -> datetime:
    """The database clock: the instant ``updated_at`` columns are compared with (never this process's clock)."""

    return (await session.execute(select(func.now()))).scalar_one()


async def fail_interrupted(session: AsyncSession, kinds: Sequence[str], *, before: datetime) -> list[uuid.UUID]:
    """End every source of ``kinds`` that is still ``listing`` or ``importing`` and was last touched before ``before``
    as ``failed``, and its unfinished items as ``error``: a restart killed the task that imported it, and it is added
    again. ``before`` is an instant of the database clock (``database_now``) read when this process started: a source
    touched since (a new import of this process or of another replica) is left alone. The ids."""

    failed = list(
        await session.scalars(
            update(Connection)
            .where(
                Connection.kind.in_(list(kinds)),
                Connection.state.in_(("listing", "importing")),
                Connection.updated_at < before,
            )
            .values(state="failed", failure_reason=INTERRUPTED_REASON, updated_at=func.now())
            .returning(Connection.id)
            .execution_options(synchronize_session=False)
        )
    )
    if failed:
        await session.execute(
            update(ConnectionItem)
            .where(ConnectionItem.connection_id.in_(failed), ConnectionItem.state.in_(("pending", "processing")))
            .values(state="error", error_reason="import_failed", updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
    return failed
