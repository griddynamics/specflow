"""Tier 1: the Drive import without Temporal or Postgres.

The workflow's own behavior is covered by ``test_drive_import_live.py`` (no test
server may be downloaded). Here: module hygiene, constants, key and id builders,
the start hook, and the activities' branches over fake repositories and the
in-memory store. Requirements: INT-GDUI-0004, FR-GDUI-0017/0018, NFR-MAIN-0001.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio import activity, workflow
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from api import workflows as api_workflows
from db import repositories
from db import sources as sources_repo
from drive.fake import TINY_PDF, FakeDriveGateway
from drive.gateway import DriveError
from drive.reasons import Reason
from logger_harness import events, fields
from settings import load_settings
from storage import ObjectStoreError
from storage.keys import (
    document_original,
    document_original_bin,
    document_original_json,
)
from storage.memory import InMemoryObjectStore
from temporal import ids
from temporal.documents import registration
from temporal.sources import activities as acts
from temporal.sources import workflows as wf
from temporal.sources.models import (
    FinishItemInput,
    ImportItemInput,
    WaitDocumentInput,
)

PID, CID, OTHER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


# ----------------------------------------------------------------- hygiene, ids
def test_the_workflow_module_imports_no_infrastructure_packages():
    code = (
        "import sys\n"
        "import temporal.sources.workflows\n"
        "bad = [m for m in ('sqlalchemy', 'asyncpg', 'google.cloud', 'googleapiclient', 'pymupdf',"
        " 'settings', 'db', 'storage', 'drive')"
        " if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_the_workflow_is_named_and_carries_the_decided_limits():
    definition = workflow._Definition.must_from_class(wf.DriveImportWorkflow)
    assert definition.name == "DriveImportWorkflow"
    assert timedelta(hours=11) == wf.SOFT_DEADLINE
    assert timedelta(hours=12) == sources_repo.DRIVE_IMPORT_EXECUTION_TIMEOUT
    assert wf.SOFT_DEADLINE < sources_repo.DRIVE_IMPORT_EXECUTION_TIMEOUT
    assert wf.PARSE_CONCURRENCY == 2  # HANDOFF "parse head-of-line"
    assert wf.ITEM_RETRY.maximum_attempts == wf.ITEM_MAX_ATTEMPTS == 3


def test_ids_and_keys_are_deterministic():
    assert ids.drive_import_workflow_id(CID) == f"drive-import-{CID}"
    assert ids.drive_import_workflow_id(CID, prefix="t-") == f"t-drive-import-{CID}"
    one = ids.drive_document_id(CID, "fileid12345")
    assert one == ids.drive_document_id(CID, "fileid12345")
    assert one != ids.drive_document_id(CID, "fileid12346")
    assert one != ids.drive_document_id(uuid.uuid4(), "fileid12345")
    assert document_original("p", "d") == "projects/p/documents/d/original.pdf"
    assert document_original_json("p", "d") == "projects/p/documents/d/original.json"
    assert document_original_bin("p", "d") == "projects/p/documents/d/original.bin"
    with pytest.raises(ValueError, match="single non-empty path segment"):
        document_original_bin("p", "../d")


# ------------------------------------------------------------------- start hook
class Temporal:
    def __init__(self, *, start_error=None, exists=False, status="RUNNING"):
        self.start_error, self.exists, self.calls = start_error, exists, []
        self.status = status

    async def start_workflow(self, workflow_name, arg, **options):
        self.calls.append({"workflow": workflow_name, "arg": arg, **options})
        if self.start_error:
            raise self.start_error
        return SimpleNamespace(id=options["id"])

    def get_workflow_handle(self, _workflow_id):
        async def describe():
            if not self.exists:
                raise RuntimeError("not found")
            return SimpleNamespace(status=SimpleNamespace(name=self.status))

        return SimpleNamespace(describe=describe)


def runtime(temporal, valid_env, prefix=""):
    return SimpleNamespace(settings=load_settings("api", valid_env), temporal=temporal, id_prefix=prefix)


async def test_the_hook_starts_the_workflow_on_the_parse_queue(valid_env):
    temporal = Temporal()
    rt = runtime(temporal, valid_env, "t-")
    result = await api_workflows.start_drive_import(rt, PID, CID)
    assert result == f"t-drive-import-{CID}"
    [call] = temporal.calls
    assert call["workflow"] == "DriveImportWorkflow"
    assert call["id"] == f"t-drive-import-{CID}"
    assert call["task_queue"] == "t-" + rt.settings.temporal.queue_parse
    assert call["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING
    assert call["execution_timeout"] == timedelta(hours=12)
    arg = call["arg"]
    assert (arg.project_id, arg.connection_id, arg.id_prefix) == (PID, CID, "t-")
    assert "name" not in arg.model_dump()  # ids only


async def test_an_ambiguous_start_finds_the_workflow_and_does_not_fail(valid_env):
    temporal = Temporal(start_error=TimeoutError("deadline"), exists=True)
    got = await api_workflows.start_drive_import(runtime(temporal, valid_env), PID, CID)
    assert got == f"drive-import-{CID}"


async def test_a_start_that_provably_did_not_happen_raises_the_original_error(
    valid_env,
):
    error = ConnectionError("down")
    temporal = Temporal(start_error=error, exists=False)
    with pytest.raises(ConnectionError) as raised:
        await api_workflows.start_drive_import(runtime(temporal, valid_env), PID, CID)
    assert raised.value is error


# ------------------------------------------------------------ activity branches
class Session:
    def begin(self):
        return contextlib.nullcontext()


class Sessions:
    def __call__(self):
        @contextlib.asynccontextmanager
        async def manager():
            yield Session()

        return manager()


def make_item(
    *,
    mime="application/pdf",
    file_id="file0123456",
    category="pdf",
    name="Quarterly plan.pdf",
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        connection_id=CID,
        project_id=PID,
        external_id=file_id,
        name=name,
        mime_type=mime,
        category=category,
        state="pending",
    )


class Repo:
    """Stands in for the database functions the activities call."""

    def __init__(self, monkeypatch, item, *, state="importing", existing_doc=None):
        self.item = item
        self.connection = SimpleNamespace(id=CID, state=state, external_id="folder01234")
        self.finished: list[dict] = []
        self.inserted: list[dict] = []
        self.existing_doc = existing_doc
        self.duplicate_of = None
        self.removed_at_fence = False  # the source row is gone when the insert would run
        self.claims = 0
        monkeypatch.setattr(sources_repo, "claim_item", self.claim_item)
        monkeypatch.setattr(sources_repo, "lock_importing_connection", self.lock_importing_connection)
        monkeypatch.setattr(sources_repo, "get_connection", self.get_connection)
        monkeypatch.setattr(sources_repo, "get_item", self.get_item)
        monkeypatch.setattr(sources_repo, "finish_item", self.finish_item)
        monkeypatch.setattr(sources_repo, "insert_connection_document", self.insert)
        monkeypatch.setattr(repositories, "get_document_state", self.get_document_state)

    async def get_connection(self, _session, _project_id, _connection_id):
        return self.connection

    async def get_item(self, _session, _item_id):
        return self.item

    async def claim_item(self, _session, _project_id, _connection_id, _item_id):
        if (
            self.connection is None
            or self.connection.state != "importing"
            or self.item.state not in ("pending", "processing")
        ):
            return None
        self.claims += 1
        self.item.state = "processing"
        return self.item

    async def lock_importing_connection(self, _session, _project_id, _connection_id):
        if self.removed_at_fence or self.connection is None:
            return None
        return self.connection if self.connection.state == "importing" else None

    async def get_document_state(self, _session, _project_id, _document_id):
        return self.existing_doc

    async def finish_item(self, _session, _item_id, state, **kw):
        self.finished.append({"state": state, **kw})
        self.item.state = state
        return True

    async def insert(self, _session, *, file, **kw):
        kw = dataclasses.asdict(file) | kw
        self.inserted.append(kw)
        if self.duplicate_of is not None:
            return self.duplicate_of, False
        return (
            SimpleNamespace(id=kw["document_id"], status=kw["status"], source_id=kw["source_id"]),
            True,
        )


class Flaky(FakeDriveGateway):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.download_error: Exception | None = None
        self.read_error: Exception | None = None
        self.payload = None

    def iter_download(self, file_id, *, chunk_size=8 * 1024 * 1024):
        if self.download_error:
            raise self.download_error
        return super().iter_download(file_id, chunk_size=chunk_size)

    def read_google(self, file_id, mime_type):
        if self.read_error:
            raise self.read_error
        if self.payload is not None:
            return self.payload
        return super().read_google(file_id, mime_type)


@pytest.fixture
def pool():
    executor = ThreadPoolExecutor(2)
    yield executor
    executor.shutdown(wait=False)


@pytest.fixture
def store():
    return InMemoryObjectStore(app_env="local")


def build(store, pool, drive=None, **kw):
    drive = drive or Flaky(app_env="local", file_delay_s=0, role="worker")
    return acts.DriveImportActivities(Sessions(), store, drive, pool, **kw), drive


def data(item, kind):
    return ImportItemInput(project_id=PID, connection_id=CID, item_id=item.id, kind=kind)


def fake_item_of(drive, category_mime):
    return next(i for i in drive.list_all("fake-synthetic-30").items if i.mime_type == category_mime)


async def run_activity(activities, function, arg, *, attempt=1):
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=attempt)
    return await env.run(function, arg)


async def test_a_pdf_streams_to_the_store_hashed_and_asks_for_the_parse(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item)

    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))

    doc_id = ids.drive_document_id(CID, pdf.id)
    assert (result.action, result.document_id) == ("parse", doc_id)
    key = document_original(str(PID), str(doc_id))
    assert store.objects[key] == TINY_PDF and store.content_types[key] == "application/pdf"
    [inserted] = repo.inserted
    assert inserted["content_hash"] == hashlib.sha256(TINY_PDF).hexdigest()
    assert (inserted["status"], inserted["size_bytes"]) == ("processing", len(TINY_PDF))
    assert repo.finished == []  # the workflow finishes it after the child parse


async def test_a_twin_in_the_same_source_links_to_the_stored_file_and_drops_its_object(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item)
    other = uuid.uuid4()
    repo.duplicate_of = SimpleNamespace(id=other, status="parsed", source_id=CID)

    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))

    assert result.action == "done"
    assert repo.finished == [
        {
            "state": "processed",
            "document_id": other,
            "error_reason": None,
            "error_step": None,
        }
    ]
    assert store.objects == {}  # no second copy


async def test_a_duplicate_of_a_failed_document_is_a_parse_error(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item)
    repo.duplicate_of = SimpleNamespace(id=uuid.uuid4(), status="failed", source_id=CID)
    await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
    assert (
        repo.finished[0]["state"],
        repo.finished[0]["error_reason"],
        repo.finished[0]["error_step"],
    ) == (
        "error",
        "parse_failed",
        "parse",
    )


async def test_a_retry_with_a_parsed_row_is_done_and_one_without_a_parse_runs_it(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item, existing_doc=SimpleNamespace(status="parsed", source_id=CID))
    done = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
    assert done.action == "done" and repo.finished[0]["state"] == "processed"

    item2 = make_item(file_id=pdf.id)
    repo2 = Repo(monkeypatch, item2, existing_doc=SimpleNamespace(status="processing", source_id=CID))
    again = await run_activity(activities, activities.drive_import_item, data(item2, "pdf"))
    assert again.action == "parse" and repo2.finished == [] and store.objects == {}


async def test_an_orphan_pdf_object_without_a_row_is_replaced(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item)
    key = document_original(str(PID), str(ids.drive_document_id(CID, pdf.id)))
    store.objects[key] = b"left by a killed attempt"

    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))

    assert result.action == "parse" and store.objects[key] == TINY_PDF
    assert repo.inserted[0]["content_hash"] == hashlib.sha256(TINY_PDF).hexdigest()


async def test_a_stored_only_file_that_exists_is_done_without_a_second_write(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    image = fake_item_of(drive, "image/png")
    item = make_item(file_id=image.id, mime="image/png", category="images")
    repo = Repo(monkeypatch, item)
    key = document_original_bin(str(PID), str(ids.drive_document_id(CID, image.id)))
    store.objects[key] = b"first copy"

    result = await run_activity(activities, activities.drive_import_item, data(item, "stored"))

    assert result.action == "done" and store.objects[key] == b"first copy"
    assert repo.finished[0]["state"] == "stored" and repo.inserted == []


async def test_a_google_file_stores_json_and_markdown_and_links_duplicates(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    doc = fake_item_of(drive, "application/vnd.google-apps.document")
    item = make_item(file_id=doc.id, mime=doc.mime_type, category="google_docs")
    repo = Repo(monkeypatch, item)

    result = await run_activity(activities, activities.drive_import_item, data(item, "google"))

    doc_id = ids.drive_document_id(CID, doc.id)
    assert (result.action, repo.finished[0]["state"]) == ("done", "processed")
    assert repo.finished[0]["document_id"] == doc_id
    key = document_original_json(str(PID), str(doc_id))
    raw = store.objects[key]
    assert json.loads(raw) and store.content_types[key] == "application/json"
    [inserted] = repo.inserted
    assert inserted["markdown"].startswith("# ") and inserted["status"] == "parsed"
    assert inserted["content_hash"] == hashlib.sha256(raw).hexdigest()
    assert "\x00" not in inserted["markdown"]
    assert "contentUri" not in raw.decode() and "googleusercontent" not in inserted["markdown"]

    # the same content already is a document: link, drop the own object
    second = [i for i in drive.list_all("fake-synthetic-30").items if i.mime_type == doc.mime_type][1]
    item2 = make_item(file_id=second.id, mime=doc.mime_type, category="google_docs")
    repo2 = Repo(monkeypatch, item2)
    repo2.duplicate_of = SimpleNamespace(id=uuid.uuid4(), status="parsed", source_id=CID)
    await run_activity(activities, activities.drive_import_item, data(item2, "google"))
    assert repo2.finished[0]["document_id"] == repo2.duplicate_of.id
    assert document_original_json(str(PID), str(ids.drive_document_id(CID, second.id))) not in store.objects


async def test_converted_markdown_over_the_cap_is_a_convert_error(monkeypatch, store, pool):
    """The output size cap (03 handoff): oversize -> ``convert_failed`` / ``convert``."""

    activities, drive = build(store, pool, max_markdown_bytes=10)
    doc = fake_item_of(drive, "application/vnd.google-apps.document")
    item = make_item(file_id=doc.id, mime=doc.mime_type, category="google_docs")
    repo = Repo(monkeypatch, item)

    result = await run_activity(activities, activities.drive_import_item, data(item, "google"))

    assert result.action == "done" and repo.inserted == []
    assert (repo.finished[0]["error_reason"], repo.finished[0]["error_step"]) == (
        "convert_failed",
        "convert",
    )
    assert acts.MAX_MARKDOWN_BYTES == 5 * 1024 * 1024
    # the JSON original stays stored: the converter can run again without Google
    assert document_original_json(str(PID), str(ids.drive_document_id(CID, doc.id))) in store.objects


async def test_a_malformed_google_payload_is_a_convert_error(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    drive.payload = []
    doc = fake_item_of(drive, "application/vnd.google-apps.document")
    item = make_item(file_id=doc.id, mime=doc.mime_type, category="google_docs")
    repo = Repo(monkeypatch, item)
    await run_activity(activities, activities.drive_import_item, data(item, "google"))
    assert repo.finished[0]["error_reason"] == "convert_failed"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (DriveError(Reason.NO_ACCESS), "no_access"),
        (DriveError(Reason.SETUP_ERROR), "unknown_error"),
        (DriveError(Reason.TOO_SLOW), "unknown_error"),
    ],
)
async def test_permanent_drive_errors_end_the_file_at_once(monkeypatch, store, pool, error, reason):
    activities, drive = build(store, pool)
    drive.download_error = error
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item)
    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
    assert result.action == "done"
    assert (repo.finished[0]["error_reason"], repo.finished[0]["error_step"]) == (
        reason,
        "download",
    )
    assert store.objects == {}


async def test_a_transient_error_retries_then_the_last_attempt_records_it(monkeypatch, store, pool, caplog):
    caplog.set_level("INFO")
    activities, drive = build(store, pool)
    drive.download_error = DriveError(Reason.DRIVE_UNAVAILABLE, http_status=503)
    image = fake_item_of(drive, "image/png")
    item = make_item(file_id=image.id, mime="image/png", category="images", name="Secret photo.png")
    repo = Repo(monkeypatch, item)

    for attempt in (1, 2):
        with pytest.raises(ApplicationError) as raised:
            await run_activity(
                activities,
                activities.drive_import_item,
                data(item, "stored"),
                attempt=attempt,
            )
        assert (raised.value.message, raised.value.type) == (
            "drive_unavailable:download",
            "ItemTransient",
        )
    assert repo.finished == []

    result = await run_activity(activities, activities.drive_import_item, data(item, "stored"), attempt=3)
    assert result.action == "done"
    assert (repo.finished[0]["error_reason"], repo.finished[0]["error_step"]) == (
        "drive_unavailable",
        "download",
    )
    [logged] = events(caplog, "drive.import.file_failed")
    expected = {
        "project_id": str(PID),
        "connection_id": str(CID),
        "file_id": image.id,
        "mime_type": "image/png",
        "step": "download",
        "reason": "drive_unavailable",
    }
    assert {k: fields(logged)[k] for k in expected} == expected
    assert "Secret photo" not in caplog.text


async def test_a_store_failure_is_store_failed_after_retries_and_leaves_no_object(monkeypatch, pool):
    class Broken(InMemoryObjectStore):
        async def open_writer(self, *_args, **_kwargs):
            raise ObjectStoreError("secret store text")

    store = Broken(app_env="local")
    activities, drive = build(store, pool)
    image = fake_item_of(drive, "image/png")
    item = make_item(file_id=image.id, mime="image/png", category="images")
    repo = Repo(monkeypatch, item)
    with pytest.raises(ApplicationError) as raised:
        await run_activity(activities, activities.drive_import_item, data(item, "stored"), attempt=1)
    assert raised.value.message == "store_failed:store"
    assert "secret" not in str(raised.value)
    await run_activity(activities, activities.drive_import_item, data(item, "stored"), attempt=3)
    assert repo.finished[0]["error_reason"] == "store_failed"


async def test_a_failed_stream_aborts_the_writer_so_no_partial_object_stays(monkeypatch, store, pool):
    class Dies(FakeDriveGateway):
        def iter_download(self, *_args, **_kwargs):
            yield b"first chunk"
            raise DriveError(Reason.DRIVE_UNAVAILABLE)

    activities, drive = build(store, pool, Dies(app_env="local", file_delay_s=0, role="worker"))
    image = fake_item_of(drive, "image/png")
    item = make_item(file_id=image.id, mime="image/png", category="images")
    Repo(monkeypatch, item)
    with pytest.raises(ApplicationError):
        await run_activity(activities, activities.drive_import_item, data(item, "stored"), attempt=1)
    assert store.objects == {}


async def test_a_deleted_or_failed_connection_or_a_finished_file_is_quiet(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    for state in ("failed", "done", "listing"):
        item = make_item(file_id=pdf.id)
        repo = Repo(monkeypatch, item, state=state)
        result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
        assert result.action == "done" and repo.finished == [] and repo.inserted == []
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item)
    repo.connection = None
    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
    assert result.action == "done" and repo.finished == [] and store.objects == {}
    item = make_item(file_id=pdf.id)
    item.state = "processed"
    repo = Repo(monkeypatch, item)
    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
    assert result.action == "done" and repo.finished == [] and store.objects == {}


async def test_an_unexpected_error_leaves_as_a_fixed_code_and_logs_only_its_type(monkeypatch, store, pool, caplog):
    caplog.set_level("INFO")
    activities, _ = build(store, pool)

    async def boom(*args, **kwargs):
        raise RuntimeError("INSERT ... name='Secret Plan.pdf'")

    monkeypatch.setattr(sources_repo, "get_connection", boom)
    with pytest.raises(ApplicationError) as raised:
        await run_activity(
            activities,
            activities.drive_list,
            SimpleNamespace(project_id=PID, connection_id=CID),
        )
    assert (raised.value.message, raised.value.type) == (
        "internal_error",
        "DriveImportError",
    )
    assert "Secret Plan" not in caplog.text
    [logged] = events(caplog, "drive.import.activity_failed")
    assert fields(logged)["error_type"] == "RuntimeError"
    assert fields(logged)["activity"] == "drive_list"


async def test_finish_item_for_an_already_final_file_changes_nothing(monkeypatch, store, pool):
    activities, _ = build(store, pool)
    item = make_item()
    item.state = "error"
    repo = Repo(monkeypatch, item)
    await run_activity(
        activities,
        activities.drive_finish_item,
        FinishItemInput(project_id=PID, connection_id=CID, item_id=item.id, state="processed"),
    )
    assert repo.finished == []


def test_the_activity_list_names_every_workflow_step(store, pool):
    activities, _ = build(store, pool)
    names = {activity._Definition.must_from_callable(fn).name for fn in activities.activities}
    assert names == {
        wf.LIST_ACTIVITY,
        wf.BATCH_ACTIVITY,
        wf.ITEM_ACTIVITY,
        wf.FINISH_ACTIVITY,
        wf.WAIT_ACTIVITY,
        wf.COMPLETE_ACTIVITY,
        wf.FAIL_ACTIVITY,
    }


# ------------------------------------------------------------- gateway selection log
def test_the_worker_logs_the_selected_gateway_once(caplog):
    caplog.set_level("INFO")
    registration.log_gateway_selected(FakeDriveGateway(app_env="local", role="worker"), role="worker")
    [logged] = events(caplog, "drive.gateway.selected")
    got = fields(logged)
    assert (got["mode"], got["email"], got["role"]) == (
        "fake",
        "fake-drive@specflow.local",
        "worker",
    )


def test_an_unreadable_identity_is_logged_as_a_warning_and_never_fatal(caplog):
    class NoCredentials:
        mode = "real"

        def identity(self):
            raise DriveError(Reason.SETUP_ERROR)

    caplog.set_level("INFO")
    registration.log_gateway_selected(NoCredentials(), role="worker")
    [logged] = events(caplog, "drive.gateway.selected")
    assert logged.levelname == "WARNING"
    assert (fields(logged)["mode"], fields(logged)["email"]) == ("real", None)


async def test_register_builds_the_worker_role_gateway(monkeypatch, valid_env):
    seen = {}

    def fake_build(app_env, *args, **kwargs):
        seen.update(app_env=app_env, **kwargs)
        return FakeDriveGateway(app_env="local", role=kwargs["role"])

    monkeypatch.setattr(registration, "build_drive_gateway", fake_build)
    monkeypatch.setattr(
        "storage.factory.build_object_store",
        lambda _settings, **kw: InMemoryObjectStore(app_env=kw["app_env"]),
    )
    loaded = registration.register(load_settings("worker", valid_env, queues=["parse"]))
    assert seen == {"app_env": "local", "role": "worker"}
    assert wf.DriveImportWorkflow in loaded.workflows
    await loaded.close()


async def test_a_closed_workflow_is_not_a_started_one(valid_env):
    error = TimeoutError("deadline")
    temporal = Temporal(start_error=error, exists=True, status="COMPLETED")
    with pytest.raises(TimeoutError) as raised:
        await api_workflows.start_drive_import(runtime(temporal, valid_env), PID, CID)
    assert raised.value is error


# ------------------------------------------- shared documents still being parsed
async def test_a_duplicate_of_a_processing_document_stays_pending_and_waits(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item)
    other = uuid.uuid4()
    repo.duplicate_of = SimpleNamespace(id=other, status="processing", source_id=CID)

    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))

    assert (result.action, result.document_id) == ("wait", other)
    assert repo.finished == [] and item.state == "processing"  # claimed, waiting
    assert store.objects == {}


async def test_the_wait_activity_returns_the_final_status(monkeypatch, store, pool):
    activities, _ = build(store, pool)
    activities._wait_interval = 0.01
    states = iter(["processing", "processing", "parsed"])

    async def state(session, project_id, document_id):
        return SimpleNamespace(status=next(states))

    monkeypatch.setattr(repositories, "get_document_state", state)
    arg = WaitDocumentInput(project_id=PID, document_id=uuid.uuid4())
    done = await run_activity(activities, activities.drive_wait_document, arg)
    assert done.status == "parsed"

    async def failed(session, project_id, document_id):
        return SimpleNamespace(status="failed")

    monkeypatch.setattr(repositories, "get_document_state", failed)
    assert (await run_activity(activities, activities.drive_wait_document, arg)).status == "failed"

    async def gone(session, project_id, document_id):
        return None

    monkeypatch.setattr(repositories, "get_document_state", gone)
    assert (await run_activity(activities, activities.drive_wait_document, arg)).status == "failed"

    async def stuck(session, project_id, document_id):
        return SimpleNamespace(status="processing")

    monkeypatch.setattr(repositories, "get_document_state", stuck)
    monkeypatch.setattr(acts, "WAIT_LIMIT_SECONDS", 0.05)
    assert (await run_activity(activities, activities.drive_wait_document, arg)).status == "timeout"


async def test_a_lost_child_parse_marks_its_own_processing_document_failed(monkeypatch, store, pool):
    activities, _ = build(store, pool)
    item = make_item()
    repo = Repo(monkeypatch, item)
    own = ids.drive_document_id(CID, item.external_id)
    marked = []

    async def state(session, project_id, document_id):
        return SimpleNamespace(status="processing")

    async def mark(session, project_id, document_id, code):
        marked.append((document_id, code))

    monkeypatch.setattr(repositories, "get_document_state", state)
    monkeypatch.setattr(repositories, "mark_failed", mark)
    args = {
        "project_id": PID,
        "connection_id": CID,
        "item_id": item.id,
        "state": "error",
        "reason": "parse_failed",
        "step": "parse",
    }
    await run_activity(
        activities,
        activities.drive_finish_item,
        FinishItemInput(document_id=own, **args),
    )
    assert marked == [(own, "internal_error")]
    assert repo.finished[0]["error_reason"] == "parse_failed"

    # a document of another flow (upload, other file) is never touched
    item.state = "pending"
    marked.clear()
    await run_activity(
        activities,
        activities.drive_finish_item,
        FinishItemInput(document_id=uuid.uuid4(), **args),
    )
    assert marked == []


# ------------------------------------- sources-ux: claim, fence, duplicate, finish
#: sha256 of ``temporal/sources/workflows.py`` at 6be1033d: the command sequence must
#: replay for in-flight imports, so this session changes activities only (C8).
#: Re-pinned after a replay-neutral edit (ruff format, mypy annotations, the internal
#: ``DeadlineExceeded`` -> ``DeadlineExceededError`` rename that never reaches history):
#: the AST is unchanged apart from annotations, that name and the typing/uuid imports.
DRIVE_WORKFLOW_SHA256 = "a0b5e716b5b1579f7277ab3aafc80a9f9e94836a84b6fe0045408fc4bb9cdd61"


def test_the_drive_import_workflow_file_is_unchanged_for_replay_safety():
    path = Path(wf.__file__)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == DRIVE_WORKFLOW_SHA256


async def test_a_file_is_claimed_before_work_and_a_retry_claims_it_again(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item)
    await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
    assert repo.claims == 1 and item.state == "processing"

    # a killed pod left the file ``processing``; the retried activity continues
    store.objects.clear()
    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"), attempt=2)
    assert repo.claims == 2 and result.action == "parse"
    assert len(store.objects) == 1


async def test_a_removed_or_final_file_is_never_claimed_or_written(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    for state in ("duplicate", "stored", "skipped", "error"):
        item = make_item(file_id=pdf.id)
        item.state = state
        repo = Repo(monkeypatch, item)
        result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
        assert result.action == "done" and repo.claims == 0 and repo.inserted == []
    assert store.objects == {}


async def test_the_fence_drops_a_pdf_whose_source_was_removed_during_the_download(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item)
    repo.removed_at_fence = True

    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))

    assert result.action == "done"
    assert repo.inserted == [] and repo.finished == []  # no document row, no state
    assert store.objects == {}  # the own object is deleted


async def test_the_fence_keeps_an_object_that_a_document_row_owns(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item)
    repo.removed_at_fence = True
    rows = iter([None, SimpleNamespace(status="parsed", source_id=OTHER)])

    async def state(session, project_id, document_id):
        return next(rows)  # none at the start, a handed-over row at the fence

    monkeypatch.setattr(repositories, "get_document_state", state)
    await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
    assert len(store.objects) == 1 and repo.inserted == []


async def test_the_fence_drops_a_google_file_whose_source_was_removed(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    doc = fake_item_of(drive, "application/vnd.google-apps.document")
    item = make_item(file_id=doc.id, mime=doc.mime_type, category="google_docs")
    repo = Repo(monkeypatch, item)
    repo.removed_at_fence = True

    result = await run_activity(activities, activities.drive_import_item, data(item, "google"))

    assert result.action == "done" and repo.inserted == [] and repo.finished == []
    assert store.objects == {}


async def test_the_insert_names_the_source_as_the_holder(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    doc = fake_item_of(drive, "application/vnd.google-apps.document")
    for kind, found, mime in (
        ("pdf", pdf, "application/pdf"),
        ("google", doc, doc.mime_type),
    ):
        item = make_item(file_id=found.id, mime=mime)
        repo = Repo(monkeypatch, item)
        await run_activity(activities, activities.drive_import_item, data(item, kind))
        assert repo.inserted[0]["source_id"] == CID


@pytest.mark.parametrize("status", ["parsed", "failed", "processing", "uploaded"])
@pytest.mark.parametrize("holder", [OTHER, None], ids=["other-source", "no-holder"])
async def test_content_held_by_another_source_is_a_duplicate_at_once(monkeypatch, store, pool, status, holder):
    """SD6: content Failed in a Local batch is a duplicate too; no wait, no parse."""

    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item)
    other = uuid.uuid4()
    repo.duplicate_of = SimpleNamespace(id=other, status=status, source_id=holder)

    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))

    assert result.action == "done"
    assert repo.finished == [
        {
            "state": "duplicate",
            "document_id": other,
            "error_reason": None,
            "error_step": None,
        }
    ]
    assert store.objects == {}  # no second copy


async def test_a_google_file_held_by_another_source_is_a_duplicate(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    doc = fake_item_of(drive, "application/vnd.google-apps.document")
    item = make_item(file_id=doc.id, mime=doc.mime_type, category="google_docs")
    repo = Repo(monkeypatch, item)
    other = uuid.uuid4()
    repo.duplicate_of = SimpleNamespace(id=other, status="parsed", source_id=OTHER)
    await run_activity(activities, activities.drive_import_item, data(item, "google"))
    assert [(f["state"], f["document_id"]) for f in repo.finished] == [("duplicate", other)]
    assert store.objects == {}


async def test_a_twin_in_the_same_folder_keeps_the_one_stored_files_state(monkeypatch, store, pool):
    """SD9: the same holder is unchanged: processed, parse error, or wait."""

    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    seen = []
    for status in ("parsed", "failed", "processing"):
        item = make_item(file_id=pdf.id)
        repo = Repo(monkeypatch, item)
        repo.duplicate_of = SimpleNamespace(id=uuid.uuid4(), status=status, source_id=CID)
        result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
        seen.append((result.action, [f["state"] for f in repo.finished]))
    assert seen == [("done", ["processed"]), ("done", ["error"]), ("wait", [])]


async def test_an_own_id_row_held_by_another_source_is_linked_and_its_object_kept(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    own = ids.drive_document_id(CID, pdf.id)
    repo = Repo(
        monkeypatch,
        item,
        existing_doc=SimpleNamespace(id=own, status="parsed", source_id=OTHER),
    )
    key = document_original(str(PID), str(own))
    store.objects[key] = b"the holder's object"

    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))

    assert result.action == "done" and repo.inserted == []  # no stream, no insert
    assert [(f["state"], f["document_id"]) for f in repo.finished] == [("duplicate", own)]
    assert store.objects == {key: b"the holder's object"}


async def test_finish_and_the_last_failed_attempt_accept_a_processing_file(monkeypatch, store, pool):
    activities, _ = build(store, pool)
    item = make_item()
    item.state = "processing"
    repo = Repo(monkeypatch, item)
    await run_activity(
        activities,
        activities.drive_finish_item,
        FinishItemInput(project_id=PID, connection_id=CID, item_id=item.id, state="processed"),
    )
    assert [f["state"] for f in repo.finished] == ["processed"]

    item2 = make_item()
    item2.state = "processing"
    repo2 = Repo(monkeypatch, item2)
    await activities._finish_error(data(item2, "pdf"), "unknown_error", "download")
    assert [f["state"] for f in repo2.finished] == ["error"]


def test_the_cleanup_id_builders_and_chunks():
    removal = uuid.uuid4()
    assert ids.cleanup_workflow_id(removal, 2) == f"source-cleanup-{removal}-2"
    assert ids.cleanup_workflow_id(removal, 0, prefix="t-") == f"t-source-cleanup-{removal}-0"
    assert ids.chunk_ids([]) == []
    twelve = [uuid.uuid4() for _ in range(12_000)]
    chunks = ids.chunk_ids(twelve)  # R07
    assert [len(c) for c in chunks] == [5000, 5000, 2000]
    assert [i for c in chunks for i in c] == twelve
    assert ids.chunk_ids([1, 2, 3], 2) == [[1, 2], [3]]
    with pytest.raises(ValueError, match="chunk size"):
        ids.chunk_ids([1], 0)


# ----------------------------------------------- review-03: M1 guards, m5 statuses
async def test_a_google_content_duplicate_keeps_an_object_that_a_row_owns(monkeypatch, store, pool):
    """M1: the own-id row is held by another source and the content matches a third
    document: the holder's ``original.json`` stays, the file is a duplicate."""

    activities, drive = build(store, pool)
    doc = fake_item_of(drive, "application/vnd.google-apps.document")
    item = make_item(file_id=doc.id, mime=doc.mime_type, category="google_docs")
    own = ids.drive_document_id(CID, doc.id)
    repo = Repo(
        monkeypatch,
        item,
        existing_doc=SimpleNamespace(id=own, status="parsed", source_id=OTHER),
    )
    third = uuid.uuid4()
    repo.duplicate_of = SimpleNamespace(id=third, status="parsed", source_id=OTHER)
    key = document_original_json(str(PID), str(own))

    await run_activity(activities, activities.drive_import_item, data(item, "google"))

    assert key in store.objects  # the holder's object is kept
    assert [(f["state"], f["document_id"]) for f in repo.finished] == [("duplicate", third)]


