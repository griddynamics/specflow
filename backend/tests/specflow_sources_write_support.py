"""Shared helpers of the tier-2 source write tests (no tests here).

Builds on ``specflow_sources_live_support`` (plain SQL rows) and adds the calls into
``db.batches`` and small readers. Gate: ``SPECFLOW_TEST_POSTGRES=1``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from db import batches
from db.batches import AddOutcome
from db.sources import DocumentFile
from specflow_sources_live_support import (
    PDF,
    SourcesWorld,
    add_item,
    secs,
    sha,
)
from temporal.ids import drive_document_id

POLL_INTERVAL_S = 0.05


async def tx[T](world: SourcesWorld, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` in one committed transaction (rolled back when it raises)."""

    async with world.factory() as session, session.begin():
        return await work(session)


async def add_file(
    session: AsyncSession,
    world: SourcesWorld,
    batch_id: uuid.UUID,
    label: str,
    *,
    project_id: uuid.UUID | None = None,
    allow_ended: bool = False,
) -> AddOutcome:
    project_id = project_id or world.project_a
    document_id = uuid.uuid4()
    return await batches.add_batch_file(
        session,
        project_id,
        batch_id,
        file=DocumentFile(
            document_id=document_id,
            content_hash=sha(label),
            filename=f"{label}.pdf",
            size_bytes=10,
            original_key=f"projects/{project_id}/documents/{document_id}/original.pdf",
        ),
        user_id=world.user_id,
        allow_ended=allow_ended,
    )


async def wait_until_blocked(world: SourcesWorld, waiting: int = 1, max_wait_s: float = 15.0) -> None:
    """Poll ``pg_stat_activity`` until ``waiting`` backends of this database wait on a lock.

    Replaces a sleep: the test goes on only when the other side really waits.
    """

    deadline = asyncio.get_running_loop().time() + max_wait_s
    while True:
        async with world.factory() as session:
            count = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
                )
            )
        if int(count or 0) >= waiting:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("no backend started waiting on a lock")
        await asyncio.sleep(POLL_INTERVAL_S)


async def scalar(session: AsyncSession, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar_one_or_none()


async def rows(session: AsyncSession, sql: str, **params: Any) -> list[Any]:
    return list((await session.execute(text(sql), params)).all())


async def source_exists(session: AsyncSession, source_id: uuid.UUID) -> bool:
    return bool(await scalar(session, "SELECT count(*) FROM connections WHERE id = :i", i=source_id))


async def doc_row(session: AsyncSession, document_id: uuid.UUID) -> Any:
    found = await rows(
        session,
        "SELECT id, status, source_id, error FROM documents WHERE id = :i",
        i=document_id,
    )
    return found[0] if found else None


async def item_count(session: AsyncSession, source_id: uuid.UUID) -> int:
    return int(await scalar(session, "SELECT count(*) FROM connection_items WHERE connection_id = :i", i=source_id))


async def put_drive_doc(
    session: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    external_id: str,
    *,
    status: str = "parsed",
    media_type: str = PDF,
    updated_ago: timedelta | None = None,
    holder: uuid.UUID | None = None,
) -> uuid.UUID:
    """A Drive document with the id ``uuid5(source, external_id)`` (the worker's rule),
    held by ``holder`` (default: the source)."""

    document_id = drive_document_id(source_id, external_id)
    await session.execute(
        text(
            "INSERT INTO documents (id, project_id, content_hash, original_filename, media_type, "
            "size_bytes, origin, original_key, status, created_at, updated_at, source_id) "
            "VALUES (:i, :p, :h, :n, :m, 5, 'connection', :k, :s, now(), "
            "now() - make_interval(secs => :ua), :src)"
        ),
        {
            "i": document_id,
            "p": project_id,
            "h": sha(f"{source_id}:{external_id}"),
            "n": f"{external_id}.pdf",
            "m": media_type,
            "s": status,
            "ua": secs(updated_ago),
            "k": f"projects/{project_id}/documents/{document_id}/original.pdf",
            "src": holder or source_id,
        },
    )
    return document_id


async def put_item(
    session: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    external_id: str,
    state: str,
    *,
    document_id: uuid.UUID | None = None,
    error: bool = False,
) -> uuid.UUID:
    """A Drive item with an explicit Drive file id (``add_item`` derives its own)."""

    item_id = await add_item(
        session,
        source_id,
        project_id,
        external_id,
        state,
        document_id=document_id,
        error_reason="parse_failed" if error else None,
        error_step="parse" if error else None,
    )
    await session.execute(
        text("UPDATE connection_items SET external_id = :e WHERE id = :i"),
        {"e": external_id, "i": item_id},
    )
    return item_id
