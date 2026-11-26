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
from api.imports import ImportFailedError, ImportJob, ImportRunner, ImportWriter
from api.imports.confluence import ConfluenceImport, ConfluenceImporter
from api.imports.jira_epic import JiraEpicImport, JiraEpicImporter
from api.imports.notion import NotionImport, NotionImporter
from atlassian_import_support import (
    FakeAtlassian,
    FakeConnections,
    install,
    jira_comment,
    jira_issue,
    storage_page,
)
from db import removal, source_files
from db import sources as sources_db
from db.github import session_transaction
from db.session import build_engine, build_session_factory
from jira.confluence_links import PageTarget
from notion.fake import PRD_PAGE_ID, WIKI_PAGE_ID
from notion.links import NotionLink
from notion_support import FakeNotionConnections
from settings import DatabaseSettings
from specflow_live import requires_live
from storage.keys import document_prefix
from storage.memory import InMemoryObjectStore
from test_api_import_notion import FAKE_CONVERTER, ONBOARDING

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


async def test_a_source_removed_while_importing_stops_the_import_at_its_next_write(factory, runner, store) -> None:
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
    assert store.objects == {}  # the original the importer stored before it found the source gone is deleted


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

    await age(factory, project)
    await runner.start_recovery()
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


async def age(factory, project) -> None:
    """Make every source of the project look untouched for an hour, as one a stopped process left."""

    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE connections SET updated_at = now() - interval '1 hour' WHERE project_id = :p"), {"p": project}
        )


async def test_the_recovery_never_ends_a_source_added_after_the_process_started(factory, runner) -> None:
    user, project = await member_project(factory)
    old = await runner.add_source(project, user, kind="confluence", name="Old", link="https://wiki.example/old")
    await age(factory, project)

    await runner.start_recovery()  # the start instant is read from the database clock here
    fresh = await runner.add_source(project, user, kind="confluence", name="Fresh", link="https://wiki.example/new")
    await asyncio.wait_for(runner._recovery, timeout=10)

    assert await scalar(factory, "SELECT state FROM connections WHERE id = :i", i=old) == "failed"
    assert await scalar(factory, "SELECT state FROM connections WHERE id = :i", i=fresh) == "listing"


async def test_a_source_touched_a_moment_before_the_start_by_a_dead_process_is_ended(factory, runner) -> None:
    user, project = await member_project(factory)
    just_before = await runner.add_source(project, user, kind="jira_epic", name="A", link="https://wiki.example/a")

    await runner.start_recovery()
    await asyncio.wait_for(runner._recovery, timeout=10)

    assert await scalar(factory, "SELECT state FROM connections WHERE id = :i", i=just_before) == "failed"
    assert (
        await scalar(factory, "SELECT failure_reason FROM connections WHERE id = :i", i=just_before) == "unknown_error"
    )


async def test_a_confluence_import_is_the_corpus_and_is_listed_by_both_sources_statuses(
    factory, runner, monkeypatch
) -> None:
    user, project = await member_project(factory)
    fake = FakeAtlassian(
        pages={
            "5": storage_page("5", "Order limits", "<h2>Limits</h2><p>Above <b>500 EUR</b>.</p>"),
            "6": storage_page("6", "Refunds", "<p>Back to the payment.</p>"),
        },
        below={"5": [{"id": "6", "title": "Refunds", "type": "page"}, {"id": "7", "title": "Hidden", "type": "page"}]},
        status={"/wiki/api/v2/pages/7": 403},
    )
    install(monkeypatch, fake)
    importer = ConfluenceImporter(FakeConnections(user_id=user), ConfluenceImport(PageTarget("5"), follow=False))

    job = await run(runner, project, user, kind="confluence", name="Order limits", importer=importer)

    corpus = await PostgresCorpusSource(factory).snapshot(str(project))
    assert sorted((c.path, c.title) for c in corpus) == [
        ("Order limits/Order limits", "Order limits"),
        ("Order limits/Refunds", "Refunds"),
    ]
    assert "Above **500 EUR**." in next(c.markdown for c in corpus if c.title == "Order limits")
    async with factory() as session:
        progress = await sources_db.sources_progress(session, project)
        summaries = await source_files.source_summaries(session, project)
    (listed,) = progress.connections
    assert (listed.connection_id, listed.state, listed.progress.total, listed.progress.finished) == (
        job.source_id,
        "done",
        3,
        3,
    )
    (card,) = summaries
    assert (card.kind, card.name, card.total, card.listing_finished, card.failure_reason) == (
        "confluence",
        "Order limits",
        3,
        True,
        None,
    )
    assert card.counts["done"] == 2 and sum(card.counts.values()) == 3  # the page nobody could read is a problem item


