"""Tier 2: connections, connection items and progress on a migrated scratch database.

Requirements: INT-GDUI-0003, INT-GDUI-0004, DATA-GDUI-0001.
``SPECFLOW_TEST_POSTGRES=1``; see specflow_db_live_support.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.errors import ApiError
from api.routers.documents import require_project
from db import repositories as repo
from db import sources
from db.agent_scope import (
    agent_scope,
    build_agent_session_factory,
    dispose_agent_session_factory,
)
from db.models import Connection, ConnectionItem, Document
from db.session import build_engine, build_session_factory
from db.sources import DocumentFile
from drive.model import DriveItem
from settings import DatabaseSettings
from specflow_db_live_support import (
    AgentRole,
)
from specflow_live import requires_live
from specflow_sources_live_support import add_item, make_batch
from specflow_sources_write_support import wait_until_blocked

pytestmark = requires_live("POSTGRES")

FOLDER = "1AbCdEfGhIjKlMnOp-9YRR3pvo3VLPMT"
DOC_MIME = "application/vnd.google-apps.document"
PDF_MIME = "application/pdf"
FORM_MIME = "application/vnd.google-apps.form"


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str
    name: str | None


@dataclass(frozen=True)
class World:
    factory: async_sessionmaker[AsyncSession]
    user_id: uuid.UUID
    stranger_id: uuid.UUID
    project_a: uuid.UUID
    project_b: uuid.UUID


@pytest_asyncio.fixture
async def world(migrated_database: DatabaseSettings) -> AsyncIterator[World]:
    engine = build_engine(migrated_database)
    factory = build_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            user = await repo.upsert_user(session, Principal("u", "u@example.com", "U"))
            stranger = await repo.upsert_user(session, Principal("s", "s@example.com", "S"))
            a = await repo.create_project(session, user.id, "A")
            b = await repo.create_project(session, user.id, "B")
            ids = (user.id, stranger.id, a.id, b.id)
        yield World(factory, *ids)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(world: World) -> AsyncIterator[AsyncSession]:
    async with world.factory() as session, session.begin():
        yield session


def item(index: int, mime: str = DOC_MIME, **extra: object) -> DriveItem:
    fields: dict[str, object] = {
        "id": f"file-{index:04d}",
        "name": f"Name {index}",
        "mime_type": mime,
        "parent_id": "root",
        "size": 100 + index,
        "modified_time": "2026-10-19T08:00:00.000Z",
        "version": index,
        "md5": None,
    }
    fields.update(extra)
    return DriveItem(**fields)


async def connect(
    session: AsyncSession,
    world: World,
    project: uuid.UUID | None = None,
    folder: str = FOLDER,
) -> Connection:
    row, created = await sources.create_connection(
        session,
        project_id=project or world.project_a,
        kind="gdrive",
        external_id=folder,
        link=f"https://drive.google.com/drive/folders/{folder}",
        name="Folder",
        connected_by=world.user_id,
    )
    assert created
    return row


async def finish_n(session: AsyncSession, connection: Connection, count: int) -> None:
    for item_id in await sources.next_pending_items(session, connection.id, count):
        assert await sources.finish_item(session, item_id, "stored")


def sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


async def add_upload(
    session: AsyncSession,
    project: uuid.UUID,
    label: str,
    status: str,
    age: timedelta | None = None,
) -> uuid.UUID:
    document_id = uuid.uuid4()
    await repo.insert_document(
        session,
        file=DocumentFile(
            document_id=document_id,
            content_hash=sha(label),
            filename=f"{label}.pdf",
            size_bytes=1,
            original_key=f"projects/{project}/documents/{document_id}/original.pdf",
        ),
        project_id=project,
        uploaded_by=None,
    )
    await session.execute(
        text("UPDATE documents SET status = :s, updated_at = now() - make_interval(secs => :age) WHERE id = :i"),
        {"s": status, "age": (age or timedelta()).total_seconds(), "i": document_id},
    )
    return document_id


# Connections (INT-GDUI-0003) -------------------------------------------------------


async def test_create_is_idempotent_per_project_kind_and_folder(session: AsyncSession, world: World) -> None:
    first = await connect(session, world)
    again, created = await sources.create_connection(
        session,
        project_id=world.project_a,
        kind="gdrive",
        external_id=FOLDER,
        link="other",
        name="Other",
        connected_by=world.stranger_id,
    )

    assert not created and again.id == first.id
    assert (again.link, again.connected_by) == (
        first.link,
        world.user_id,
    )  # nothing overwritten
    assert (first.state, first.failure_reason, first.last_synced_at) == (
        "listing",
        None,
        None,
    )
    other_project = await connect(session, world, world.project_b)
    assert other_project.id != first.id  # the same folder in another project is allowed


async def test_concurrent_creates_yield_one_row(world: World) -> None:
    async def save() -> bool:
        async with world.factory() as session, session.begin():
            _, created = await sources.create_connection(
                session,
                project_id=world.project_a,
                kind="gdrive",
                external_id=FOLDER,
                link="l",
                name="n",
                connected_by=world.user_id,
            )
            if created:  # hold the transaction until another saver queues on the unique index
                await wait_until_blocked(world)
            return created

    results = await asyncio.gather(*(save() for _ in range(5)))

    assert sorted(results) == [False] * 4 + [True]
    async with world.factory() as session:
        rows = await sources.list_connections(session, world.project_a)
    assert len(rows) == 1


async def test_find_get_and_list_are_scoped_to_the_project(session: AsyncSession, world: World) -> None:
    mine = await connect(session, world)
    await connect(session, world, folder="2" * 12)
    theirs = await connect(session, world, world.project_b)

    assert (await sources.find_connection(session, world.project_a, "gdrive", FOLDER)).id == mine.id
    assert await sources.find_connection(session, world.project_b, "gdrive", "9" * 12) is None
    assert (await sources.get_connection(session, world.project_a, mine.id)).id == mine.id
    assert await sources.get_connection(session, world.project_a, theirs.id) is None
    listed = await sources.list_connections(session, world.project_a)
    # Both rows come from one transaction, so created_at ties and the random id decides the order: compare the set.
    assert sorted(c.external_id for c in listed) == sorted([FOLDER, "2" * 12])


async def test_delete_if_empty_removes_an_empty_connection_only(session: AsyncSession, world: World) -> None:
    empty = await connect(session, world)
    full = await connect(session, world, folder="3" * 12)
    await sources.upsert_items(session, full.id, world.project_a, [item(1)])

    assert await sources.delete_connection_if_empty(session, world.project_b, empty.id) is False
    assert await sources.delete_connection_if_empty(session, world.project_a, empty.id) is True
    assert await sources.delete_connection_if_empty(session, world.project_a, empty.id) is False
    assert await sources.delete_connection_if_empty(session, world.project_a, full.id) is False


async def test_set_connection_state_stamps_done_and_stores_the_reason(session: AsyncSession, world: World) -> None:
    row = await connect(session, world)

    assert await sources.set_connection_state(session, row.id, "importing")
    assert await sources.set_connection_state(session, row.id, "done")
    done = await session.scalar(
        select(Connection).where(Connection.id == row.id).execution_options(populate_existing=True)
    )
    assert (done.state, done.failure_reason) == (
        "done",
        None,
    ) and done.last_synced_at is not None

    assert await sources.set_connection_state(session, row.id, "failed", failure_reason="no_access")
    failed = await session.scalar(
        select(Connection).where(Connection.id == row.id).execution_options(populate_existing=True)
    )
    assert (failed.state, failed.failure_reason) == ("failed", "no_access")
    assert await sources.set_connection_state(session, uuid.uuid4(), "done") is False


async def test_the_database_refuses_inconsistent_connections(
    world: World,
) -> None:
    async def insert(**extra: str | None) -> None:
        async with world.factory() as session, session.begin():
            values = {
                "project_id": world.project_a,
                "kind": "gdrive",
                "external_id": FOLDER,
                "link": "l",
                "name": "n",
                "connected_by": world.user_id,
            }
            session.add(Connection(**{**values, **extra}))

    for bad in (
        {"state": "failed"},  # failed without a reason
        {"state": "done", "failure_reason": "no_access"},  # reason without failed
        {"external_id": "short"},
        {"kind": "github"},
        {"state": "paused"},
        {"state": "failed", "failure_reason": "import_failed"},  # file-level reason
    ):
        with pytest.raises(IntegrityError):
            await insert(**bad)


# Items (DATA-GDUI-0001) -----------------------------------------------------------


async def test_upsert_items_categorizes_and_never_resets_progress(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)
    listed = [
        item(1),
        item(2, PDF_MIME),
        item(3, FORM_MIME),
        item(4, "image/png"),
        item(5, DOC_MIME, name="report.docx"),
    ]

    assert await sources.upsert_items(session, conn.id, world.project_a, listed) == 5
    first = (await sources.next_pending_items(session, conn.id, 1))[0]
    assert await sources.finish_item(session, first, "stored")
    assert await sources.upsert_items(session, conn.id, world.project_a, listed) == 0

    rows = (await session.scalars(select(ConnectionItem).where(ConnectionItem.connection_id == conn.id))).all()
    by_id = {r.external_id: r for r in rows}
    assert {k: (v.category, v.processing_class) for k, v in by_id.items()} == {
        "file-0001": ("google_docs", "processed"),
        "file-0002": ("pdf", "processed"),
        "file-0003": ("other_google", "skipped"),
        "file-0004": ("images", "stored_only"),
        "file-0005": ("google_docs", "processed"),
    }
    assert sorted(r.state for r in rows) == ["pending"] * 4 + ["stored"]
    assert by_id["file-0001"].modified_time is not None and by_id["file-0001"].drive_version == 1


async def test_upsert_items_handles_more_than_one_chunk(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)

    assert await sources.upsert_items(session, conn.id, world.project_a, [item(i) for i in range(1203)]) == 1203


async def test_next_pending_items_returns_a_limited_batch_of_pending_only(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(i) for i in range(7)])
    await finish_n(session, conn, 3)

    batch = await sources.next_pending_items(session, conn.id, 50)
    assert len(batch) == 4
    assert len(await sources.next_pending_items(session, conn.id, 2)) == 2
    assert (await sources.get_item(session, batch[0])).state == "pending"
    assert await sources.get_item(session, uuid.uuid4()) is None


async def test_finish_item_acts_once_and_only_from_pending(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(1), item(2)])
    first, second = await sources.next_pending_items(session, conn.id, 2)

    assert await sources.finish_item(session, first, "processed", document_id=None)
    assert await sources.finish_item(session, first, "error", error_reason="convert_failed") is False
    assert await sources.finish_item(session, second, "error", error_reason="convert_failed", error_step="convert")
    assert await sources.finish_item(session, second, "stored") is False

    done = await session.scalar(
        select(ConnectionItem).where(ConnectionItem.id == second).execution_options(populate_existing=True)
    )
    assert (done.state, done.error_reason, done.error_step) == (
        "error",
        "convert_failed",
        "convert",
    )


async def test_fail_unfinished_items_ends_only_the_pending_ones(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(i) for i in range(10)])
    await finish_n(session, conn, 4)

    assert await sources.fail_unfinished_items(session, conn.id, "download") == 6
    assert await sources.fail_unfinished_items(session, conn.id) == 0

    rows = (
        await session.scalars(
            select(ConnectionItem)
            .where(ConnectionItem.connection_id == conn.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    errors = [r for r in rows if r.state == "error"]
    assert len(errors) == 6 and {(r.error_reason, r.error_step) for r in errors} == {("import_failed", "download")}


async def test_the_database_refuses_inconsistent_items(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)
    base = {
        "connection_id": conn.id,
        "project_id": world.project_a,
        "external_id": "x1",
        "name": "n",
        "mime_type": DOC_MIME,
        "category": "google_docs",
        "processing_class": "processed",
    }
    for bad in (
        {"state": "error"},  # error without reason
        {"state": "stored", "error_reason": "convert_failed"},
        {"category": "spreadsheets"},
        {"processing_class": "other"},
        {"state": "error", "error_reason": "oops"},
        {"state": "error", "error_reason": "convert_failed", "error_step": "oops"},
    ):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(ConnectionItem(**{**base, **bad}))


# Documents of a connection ----------------------------------------------------------


async def test_insert_connection_document_inserts_with_origin_connection(session: AsyncSession, world: World) -> None:
    document_id = uuid.uuid4()

    row, created = await sources.insert_connection_document(
        session,
        file=DocumentFile(
            document_id=document_id,
            content_hash=sha("g1"),
            filename="doc",
            size_bytes=12,
            original_key="projects/x/documents/y/original.json",
        ),
        project_id=world.project_a,
        media_type=DOC_MIME,
        markdown="# Doc",
        parser="google_docs",
        status="parsed",
    )

    assert created and row.id == document_id
    assert (row.origin, row.status, row.markdown, row.uploaded_by) == (
        "connection",
        "parsed",
        "# Doc",
        None,
    )
    assert (row.version, row.summarization_status) == (1, "none")


async def test_a_duplicate_hash_returns_the_existing_row(session: AsyncSession, world: World) -> None:
    upload_id = await add_upload(session, world.project_a, "same", "parsed")

    row, created = await sources.insert_connection_document(
        session,
        file=DocumentFile(
            document_id=uuid.uuid4(),
            content_hash=sha("same"),
            filename="again",
            size_bytes=1,
            original_key="k",
        ),
        project_id=world.project_a,
        media_type=PDF_MIME,
    )

    assert not created and row.id == upload_id and row.origin == "upload"
    count = await session.scalar(text("SELECT count(*) FROM documents"))
    assert count == 1


async def test_a_retry_with_the_same_id_and_hash_is_a_no_op(session: AsyncSession, world: World) -> None:
    kwargs = {
        "project_id": world.project_a,
        "file": DocumentFile(
            document_id=uuid.uuid4(),
            content_hash=sha("r"),
            filename="r",
            size_bytes=1,
            original_key="k",
        ),
        "media_type": PDF_MIME,
    }
    first, created = await sources.insert_connection_document(session, **kwargs)
    again, created_again = await sources.insert_connection_document(session, **kwargs)

    assert created and not created_again and again.id == first.id
    assert (await session.scalar(select(Document.id).where(Document.project_id == world.project_a))) == first.id


async def test_the_same_hash_in_another_project_is_a_new_document(session: AsyncSession, world: World) -> None:
    await add_upload(session, world.project_a, "shared", "parsed")

    _, created = await sources.insert_connection_document(
        session,
        file=DocumentFile(
            document_id=uuid.uuid4(),
            content_hash=sha("shared"),
            filename="x",
            size_bytes=1,
            original_key="k",
        ),
        project_id=world.project_b,
        media_type=PDF_MIME,
    )

    assert created


# Progress (INT-GDUI-0004) ------------------------------------------------------------


async def test_progress_ac1_drive_17_of_23_and_uploads_2_of_2(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(i) for i in range(23)])
    await finish_n(session, conn, 17)
    await sources.set_connection_state(session, conn.id, "importing")
    await add_upload(session, world.project_a, "u1", "parsed")
    await add_upload(session, world.project_a, "u2", "failed")

    result = await sources.sources_progress(session, world.project_a)

    assert result.progress == sources.Progress(listing_finished=True, total=25, finished=19)
    (only,) = result.connections
    assert (only.connection_id, only.state, only.failure_reason) == (
        conn.id,
        "importing",
        None,
    )
    assert only.progress == sources.Progress(listing_finished=True, total=23, finished=17)
    assert result.uploads == sources.UploadsProgress(sources.Progress(listing_finished=True, total=2, finished=2), 2, 1)


async def test_processed_documents_count_only_parsed_ones_of_the_project(session: AsyncSession, world: World) -> None:
    assert await sources.count_processed_documents(session, world.project_a) == 0
    await add_upload(session, world.project_a, "p1", "parsed")
    await add_upload(session, world.project_a, "p2", "ingested")
    await add_upload(session, world.project_a, "p3", "partially_ingested")
    await add_upload(session, world.project_a, "p4", "processing")
    await add_upload(session, world.project_a, "p5", "failed")
    await add_upload(session, world.project_b, "p6", "parsed")

    assert await sources.count_processed_documents(session, world.project_a) == 3


async def test_progress_ac2_a_file_in_error_is_finished(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(1), item(2)])
    first = (await sources.next_pending_items(session, conn.id, 1))[0]
    await sources.finish_item(session, first, "error", error_reason="parse_failed", error_step="parse")
    await sources.set_connection_state(session, conn.id, "importing")

    assert (await sources.sources_progress(session, world.project_a)).progress == sources.Progress(
        listing_finished=True, total=2, finished=1
    )


async def test_progress_ac3_listing_not_finished_for_source_and_project(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)  # state listing, no items yet
    await add_upload(session, world.project_a, "u1", "parsed")

    result = await sources.sources_progress(session, world.project_a)

    assert result.connections[0].progress == sources.Progress(listing_finished=False, total=0, finished=0)
    assert result.progress == sources.Progress(listing_finished=False, total=1, finished=1)
    assert result.connections[0].connection_id == conn.id


async def test_progress_ac4_failed_import_is_10_of_10_with_the_reason(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(i) for i in range(10)])
    await finish_n(session, conn, 4)
    await sources.fail_unfinished_items(session, conn.id)
    await sources.set_connection_state(session, conn.id, "failed", failure_reason="no_access")

    result = await sources.sources_progress(session, world.project_a)

    (only,) = result.connections
    assert (only.state, only.failure_reason) == ("failed", "no_access")
    assert only.progress == sources.Progress(listing_finished=True, total=10, finished=10)
    assert result.progress == sources.Progress(listing_finished=True, total=10, finished=10)


async def test_progress_counts_a_failed_state_as_finished_even_with_pending_rows(
    session: AsyncSession, world: World
) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(i) for i in range(5)])
    await sources.set_connection_state(session, conn.id, "failed", failure_reason="drive_unavailable")

    (only,) = (await sources.sources_progress(session, world.project_a)).connections

    assert only.progress == sources.Progress(listing_finished=True, total=5, finished=5)


async def test_progress_a_stale_import_reads_as_failed_without_a_write(session: AsyncSession, world: World) -> None:
    stale = await connect(session, world)
    fresh = await connect(session, world, folder="4" * 12)
    for conn in (stale, fresh):
        await sources.upsert_items(session, conn.id, world.project_a, [item(i) for i in range(3)])
    await sources.set_connection_state(session, stale.id, "importing")
    old = (sources.DRIVE_IMPORT_EXECUTION_TIMEOUT + timedelta(minutes=1)).total_seconds()
    await session.execute(
        text("UPDATE connections SET created_at = now() - make_interval(secs => :s) WHERE id = :i"),
        {"s": old, "i": stale.id},
    )

    result = await sources.sources_progress(session, world.project_a)

    by_id = {c.connection_id: c for c in result.connections}
    assert (by_id[stale.id].state, by_id[stale.id].failure_reason) == (
        "failed",
        "unknown_error",
    )
    assert by_id[stale.id].progress == sources.Progress(listing_finished=True, total=3, finished=3)
    assert (by_id[fresh.id].state, by_id[fresh.id].progress) == (
        "listing",
        sources.Progress(listing_finished=False, total=3, finished=0),
    )
    stored = await session.scalar(text("SELECT state FROM connections WHERE id = :i"), {"i": stale.id})
    assert stored == "importing"  # read time only


async def test_progress_a_done_connection_is_never_stale(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(1)])
    await finish_n(session, conn, 1)
    await sources.set_connection_state(session, conn.id, "done")
    await session.execute(
        text("UPDATE connections SET created_at = now() - interval '30 days' WHERE id = :i"),
        {"i": conn.id},
    )

    (only,) = (await sources.sources_progress(session, world.project_a)).connections

    assert (only.state, only.failure_reason) == ("done", None)


async def test_progress_legacy_uploads_only_and_the_stale_upload_rule(session: AsyncSession, world: World) -> None:
    empty = await sources.sources_progress(session, world.project_a)
    assert empty == sources.SourcesProgress(sources.Progress(listing_finished=True, total=0, finished=0), (), None)

    await add_upload(session, world.project_a, "ok", "parsed")
    await add_upload(session, world.project_a, "running", "processing", timedelta(minutes=5))
    await add_upload(session, world.project_a, "stuck", "processing", timedelta(minutes=61))
    await add_upload(session, world.project_a, "bad", "failed")
    await add_upload(session, world.project_a, "waiting", "uploaded", timedelta(minutes=5))
    # Documents of other origins are not uploads.
    await sources.insert_connection_document(
        session,
        file=DocumentFile(
            document_id=uuid.uuid4(),
            content_hash=sha("drive"),
            filename="d",
            size_bytes=1,
            original_key="k",
        ),
        project_id=world.project_a,
        media_type=DOC_MIME,
    )

    result = await sources.sources_progress(session, world.project_a)

    assert result.connections == ()
    # 5 uploads: parsed + stuck(stale) + failed are finished; the fresh processing and
    # the fresh 'uploaded' ones are not (FR-GDUI-0021).
    assert result.uploads == sources.UploadsProgress(sources.Progress(listing_finished=True, total=5, finished=3), 5, 2)
    assert result.progress == sources.Progress(listing_finished=True, total=5, finished=3)


async def test_progress_is_scoped_to_the_project(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world, world.project_b)
    await sources.upsert_items(session, conn.id, world.project_b, [item(1)])
    await add_upload(session, world.project_b, "elsewhere", "parsed")

    result = await sources.sources_progress(session, world.project_a)

    assert result == sources.SourcesProgress(sources.Progress(listing_finished=True, total=0, finished=0), (), None)


# Sources of kind upload, claims and holders (DATA-SRC-0002, FR-SRC-0021) -----------------


async def test_claim_item_claims_pending_and_processing_only_while_the_source_imports(
    session: AsyncSession, world: World
) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(1), item(2)])
    first, second = await sources.next_pending_items(session, conn.id, 2)

    # listing: not importing yet
    assert await sources.claim_item(session, world.project_a, conn.id, first) is None
    await sources.set_connection_state(session, conn.id, "importing")

    claimed = await sources.claim_item(session, world.project_a, conn.id, first)
    assert claimed is not None and claimed.state == "processing"
    again = await sources.claim_item(session, world.project_a, conn.id, first)  # a retry
    assert again is not None and again.id == first
    assert await sources.finish_item(session, first, "processed")
    assert await sources.claim_item(session, world.project_a, conn.id, first) is None  # final
    # wrong project or wrong connection never claims
    assert await sources.claim_item(session, world.project_b, conn.id, second) is None
    assert await sources.claim_item(session, world.project_a, uuid.uuid4(), second) is None
    assert (await sources.get_item(session, second)).state == "pending"

    await sources.set_connection_state(session, conn.id, "failed", failure_reason="no_access")
    assert await sources.claim_item(session, world.project_a, conn.id, second) is None


async def test_lock_importing_connection_returns_the_row_only_for_an_importing_source(
    session: AsyncSession, world: World
) -> None:
    conn = await connect(session, world)
    assert await sources.lock_importing_connection(session, world.project_a, conn.id) is None
    await sources.set_connection_state(session, conn.id, "importing")

    locked = await sources.lock_importing_connection(session, world.project_a, conn.id)

    assert locked is not None and locked.id == conn.id
    assert await sources.lock_importing_connection(session, world.project_b, conn.id) is None


async def test_a_removal_style_lock_waits_for_the_share_lock_of_a_claim(
    world: World,
) -> None:
    async with world.factory() as setup, setup.begin():
        conn = await connect(setup, world)
        await sources.upsert_items(setup, conn.id, world.project_a, [item(1)])
        await sources.set_connection_state(setup, conn.id, "importing")
        (item_id,) = await sources.next_pending_items(setup, conn.id, 1)

    async with world.factory() as worker, worker.begin():
        assert await sources.claim_item(worker, world.project_a, conn.id, item_id) is not None
        async with world.factory() as remover, remover.begin():
            await remover.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(DBAPIError):  # FOR UPDATE queues behind FOR SHARE
                await remover.execute(
                    text("SELECT id FROM connections WHERE id = :i FOR UPDATE"),
                    {"i": conn.id},
                )


async def test_finish_item_accepts_processing_and_duplicate_and_the_first_final_state_stays(
    session: AsyncSession, world: World
) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(1), item(2), item(3)])
    await sources.set_connection_state(session, conn.id, "importing")
    first, second, third = await sources.next_pending_items(session, conn.id, 3)
    holder = await add_upload(session, world.project_a, "held", "parsed")

    assert await sources.claim_item(session, world.project_a, conn.id, first)
    assert await sources.finish_item(session, first, "processed")  # from processing
    assert await sources.finish_item(session, second, "duplicate", document_id=holder)  # from pending
    assert await sources.finish_item(session, second, "processed") is False
    with pytest.raises(ValueError, match="needs the document"):
        await sources.finish_item(session, third, "duplicate")
    # the database refuses a duplicate without a document too
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("UPDATE connection_items SET state = 'duplicate', document_id = NULL WHERE id = :i"),
                {"i": third},
            )


async def test_fail_unfinished_items_also_ends_processing_files(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(i) for i in range(4)])
    await sources.set_connection_state(session, conn.id, "importing")
    first, second, *_ = await sources.next_pending_items(session, conn.id, 4)
    await sources.claim_item(session, world.project_a, conn.id, first)
    await sources.finish_item(session, second, "stored")

    assert await sources.fail_unfinished_items(session, conn.id, "download") == 3
    assert await sources.fail_unfinished_items(session, conn.id) == 0


async def test_duplicate_counts_as_finished_and_processing_does_not_in_the_legacy_progress(
    session: AsyncSession, world: World
) -> None:
    conn = await connect(session, world)
    await sources.upsert_items(session, conn.id, world.project_a, [item(i) for i in range(4)])
    await sources.set_connection_state(session, conn.id, "importing")
    ids = await sources.next_pending_items(session, conn.id, 4)
    holder = await add_upload(session, world.project_a, "dup-holder", "parsed")
    await sources.finish_item(session, ids[0], "duplicate", document_id=holder)
    await sources.finish_item(session, ids[1], "processed")
    await sources.claim_item(session, world.project_a, conn.id, ids[2])  # processing

    result = await sources.sources_progress(session, world.project_a)

    (only,) = result.connections
    assert only.progress == sources.Progress(listing_finished=True, total=4, finished=2)


async def test_the_legacy_progress_ignores_upload_batches_and_the_12h_rule_is_drive_only(
    session: AsyncSession, world: World
) -> None:
    batch = await make_batch(
        session,
        world.project_a,
        world.user_id,
        age=timedelta(hours=13),
        state="importing",
    )
    document = await add_upload(session, world.project_a, "in-batch", "parsed")
    await add_item(session, batch, world.project_a, "in-batch", "pending", document_id=document)

    result = await sources.sources_progress(session, world.project_a)

    assert result.connections == ()  # a batch is not a Drive row, never read as failed
    assert result.uploads is not None and result.uploads.documents == 1


async def test_list_connections_filters_by_kind_and_a_batch_has_no_link(session: AsyncSession, world: World) -> None:
    drive = await connect(session, world)
    batch = await make_batch(session, world.project_a, world.user_id)

    assert [c.id for c in await sources.list_connections(session, world.project_a, "gdrive")] == [drive.id]
    assert [c.id for c in await sources.list_connections(session, world.project_a, "upload")] == [batch]
    assert len(await sources.list_connections(session, world.project_a)) == 2
    row = await sources.get_connection(session, world.project_a, batch)
    assert (row.kind, row.link, row.name, row.name_suffix, row.ended_at) == (
        "upload",
        None,
        "Local files",
        1,
        None,
    )


async def test_the_database_refuses_inconsistent_batches_and_drive_sources(
    world: World,
) -> None:
    async def insert(**extra: object) -> None:
        async with world.factory() as session, session.begin():
            values = {
                "project_id": world.project_a,
                "kind": "upload",
                "external_id": "batch-0123456789",
                "link": None,
                "name": "Local files",
                "connected_by": world.user_id,
            }
            session.add(Connection(**{**values, **extra}))

    await insert()  # a valid batch
    for bad in (
        {
            "kind": "gdrive",
            "external_id": "folder-0123456789",
        },  # a Drive source needs its link
        {"external_id": "batch-1123456789", "expected_files": 0},
        {"external_id": "batch-2123456789", "expected_files": 10_001},
        {"external_id": "batch-3123456789", "name_suffix": 0},
    ):
        with pytest.raises(IntegrityError):
            await insert(**bad)


async def test_a_document_gets_its_holder_from_insert_connection_document(session: AsyncSession, world: World) -> None:
    conn = await connect(session, world)

    row, created = await sources.insert_connection_document(
        session,
        file=DocumentFile(
            document_id=uuid.uuid4(),
            content_hash=sha("held"),
            filename="held",
            size_bytes=1,
            original_key="k",
        ),
        project_id=world.project_a,
        media_type=DOC_MIME,
        source_id=conn.id,
    )
    bare, _ = await sources.insert_connection_document(
        session,
        file=DocumentFile(
            document_id=uuid.uuid4(),
            content_hash=sha("bare"),
            filename="bare",
            size_bytes=1,
            original_key="k",
        ),
        project_id=world.project_a,
        media_type=DOC_MIME,
    )

    assert created and row.source_id == conn.id and bare.source_id is None


async def test_the_agent_role_has_no_grant_on_the_holder_column(
    agent_factory: async_sessionmaker[AsyncSession], world: World
) -> None:
    with pytest.raises(DBAPIError) as caught:
        async with agent_scope(agent_factory, world.project_a) as session:
            await session.execute(text("SELECT source_id FROM documents"))
    assert "permission denied" in str(caught.value.orig)


# Isolation ----------------------------------------------------------------------------


async def test_a_non_member_gets_project_not_found_through_require_project(
    world: World,
) -> None:
    class Runtime:
        @asynccontextmanager
        async def transaction(self) -> AsyncIterator[AsyncSession]:
            async with world.factory() as session, session.begin():
                yield session

    await require_project(Runtime(), world.user_id, world.project_a)
    with pytest.raises(ApiError) as caught:
        await require_project(Runtime(), world.stranger_id, world.project_a)
    assert (caught.value.status, caught.value.code) == (404, "project_not_found")


@pytest_asyncio.fixture
async def agent_factory(
    migrated_database: DatabaseSettings, agent_role: AgentRole
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = build_agent_session_factory(migrated_database, agent_role.settings())
    try:
        yield factory
    finally:
        await dispose_agent_session_factory(factory)


# (table, ids, project ids, one) reads of the agent role, per source table of migration 0001_initial
AGENT_READS = (
    ("connections", "SELECT id FROM connections", "SELECT project_id FROM connections", "SELECT 1 FROM connections"),
    (
        "connection_items",
        "SELECT id FROM connection_items",
        "SELECT project_id FROM connection_items",
        "SELECT 1 FROM connection_items",
    ),
)


async def test_the_agent_role_has_no_grant_on_the_new_tables(
    agent_factory: async_sessionmaker[AsyncSession], world: World
) -> None:
    for _table, read_ids, _read_projects, _read_one in AGENT_READS:
        with pytest.raises(DBAPIError) as caught:
            async with agent_scope(agent_factory, world.project_a) as session:
                await session.execute(text(read_ids))
        assert "permission denied" in str(caught.value.orig)


async def test_the_rls_policy_scopes_the_agent_role_to_its_project(
    agent_factory: async_sessionmaker[AsyncSession], agent_role: AgentRole, world: World
) -> None:
    # Skipped like the migration's agent role check when the agent role does not exist: the
    # ``agent_role`` fixture creates it, so it runs wherever the live tier runs.
    async with world.factory() as session, session.begin():
        for project in (world.project_a, world.project_b):
            conn = await connect(session, world, project)
            await sources.upsert_items(session, conn.id, project, [item(1)])
        # The scratch database is the test's own: grant only here to exercise the policy.
        role = f'"{agent_role.name}"'
        await session.execute(text(f"GRANT SELECT ON connections, connection_items TO {role}"))

    for _table, _read_ids, read_projects, read_one in AGENT_READS:
        async with agent_scope(agent_factory, world.project_a) as session:
            rows = (await session.execute(text(read_projects))).all()
        assert {r.project_id for r in rows} == {world.project_a}
        async with agent_scope(agent_factory, uuid.uuid4()) as session:
            assert (await session.execute(text(read_one))).all() == []
        async with agent_factory() as session:  # no scope set
            assert (await session.execute(text(read_one))).all() == []
