"""Tier 2: repository functions and status transitions on a migrated scratch database.

``SPECFLOW_TEST_POSTGRES=1``; see specflow_db_live_support.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import attributes

from db import repositories as repo
from db.models import Document, ProjectMember
from db.session import build_engine, build_session_factory
from db.sources import DocumentFile
from settings import DatabaseSettings
from specflow_live import requires_live

pytestmark = requires_live("POSTGRES")

# Pause between looks at the server's lock table: the condition lives in Postgres, not in this process.
LOCK_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str
    name: str | None


@pytest_asyncio.fixture
async def session(migrated_database: DatabaseSettings) -> AsyncIterator[AsyncSession]:
    engine = build_engine(migrated_database)
    factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            yield session
    finally:
        await engine.dispose()


async def add_document(
    session: AsyncSession, project_id: uuid.UUID, label: str, user_id: uuid.UUID | None = None
) -> Document:
    did = uuid.uuid4()
    return await repo.insert_document(
        session,
        file=DocumentFile(
            document_id=did,
            content_hash=hashlib.sha256(label.encode()).hexdigest(),
            filename=f"{label}.pdf",
            size_bytes=len(label),
            original_key=f"projects/{project_id}/documents/{did}/original.pdf",
        ),
        project_id=project_id,
        uploaded_by=user_id,
    )


async def test_upsert_user_provisions_once_and_refreshes(session: AsyncSession) -> None:
    first = await repo.upsert_user(session, Principal("sub-1", "J.Doe@x.org", "J. Doe"))
    again = await repo.upsert_user(session, Principal("sub-1", "jd@x.org", "N" * 300))

    assert again.id == first.id
    assert first.actor_id == "human:j.doe"
    assert (again.email, len(again.display_name)) == ("jd@x.org", 200)


async def test_upsert_user_falls_back_to_the_subject_on_an_actor_collision(
    session: AsyncSession,
) -> None:
    await repo.upsert_user(session, Principal("sub-1", "same@x.org", None))

    other = await repo.upsert_user(session, Principal("sub-2", "same@y.org", None))

    assert other.actor_id == "human:sub-2"
    assert other.display_name == "same@y.org"


async def test_create_project_adds_the_owner_and_membership_gates_access(
    session: AsyncSession,
) -> None:
    owner = await repo.upsert_user(session, Principal("o", "owner@x.org", "O"))
    stranger = await repo.upsert_user(session, Principal("s", "stranger@x.org", "S"))

    project = await repo.create_project(session, owner.id, "Demo")

    member = await session.scalar(select(ProjectMember).where(ProjectMember.project_id == project.id))
    assert member is not None and (member.user_id, member.role) == (owner.id, "owner")
    assert project.created_at is not None
    assert (await repo.get_member_project(session, owner.id, project.id)) is not None
    assert await repo.get_member_project(session, stranger.id, project.id) is None


async def test_list_projects_pages_newest_first_only_for_members(session: AsyncSession) -> None:
    owner = await repo.upsert_user(session, Principal("o", "owner@x.org", "O"))
    other = await repo.upsert_user(session, Principal("p", "other@x.org", "P"))
    created = [await repo.create_project(session, owner.id, f"P{i}") for i in range(5)]
    await repo.create_project(session, other.id, "not mine")

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    while True:
        page = await repo.list_projects(session, owner.id, 2, cursor)
        seen += [p.id for p in page.items]
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    expected = sorted(created, key=lambda p: (p.created_at, p.id), reverse=True)
    assert seen == [p.id for p in expected]


async def test_documents_insert_duplicate_find_list_and_get(session: AsyncSession) -> None:
    owner = await repo.upsert_user(session, Principal("o", "owner@x.org", "O"))
    a = await repo.create_project(session, owner.id, "A")
    b = await repo.create_project(session, owner.id, "B")

    doc = await add_document(session, a.id, "alpha", owner.id)
    with pytest.raises(repo.DuplicateDocumentError) as caught:
        await add_document(session, a.id, "alpha")
    in_b = await add_document(session, b.id, "alpha")

    assert caught.value.existing.id == doc.id
    assert (doc.status, doc.summarization_status, doc.media_type) == (
        "uploaded",
        "none",
        "application/pdf",
    )
    assert (await repo.find_by_hash(session, a.id, doc.content_hash)).id == doc.id
    assert [d.id for d in (await repo.list_documents(session, a.id, 10)).items] == [doc.id]
    assert await repo.get_document(session, a.id, in_b.id) is None
    assert (await repo.get_document(session, b.id, in_b.id)).id == in_b.id


async def test_parse_and_summary_transitions_are_single_flight(session: AsyncSession) -> None:
    owner = await repo.upsert_user(session, Principal("o", "owner@x.org", "O"))
    project = await repo.create_project(session, owner.id, "A")
    doc = await add_document(session, project.id, "alpha")
    pid, did = project.id, doc.id

    assert await repo.mark_summarizing(session, pid, did) is False  # not parsed yet
    assert await repo.mark_processing(session, pid, did) is True
    assert await repo.mark_processing(session, pid, did) is False  # repeated click
    assert await repo.mark_failed(session, pid, did, "pdf_parse_failed") is True
    assert (doc.status, doc.error) == ("failed", "pdf_parse_failed")
    assert await repo.mark_processing(session, pid, did) is True  # retry from failed
    assert doc.error is None
    assert await repo.mark_parsed(session, pid, did, "# md", "pymupdf") is True
    assert (doc.status, doc.markdown, doc.parser) == ("parsed", "# md", "pymupdf")
    assert await repo.mark_processing(session, pid, did) is False  # already parsed

    assert await repo.mark_summarizing(session, pid, did) is True
    assert await repo.mark_summarizing(session, pid, did) is False  # double click
    assert await repo.save_summarization(session, pid, did, "first", "m1") is True
    assert (doc.summarization_status, doc.summarization) == ("succeeded", "first")
    assert doc.summarized_at is not None

    assert await repo.mark_summarizing(session, pid, did) is True  # rerun (C8)
    assert await repo.mark_summarization_failed(session, pid, did, "tool_failed") is True
    assert (doc.summarization_status, doc.summarization_error) == ("failed", "tool_failed")
    assert doc.summarization == "first"  # a failure keeps the old summary
    assert await repo.mark_summarizing(session, pid, did) is True
    assert await repo.save_summarization(session, pid, did, "second", "m2") is True
    assert (doc.summarization, doc.summarization_model, doc.summarization_error) == (
        "second",
        "m2",
        None,
    )
    assert doc.updated_at >= doc.created_at  # refreshed by RETURNING, no lazy load


async def test_updates_are_scoped_to_the_project(session: AsyncSession) -> None:
    owner = await repo.upsert_user(session, Principal("o", "owner@x.org", "O"))
    a = await repo.create_project(session, owner.id, "A")
    b = await repo.create_project(session, owner.id, "B")
    doc = await add_document(session, a.id, "alpha")

    assert await repo.mark_processing(session, b.id, doc.id) is False
    assert await repo.mark_parsed(session, b.id, doc.id, "x", "p") is False
    assert doc.status == "uploaded"


async def test_concurrent_first_sign_ins_of_one_subject_give_one_row(
    migrated_database: DatabaseSettings,
) -> None:
    """The race branch: B's insert waits on A's uncommitted row, then re-reads it."""

    engine = build_engine(migrated_database)
    factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)
    principal = Principal("sub-race", "race@x.org", "Race")
    try:
        async with factory() as first, factory() as second:
            await first.begin()
            winner = await repo.upsert_user(first, principal)  # inserted, not committed

            async def late() -> object:
                async with second.begin():
                    return await repo.upsert_user(second, principal)

            racer = asyncio.create_task(late())
            # B's select saw nothing; its insert now blocks on the unique index.
            for _ in range(50):
                waiting = await engine_scalar(
                    engine,
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'",
                )
                if waiting:
                    break
                await asyncio.sleep(LOCK_POLL_SECONDS)
            assert waiting == 1
            await first.commit()
            loser = await asyncio.wait_for(racer, timeout=10)

        assert loser.id == winner.id
        rows = await engine_scalar(engine, "SELECT count(*) FROM users WHERE auth_subject = 'sub-race'")
        assert rows == 1
    finally:
        await engine.dispose()


async def engine_scalar(engine: object, statement: str) -> object:
    async with engine.connect() as connection:
        return await connection.scalar(text(statement))


async def test_the_duplicate_snapshot_is_readable_after_rollback(
    migrated_database: DatabaseSettings,
) -> None:
    engine = build_engine(migrated_database)
    factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                owner = await repo.upsert_user(session, Principal("o", "owner@x.org", "O"))
                project = await repo.create_project(session, owner.id, "A")
                first = await add_document(session, project.id, "alpha")
                first_id = first.id
            with pytest.raises(repo.DuplicateDocumentError) as caught:
                async with session.begin():  # rolls back on the exception
                    await add_document(session, project.id, "alpha")
        existing = caught.value.existing
        assert (existing.id, existing.status, existing.original_filename) == (
            first_id,
            "uploaded",
            "alpha.pdf",
        )
    finally:
        await engine.dispose()


async def test_blank_project_names_are_rejected_including_tabs_and_newlines(
    session: AsyncSession,
) -> None:
    owner = await repo.upsert_user(session, Principal("o", "owner@x.org", "O"))
    for blank in ("", "   ", "\t\n", " \r\n\t "):
        with pytest.raises(IntegrityError, match="ck_projects_name_not_blank"):
            async with session.begin_nested():
                await repo.create_project(session, owner.id, blank)
    assert (await repo.create_project(session, owner.id, " ok ")).name == " ok "


async def test_list_and_status_reads_do_not_load_the_large_columns(session: AsyncSession) -> None:
    """R-code M4 on a real database: deferred in lists, present in the detail read."""

    owner = await repo.upsert_user(session, Principal("o", "owner@x.org", "O"))
    project = await repo.create_project(session, owner.id, "A")
    doc = await add_document(session, project.id, "big")
    assert await repo.mark_processing(session, project.id, doc.id)
    assert await repo.mark_parsed(session, project.id, doc.id, "# " + "x" * 5000, "pymupdf")
    session.expunge_all()  # forget the instances: the reads below hit the database

    [listed] = (await repo.list_documents(session, project.id, 10)).items
    state = await repo.get_document_state(session, project.id, doc.id)
    for row in (listed, state):
        unloaded = attributes.instance_state(row).unloaded
        assert {"markdown", "summarization"} <= unloaded
        assert row.status == "parsed"
    session.expunge_all()
    detail = await repo.get_document(session, project.id, doc.id)
    assert detail is not None and detail.markdown.startswith("# xxx")


async def test_insert_document_writes_the_holder_and_defaults_to_none(session: AsyncSession) -> None:
    owner = await repo.upsert_user(session, Principal("o2", "owner2@x.org", "O"))
    project = await repo.create_project(session, owner.id, "Holders")
    batch = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO connections (id, project_id, kind, external_id, link, name, state, connected_by) "
            "VALUES (:i, :p, 'upload', :e, NULL, 'Local files', 'importing', :u)"
        ),
        {"i": batch, "p": project.id, "e": str(batch), "u": owner.id},
    )
    bare = await add_document(session, project.id, "bare")
    did = uuid.uuid4()
    held = await repo.insert_document(
        session,
        file=DocumentFile(
            document_id=did,
            content_hash=hashlib.sha256(b"held").hexdigest(),
            filename="held.pdf",
            size_bytes=4,
            original_key=f"projects/{project.id}/documents/{did}/original.pdf",
        ),
        project_id=project.id,
        uploaded_by=owner.id,
        source_id=batch,
    )
    assert bare.source_id is None and held.source_id == batch


async def test_mark_processing_takes_a_stale_document_only_with_the_keyword(session: AsyncSession) -> None:
    """R10 / M1: processing or uploaded for more than 60 minutes reads Failed and may restart."""

    owner = await repo.upsert_user(session, Principal("o3", "owner3@x.org", "O"))
    project = await repo.create_project(session, owner.id, "Stale")
    pid = project.id

    async def make(label: str, status: str, minutes: int) -> uuid.UUID:
        doc = await add_document(session, pid, label)
        await session.execute(
            text("UPDATE documents SET status = :s, updated_at = now() - make_interval(mins => :m) WHERE id = :i"),
            {"s": status, "m": minutes, "i": doc.id},
        )
        return doc.id

    old_processing = await make("op", "processing", 61)
    fresh_processing = await make("fp", "processing", 59)
    old_uploaded = await make("ou", "uploaded", 61)
    parsed = await make("pa", "parsed", 600)

    assert await repo.mark_processing(session, pid, old_processing) is False  # default: unchanged
    assert await repo.mark_processing(session, pid, old_processing, include_stale=True) is True
    assert await repo.mark_processing(session, pid, old_processing, include_stale=True) is False  # fresh now
    assert await repo.mark_processing(session, pid, fresh_processing, include_stale=True) is False
    assert await repo.mark_processing(session, pid, old_uploaded, include_stale=True) is True
    assert await repo.mark_processing(session, pid, parsed, include_stale=True) is False
