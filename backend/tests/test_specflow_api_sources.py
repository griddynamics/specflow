"""Tier 1: the Drive sources API (INT-GDUI-0001..0004) on the fake gateway.

Requirements: INT-GDUI-0001..0004, NFR-REL-0001/0002, NFR-MAIN-0001.AC4,
FR-GDUI-0012. The repository is an in-memory stand-in; the SQL is covered by
``test_specflow_api_sources_live.py``.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
import uuid

import pytest

from api import workflows
from api.app import create_app
from api.routers import sources as router
from api.runtime import Runtime
from api.workflows import start_drive_import as real_start_drive_import
from db import removal
from db import sources as sources_repo
from drive.fake import FakeDriveGateway
from drive.gateway import DriveError
from drive.reasons import Reason
from health import Readiness
from logger_harness import events, fields
from settings import ConfigurationError, load_settings
from specflow_api_support import PDF, bearer
from specflow_api_wait_support import wait_until
from specflow_sources_support import (
    PROJECT_FOLDER,
    SYNTHETIC,
    new_project,
)

SOURCE_KEYS = {
    "id",
    "kind",
    "folder_id",
    "link",
    "name",
    "state",
    "failure_reason",
    "connected_by",
    "created_at",
    "last_synced_at",
    "progress",
}


def link(folder_id: str) -> str:
    return PROJECT_FOLDER + folder_id


def code(response) -> str:
    return response.json()["error"]["code"]


class SpyGateway(FakeDriveGateway):
    """Counts the calls that reach "Google" and records the calling thread."""

    def __init__(self, delay: float = 0.0, list_delay: float = 0.0) -> None:
        super().__init__(app_env="local", file_delay_s=0)
        self.calls: list[str] = []
        self.threads: list[str] = []
        self.delay, self.list_delay = delay, list_delay

    def identity(self) -> str:
        self.calls.append("identity")
        self.threads.append(threading.current_thread().name)
        return super().identity()

    def check_folder(self, folder_id):
        self.calls.append("check_folder")
        self.threads.append(threading.current_thread().name)
        threading.Event().wait(self.delay)  # a blocked Drive call: the thread stays busy
        return super().check_folder(folder_id)

    def list_all(self, folder_id, *, deadline=None):
        self.calls.append("list_all")
        threading.Event().wait(self.list_delay)
        return super().list_all(folder_id, deadline=deadline)


# ---- INT-GDUI-0001: identity ----------------------------------------------
async def test_identity_returns_the_drive_email_on_the_api_pool(sources):
    spy = sources.harness.runtime.drive = SpyGateway()
    response = await sources.client.get("/api/v1/drive/identity", headers=bearer())
    assert response.status_code == 200
    assert response.json() == {"email": "fake-drive@specflow.local"}
    assert spy.threads and spy.threads[0].startswith("drive-identity")


async def test_identity_needs_a_signed_in_user(sources):
    response = await sources.client.get("/api/v1/drive/identity")
    assert response.status_code == 401


async def test_identity_without_a_gateway_is_503_setup_error(sources):
    sources.harness.runtime.drive = None
    response = await sources.client.get("/api/v1/drive/identity", headers=bearer())
    assert response.status_code == 503 and code(response) == "setup_error"


@pytest.mark.parametrize("reason", [Reason.SETUP_ERROR, Reason.DRIVE_UNAVAILABLE])
async def test_an_unreadable_identity_is_503_with_the_reason_code(sources, reason):
    class Broken(SpyGateway):
        def identity(self):
            raise DriveError(reason, http_status=403, google_reason="Secret text")

    sources.harness.runtime.drive = Broken()
    response = await sources.client.get("/api/v1/drive/identity", headers=bearer())
    assert response.status_code == 503 and code(response) == reason.value
    assert "Secret" not in response.text


# ---- INT-GDUI-0002: test ---------------------------------------------------
async def test_a_test_counts_the_folder_and_creates_nothing(sources):
    await new_project(sources)
    response = await sources.test(SYNTHETIC)
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "ok"
    assert set(body["folder"]) == {"id", "name"} and body["folder"]["id"] == ("fake-synthetic-30")
    cats = body["categories"]
    assert body["file_count"] == sum(c["count"] for c in cats) > 0
    assert body["folder_count"] >= 2
    assert {c["category"] for c in cats} >= {"google_docs", "pdf"}
    assert all(set(c) == {"category", "processing_class", "count"} for c in cats)
    classes = [c["processing_class"] for c in cats]  # FR-GDUI-0010 order
    assert classes == sorted(classes, key=["processed", "stored_only", "skipped"].index)
    assert not sources.repo.rows and not sources.harness.temporal.calls
    assert not sources.harness.store.objects and not sources.started


@pytest.mark.parametrize(
    ("folder_id", "files", "category"),
    [("fake-empty-folder", 0, None), ("fake-forms-only", 2, "other_google")],
)
async def test_empty_and_forms_only_folders_are_ok(sources, folder_id, files, category):
    await new_project(sources)
    body = (await sources.test(link(folder_id))).json()
    assert body["result"] == "ok" and body["file_count"] == files
    assert [c["category"] for c in body["categories"]] == ([category] if category else [])


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not a link",
        "https://example.com/drive/folders/fake-synthetic-30",
        "https://drive.google.com/file/d/fake-synthetic-30/view",
        link("fake-synthetic-30") + "#frag",
        "short",
    ],
)
async def test_an_unusable_link_is_invalid_link_without_a_drive_call(sources, text):
    await new_project(sources)
    spy = sources.harness.runtime.drive = SpyGateway()
    response = await sources.test(text)
    assert response.status_code == 200
    assert response.json() == {"result": "failed", "reason": "invalid_link"}
    assert spy.calls == []


@pytest.mark.parametrize(
    "reason",
    ["no_access", "not_a_folder", "drive_unavailable", "setup_error", "unknown_error"],
)
async def test_each_drive_reason_comes_back_as_a_result(sources, reason):
    await new_project(sources)
    response = await sources.test(link(f"fake-reason-{reason}"))
    assert response.status_code == 200
    assert response.json() == {"result": "failed", "reason": reason}


async def test_an_unknown_folder_is_no_access_and_a_file_is_not_a_folder(sources):
    await new_project(sources)
    unknown = (await sources.test(link("some-unknown-id-123"))).json()
    assert unknown == {"result": "failed", "reason": "no_access"}
    item = (await sources.test(link("fake-synthetic-30-i1"))).json()
    assert item == {"result": "failed", "reason": "not_a_folder"}


async def test_a_connected_folder_is_already_connected_without_a_drive_call(sources):
    await new_project(sources)
    assert (await sources.save(SYNTHETIC)).status_code == 201
    spy = sources.harness.runtime.drive = SpyGateway()
    response = await sources.test(SYNTHETIC + "?usp=sharing")
    assert response.json() == {"result": "failed", "reason": "already_connected"}
    assert spy.calls == []


async def test_the_test_is_scoped_to_the_project_members(sources):
    await new_project(sources)
    response = await sources.test(SYNTHETIC, email="mallory@example.com")
    assert response.status_code == 404 and code(response) == "project_not_found"
    sources.pid = str(uuid.uuid4())
    assert (await sources.test(SYNTHETIC)).status_code == 404


@pytest.mark.parametrize("body", [{}, {"link": 5}, {"link": "x" * 2049}, {"x": 1}])
async def test_a_malformed_body_is_422_validation_failed(sources, body):
    await new_project(sources)
    response = await sources.post("/test", json=body)
    assert response.status_code == 422 and code(response) == "validation_failed"
    assert "x" * 50 not in response.text


# ---- INT-GDUI-0003: save ---------------------------------------------------
async def test_a_save_creates_one_source_in_listing_and_starts_the_import(sources):
    await new_project(sources)
    response = await sources.save(SYNTHETIC + "?usp=sharing")
    assert response.status_code == 201
    source = response.json()["source"]
    assert set(source) == SOURCE_KEYS
    assert source["kind"] == "gdrive" and source["folder_id"] == "fake-synthetic-30"
    assert source["link"] == SYNTHETIC  # canonical, not the pasted text
    assert source["state"] == "listing" and source["failure_reason"] is None
    assert source["progress"] == {
        "listing_finished": False,
        "total": 0,
        "finished": 0,
    }
    assert source["last_synced_at"] is None
    me = (await sources.client.get("/api/v1/me", headers=bearer())).json()
    assert source["connected_by"] == me["id"]
    assert [(str(p), str(c)) for p, c in sources.started] == [(sources.pid, source["id"])]
    assert not sources.harness.temporal.calls  # the hook is the only starter


async def test_the_default_hook_starts_the_import_workflow_and_state_stays_listing(sources, monkeypatch):
    monkeypatch.setattr(workflows, "start_drive_import", real_start_drive_import)
    await new_project(sources)
    response = await sources.save(SYNTHETIC)
    assert response.status_code == 201
    source = response.json()["source"]
    assert source["state"] == "listing"  # the worker moves it on
    [call] = sources.harness.temporal.calls
    assert call["workflow"] == "DriveImportWorkflow"
    assert call["id"] == f"drive-import-{source['id']}"


async def test_saving_the_same_folder_twice_is_409_already_connected(sources):
    await new_project(sources)
    await sources.save(SYNTHETIC)
    again = await sources.save(link("fake-synthetic-30") + "/")
    assert again.status_code == 409 and code(again) == "already_connected"
    assert len(sources.repo.rows) == 1 and len(sources.started) == 1


async def test_the_router_keeps_no_memory_of_a_removed_folder(sources, monkeypatch):
    # Router-wiring guard for FR-SRC-0016.AC4 (not the AC4 proof: the fake row is dropped
    # by this test itself). The proof is test_db_sources_readd_live.py on real Postgres.
    # Scenario: after DELETE the router accepts the same Drive folder as a NEW source.
    # Scenario: save X -> 201; a second save -> 409 (control); DELETE the source -> 204;
    # save X again -> 201 with a different source id.

    await new_project(sources)
    first = await sources.save(SYNTHETIC)
    assert first.status_code == 201
    old_id = first.json()["source"]["id"]
    assert (await sources.save(SYNTHETIC)).status_code == 409

    # The removal fake (FakeBatches) knows nothing of the in-memory connection rows, so
    # mirror the source into it and drop the connection row when it is removed.
    batches, rows = sources.harness.batches, sources.repo.rows
    mirror = batches.add_source(uuid.UUID(sources.pid), kind="gdrive", external_id="fake-synthetic-30")
    batches.sources[uuid.UUID(old_id)] = batches.sources.pop(mirror.id)
    mirror.id = uuid.UUID(old_id)
    remove_source = batches.remove_source

    async def removing(session, project_id, source_id):
        result = await remove_source(session, project_id, source_id)
        rows.pop(source_id, None)
        return result

    monkeypatch.setattr(removal, "remove_source", removing)

    deleted = await sources.client.delete(f"/api/v1/projects/{sources.pid}/sources/{old_id}", headers=bearer())
    assert deleted.status_code == 204 and uuid.UUID(old_id) not in rows

    again = await sources.save(link("fake-synthetic-30") + "/")
    assert again.status_code == 201
    new = again.json()["source"]
    assert new["id"] != old_id and new["folder_id"] == "fake-synthetic-30"
    assert new["state"] == "listing"
    assert len(rows) == 1 and len(sources.started) == 2


async def test_concurrent_saves_of_one_folder_create_one_source(sources):
    await new_project(sources)
    sources.harness.runtime.drive = SpyGateway(delay=0.05)  # all pass the pre-check
    responses = await asyncio.gather(*(sources.save(SYNTHETIC) for _ in range(4)))
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]
    assert {code(r) for r in responses if r.status_code == 409} == {"already_connected"}
    assert len(sources.repo.rows) == 1 and len(sources.started) == 1


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("no_access", 409),
        ("not_a_folder", 422),
        ("drive_unavailable", 503),
        ("setup_error", 503),
        ("too_slow", 504),
        ("unknown_error", 502),
    ],
)
async def test_a_failed_recheck_uses_the_reason_as_code(sources, reason, status):
    await new_project(sources)
    response = await sources.save(link(f"fake-reason-{reason}"))
    assert response.status_code == status and code(response) == reason
    assert not sources.repo.rows and not sources.started


async def test_an_invalid_link_cannot_be_saved(sources):
    await new_project(sources)
    response = await sources.save("nope")
    assert response.status_code == 422 and code(response) == "invalid_link"
    assert not sources.repo.rows


async def test_a_failed_import_start_removes_the_source_and_says_unknown_error(sources, monkeypatch, caplog):
    await new_project(sources)

    async def boom(runtime, project_id, connection_id):
        raise RuntimeError("temporal down secret-text")

    monkeypatch.setattr(workflows, "start_drive_import", boom)
    with caplog.at_level("DEBUG"):
        response = await sources.save(SYNTHETIC)
    assert response.status_code == 502 and code(response) == "unknown_error"
    assert not sources.repo.rows  # not stuck in "listing"
    [failed] = events(caplog, "api.drive.import_start_failed")
    assert fields(failed)["error_type"] == "RuntimeError"
    assert "secret-text" not in caplog.text and "secret-text" not in response.text
    [saved] = events(caplog, "api.drive.saved")
    assert fields(saved)["result"] == "failed"
    assert fields(saved)["reason"] == "unknown_error"
    assert fields(saved)["connection_id"] is None
    monkeypatch.setattr(workflows, "start_drive_import", real_start_drive_import)
    assert (await sources.save(SYNTHETIC)).status_code == 201  # can be saved again


async def test_a_save_is_scoped_to_the_project_members(sources):
    await new_project(sources)
    response = await sources.save(SYNTHETIC, email="mallory@example.com")
    assert response.status_code == 404 and code(response) == "project_not_found"
    assert not sources.repo.rows


async def test_a_member_of_another_project_cannot_see_or_save_here(sources):
    mine = await new_project(sources)
    await sources.save(SYNTHETIC)
    await new_project(sources, "bob@example.com")  # bob's own project
    sources.pid = mine
    assert (await sources.status("bob@example.com")).status_code == 404
    assert (await sources.save(SYNTHETIC, email="bob@example.com")).status_code == 404


# ---- time limits and the Drive pool (NFR-REL-0001/0002) --------------------
def test_the_server_limits_sit_inside_the_ui_limits():
    assert router.TEST_LIMIT_SECONDS == 55 and router.SAVE_LIMIT_SECONDS == 25
    assert router.TEST_LIMIT_SECONDS < 60 and router.SAVE_LIMIT_SECONDS < 30


async def test_a_slow_test_is_too_slow(sources, monkeypatch):
    await new_project(sources)
    monkeypatch.setattr(router, "TEST_LIMIT_SECONDS", 0.2)
    sources.harness.runtime.drive = SpyGateway(delay=2.0)
    started = time.monotonic()
    response = await sources.test(SYNTHETIC)
    assert response.json() == {"result": "failed", "reason": "too_slow"}
    assert time.monotonic() - started < 1.2  # answered at the limit, not after the 2 s


async def test_a_timed_out_call_keeps_its_slot_until_its_thread_ends(sources, monkeypatch):
    await new_project(sources)
    monkeypatch.setattr(router, "SAVE_LIMIT_SECONDS", 0.2)
    sources.harness.runtime.drive = SpyGateway(delay=0.8)
    sources.harness.runtime.drive_slots = slots = asyncio.Semaphore(1)
    response = await sources.save(SYNTHETIC)
    assert response.status_code == 504
    assert slots.locked()  # the thread is still running: its slot is still taken
    await wait_until(lambda: not slots.locked())
    assert not slots.locked()  # released by the thread, not by the timeout


async def test_identity_is_not_blocked_by_stuck_folder_calls(sources, monkeypatch):
    await new_project(sources)
    monkeypatch.setattr(router, "TEST_LIMIT_SECONDS", 0.2)
    sources.harness.runtime.drive = SpyGateway(delay=0.8)
    sources.harness.runtime.drive_slots = asyncio.Semaphore(1)
    stuck = asyncio.create_task(sources.test(SYNTHETIC))
    await wait_until(lambda: "check_folder" in sources.harness.runtime.drive.calls)
    response = await sources.client.get("/api/v1/drive/identity", headers=bearer())
    assert response.status_code == 200
    assert (await stuck).json()["reason"] == "too_slow"
    assert any(t.startswith("drive-identity") for t in sources.harness.runtime.drive.threads)


async def test_a_slow_save_is_504_too_slow_and_creates_nothing(sources, monkeypatch):
    await new_project(sources)
    monkeypatch.setattr(router, "SAVE_LIMIT_SECONDS", 0.2)
    sources.harness.runtime.drive = SpyGateway(delay=0.5)
    response = await sources.save(SYNTHETIC)
    assert response.status_code == 504 and code(response) == "too_slow"
    assert not sources.repo.rows and not sources.started


async def test_waiting_for_a_drive_slot_counts_against_the_limit(sources, monkeypatch):
    await new_project(sources)
    monkeypatch.setattr(router, "SAVE_LIMIT_SECONDS", 0.7)
    sources.harness.runtime.drive = SpyGateway(delay=0.5)
    sources.harness.runtime.drive_slots = asyncio.Semaphore(1)
    first, second = await asyncio.gather(
        sources.save(link("fake-synthetic-30")), sources.save(link("fake-synthetic-23"))
    )
    assert first.status_code == 201  # got the slot at once
    assert second.status_code == 504 and code(second) == "too_slow"  # waited 0.5 s
    assert len(sources.repo.rows) == 1


async def test_drive_calls_never_run_more_than_four_at_once(sources):
    await new_project(sources)
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    class Counting(SpyGateway):
        def check_folder(self, folder_id):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            try:
                return super().check_folder(folder_id)
            finally:
                with lock:
                    state["now"] -= 1

    sources.harness.runtime.drive = Counting(delay=0.05)
    await asyncio.gather(*(sources.test(link("fake-empty-folder")) for _ in range(12)))
    assert state["max"] == 4  # DRIVE_TEST_CONCURRENCY


# ---- INT-GDUI-0004: status -------------------------------------------------
async def test_an_empty_project_has_a_finished_listing_and_no_uploads(sources):
    await new_project(sources)
    response = await sources.status()
    assert response.status_code == 200
    assert response.json() == {
        "progress": {"listing_finished": True, "total": 0, "finished": 0},
        "sources": [],
        "uploads": None,
        "processed_documents": 0,
    }


async def test_the_status_counts_processed_documents_for_ready(sources):
    await new_project(sources)
    sources.repo.processed = 3
    assert (await sources.status()).json()["processed_documents"] == 3


async def test_the_status_maps_each_source_and_the_uploads(sources):
    await new_project(sources)
    first = (await sources.save(SYNTHETIC)).json()["source"]
    second = (await sources.save(link("fake-synthetic-23"))).json()["source"]
    repo = sources.repo
    progress = functools.partial(sources_repo.Progress, listing_finished=True)
    repo.progress[uuid.UUID(first["id"])] = sources_repo.ConnectionProgress(
        uuid.UUID(first["id"]), "importing", None, progress(total=30, finished=12)
    )
    repo.progress[uuid.UUID(second["id"])] = sources_repo.ConnectionProgress(
        uuid.UUID(second["id"]), "failed", "no_access", progress(total=23, finished=23)
    )
    repo.uploads = sources_repo.UploadsProgress(progress(total=4, finished=3), documents=4, failed=1)
    body = (await sources.status()).json()
    assert body["progress"] == {"listing_finished": True, "total": 57, "finished": 38}
    assert [s["id"] for s in body["sources"]] == [first["id"], second["id"]]
    one, two = body["sources"]
    assert (one["state"], one["failure_reason"]) == ("importing", None)
    assert one["progress"] == {"listing_finished": True, "total": 30, "finished": 12}
    assert (two["state"], two["failure_reason"]) == ("failed", "no_access")
    assert set(one) == SOURCE_KEYS
    assert body["uploads"] == {
        "progress": {"listing_finished": True, "total": 4, "finished": 3},
        "documents": 4,
        "failed": 1,
    }


async def test_a_listing_source_keeps_the_project_unfinished(sources):
    await new_project(sources)
    await sources.save(SYNTHETIC)
    body = (await sources.status()).json()
    assert body["progress"]["listing_finished"] is False
    assert body["sources"][0]["state"] == "listing"


async def test_the_status_is_404_for_non_members(sources):
    await new_project(sources)
    response = await sources.status("mallory@example.com")
    assert response.status_code == 404 and code(response) == "project_not_found"


# ---- NFR-MAIN-0001: logs ----------------------------------------------------
async def test_one_event_per_test_and_save_and_no_names_or_links(sources, caplog):
    await new_project(sources)
    pasted = SYNTHETIC + "?usp=sharing"
    with caplog.at_level("DEBUG"):
        ok = (await sources.test(pasted)).json()
        await sources.test("garbage link text")
        saved = (await sources.save(pasted)).json()["source"]
        await sources.save(pasted)
    tested = events(caplog, "api.drive.tested")
    assert len(tested) == 2
    first, second = (fields(r) for r in tested)
    assert first["result"] == "ok" and first["reason"] is None
    assert first["folder_id"] == "fake-synthetic-30" and first["project_id"] == (sources.pid)
    assert first["file_count"] == ok["file_count"] and first["duration_ms"] >= 0
    assert first["counts"] == {c["category"]: c["count"] for c in ok["categories"]}
    assert first["folder_count"] == ok["folder_count"] and first["user_id"]
    assert second["result"] == "failed" and second["reason"] == "invalid_link"
    assert second["folder_id"] is None
    saves = [fields(r) for r in events(caplog, "api.drive.saved")]
    assert len(saves) == 2
    assert saves[0]["result"] == "ok" and saves[0]["connection_id"] == saved["id"]
    assert saves[0]["folder_id"] == "fake-synthetic-30" and saves[0]["reason"] is None
    assert saves[1]["result"] == "failed" and saves[1]["reason"] == "already_connected"
    text = caplog.text
    for secret in (
        ok["folder"]["name"],
        "usp=sharing",
        "garbage link text",
        "fake-drive@specflow.local",
        bearer()["Authorization"].split()[1],
    ):
        assert secret not in text


async def test_no_google_text_reaches_the_response_or_the_log(sources, caplog):
    await new_project(sources)

    class Leaky(SpyGateway):
        def check_folder(self, _folder_id):
            raise DriveError(Reason.NO_ACCESS, http_status=404, google_reason="notFound")

        def list_all(self, _folder_id, **_options):
            raise RuntimeError("secret file name.pdf for bob@corp.com")

    sources.harness.runtime.drive = Leaky()
    with caplog.at_level("DEBUG"):
        a = await sources.test(SYNTHETIC)
        sources.harness.runtime.drive = type("L2", (SpyGateway,), {"list_all": Leaky.list_all})()
        b = await sources.test(link("fake-empty-folder"))
    assert a.json()["reason"] == "no_access"
    assert b.json() == {"result": "failed", "reason": "unknown_error"}
    for leaked in ("secret file", "bob@corp.com"):
        assert leaked not in a.text + b.text + caplog.text


async def test_the_link_is_never_fetched_as_a_url(sources):
    """No URL fetch: a link to a foreign host is rejected before any I/O."""

    await new_project(sources)
    spy = sources.harness.runtime.drive = SpyGateway()
    for text in (
        "http://169.254.169.254/drive/folders/abcdefghij",
        "file:///etc/passwd",
    ):
        assert (await sources.test(text)).json()["reason"] == "invalid_link"
    assert spy.calls == []


# ---- the API lifespan: Drive mode and email --------------------------------
def runtime_factory(drive=None):

    class Store:
        async def check(self):
            return None

    async def ok():
        return None

    async def factory(settings):
        return Runtime(
            settings=settings,
            session_factory=None,
            store=Store(),
            temporal=None,
            driver=None,
            readiness=Readiness({"x": ok}, ttl=0),
            drive=drive,
        )

    return factory


async def test_startup_logs_the_drive_mode_and_email(valid_env, caplog):

    app = create_app(load_settings("api", valid_env), runtime_factory=runtime_factory())
    with caplog.at_level("DEBUG"):
        async with app.router.lifespan_context(app):
            assert app.state.runtime.drive.mode == "fake"  # APP_ENV=local
    [record] = events(caplog, "drive.gateway.selected")
    data = fields(record)
    assert data["mode"] == "fake" and data["role"] == "api"
    assert data["email"] == "fake-drive@specflow.local"


async def test_a_fake_gateway_inside_kubernetes_refuses_the_start(monkeypatch, valid_env):

    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    app = create_app(load_settings("api", valid_env), runtime_factory=runtime_factory())
    with pytest.raises(ConfigurationError):
        async with app.router.lifespan_context(app):
            pass


async def test_an_unreadable_email_is_logged_and_does_not_stop_the_start(valid_env, caplog):

    class NoEmail(SpyGateway):
        mode = "real"

        def identity(self):
            raise DriveError(Reason.SETUP_ERROR)

    app = create_app(load_settings("api", valid_env), runtime_factory=runtime_factory(NoEmail()))
    with caplog.at_level("DEBUG"):
        async with app.router.lifespan_context(app):
            pass
    [record] = events(caplog, "drive.gateway.selected")
    data = fields(record)
    assert data["mode"] == "real" and data["email"] is None
    assert data["reason"] == "setup_error"


def test_pdf_constant_is_importable():  # keeps the shared import used
    assert PDF.startswith(b"%PDF-")


# ---- review fixes ------------------------------------------------------------
async def _failing_start(monkeypatch):
    async def boom(runtime, project_id, connection_id):
        raise RuntimeError("temporal down")

    monkeypatch.setattr(workflows, "start_drive_import", boom)


async def test_a_failed_delete_marks_the_source_failed(sources, monkeypatch, caplog):
    await new_project(sources)
    await _failing_start(monkeypatch)

    async def no_delete(session, project_id, connection_id):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(sources_repo, "delete_connection_if_empty", no_delete)
    with caplog.at_level("DEBUG"):
        response = await sources.save(SYNTHETIC)
    assert response.status_code == 502 and code(response) == "unknown_error"
    [row] = sources.repo.rows.values()  # never left in "listing" without a worker
    assert (row.state, row.failure_reason) == ("failed", "unknown_error")
    assert not events(caplog, "api.drive.compensation_failed")


async def test_when_nothing_can_be_cleaned_up_only_ids_are_logged(sources, monkeypatch, caplog):
    await new_project(sources)
    await _failing_start(monkeypatch)

    async def broken(*args, **kwargs):
        raise RuntimeError("secret db text")

    monkeypatch.setattr(sources_repo, "delete_connection_if_empty", broken)
    monkeypatch.setattr(sources_repo, "set_connection_state", broken)
    with caplog.at_level("DEBUG"):
        response = await sources.save(SYNTHETIC)
    assert response.status_code == 502
    [record] = events(caplog, "api.drive.compensation_failed")
    data = fields(record)
    assert data["project_id"] == sources.pid and data["connection_id"]
    assert data["error_type"] == "RuntimeError"
    assert "secret db text" not in caplog.text


async def test_an_unexpected_save_failure_is_logged_as_error_and_502(sources, monkeypatch, caplog):
    await new_project(sources)

    async def boom(*args, **kwargs):
        raise RuntimeError("secret db text")

    monkeypatch.setattr(sources_repo, "create_connection", boom)
    with caplog.at_level("DEBUG"):
        response = await sources.save(SYNTHETIC)
    assert response.status_code == 502 and code(response) == "unknown_error"
    [record] = events(caplog, "api.drive.saved")
    data = fields(record)
    assert (data["result"], data["reason"]) == ("error", "unknown_error")
    assert data["error_type"] == "RuntimeError" and data["connection_id"] is None
    assert "secret db text" not in caplog.text + response.text


async def test_an_unreadable_identity_is_logged_with_the_reason(sources, caplog):
    class Broken(SpyGateway):
        def identity(self):
            raise DriveError(Reason.SETUP_ERROR)

    sources.harness.runtime.drive = Broken()
    with caplog.at_level("DEBUG"):
        await sources.client.get("/api/v1/drive/identity", headers=bearer())
    [record] = events(caplog, "api.drive.identity")
    assert fields(record)["reason"] == "setup_error"


async def test_a_refused_start_closes_the_runtime(monkeypatch, valid_env):

    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    closed: list[bool] = []
    inner = runtime_factory()

    async def factory(settings):
        runtime = await inner(settings)

        async def close():
            closed.append(True)

        runtime.cleanups.append(close)
        return runtime

    app = create_app(load_settings("api", valid_env), runtime_factory=factory)
    with pytest.raises(ConfigurationError):
        async with app.router.lifespan_context(app):
            pass
    assert closed == [True]
