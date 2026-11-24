"""Tier 2: the importer seam end to end on a real Postgres (migrated scratch database, in-memory object store): a fake
importer stores pages through ``ImportWriter`` and the rows are what the graph build reads (``PostgresCorpusSource``),
a removal behaves as an upload's, a restart ends an interrupted import. Skipped unless ``SPECFLOW_TEST_POSTGRES=1``.

Sequence of the first test::

    test -> runner.add_source -> connections row (confluence, ``listing``)
    test -> runner.start(job, importer): importing -> importer: writer.declare / writer.add per page
            (object stored, documents row ``parsed`` held by the source, connection_items ``processed``) -> done
    test -> PostgresCorpusSource.snapshot -> one revision per page (the graph build's input)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.graph_build.corpus import PostgresCorpusSource
from api.imports import ImportJob, ImportRunner, ImportWriter
from db import removal
from db.github import session_transaction
from db.session import build_engine, build_session_factory
from settings import DatabaseSettings
from specflow_live import requires_live
from storage.keys import document_prefix
from storage.memory import InMemoryObjectStore

pytestmark = requires_live("POSTGRES")

PAGES = (
    ("page-1", "Order limits", "# Order limits\n\nAbove 500 EUR a second approval is needed."),
    ("page-2", "Refunds", "# Refunds\n\nRefunds go back to the original payment."),
)


@pytest.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def store() -> InMemoryObjectStore:
    return InMemoryObjectStore(app_env="local")


@pytest.fixture
async def runner(factory, store) -> AsyncIterator[ImportRunner]:
    made = ImportRunner(transaction=session_transaction(factory), store=store)
    yield made
    await made.aclose()


async def member_project(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    user, project = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, actor_id, display_name, email) VALUES (:id, :a, 'n', :e)"),
            {"id": user, "a": f"actor-{user}", "e": f"{user}@example.com"},
        )
        await session.execute(
            text("INSERT INTO projects (id, name, created_by) VALUES (:id, 'P', :u)"), {"id": project, "u": user}
        )
    return user, project


def pages_importer(pages=PAGES):
    async def importer(_job: ImportJob, writer: ImportWriter) -> None:
        for external_id, name, _markdown in pages:
            await writer.declare(external_id, name)
        for external_id, name, markdown in pages:
            await writer.add(external_id, name=name, original=markdown.encode(), markdown=markdown)

    return importer


async def run(runner: ImportRunner, project, user, *, kind: str, name: str, importer) -> ImportJob:
    source = await runner.add_source(project, user, kind=kind, name=name, link=f"https://wiki.example/{name}")
    job = ImportJob(project, source, user, kind)
    runner.start(job, importer)
    await asyncio.wait_for(asyncio.gather(*runner._tasks.values()), timeout=10)
    return job


async def scalar(factory, statement: str, **params):
    async with factory() as session:
        return (await session.execute(text(statement), params)).scalar_one()


async def test_pages_stored_by_an_importer_are_the_graph_corpus_of_the_project(factory, runner, store) -> None:
    user, project = await member_project(factory)

    job = await run(runner, project, user, kind="confluence", name="Space ORD", importer=pages_importer())

    assert await scalar(factory, "SELECT state FROM connections WHERE id = :s", s=job.source_id) == "done"
    assert await scalar(factory, "SELECT last_synced_at IS NOT NULL FROM connections WHERE id = :s", s=job.source_id)
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT d.original_filename, d.origin, d.status, d.parser, d.source_id, d.markdown, d.original_key,"
                    " i.state, i.external_id, i.document_id = d.id AS linked "
                    "FROM documents d JOIN connection_items i ON i.document_id = d.id "
                    "WHERE d.project_id = :p ORDER BY d.original_filename"
                ),
                {"p": project},
            )
        ).all()
    assert [(r.original_filename, r.external_id) for r in rows] == [("Order limits", "page-1"), ("Refunds", "page-2")]
    assert all(
        (r.origin, r.status, r.parser, r.source_id, r.state, r.linked)
        == ("connection", "parsed", "confluence-convert", job.source_id, "processed", True)
        for r in rows
    )
    assert [r.markdown for r in rows] == [PAGES[0][2], PAGES[1][2]]
    for row in rows:
        assert await store.exists(row.original_key)
    corpus = await PostgresCorpusSource(factory).snapshot(str(project))
    assert [(c.path, c.title, c.markdown) for c in corpus] == [
        ("Space ORD/Order limits", "Order limits", PAGES[0][2]),
        ("Space ORD/Refunds", "Refunds", PAGES[1][2]),
    ]
    assert all(c.lineage_id.startswith("doc:") and c.revision_id.endswith(":confluence-convert") for c in corpus)


async def test_content_the_project_already_holds_is_a_duplicate_that_links_the_first_document(
    factory, runner, store
) -> None:
    user, project = await member_project(factory)
    first = await run(runner, project, user, kind="confluence", name="Space ORD", importer=pages_importer(PAGES[:1]))

    second = await run(runner, project, user, kind="jira_epic", name="Epic ABC-1", importer=pages_importer(PAGES[:1]))

    assert await scalar(factory, "SELECT count(*) FROM documents WHERE project_id = :p", p=project) == 1
    held = await scalar(factory, "SELECT source_id FROM documents WHERE project_id = :p", p=project)
    assert held == first.source_id
    item = await scalar(factory, "SELECT state FROM connection_items WHERE connection_id = :s", s=second.source_id)
    linked = await scalar(
        factory, "SELECT document_id IS NOT NULL FROM connection_items WHERE connection_id = :s", s=second.source_id
    )
    assert (item, linked) == ("duplicate", True)
    assert len(await PostgresCorpusSource(factory).snapshot(str(project))) == 1
    assert await scalar(factory, "SELECT state FROM connections WHERE id = :s", s=second.source_id) == "done"


async def test_a_failing_importer_ends_the_source_and_its_unfinished_items(factory, runner) -> None:
    user, project = await member_project(factory)

    async def importer(_job: ImportJob, writer: ImportWriter) -> None:
        await writer.declare("page-1", "Order limits")
        await writer.add("page-1", name="Order limits", original=b"{}", markdown=PAGES[0][2])
        await writer.declare("page-2", "Refunds")
        raise RuntimeError("boom")

    job = await run(runner, project, user, kind="confluence", name="Space ORD", importer=importer)

    row = await _first(factory, "SELECT state, failure_reason FROM connections WHERE id = :s", s=job.source_id)
    assert tuple(row) == ("failed", "unknown_error")
    async with factory() as session:
        states = (
            await session.execute(
                text("SELECT external_id, state, error_reason FROM connection_items WHERE connection_id = :s"),
                {"s": job.source_id},
            )
        ).all()
    assert sorted(tuple(s) for s in states) == [("page-1", "processed", None), ("page-2", "error", "import_failed")]


async def test_removing_the_source_deletes_its_documents_and_items_as_an_upload_does(factory, runner, store) -> None:
    user, project = await member_project(factory)
    job = await run(runner, project, user, kind="confluence", name="Space ORD", importer=pages_importer())
    documents = [
        row[0] async for row in _rows(factory, "SELECT id FROM documents WHERE source_id = :s", s=job.source_id)
    ]

    async with factory() as session, session.begin():
        done = await removal.remove_source(session, project, job.source_id)

    assert done.kind == "confluence" and not done.handed_over and not done.drive_import_open
    assert sorted(done.object_prefixes) == sorted(document_prefix(str(project), str(d)) for d in documents)
    assert await scalar(factory, "SELECT count(*) FROM documents WHERE project_id = :p", p=project) == 0
    assert await scalar(factory, "SELECT count(*) FROM connection_items WHERE project_id = :p", p=project) == 0
    assert await scalar(factory, "SELECT count(*) FROM connections WHERE project_id = :p", p=project) == 0
    assert await PostgresCorpusSource(factory).snapshot(str(project)) == []


async def test_a_source_removed_while_importing_stops_the_import_at_its_next_write(factory, runner) -> None:
    user, project = await member_project(factory)
    reached, go = asyncio.Event(), asyncio.Event()

    async def importer(_job: ImportJob, writer: ImportWriter) -> None:
        await writer.declare("page-1", "Order limits")
        reached.set()
        await go.wait()
        await writer.add("page-1", name="Order limits", original=b"{}", markdown=PAGES[0][2])

    source = await runner.add_source(project, user, kind="confluence", name="Space ORD", link="https://wiki.example/o")
    runner.start(ImportJob(project, source, user, "confluence"), importer)
    await reached.wait()
    async with factory() as session, session.begin():
        await removal.remove_source(session, project, source)
    go.set()
    await asyncio.wait_for(asyncio.gather(*runner._tasks.values()), timeout=10)

    assert await scalar(factory, "SELECT count(*) FROM documents WHERE project_id = :p", p=project) == 0
    assert await scalar(factory, "SELECT count(*) FROM connections WHERE project_id = :p", p=project) == 0


async def test_the_start_ends_interrupted_imports_of_the_imported_kinds_and_leaves_every_other_source(
    factory, runner
) -> None:
    user, project = await member_project(factory)
    ids = {}
    async with factory() as session, session.begin():
        for kind, state in (
            ("confluence", "listing"),
            ("jira_epic", "importing"),
            ("confluence", "done"),
            ("upload", "importing"),
        ):
            ids[kind, state] = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO connections (id, project_id, kind, external_id, name, state, connected_by) "
                    "VALUES (:id, :p, :k, :e, 'S', :st, :u)"
                ),
                {"id": ids[kind, state], "p": project, "k": kind, "e": str(uuid.uuid4()), "st": state, "u": user},
            )
        await session.execute(
            text(
                "INSERT INTO connection_items (id, connection_id, project_id, external_id, name, mime_type, category, "
                "processing_class, state) VALUES (gen_random_uuid(), :c, :p, 'page-1', 'Page', 'text/markdown', "
                "'other_files', 'processed', 'pending')"
            ),
            {"c": ids["jira_epic", "importing"], "p": project},
        )

    runner.start_recovery()
    await asyncio.wait_for(runner._recovery, timeout=10)

    states = {key: await scalar(factory, "SELECT state FROM connections WHERE id = :i", i=i) for key, i in ids.items()}
    assert states == {
        ("confluence", "listing"): "failed",
        ("jira_epic", "importing"): "failed",
        ("confluence", "done"): "done",
        ("upload", "importing"): "importing",  # Temporal ends those
    }
    reason = await scalar(
        factory, "SELECT failure_reason FROM connections WHERE id = :i", i=ids["jira_epic", "importing"]
    )
    item = await scalar(factory, "SELECT error_reason FROM connection_items WHERE project_id = :p", p=project)
    assert (reason, item) == ("unknown_error", "import_failed")


async def _rows(factory, statement: str, **params):
    async with factory() as session:
        for row in (await session.execute(text(statement), params)).all():
            yield row


async def _first(factory, statement: str, **params):
    async with factory() as session:
        return (await session.execute(text(statement), params)).one()
