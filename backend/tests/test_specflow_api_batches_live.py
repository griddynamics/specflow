"""Tier 2 (``SPECFLOW_TEST_POSTGRES=1``): the sources-ux API on a real, migrated database.

Requirements: INT-SRC-0001..0004, FR-SRC-0016/0018/0019, NFR-REL-0003, NFR-SEC-0002.
Covers session 04: batch start / add / end, the file list, the legacy upload in its
day batch, removal (rows, objects, counter, rollback, lock timeout, concurrency), and
membership and cross-project ids on every route. Object storage is the in-memory
store, Drive is the fake gateway and Temporal is ``FakeTemporal`` (it records the
started workflows and the terminations).

The last test also runs against the real local Temporal (``SPECFLOW_TEST_TEMPORAL=1``, a
unique id prefix and task queue; no worker polls it). The Temporal-only tests of the API's
workflow starts are in ``test_specflow_api_temporal_live.py``.

Run (against your local stack; the env file is the one of your main checkout):
``cd backend && SPECFLOW_TEST_POSTGRES=1 SPECFLOW_TEST_TEMPORAL=1 uv run --env-file
../.env.local pytest
tests/test_specflow_api_sources_live.py tests/test_specflow_api_batches_live.py
tests/test_specflow_api_temporal_live.py tests/test_specflow_api_live_db.py``
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest_asyncio
from sqlalchemy import select, text
from temporalio.client import WorkflowExecutionStatus

from db import removal
from db.models import Connection, ConnectionItem, Document, Project
from drive.fake import FakeDriveGateway
from specflow_api_live_support import open_harness
from specflow_api_support import PDF, FakeDriver, Harness, bearer
from specflow_api_temporal_support import terminate_prefixed
from specflow_live import (
    requires_live,
)
from specflow_sources_live_support import add_item
from test_specflow_api_live_db import (
    make_project,
    put,
    select_all,
)
from test_specflow_api_sources_live import FOLDER, post, status

# The Drive import stack pulls in the PDF parser, so the conftest does not load it for every run.
pytest_plugins = ["specflow_drive_import_support"]
pytestmark = requires_live("POSTGRES")
PDF_HEADERS = {"Content-Type": "application/pdf"}
ALICE = "alice@example.com"
VISIBILITY_POLL_SECONDS = 0.5  # Temporal visibility is eventually consistent


def pdf(n: int) -> bytes:
    return PDF + str(n).encode()


def src(pid, tail=""):
    return f"/api/v1/projects/{pid}/sources{tail}"


async def start_batch(live, pid, expected=3, email=ALICE):
    response = await live.client.post(src(pid, "/uploads"), json={"expected_files": expected}, headers=bearer(email))
    assert response.status_code == 201, response.text
    return response.json()["source"]["id"]


async def add_pdf(live, pid, sid, n, name=None, *, email=ALICE):
    return await live.client.post(
        src(pid, f"/{sid}/files?filename={name or f'f{n}.pdf'}"),
        content=pdf(n),
        headers={**bearer(email), **PDF_HEADERS},
    )


async def end_batch(live, pid, sid, email=ALICE):
    return await live.client.post(src(pid, f"/{sid}/end"), headers=bearer(email))


async def remove(live, pid, sid, email=ALICE):
    return await live.client.delete(src(pid, f"/{sid}"), headers=bearer(email))


async def counts(live):
    # a list, not a generator: ``await`` inside a generator expression makes an async generator
    return tuple([len(await select_all(live, select(model))) for model in (Connection, ConnectionItem, Document)])


async def test_a_batch_end_to_end_start_add_end_then_the_cards_and_the_file_list(live):
    pid = await make_project(live)
    sid = await start_batch(live, pid, 3)
    first = await add_pdf(live, pid, sid, 1)
    second = await add_pdf(live, pid, sid, 2)
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["outcome"] == "added"
    assert first.json()["file"]["state"] == "processing"
    ended = await end_batch(live, pid, sid)
    assert ended.status_code == 200
    card = ended.json()["source"]
    assert (card["id"], card["open"], card["total"]) == (sid, False, 2)
    docs = await select_all(live, select(Document))
    assert {str(d.source_id) for d in docs} == {sid} and len(docs) == 2
    items = await select_all(live, select(ConnectionItem))
    assert {i.state for i in items} == {"pending"} and len(items) == 2
    page = (await live.client.get(src(pid, f"/{sid}/files"), headers=bearer())).json()
    assert [i["state"] for i in page["items"]] == ["processing", "processing"]
    body = (await live.client.get(src(pid, "?batches=1"), headers=bearer())).json()
    assert [s["id"] for s in body["sources"]] == [sid] and body["removed_files"] == 0
    assert [c["workflow"] for c in live.temporal.calls] == ["ParseDocumentWorkflow"] * 2


async def test_add_outcomes_on_the_real_database(live):
    pid = await make_project(live)
    one = await start_batch(live, pid, 2)
    assert (await add_pdf(live, pid, one, 1)).json()["outcome"] == "added"
    again = await add_pdf(live, pid, one, 1, "again.pdf")
    assert again.json() == {"outcome": "duplicate", "file": None, "holder": None}
    await end_batch(live, pid, one)
    async with live.repo() as session, session.begin():
        await session.execute(text("UPDATE documents SET status = 'parsed'"))
    two = await start_batch(live, pid, 1)
    linked = (await add_pdf(live, pid, two, 1, "same.pdf")).json()
    assert linked["outcome"] == "already_in_project" and linked["holder"]["id"] == one
    assert (await add_pdf(live, pid, one, 9)).status_code == 409  # `one` is ended
    # a failed file moves to the newest batch; the emptied old batches disappear or stay
    async with live.repo() as session, session.begin():
        await session.execute(text("UPDATE documents SET status = 'failed', error = 'pdf_parse_failed'"))
    three = await start_batch(live, pid, 1)
    moved = (await add_pdf(live, pid, three, 1, "retry.pdf")).json()
    assert moved["outcome"] == "read_again" and moved["file"]["state"] == "processing"
    [doc] = await select_all(live, select(Document))
    assert str(doc.source_id) == three and doc.status == "processing"
    assert doc.original_key in live.store.objects  # the moved document keeps its own object
    assert set(live.store.objects) == {doc.original_key}  # every new object was deleted
    assert uuid.UUID(one) not in {c.id for c in await select_all(live, select(Connection))}


async def test_the_legacy_upload_fills_one_ended_day_batch(live):
    pid = await make_project(live)
    assert (await put(live, pid, pdf(1))).status_code == 201
    assert (await put(live, pid, pdf(2))).status_code == 201
    assert (await put(live, pid, pdf(1))).json()["outcome"] == "duplicate"
    [batch] = await select_all(live, select(Connection))
    assert batch.kind == "upload" and batch.external_id.startswith("legacy-")
    assert batch.ended_at is not None
    docs = await select_all(live, select(Document))
    assert {d.source_id for d in docs} == {batch.id}  # no upload leaves source_id NULL
    card = (await live.client.get(src(pid, "?batches=1"), headers=bearer())).json()["sources"][0]
    assert (card["open"], card["total"]) == (False, 2)
    legacy = (await status(live, pid)).json()  # the old shape is unchanged
    assert set(legacy) == {"progress", "sources", "uploads", "processed_documents"}
    assert legacy["sources"] == [] and legacy["uploads"]["documents"] == 2


async def test_removing_a_batch_deletes_rows_and_objects_and_counts_the_files(live):
    pid = await make_project(live)
    sid = await start_batch(live, pid, 2)
    await add_pdf(live, pid, sid, 1)
    await add_pdf(live, pid, sid, 2)
    await end_batch(live, pid, sid)
    live.temporal.calls.clear()
    assert (await remove(live, pid, sid)).status_code == 204
    assert await counts(live) == (0, 0, 0) and not live.store.objects
    [project] = await select_all(live, select(Project))
    assert project.removed_files == 2
    [cleanup] = live.temporal.calls
    assert cleanup["workflow"] == "SourceCleanupWorkflow"
    assert len(cleanup["arg"].document_ids) == 2
    gone = await remove(live, pid, sid)
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "source_not_found"
    body = (await live.client.get(src(pid, "?batches=1"), headers=bearer())).json()
    assert body["sources"] == [] and body["removed_files"] == 2


async def test_a_cleanup_start_failure_rolls_the_whole_removal_back(live):
    pid = await make_project(live)
    sid = await start_batch(live, pid, 1)
    await add_pdf(live, pid, sid, 1)
    await end_batch(live, pid, sid)
    before, objects = await counts(live), set(live.store.objects)
    live.temporal.fail = RuntimeError("temporal down")
    response = await remove(live, pid, sid)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "removal_unavailable"
    assert await counts(live) == before and set(live.store.objects) == objects
    [project] = await select_all(live, select(Project))
    assert project.removed_files == 0
    live.temporal.fail = None
    assert (await remove(live, pid, sid)).status_code == 204


async def test_a_lock_timeout_is_503_removal_unavailable_and_changes_nothing(live, monkeypatch):

    pid = await make_project(live)
    sid = await start_batch(live, pid, 1)
    await add_pdf(live, pid, sid, 1)
    await end_batch(live, pid, sid)
    before = await counts(live)
    monkeypatch.setattr(removal, "LOCK_TIMEOUT", "1s")
    async with live.repo() as holder, holder.begin():  # somebody else holds the project row
        await holder.execute(text("SELECT id FROM projects WHERE id = :p FOR UPDATE"), {"p": uuid.UUID(pid)})
        response = await remove(live, pid, sid)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "removal_unavailable"
    assert await counts(live) == before
    assert (await remove(live, pid, sid)).status_code == 204  # the lock is free again


async def test_two_concurrent_removals_give_one_204_and_one_404(live):
    pid = await make_project(live)
    sid = await start_batch(live, pid, 1)
    await add_pdf(live, pid, sid, 1)
    await end_batch(live, pid, sid)
    first, second = await asyncio.gather(remove(live, pid, sid), remove(live, pid, sid))
    assert sorted([first.status_code, second.status_code]) == [204, 404]
    [project] = await select_all(live, select(Project))
    assert project.removed_files == 1


async def test_removing_a_drive_source_terminates_its_import(live):
    live.runtime.drive = FakeDriveGateway(app_env="local", file_delay_s=0)
    pid = await make_project(live)
    saved = (await post(live, pid, "", FOLDER)).json()["source"]
    assert (await remove(live, pid, saved["id"])).status_code == 204
    assert live.temporal.terminated == [f"drive-import-{saved['id']}"]
    assert not await select_all(live, select(Connection))


async def test_another_projects_source_and_a_non_member_on_every_route(live):
    mine, theirs = await make_project(live), await make_project(live)
    sid = await start_batch(live, theirs, 1)
    await add_pdf(live, theirs, sid, 1)
    before = await counts(live)
    other = {
        "files": await live.client.get(src(mine, f"/{sid}/files"), headers=bearer()),
        "delete": await remove(live, mine, sid),
        "add": await add_pdf(live, mine, sid, 2),
        "end": await end_batch(live, mine, sid),
    }
    for name, response in other.items():
        assert response.status_code == 404, name
        assert response.json()["error"]["code"] == "source_not_found", name
    mallory = "mallory@example.com"
    strangers = [
        await live.client.get(src(theirs, "?batches=1"), headers=bearer(mallory)),
        await live.client.get(src(theirs, f"/{sid}/files"), headers=bearer(mallory)),
        await remove(live, theirs, sid, mallory),
        await add_pdf(live, theirs, sid, 3, email=mallory),
        await end_batch(live, theirs, sid, mallory),
        await live.client.post(src(theirs, "/uploads"), json={"expected_files": 1}, headers=bearer(mallory)),
    ]
    assert {r.status_code for r in strangers} == {404}
    assert {r.json()["error"]["code"] for r in strangers} == {"project_not_found"}
    assert await counts(live) == before


async def test_a_450_file_source_pages_by_200_on_the_real_query(live):
    live.runtime.drive = FakeDriveGateway(app_env="local", file_delay_s=0)
    pid = await make_project(live)
    saved = (await post(live, pid, "", FOLDER)).json()["source"]
    async with live.repo() as session, session.begin():
        for n in range(450):
            await add_item(session, uuid.UUID(saved["id"]), uuid.UUID(pid), f"{n:03d}", "processed")
    seen, cursor, sizes = [], None, []
    while True:
        query = f"?cursor={cursor}" if cursor else ""
        body = (await live.client.get(src(pid, f"/{saved['id']}/files{query}"), headers=bearer())).json()
        sizes.append(len(body["items"]))
        seen += [i["id"] for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sizes == [200, 200, 50] and len(set(seen)) == 450
    assert body["source"]["total"] == 450 and body["source"]["counts"]["done"] == 450
    bad = await live.client.get(src(pid, f"/{saved['id']}/files?cursor=!!"), headers=bearer())
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_failed"


@pytest_asyncio.fixture
async def real_temporal_live(drive_stack, valid_env):
    """The same API on the real database AND the real local Temporal (unique id prefix)."""

    prefix = drive_stack.temporal.id_prefix
    engine, factory, store, app = await open_harness(
        drive_stack.database, valid_env, drive_stack.client, id_prefix=prefix
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            try:
                yield Harness(
                    app,
                    client,
                    factory,
                    store,
                    drive_stack.client,
                    FakeDriver(),
                    app.state.runtime,
                    {},
                )
            finally:
                await terminate_prefixed(drive_stack.client, prefix)
    await engine.dispose()


@requires_live("TEMPORAL")
async def test_a_removal_end_to_end_on_the_real_database_and_the_real_temporal(real_temporal_live):
    """M1: DELETE starts a real ``SourceCleanupWorkflow`` before the commit (NFR-REL-0003)."""

    live = real_temporal_live
    prefix = live.runtime.id_prefix
    pid = await make_project(live)
    sid = await start_batch(live, pid, 2)
    assert (await add_pdf(live, pid, sid, 1)).status_code == 201  # a real parse start
    assert (await add_pdf(live, pid, sid, 2)).status_code == 201
    await end_batch(live, pid, sid)
    assert len(live.store.objects) == 2
    assert (await remove(live, pid, sid)).status_code == 204
    assert await counts(live) == (0, 0, 0) and not live.store.objects
    # the drive_stack database may hold other projects: pick this test's own
    [project] = await select_all(live, select(Project).where(Project.id == uuid.UUID(pid)))
    assert project.removed_files == 2
    query = f'WorkflowType = "SourceCleanupWorkflow" AND WorkflowId STARTS_WITH "{prefix}source-cleanup-"'
    found = []
    for _ in range(20):  # the visibility store is eventually consistent
        found = [w async for w in live.temporal.list_workflows(query)]
        if found:
            break
        await asyncio.sleep(VISIBILITY_POLL_SECONDS)
    assert len(found) == 1 and found[0].id.endswith("-0")
    described = await live.temporal.get_workflow_handle(found[0].id).describe()
    assert described.status == WorkflowExecutionStatus.RUNNING
    assert described.task_queue.startswith("pytest-")  # never the owner's parse queue
    gone = await remove(live, pid, sid)
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "source_not_found"
