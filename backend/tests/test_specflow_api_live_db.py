"""Tier 2 (``SPECFLOW_TEST_POSTGRES=1``): the API on a real, migrated scratch database.

AC-7.2, 7.3, 7.4 and 7.5, 7.6, 7.8, 7.9, 7.10 against the real repositories.
Object storage is the in-memory store and Temporal is a fake; with
``SPECFLOW_TEST_TEMPORAL=1`` one more test starts the real workflow by name.
"""

from __future__ import annotations

import hashlib
import uuid

import httpx
from sqlalchemy import select
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from db.models import Document, ProjectMember, User
from specflow_api_live_support import open_harness
from specflow_api_support import PDF, bearer
from specflow_live import requires_live

pytestmark = requires_live("POSTGRES")
PDF_HEADERS = {"Content-Type": "application/pdf"}


async def make_project(live, email="alice@example.com") -> str:
    response = await live.client.post("/api/v1/projects", json={"name": "p"}, headers=bearer(email))
    assert response.status_code == 201
    return response.json()["id"]


async def put(live, pid, data=PDF, email="alice@example.com"):
    return await live.client.post(
        f"/api/v1/projects/{pid}/documents?filename=a.pdf",
        content=data,
        headers={**bearer(email), **PDF_HEADERS},
    )


async def select_all(live, statement):
    async with live.repo() as session:
        return list((await session.scalars(statement)).all())


async def test_me_and_project_create_use_one_user_and_an_owner_member(live):
    first = (await live.client.get("/api/v1/me", headers=bearer("Alice@example.com"))).json()
    await live.client.get("/api/v1/me", headers=bearer("Alice@example.com", name="Alice Two"))
    users = await select_all(live, select(User))
    assert len(users) == 1 and users[0].display_name == "Alice Two"
    assert first["actor_id"] == "human:alice"
    pid = await make_project(live, "Alice@example.com")
    [member] = await select_all(live, select(ProjectMember))
    assert str(member.project_id) == pid and member.role == "owner"
    blank = await live.client.post("/api/v1/projects", json={"name": " "}, headers=bearer())
    assert blank.status_code == 422


async def test_a_non_member_gets_404_for_project_and_documents(live):
    pid = await make_project(live)
    document = (await put(live, pid)).json()["document"]
    eve = bearer("eve@example.com")
    for path in (
        f"/api/v1/projects/{pid}",
        f"/api/v1/projects/{pid}/documents",
        f"/api/v1/projects/{pid}/documents/{document['id']}",
    ):
        assert (await live.client.get(path, headers=eve)).status_code == 404


async def test_upload_matches_hash_and_size_and_repeats_as_duplicate(live):
    pid = await make_project(live)
    data = PDF * 100
    created = await put(live, pid, data)
    assert created.status_code == 201
    document = created.json()["document"]
    [row] = await select_all(live, select(Document))
    assert row.status == "processing" and row.size_bytes == len(data)  # K5
    assert [c["workflow"] for c in live.temporal.calls] == ["ParseDocumentWorkflow"]
    assert row.content_hash == hashlib.sha256(data).hexdigest()
    key = f"projects/{pid}/documents/{document['id']}/original.pdf"
    assert row.original_key == key and live.store.objects[key] == data

    again = await put(live, pid, data)
    assert again.status_code == 200 and again.json()["outcome"] == "duplicate"
    assert again.json()["document"]["id"] == document["id"]
    assert list(live.store.objects) == [key]  # the second object was deleted (C20)

    other = await make_project(live)
    assert (await put(live, other, data)).status_code == 201


async def test_a_reupload_of_a_failed_document_parses_the_real_row_again(live):
    pid = await make_project(live)
    did = (await put(live, pid)).json()["document"]["id"]
    async with live.repo() as session, session.begin():
        row = await session.get(Document, uuid.UUID(did))
        row.status, row.error = "failed", "pdf_parse_failed"
    live.temporal.calls.clear()  # the first parse has finished

    again = await put(live, pid)
    assert again.status_code == 200
    body = again.json()
    assert body["outcome"] == "retried" and body["document"]["id"] == did
    assert body["document"]["status"] == "processing"
    [row] = await select_all(live, select(Document))
    assert (row.status, row.error) == ("processing", None)
    assert [c["workflow"] for c in live.temporal.calls] == ["ParseDocumentWorkflow"]
    assert list(live.store.objects) == [row.original_key]

    third = await put(live, pid)  # now processing: a plain duplicate
    assert third.json()["outcome"] == "duplicate"
    assert len(live.temporal.calls) == 1


