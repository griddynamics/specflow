"""Tier 2 (``SPECFLOW_TEST_POSTGRES=1``): the Drive sources API on a real database.

Requirements: INT-GDUI-0003 (one source per folder, also under concurrency; the
start-failure compensation), INT-GDUI-0004 (progress incl. uploads), K5.
Drive is the fake gateway; Temporal is a fake.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from api import workflows
from db import sources as sources_repo
from db.models import Connection, ConnectionItem
from drive.fake import FakeDriveGateway
from specflow_api_support import bearer
from specflow_live import requires_live
from test_specflow_api_live_db import make_project, put, select_all

pytestmark = requires_live("POSTGRES")
FOLDER = "https://drive.google.com/drive/folders/fake-synthetic-30"


async def post(live, pid, path, link, email="alice@example.com"):
    return await live.client.post(
        f"/api/v1/projects/{pid}/sources/gdrive{path}",
        json={"link": link},
        headers=bearer(email),
    )


async def status(live, pid, email="alice@example.com"):
    return await live.client.get(f"/api/v1/projects/{pid}/sources", headers=bearer(email))


async def test_save_persists_one_canonical_source_and_the_status_shows_it(live):
    live.runtime.drive = FakeDriveGateway(app_env="local", file_delay_s=0)
    pid = await make_project(live)
    tested = await post(live, pid, "/test", FOLDER)
    assert tested.json()["result"] == "ok"
    assert not await select_all(live, select(Connection))  # a test creates nothing
    saved = await post(live, pid, "", FOLDER + "?usp=sharing")
    assert saved.status_code == 201
    source = saved.json()["source"]
    [row] = await select_all(live, select(Connection))
    assert str(row.id) == source["id"] and row.link == FOLDER
    assert (row.state, row.kind, row.external_id) == (
        "listing",
        "gdrive",
        "fake-synthetic-30",
    )
    again = await post(live, pid, "/test", FOLDER)
    assert again.json() == {"result": "failed", "reason": "already_connected"}
    body = (await status(live, pid)).json()
    assert body["progress"] == {"listing_finished": False, "total": 0, "finished": 0}
    assert body["sources"][0]["id"] == source["id"] and body["uploads"] is None


async def test_concurrent_saves_create_exactly_one_source(live):
    live.runtime.drive = FakeDriveGateway(app_env="local", file_delay_s=0)
    pid = await make_project(live)
    responses = await asyncio.gather(*(post(live, pid, "", FOLDER) for _ in range(5)))
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409, 409]
    assert len(await select_all(live, select(Connection))) == 1


async def test_a_failed_import_start_deletes_the_new_source(live, monkeypatch):
    live.runtime.drive = FakeDriveGateway(app_env="local", file_delay_s=0)
    pid = await make_project(live)

    async def boom(runtime, project_id, connection_id):
        raise RuntimeError("temporal down")

    monkeypatch.setattr(workflows, "start_drive_import", boom)
    response = await post(live, pid, "", FOLDER)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "unknown_error"
    assert not await select_all(live, select(Connection))
    monkeypatch.undo()
    assert (await post(live, pid, "", FOLDER)).status_code == 201


async def test_a_non_member_gets_404_on_every_sources_endpoint(live):
    live.runtime.drive = FakeDriveGateway(app_env="local", file_delay_s=0)
    pid = await make_project(live)
    await post(live, pid, "", FOLDER)
    for path in ("/test", ""):
        response = await post(live, pid, path, FOLDER, email="mallory@example.com")
        assert response.status_code == 404
    assert (await status(live, pid, "mallory@example.com")).status_code == 404
    assert len(await select_all(live, select(Connection))) == 1


async def test_status_counts_drive_files_and_uploads_and_an_upload_starts_its_parse(
    live,
):
    live.runtime.drive = FakeDriveGateway(app_env="local", file_delay_s=0)
    pid = await make_project(live)
    source = (await post(live, pid, "", FOLDER)).json()["source"]
    drive = live.runtime.drive
    listing = drive.list_all("fake-synthetic-30")
    async with live.repo() as session, session.begin():
        await sources_repo.upsert_items(session, uuid.UUID(source["id"]), uuid.UUID(pid), listing.items[:4])
        await sources_repo.set_connection_state(session, uuid.UUID(source["id"]), "importing")
    item_ids = [i.id for i in await select_all(live, select(ConnectionItem))]
    async with live.repo() as session, session.begin():
        await sources_repo.finish_item(session, item_ids[0], "skipped")
        await sources_repo.finish_item(session, item_ids[1], "stored")
    # K5: the upload itself marks processing and starts the workflow.
    uploaded = await put(live, pid)
    assert uploaded.status_code == 201
    assert uploaded.json()["document"]["status"] == "processing"
    # the save started the import workflow (05); the upload started the parse
    assert [c["workflow"] for c in live.temporal.calls] == [
        "DriveImportWorkflow",
        "ParseDocumentWorkflow",
    ]
    body = (await status(live, pid)).json()
    [one] = body["sources"]
    assert one["state"] == "importing"
    assert one["progress"] == {"listing_finished": True, "total": 4, "finished": 2}
    assert body["uploads"] == {
        "progress": {"listing_finished": True, "total": 1, "finished": 0},
        "documents": 1,
        "failed": 0,
    }
    assert body["progress"] == {"listing_finished": True, "total": 5, "finished": 2}
    docs = (await live.client.get(f"/api/v1/projects/{pid}/documents", headers=bearer())).json()
    assert docs["items"][0]["status"] == "processing"