async def test_a_failed_import_shows_its_closed_reason_in_both_sources_statuses(factory, runner) -> None:
    user, project = await member_project(factory)

    async def importer(_job: ImportJob, writer: ImportWriter) -> None:
        await writer.declare("page:5", "Order limits")
        raise ImportFailedError("no_access")

    job = await run(runner, project, user, kind="jira_epic", name="Epic ABC-1", importer=importer)

    async with factory() as session:
        progress = await sources_db.sources_progress(session, project)
        (card,) = await source_files.source_summaries(session, project)
    (listed,) = progress.connections
    assert (listed.connection_id, listed.state, listed.failure_reason) == (job.source_id, "failed", "no_access")
    assert (card.kind, card.failure_reason, card.listing_finished) == ("jira_epic", "no_access", True)


async def test_a_jira_epic_import_is_the_corpus_with_its_issues_and_comments(factory, runner, monkeypatch) -> None:
    user, project = await member_project(factory)
    epic = jira_issue("PAY-1", "Checkout", "The epic.")
    epic["fields"]["issuetype"] = {"name": "Epic", "hierarchyLevel": 1}
    card = {**jira_issue("PAY-2", "Card flow", "Pay by card.", [jira_comment("Ada", "Looks good")]), "parent": "PAY-1"}
    install(monkeypatch, FakeAtlassian(issues={"PAY-1": epic, "PAY-2": card}))
    importer = JiraEpicImporter(FakeConnections(user_id=user), JiraEpicImport("PAY-1"))

    job = await run(runner, project, user, kind="jira_epic", name="PAY-1: Checkout", importer=importer)

    corpus = await PostgresCorpusSource(factory).snapshot(str(project))
    assert sorted(c.title for c in corpus) == ["PAY-1: Checkout", "PAY-2: Card flow"]
    assert all(c.path.startswith("PAY-1: Checkout/") for c in corpus)
    assert "**Ada** (2026-03-01):\n\nLooks good" in next(c.markdown for c in corpus if c.title.startswith("PAY-2"))
    async with factory() as session:
        (listed,) = (await sources_db.sources_progress(session, project)).connections
        (summary,) = await source_files.source_summaries(session, project)
    assert (listed.connection_id, listed.state, listed.progress.total) == (job.source_id, "done", 2)
    assert (summary.kind, summary.counts["done"], summary.failure_reason) == ("jira_epic", 2, None)


# Notion: the importer over the fixture-backed gateway and the real writer ----------------------------------------


async def test_a_notion_import_is_the_corpus_with_slim_originals_and_a_skipped_page_is_a_problem_item(
    factory, runner, store
) -> None:

    user, project = await member_project(factory)
    connections = FakeNotionConnections()
    connections.users = {user: "active"}
    connections.fake.unshare(ONBOARDING)
    importer = NotionImporter(connections, NotionImport(NotionLink(WIKI_PAGE_ID)), FAKE_CONVERTER)

    job = await run(runner, project, user, kind="notion", name="Engineering Wiki", importer=importer)

    corpus = await PostgresCorpusSource(factory).snapshot(str(project))
    assert sorted(c.title for c in corpus) == ["Engineering Wiki", "Runbook - Points backfill", "Runbooks"]
    assert all("<page" not in c.markdown for c in corpus)
    assert (
        await scalar(
            factory, "SELECT count(*) FROM documents WHERE project_id = :p AND parser = 'notion-convert'", p=project
        )
        == 3
    )
    async with factory() as session:
        (card,) = await source_files.source_summaries(session, project)
    assert (card.kind, card.name, card.total, card.listing_finished, card.failure_reason) == (
        "notion",
        "Engineering Wiki",
        4,
        True,
        None,
    )
    assert card.counts["done"] == 3 and sum(card.counts.values()) == 4  # the page nobody shared is a problem item
    assert job.source_id is not None and all(b"person@example.com" not in blob for blob in store.objects.values())


async def test_a_notion_import_of_an_unshared_link_fails_the_source_with_no_access(factory, runner) -> None:

    user, project = await member_project(factory)
    connections = FakeNotionConnections()
    connections.users = {user: "active"}
    connections.fake.unshare(PRD_PAGE_ID)

    job = await run(
        runner,
        project,
        user,
        kind="notion",
        name="PRD",
        importer=NotionImporter(connections, NotionImport(NotionLink(PRD_PAGE_ID)), FAKE_CONVERTER),
    )

    assert await scalar(factory, "SELECT state FROM connections WHERE id = :i", i=job.source_id) == "failed"
    assert await scalar(factory, "SELECT failure_reason FROM connections WHERE id = :i", i=job.source_id) == "no_access"


async def test_the_start_ends_an_interrupted_notion_import(factory, runner) -> None:
    user, project = await member_project(factory)
    source = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO connections (id, project_id, kind, external_id, name, state, connected_by) "
                "VALUES (:id, :p, 'notion', :e, 'S', 'importing', :u)"
            ),
            {"id": source, "p": project, "e": str(uuid.uuid4()), "u": user},
        )

    await age(factory, project)
    await runner.start_recovery()
    await asyncio.wait_for(runner._recovery, timeout=10)

    assert await scalar(factory, "SELECT state FROM connections WHERE id = :i", i=source) == "failed"
    assert await scalar(factory, "SELECT failure_reason FROM connections WHERE id = :i", i=source) == "unknown_error"
