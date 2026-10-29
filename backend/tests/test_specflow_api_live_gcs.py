"""Tier 2 (``SPECFLOW_TEST_GCS=1``): a real uvicorn server, a real disconnecting client, real GCS.

AC-7.7 (abort leaves no object and no row) and the happy path through the
streaming handler. Every object lives under ``dev/local/pytest/<run-id>/``
(constructor override, AC-4.7); teardown deletes the prefix and the counts
before and after are reported (expected 0 -> 0). Cost: one 5 MiB aborted
session and one tiny object.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid

import httpx
import pytest_asyncio
import uvicorn
from google.cloud import storage

from api.app import create_app
from api.runtime import Runtime
from health import Readiness
from settings import StorageSettings, load_settings
from specflow_api_support import PDF, FakeDriver, FakeRepo, FakeTemporal, bearer
from specflow_api_wait_support import wait_until
from specflow_live import requires_live
from storage.gcs import GcsObjectStore

pytestmark = requires_live("GCS")
MiB = 1024 * 1024


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest_asyncio.fixture
async def server(request, valid_env, monkeypatch):
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    prefix = f"dev/local/pytest/{uuid.uuid4().hex[:12]}/"
    config = StorageSettings(
        STORAGE_BUCKET=os.environ["STORAGE_BUCKET"],
        STORAGE_PREFIX=prefix,
        GOOGLE_CLOUD_PROJECT=os.environ["GOOGLE_CLOUD_PROJECT"],
    )
    client = await asyncio.to_thread(storage.Client, project=config.google_cloud_project)

    def count() -> int:
        return sum(1 for _ in client.list_blobs(config.bucket, prefix=prefix))

    def purge() -> None:
        for blob in list(client.list_blobs(config.bucket, prefix=prefix)):
            blob.delete()

    before = count()
    fake = FakeRepo()
    fake.install(monkeypatch)
    store = GcsObjectStore(config, client=client)

    async def build(settings):
        async def ok() -> None:
            return None

        return Runtime(
            settings=settings,
            session_factory=fake,
            store=store,
            temporal=FakeTemporal(),
            driver=FakeDriver(),
            readiness=Readiness({"storage": ok}, ttl=0),
        )

    app = create_app(load_settings("api", valid_env), runtime_factory=build)
    port = free_port()
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    task = asyncio.create_task(uv.serve())
    await wait_until(lambda: uv.started)
    try:
        yield port, fake, count, uv
    finally:
        uv.should_exit = True
        await task
        await asyncio.to_thread(purge)
        after = count()
        if reporter:
            reporter.write_line(f"\nAPI live GCS objects before: {before}, after: {after} under {prefix}")
        assert before == 0 and after == 0


async def test_an_upload_through_the_server_lands_in_gcs_and_a_real_disconnect_leaves_nothing(
    server,
):
    port, fake, count, uv = server
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base) as http:
        pid = (await http.post("/api/v1/projects", json={"name": "g"}, headers=bearer())).json()["id"]
        ok = await http.post(
            f"/api/v1/projects/{pid}/documents?filename=live.pdf",
            content=PDF,
            headers={**bearer(), "Content-Type": "application/pdf"},
        )
        assert ok.status_code == 201 and ok.json()["document"]["size_bytes"] == len(PDF)
        assert count() == 1 and len(fake.documents) == 1

        # A client that announces 50 MiB, sends 5.5 MiB (one full resumable chunk) and vanishes.
        token = bearer()["Authorization"]
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            (
                f"POST /api/v1/projects/{pid}/documents?filename=cut.pdf HTTP/1.1\r\n"
                f"Host: 127.0.0.1\r\nAuthorization: {token}\r\n"
                f"Content-Type: application/pdf\r\nContent-Length: {50 * MiB}\r\n\r\n"
            ).encode()
        )
        writer.write(PDF)
        writer.write(b"y" * (int(5.5 * MiB)))
        await writer.drain()
        await wait_until(lambda: bool(uv.server_state.tasks))  # the server is handling the upload
        writer.transport.abort()  # RST: a real disconnect
        await wait_until(lambda: not uv.server_state.tasks, 15.0)  # the cut request has ended
        assert len(fake.documents) == 1  # no row for the cut upload
        await wait_until(lambda: count() == 1, 15.0)  # only the first, complete object
        # the server is still healthy and serves the next upload
        again = await http.post(
            f"/api/v1/projects/{pid}/documents?filename=n.pdf",
            content=PDF + b"2",
            headers={**bearer(), "Content-Type": "application/pdf"},
        )
        assert again.status_code == 201 and count() == 2
