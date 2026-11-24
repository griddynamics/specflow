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

from db.models import Connection, ConnectionItem

INTERRUPTED_REASON = "unknown_error"  # a closed reason of ``connections.failure_reason``
ITEM_CATEGORY = "other_files"
ITEM_PROCESSING_CLASS = "processed"


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
) -> uuid.UUID:
    """The id of the source's item ``external_id``: a new ``pending`` row, or the one it already has."""

    inserted: uuid.UUID | None = await session.scalar(
        pg_insert(ConnectionItem)
        .values(
            id=uuid.uuid4(),
            connection_id=connection_id,
            project_id=project_id,
            external_id=external_id,
            name=name,
            mime_type=mime_type,
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


async def fail_interrupted(session: AsyncSession, kinds: Sequence[str], *, before: datetime) -> list[uuid.UUID]:
    """End every source of ``kinds`` that is still ``listing`` or ``importing`` and was last touched before ``before``
    as ``failed``, and its unfinished items as ``error``: a restart killed the task that imported it, and it is added
    again. A source touched since (a new import of this process or of another replica) is left alone. The ids."""

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