async def test_a_google_content_duplicate_without_an_own_row_drops_its_object(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    doc = fake_item_of(drive, "application/vnd.google-apps.document")
    item = make_item(file_id=doc.id, mime=doc.mime_type, category="google_docs")
    repo = Repo(monkeypatch, item)
    repo.duplicate_of = SimpleNamespace(id=uuid.uuid4(), status="parsed", source_id=OTHER)
    await run_activity(activities, activities.drive_import_item, data(item, "google"))
    assert store.objects == {} and repo.finished[0]["state"] == "duplicate"


async def test_a_pdf_content_duplicate_keeps_an_object_that_a_row_owns(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    own = ids.drive_document_id(CID, pdf.id)
    repo = Repo(monkeypatch, item)
    rows = iter([None, SimpleNamespace(id=own, status="parsed", source_id=OTHER)])

    async def state(session, project_id, document_id):
        return next(rows)  # none at the start, a row at the guard (a race)

    monkeypatch.setattr(repositories, "get_document_state", state)
    repo.duplicate_of = SimpleNamespace(id=uuid.uuid4(), status="parsed", source_id=OTHER)
    await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
    assert len(store.objects) == 1 and repo.finished[0]["state"] == "duplicate"


async def test_the_google_fence_keeps_an_object_that_a_document_row_owns(monkeypatch, store, pool):
    activities, drive = build(store, pool)
    doc = fake_item_of(drive, "application/vnd.google-apps.document")
    item = make_item(file_id=doc.id, mime=doc.mime_type, category="google_docs")
    repo = Repo(
        monkeypatch,
        item,
        existing_doc=SimpleNamespace(status="parsed", source_id=OTHER),
    )
    repo.removed_at_fence = True
    await run_activity(activities, activities.drive_import_item, data(item, "google"))
    assert len(store.objects) == 1 and repo.inserted == [] and repo.finished == []


@pytest.mark.parametrize("status", ["ingested", "partially_ingested"])
async def test_every_processed_status_is_done_on_a_retry_and_for_a_twin(monkeypatch, store, pool, status):
    """m5: ``parsed``, ``ingested`` and ``partially_ingested`` all count as processed."""

    activities, drive = build(store, pool)
    pdf = fake_item_of(drive, "application/pdf")
    item = make_item(file_id=pdf.id)
    repo = Repo(monkeypatch, item, existing_doc=SimpleNamespace(status=status, source_id=CID))
    result = await run_activity(activities, activities.drive_import_item, data(item, "pdf"))
    assert result.action == "done" and repo.finished[0]["state"] == "processed"

    twin = make_item(file_id=pdf.id)
    repo2 = Repo(monkeypatch, twin)
    repo2.duplicate_of = SimpleNamespace(id=uuid.uuid4(), status=status, source_id=CID)
    result = await run_activity(activities, activities.drive_import_item, data(twin, "pdf"))
    assert result.action == "done" and repo2.finished[0]["state"] == "processed"