async def test_parse_and_summarize_transitions_on_the_real_rows(live):
    pid = await make_project(live)
    did = (await put(live, pid)).json()["document"]["id"]
    base = f"/api/v1/projects/{pid}/documents/{did}"
    assert (await live.client.post(f"{base}/summarize", headers=bearer())).status_code == 409

    first = await live.client.post(f"{base}/parse", json={}, headers=bearer())
    second = await live.client.post(f"{base}/parse", json={}, headers=bearer())
    assert first.status_code == second.status_code == 202 and first.json() == second.json()
    assert (await select_all(live, select(Document.status)))[0] == "processing"

    async with live.repo() as session, session.begin():
        row = await session.get(Document, uuid.UUID(did))
        row.status = "parsed"
    assert (await live.client.post(f"{base}/parse", headers=bearer())).status_code == 409

    one = await live.client.post(f"{base}/summarize", json={}, headers=bearer())
    two = await live.client.post(f"{base}/summarize", json={}, headers=bearer())
    assert one.status_code == two.status_code == 202 and one.json() == two.json()
    [row] = await select_all(live, select(Document))
    assert row.summarization_status == "running"
    assert {c["id"] for c in live.temporal.calls if c["workflow"] == "SummarizeDocumentWorkflow"} == {
        f"doc-summary-{did}"
    }


async def test_a_failed_start_marks_the_real_row_failed(live):
    pid = await make_project(live)
    live.temporal.fail = RuntimeError("down")
    uploaded = await put(live, pid)  # K5: the upload's own start fails
    assert uploaded.status_code == 201
    assert uploaded.json()["document"]["status"] == "failed"
    did = uploaded.json()["document"]["id"]
    response = await live.client.post(f"/api/v1/projects/{pid}/documents/{did}/parse", headers=bearer())
    assert response.status_code == 503
    [row] = await select_all(live, select(Document))
    assert (row.status, row.error) == ("failed", "workflow_start_failed")
    live.temporal.fail = None
    assert (
        await live.client.post(f"/api/v1/projects/{pid}/documents/{did}/parse", headers=bearer())
    ).status_code == 202  # a failed document can be retried


@requires_live("TEMPORAL")
async def test_the_real_temporal_accepts_both_workflow_starts_by_name(migrated_database, valid_env, temporal_live):
    """No workflow code needs to exist: the start only records the workflow on its queue."""

    client = await Client.connect(
        valid_env["TEMPORAL_ADDRESS"],
        namespace=temporal_live.client.namespace,
        data_converter=pydantic_data_converter,
    )
    engine, factory, _store, app = await open_harness(
        migrated_database, valid_env, client, id_prefix=temporal_live.id_prefix
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            pid = (await http.post("/api/v1/projects", json={"name": "t"}, headers=bearer())).json()["id"]
            did = (
                await http.post(
                    f"/api/v1/projects/{pid}/documents?filename=a.pdf",
                    content=PDF,
                    headers={**bearer(), **PDF_HEADERS},
                )
            ).json()["document"]["id"]
            parse = await http.post(f"/api/v1/projects/{pid}/documents/{did}/parse", headers=bearer())
            again = await http.post(f"/api/v1/projects/{pid}/documents/{did}/parse", headers=bearer())
            assert parse.status_code == again.status_code == 202
            assert (
                parse.json()["workflow_id"]
                == again.json()["workflow_id"]
                == (f"{temporal_live.id_prefix}doc-parse-{did}")
            )
            handle = client.get_workflow_handle(parse.json()["workflow_id"])
            temporal_live.track(handle)
            assert (await handle.describe()).status.name == "RUNNING"
            async with factory() as session, session.begin():
                (await session.get(Document, uuid.UUID(did))).status = "parsed"
            summary = await http.post(f"/api/v1/projects/{pid}/documents/{did}/summarize", headers=bearer())
            assert summary.status_code == 202
            temporal_live.track(client.get_workflow_handle(summary.json()["workflow_id"]))
    await engine.dispose()
