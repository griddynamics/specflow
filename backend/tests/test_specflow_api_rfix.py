"""Tier 1: R-code fixes in the API, storage and provider (audit field, close paths)."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import db.session as session_module
import graph.core as graph_module
import storage.gcs as gcs_module
import temporal.client as temporal_module
from api import runtime as runtime_module
from logger_harness import events, fields
from settings import StorageSettings, load_settings
from specflow_api_support import PDF, bearer
from storage.gcs import GcsObjectStore

PDF_HEADERS = {"Content-Type": "application/pdf"}
MUTATIONS = (
    "api.project.created",
    "api.document.uploaded",
    "api.document.parse_started",
    "api.summary.started",
)


async def test_the_four_mutation_events_log_the_acting_user_and_no_email_or_token(
    harness,
    caplog,
):
    """R-code M3 (SPECS section 19, Repudiation)."""

    headers = bearer("carol.secret@example.com")
    with caplog.at_level("DEBUG"):
        created = await harness.client.post("/api/v1/projects", json={"name": "p"}, headers=headers)
        pid = created.json()["id"]
        up = await harness.client.post(
            f"/api/v1/projects/{pid}/documents?filename=a.pdf",
            content=PDF,
            headers={**headers, **PDF_HEADERS},
        )
        did = up.json()["document"]["id"]
        base = f"/api/v1/projects/{pid}/documents/{did}"
        assert (await harness.client.post(f"{base}/parse", headers=headers, json={})).status_code == 202
        harness.repo.documents[uuid.UUID(did)].status = "parsed"
        assert (await harness.client.post(f"{base}/summarize", headers=headers, json={})).status_code == 202

    [user] = harness.repo.users.values()
    for event in MUTATIONS:
        # K5: the upload starts a parse, then the explicit click starts it again.
        records = events(caplog, event)
        assert len(records) == (2 if event == "api.document.parse_started" else 1)
        for record in records:
            logged = fields(record)
            assert logged["user_id"] == str(user.id), event
            assert logged["project_id"] == pid, event
    for event in MUTATIONS[1:]:
        assert fields(events(caplog, event)[0])["document_id"] == did
    assert "carol.secret" not in caplog.text
    assert headers["Authorization"].split()[1] not in caplog.text


async def test_the_store_is_closed_when_the_api_runtime_shuts_down(monkeypatch, valid_env):
    """R-code LOW 8: ``build_runtime`` pushes ``store.close`` on its exit stack."""

    order: list[str] = []

    class Engine:
        async def dispose(self):
            order.append("engine")

    class Driver:
        async def close(self):
            order.append("driver")

    class Store:
        def __init__(self, settings, *, app_env):
            pass

        async def close(self):
            order.append("store")

        async def check(self):
            return None

    async def open_connection(settings):
        return SimpleNamespace(
            client=SimpleNamespace(), tracing=SimpleNamespace(shutdown=lambda: order.append("tracing"))
        )

    monkeypatch.setattr(session_module, "build_engine", lambda _settings: Engine())
    monkeypatch.setattr(graph_module, "connect", lambda _config: Driver())
    monkeypatch.setattr("storage.factory.build_object_store", Store)
    monkeypatch.setattr(temporal_module, "open_connection", open_connection)

    runtime = await runtime_module.build_runtime(load_settings("api", valid_env))
    assert order == []
    await runtime.aclose()
    await runtime.aclose()  # a second close changes nothing

    assert sorted(order) == ["driver", "engine", "store", "tracing"]
    assert order.index("store") < order.index("engine")


class FakeBucketClient:
    """Stands in for ``google.cloud.storage.Client``; counts how often it was closed."""

    closed = 0

    def __init__(self, **_options):
        pass

    def bucket(self, _name):
        return SimpleNamespace()

    def close(self):
        FakeBucketClient.closed += 1


async def test_the_gcs_store_closes_its_own_client_once_and_leaves_a_given_one_alone(monkeypatch):
    monkeypatch.setattr(FakeBucketClient, "closed", 0)
    settings = StorageSettings(
        STORAGE_BUCKET="b", STORAGE_PREFIX="dev/local/pytest/x/", GOOGLE_CLOUD_PROJECT="synthetic"
    )
    given = GcsObjectStore(settings, client=FakeBucketClient())
    await given.close()
    assert FakeBucketClient.closed == 0  # not ours to close

    monkeypatch.setattr(gcs_module.storage, "Client", FakeBucketClient)
    store = GcsObjectStore(settings)
    await asyncio.gather(store.close(), store.close())
    await store.close()

    assert FakeBucketClient.closed == 1
