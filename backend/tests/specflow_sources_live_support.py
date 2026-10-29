"""Shared helpers of the tier-2 source tests (no tests here).

Every row is written with plain SQL and leaves the batch columns to their defaults, so
the builders write the unheld rows an old pod writes (the repair tests) and never
depend on the write-side functions of later sessions. ``source_id`` is the only extra
column and is passed only when given.

Gate: ``SPECFLOW_TEST_POSTGRES=1``; see specflow_db_live_support.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypedDict, Unpack

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.session import build_engine, build_session_factory
from settings import DatabaseSettings

PDF = "application/pdf"


def sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def secs(age: timedelta | None) -> float:
    return (age or timedelta()).total_seconds()


@dataclass(frozen=True)
class SourcesWorld:
    factory: async_sessionmaker[AsyncSession]
    user_id: uuid.UUID
    other_user_id: uuid.UUID
    project_a: uuid.UUID
    project_b: uuid.UUID


async def make_user(session: AsyncSession, label: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, actor_id, display_name, email) VALUES (:i, :a, :n, :e)"),
        {"i": user_id, "a": f"actor-{label}-{user_id.hex[:6]}", "n": f"Name {label}", "e": f"{label}@example.com"},
    )
    return user_id


async def make_project(session: AsyncSession, user_id: uuid.UUID, name: str) -> uuid.UUID:
    project_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, created_by) VALUES (:i, :n, :u)"),
        {"i": project_id, "n": name, "u": user_id},
    )
    await session.execute(
        text("INSERT INTO project_members (project_id, user_id) VALUES (:p, :u)"),
        {"p": project_id, "u": user_id},
    )
    return project_id


async def make_drive(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    state: str = "importing",
    failure_reason: str | None = None,
    age: timedelta | None = None,
    name: str = "Folder",
    folder: str | None = None,
) -> uuid.UUID:
    source_id = uuid.uuid4()
    folder = folder or f"folder-{source_id.hex}"
    await session.execute(
        text(
            "INSERT INTO connections (id, project_id, kind, external_id, link, name, state, "
            "failure_reason, connected_by, created_at, updated_at) "
            "VALUES (:i, :p, 'gdrive', :f, :l, :n, :s, :r, :u, "
            "now() - make_interval(secs => :age), now())"
        ),
        {
            "i": source_id,
            "p": project_id,
            "f": folder,
            "l": f"https://drive.google.com/drive/folders/{folder}",
            "n": name,
            "s": state,
            "r": failure_reason,
            "u": user_id,
            "age": secs(age),
        },
    )
    return source_id


class BatchOptions(TypedDict, total=False):
    expected: int | None
    age: timedelta | None
    ended: bool
    suffix: int
    state: str
    external_id: str | None


async def make_batch(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    **options: Unpack[BatchOptions],
) -> uuid.UUID:
    """A Local files batch (0005 columns). ``state`` is never read for an upload."""

    source_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO connections (id, project_id, kind, external_id, link, name, state, "
            "connected_by, expected_files, name_suffix, created_at, updated_at, ended_at) "
            "VALUES (:i, :p, 'upload', :e, NULL, 'Local files', :s, :u, :x, :sf, "
            "now() - make_interval(secs => :age), now(), "
            "CASE WHEN :ended THEN now() ELSE NULL END)"
        ),
        {
            "i": source_id,
            "p": project_id,
            "e": options.get("external_id") or str(source_id),
            "s": options.get("state", "importing"),
            "u": user_id,
            "x": options.get("expected"),
            "sf": options.get("suffix", 1),
            "age": secs(options.get("age")),
            "ended": options.get("ended", False),
        },
    )
    return source_id


_INSERT_DOCUMENT = (
    "INSERT INTO documents (id, project_id, content_hash, original_filename, media_type, "
    "size_bytes, origin, original_key, status, error, uploaded_by, created_at, updated_at) "
    "VALUES (:i, :p, :h, :n, :m, 123, :o, :k, :s, :e, :u, "
    "COALESCE(CAST(:cat AS timestamptz), now() - make_interval(secs => :ca)), "
    "now() - make_interval(secs => :ua))"
)
_INSERT_HELD_DOCUMENT = (
    "INSERT INTO documents (id, project_id, content_hash, original_filename, media_type, "
    "size_bytes, origin, original_key, status, error, uploaded_by, created_at, updated_at, "
    "source_id) VALUES (:i, :p, :h, :n, :m, 123, :o, :k, :s, :e, :u, "
    "COALESCE(CAST(:cat AS timestamptz), now() - make_interval(secs => :ca)), "
    "now() - make_interval(secs => :ua), :src)"
)


class DocumentOptions(TypedDict, total=False):
    status: str
    origin: str
    source_id: uuid.UUID | None
    updated_ago: timedelta | None
    created_ago: timedelta | None
    created_at: datetime | None
    error: str | None
    uploaded_by: uuid.UUID | None
    media_type: str


async def add_doc(
    session: AsyncSession,
    project_id: uuid.UUID,
    label: str,
    **options: Unpack[DocumentOptions],
) -> uuid.UUID:
    """A document. ``created_at`` (timezone-aware) wins over ``created_ago``."""

    document_id = uuid.uuid4()
    source_id = options.get("source_id")
    params: dict[str, Any] = {
        "i": document_id,
        "p": project_id,
        "h": sha(label),
        "n": f"{label}.pdf",
        "m": options.get("media_type", PDF),
        "o": options.get("origin", "upload"),
        "k": f"projects/{project_id}/documents/{document_id}/original.pdf",
        "s": options.get("status", "uploaded"),
        "e": options.get("error"),
        "u": options.get("uploaded_by"),
        "ua": secs(options.get("updated_ago")),
        "ca": secs(options.get("created_ago")),
        "cat": options.get("created_at"),
    }
    if source_id is not None:
        params["src"] = source_id
    await session.execute(text(_INSERT_DOCUMENT if source_id is None else _INSERT_HELD_DOCUMENT), params)
    return document_id


class ItemOptions(TypedDict, total=False):
    document_id: uuid.UUID | None
    error_reason: str | None
    error_step: str | None
    age: timedelta | None
    category: str
    name: str | None


async def add_item(
    session: AsyncSession,
    source_id: uuid.UUID,
    project_id: uuid.UUID,
    label: str,
    state: str = "pending",
    **options: Unpack[ItemOptions],
) -> uuid.UUID:
    item_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO connection_items (id, connection_id, project_id, external_id, name, "
            "mime_type, category, processing_class, state, error_reason, error_step, document_id, "
            "created_at, updated_at) VALUES (:i, :c, :p, :e, :n, :m, :cat, 'processed', :s, :r, :st, "
            ":d, now() - make_interval(secs => :age), now())"
        ),
        {
            "i": item_id,
            "c": source_id,
            "p": project_id,
            "e": f"ext-{label}",
            "n": options.get("name") or f"{label}.pdf",
            "m": PDF,
            "cat": options.get("category", "pdf"),
            "s": state,
            "r": options.get("error_reason"),
            "st": options.get("error_step"),
            "d": options.get("document_id"),
            "age": secs(options.get("age")),
        },
    )
    return item_id


def run_in_database[T](settings: DatabaseSettings, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` in one transaction on ``settings`` from a sync test (Alembic tests)."""

    async def go() -> T:
        engine = build_engine(settings)
        try:
            async with build_session_factory(engine)() as session, session.begin():
                return await work(session)
        finally:
            await engine.dispose()

    return asyncio.run(go())
