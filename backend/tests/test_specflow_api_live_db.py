"""Tier 2 (``SPECFLOW_TEST_POSTGRES=1``): the API on a real, migrated scratch database.

AC-7.2, 7.3 and 7.4 against the real repositories (``/me``, project create) and the
batch upload route on the real rows. Object storage is the in-memory store and
Temporal is a fake; with ``SPECFLOW_TEST_TEMPORAL=1`` one more test starts the real
parse workflow by name.
"""

from __future__ import annotations

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
    """One file through a fresh Local files batch — the upload route the canvas uses (K5 starts the parse)."""

    started = await live.client.post(
        f"/api/v1/projects/{pid}/sources/uploads",
        json={"expected_files": 1},
        headers=bearer(email),
    )
    assert started.status_code == 201
    sid = started.json()["source"]["id"]
    return await live.client.post(
        f"/api/v1/projects/{pid}/sources/{sid}/files?filename=a.pdf",
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


async def test_a_non_member_gets_404_on_the_project_routes(live):
    pid = await make_project(live)
    eve = bearer("eve@example.com")
    for path in (f"/api/v1/projects/{pid}", f"/api/v1/projects/{pid}/gaps", f"/api/v1/projects/{pid}/plan"):
        assert (await live.client.get(path, headers=eve)).status_code == 404


@requires_live("TEMPORAL")
async def test_the_real_temporal_accepts_the_parse_start_by_name(migrated_database, valid_env, temporal_live):
    """No workflow code needs to exist: the upload's start only records the workflow on its queue (K5)."""

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
            started = await http.post(
                f"/api/v1/projects/{pid}/sources/uploads",
                json={"expected_files": 1},
                headers=bearer(),
            )
            assert started.status_code == 201
            sid = started.json()["source"]["id"]
            added = await http.post(
                f"/api/v1/projects/{pid}/sources/{sid}/files?filename=a.pdf",
                content=PDF,
                headers={**bearer(), **PDF_HEADERS},
            )
            assert added.status_code == 201
            async with factory() as session:
                [row] = list((await session.scalars(select(Document))).all())
            handle = client.get_workflow_handle(f"{temporal_live.id_prefix}doc-parse-{row.id}")
            temporal_live.track(handle)
            assert (await handle.describe()).status.name == "RUNNING"
    await engine.dispose()
