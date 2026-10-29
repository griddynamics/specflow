"""Tier 2 (``SPECFLOW_TEST_POSTGRES=1`` + ``SPECFLOW_TEST_TEMPORAL=1``): the Drive import end to end.

Real local Temporal (``specflow-local``; a unique task queue and id prefix, never
``parse``), a scratch database, the fake Drive in the ``worker`` role and the
in-memory object store (a directory store for the SIGKILL test). Requirements:
INT-GDUI-0004, FR-GDUI-0017/0018, NFR-PORT-0002.AC3, NFR-MAIN-0001.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import func, select
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from api import workflows as api_workflows
from api.runtime import Runtime
from db import removal, sources
from db.models import ConnectionItem
from db.sources import DocumentFile
from drive.categories import ProcessingClass, categorize
from drive.fake import TINY_PDF, FakeDriveGateway
from drive.gateway import DriveError
from drive.reasons import Reason
from logger_harness import events, fields
from settings import load_settings
from specflow_drive_import_support import (
    DirObjectStore,
    DriveStack,
)
from specflow_live import requires_live
from storage.base import ObjectStoreError
from storage.keys import document_original_bin
from storage.memory import InMemoryObjectStore
from temporal.cleanup.activities import SourceCleanupActivities
from temporal.cleanup.models import SweepInput
from temporal.ids import drive_document_id
from temporal.queues import queue_name
from temporal.sources import workflows as wfm
from temporal.sources.models import (
    BatchItem,
    BatchRequest,
    BatchResult,
    CompleteInput,
    DriveImportInput,
    ImportEnd,
    ImportItemInput,
    ImportItemResult,
    ListResult,
)
from temporal.sources.workflows import DriveImportWorkflow

pytest_plugins = ["specflow_drive_import_support"]
pytestmark = [requires_live("POSTGRES"), requires_live("TEMPORAL")]
POLL_INTERVAL_S = 0.2  # between two checks of a condition
HISTORY_POLL_S = 0.1  # between two samples of the workflow progress
PARSE_WORK_S = 0.4  # the time one fake parse takes, so that parses overlap
EXPECTED_STATE = {
    ProcessingClass.PROCESSED: "processed",
    ProcessingClass.STORED_ONLY: "stored",
    ProcessingClass.SKIPPED: "skipped",
}


def expected_states(gateway: FakeDriveGateway, folder: str) -> dict[str, str]:
    return {i.id: EXPECTED_STATE[categorize(i.mime_type)[1]] for i in gateway.list_all(folder).items}


async def wait_until(check, *, max_wait_s: float, what: str):
    deadline = time.monotonic() + max_wait_s
    while True:
        value = await check()
        if value:
            return value
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(POLL_INTERVAL_S)


# ---------------------------------------------------------------- happy paths
async def test_synthetic_30_started_by_the_save_hook_reaches_the_right_final_states(
    drive_stack: DriveStack, valid_env, caplog
):
    """INT-GDUI-0004 progress, FR-GDUI-0017, the hook (USE_EXISTING, parse queue), B6 logs."""

    s = drive_stack
    caplog.set_level("INFO")
    cid = await s.connect("fake-synthetic-30", name="Secret Folder Name")
    settings = load_settings(
        "api",
        {
            **valid_env,
            "TEMPORAL_ADDRESS": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        },
    )
    runtime = Runtime(
        settings=settings,
        session_factory=None,
        store=None,
        temporal=s.client,
        driver=None,
        readiness=None,
        id_prefix=s.temporal.id_prefix,
    )
    parse_queue = queue_name(settings.temporal, "parse", prefix=s.temporal.id_prefix)
    assert parse_queue != "parse" and parse_queue.startswith("pytest-")
    expected = expected_states(s.gateway(), "fake-synthetic-30")

    seen: list[tuple[bool, int, int]] = []

    async def sample():
        p = (await s.progress()).progress
        seen.append((p.listing_finished, p.total, p.finished))
        return False

    async with s.worker(s.gateway(delay=0.05), queue=parse_queue):
        workflow_id = await api_workflows.start_drive_import(runtime, s.project_id, cid)
        again = await api_workflows.start_drive_import(runtime, s.project_id, cid)
        assert workflow_id == again == f"{s.temporal.id_prefix}drive-import-{cid}"
        handle = s.client.get_workflow_handle(workflow_id, result_type=ImportEnd)
        s.temporal.track(handle)
        poll = asyncio.create_task(_poll(sample, handle))
        result = await asyncio.wait_for(handle.result(), 120)
        await poll
    assert result.state == "done"

    connection = await s.connection(cid)
    assert (connection.state, connection.failure_reason) == ("done", None)
    assert connection.last_synced_at is not None
    items = await s.items(cid)
    assert {i.external_id: i.state for i in items} == expected
    assert all(i.error_reason is None for i in items)
    final = (await s.progress()).progress
    assert (final.listing_finished, final.total, final.finished) == (True, 30, 30)
    # progress never goes backwards; the total is fixed once the listing finished
    totals = {t for done, t, _ in seen if done}
    assert len(totals) <= 1
    assert [f for _, _, f in seen] == sorted(f for _, _, f in seen)
    assert (False, 0, 0) in seen or seen[0][0] is True  # listing visible when sampled in time

    await _assert_documents(s, expected)
    await _assert_logs_and_history(caplog, items, handle)


async def _assert_documents(s: DriveStack, expected: dict[str, str]) -> None:
    documents = await s.documents()
    assert {d.origin for d in documents} == {"connection"}
    assert all(d.status == "parsed" for d in documents)
    google = [d for d in documents if d.media_type == "application/json"]
    assert google and all(d.markdown.startswith("# ") for d in google)
    pdfs = [d for d in documents if d.media_type == "application/pdf"]
    assert len(pdfs) == 1 and pdfs[0].markdown and pdfs[0].parser == "pymupdf4llm"
    # one object per stored-only file, one original per document, nothing else
    stored = sum(1 for v in expected.values() if v == "stored")
    assert len(s.store.objects) == stored + len(documents)
    assert sum(k.endswith("original.bin") for k in s.store.objects) == stored
    assert sum(k.endswith("original.json") for k in s.store.objects) == len(google)
    assert sum(k.endswith("original.pdf") for k in s.store.objects) == 1
    assert all(k.startswith(f"projects/{s.project_id}/documents/") for k in s.store.objects)


async def _assert_logs_and_history(caplog, items, handle) -> None:
    # B6 logs: ids and counts only, never a name
    [listed] = events(caplog, "drive.import.listed")
    assert fields(listed)["file_count"] == 30 and fields(listed)["folder_id"] == "fake-synthetic-30"
    assert fields(listed)["counts"] == {
        "processed": 17,
        "stored_only": 10,
        "skipped": 3,
    }
    [done] = events(caplog, "drive.import.finished")
    assert (
        fields(done)["processed"],
        fields(done)["stored"],
        fields(done)["skipped"],
        fields(done)["error"],
    ) == (17, 10, 3, 0)
    assert "Secret Folder Name" not in caplog.text
    for item in items:
        assert item.name not in caplog.text
    # history: ids and fixed codes only
    raw = (await handle.fetch_history()).to_json()
    assert "Secret Folder Name" not in raw
    assert all(i.name not in raw for i in items)
    assert len(raw) < 2 * 1024 * 1024


async def _poll(sample, handle):
    while True:
        await sample()
        desc = await handle.describe()
        if desc.status is not None and desc.status.name != "RUNNING":
            return
        await asyncio.sleep(HISTORY_POLL_S)


async def test_synthetic_23_finishes_23_of_23_within_60_seconds(
    drive_stack: DriveStack,
):
    """NFR-PORT-0002.AC3: the real 1 s per-file delay of the local fake."""

    s = drive_stack
    cid = await s.connect("fake-synthetic-23")
    started = time.monotonic()
    async with s.worker(s.gateway(delay=1.0)):
        handle = await s.start(cid)
        result = await asyncio.wait_for(handle.result(), 90)
    elapsed = time.monotonic() - started
    assert result.state == "done"
    progress = (await s.progress()).progress
    assert (progress.listing_finished, progress.total, progress.finished) == (
        True,
        23,
        23,
    )
    assert elapsed <= 60, f"23 files took {elapsed:.1f} s"


async def test_fake_import_fails_ends_with_a_reason_and_everything_finished(drive_stack: DriveStack, caplog):
    """FR-GDUI-0018.AC4 / INT-GDUI-0004.AC4: failed at listing, listing finished, no open file."""

    s = drive_stack
    caplog.set_level("INFO")
    cid = await s.connect("fake-import-fails")
    async with s.worker():
        result = await asyncio.wait_for((await s.start(cid)).result(), 60)
    assert result.state == "failed"
    connection = await s.connection(cid)
    assert (connection.state, connection.failure_reason) == ("failed", "no_access")
    status = (await s.progress()).connections[0]
    assert status.state == "failed" and status.progress.listing_finished is True
    assert status.progress.finished == status.progress.total
    [failed] = events(caplog, "drive.import.failed")
    assert fields(failed)["reason"] == "no_access" and fields(failed)["failed_items"] == 0


async def test_api_role_fake_import_fails_folder_imports_like_synthetic_23(
    drive_stack: DriveStack,
):
    """The role switch: the same folder passes in the api role (Test and Save use it)."""

    s = drive_stack
    cid = await s.connect("fake-import-fails")
    async with s.worker(s.gateway(role="api")):
        result = await asyncio.wait_for((await s.start(cid)).result(), 60)
    assert result.state == "done"
    assert (await s.progress()).progress.total == 23


# -------------------------------------------------------------- failure paths
class BrokenListing(FakeDriveGateway):
    def list_all(self, *_args, **_kwargs):
        raise RuntimeError("secret boom text")


async def test_an_unexpected_failure_leaves_the_connection_failed_unknown_error(drive_stack: DriveStack, caplog):
    """Never ``listing``/``importing`` after a forced unexpected failure (INT-GDUI-0004.AC4)."""

    s = drive_stack
    caplog.set_level("INFO")
    cid = await s.connect("fake-synthetic-23")
    broken = BrokenListing(app_env="local", file_delay_s=0, role="worker")
    async with s.worker(broken):
        handle = await s.start(cid)
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), 90)
    connection = await s.connection(cid)
    assert (connection.state, connection.failure_reason) == ("failed", "unknown_error")
    assert "secret boom text" not in caplog.text
    raw = (await handle.fetch_history()).to_json()
    assert "secret boom text" not in raw


async def test_the_soft_deadline_fails_the_import_and_ends_open_files(
    drive_stack: DriveStack,
):
    """Soft deadline 11 h -> ``fail_import``: failed/unknown_error, open files import_failed."""

    s = drive_stack
    cid = await s.connect("fake-synthetic-30")
    async with s.worker():
        handle = await s.client.start_workflow(
            DriveImportWorkflow.run,
            DriveImportInput(
                project_id=s.project_id,
                connection_id=cid,
                id_prefix=s.temporal.id_prefix,
                started_epoch=time.time() - 12 * 3600,
            ),
            id=f"{s.temporal.id_prefix}drive-import-{cid}",
            task_queue=s.queue,
        )
        s.temporal.track(handle)
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), 60)
    connection = await s.connection(cid)
    assert (connection.state, connection.failure_reason) == ("failed", "unknown_error")
    items = await s.items(cid)
    assert len(items) == 30
    assert {(i.state, i.error_reason) for i in items} == {("error", "import_failed")}
    status = (await s.progress()).connections[0]
    assert status.progress.finished == status.progress.total == 30


class Flaky(FakeDriveGateway):
    """Per-file failures chosen by file id."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.no_access_read: set[str] = set()
        self.garbage_read: set[str] = set()
        self.no_access_download: set[str] = set()
        self.unavailable_download: set[str] = set()

    def read_google(self, file_id, mime_type):
        if file_id in self.no_access_read:
            raise DriveError(Reason.NO_ACCESS)
        if file_id in self.garbage_read:
            return []  # not a document: the converter fails
        return super().read_google(file_id, mime_type)

    def iter_download(self, file_id, *, chunk_size=8 * 1024 * 1024):
        if file_id in self.no_access_download:
            raise DriveError(Reason.NO_ACCESS)
        if file_id in self.unavailable_download:
            raise DriveError(Reason.DRIVE_UNAVAILABLE)
        return super().iter_download(file_id, chunk_size=chunk_size)


class FailingStore(InMemoryObjectStore):
    def __init__(self, bad_key: str):
        super().__init__(app_env="local")
        self.bad_key = bad_key

    async def open_writer(self, key, *, content_type):
        if key == self.bad_key:
            raise ObjectStoreError("secret store text")
        return await super().open_writer(key, content_type=content_type)


async def test_a_file_failure_ends_that_file_only_and_the_run_still_finishes(drive_stack: DriveStack, caplog):
    """FR-GDUI-0018: bounded retries, then ``error`` with reason and step; the run ends ``done``."""

    s = drive_stack
    caplog.set_level("INFO")
    cid = await s.connect("fake-synthetic-30")
    gateway = Flaky(app_env="local", file_delay_s=0, role="worker")
    by_category = {}
    for item in gateway.list_all("fake-synthetic-30").items:
        by_category.setdefault(categorize(item.mime_type)[0].value, []).append(item)
    no_access_read = by_category["google_docs"][0]
    garbage = by_category["google_slides"][0]
    no_access_dl = by_category["pdf"][0]
    unavailable = by_category["images"][0]
    store_bad = by_category["videos"][0]
    gateway.no_access_read.add(no_access_read.id)
    gateway.garbage_read.add(garbage.id)
    gateway.no_access_download.add(no_access_dl.id)
    gateway.unavailable_download.add(unavailable.id)
    bad_key = document_original_bin(str(s.project_id), str(drive_document_id(cid, store_bad.id)))
    store = FailingStore(bad_key)

    async with s.worker(gateway, store=store):
        result = await asyncio.wait_for((await s.start(cid)).result(), 120)
    assert result.state == "done"
    items = {i.external_id: i for i in await s.items(cid)}
    got = {
        iid: (items[iid].state, items[iid].error_reason, items[iid].error_step)
        for iid in (
            no_access_read.id,
            garbage.id,
            no_access_dl.id,
            unavailable.id,
            store_bad.id,
        )
    }
    assert got == {
        no_access_read.id: ("error", "no_access", "read"),
        garbage.id: ("error", "convert_failed", "convert"),
        no_access_dl.id: ("error", "no_access", "download"),
        unavailable.id: ("error", "drive_unavailable", "download"),
        store_bad.id: ("error", "store_failed", "store"),
    }
    assert Counter(i.state for i in items.values())["error"] == 5
    assert all(i.state != "pending" for i in items.values())
    progress = (await s.progress()).progress
    assert (progress.total, progress.finished) == (30, 30)
    failed = events(caplog, "drive.import.file_failed")
    assert len(failed) == 5
    one = next(f for f in failed if fields(f)["file_id"] == no_access_read.id)
    assert (fields(one)["step"], fields(one)["reason"]) == ("read", "no_access")
    assert fields(one)["mime_type"] == no_access_read.mime_type
    assert "secret store text" not in caplog.text
    for entry in by_category["google_docs"][:1] + by_category["pdf"][:1]:
        assert entry.name not in caplog.text
    [done] = events(caplog, "drive.import.finished")
    assert fields(done)["error"] == 5


async def test_a_deleted_or_failed_connection_ends_quietly_without_writes(
    drive_stack: DriveStack,
):
    """04 handoff: no ``fail_import`` for a vanished source; a ``failed`` source is not reopened."""

    s = drive_stack
    ghost = uuid.uuid4()
    failed = await s.connect("fake-synthetic-23")
    async with s.session_factory_tx() as session:
        await sources.set_connection_state(session, failed, "failed", failure_reason="no_access")
    async with s.worker():
        gone = await asyncio.wait_for((await s.start(ghost)).result(), 60)
        kept = await asyncio.wait_for((await s.start(failed)).result(), 60)
    assert gone.state == "stop" and kept.state == "stop"
    connection = await s.connection(failed)
    assert (connection.state, connection.failure_reason) == ("failed", "no_access")
    assert await s.items(failed) == []
    assert s.store.objects == {}


# --------------------------------------------------------- parse cap (HANDOFF risk)
class DistinctPdfs(FakeDriveGateway):
    def iter_download(self, file_id, *, chunk_size=8 * 1024 * 1024):
        item = self._item(file_id)
        if item.mime_type == "application/pdf":
            yield TINY_PDF + b"\n% " + file_id.encode() + b"\n"
        else:
            yield from super().iter_download(file_id, chunk_size=chunk_size)


async def test_at_most_two_child_parses_are_in_flight_per_import(
    drive_stack: DriveStack,
):
    """HANDOFF "parse head-of-line": default cap 2; other items keep importing."""

    s = drive_stack
    cid = await s.connect("fake-synthetic-30")
    gateway = DistinctPdfs(app_env="local", file_delay_s=0, role="worker")
    current = peak = calls = 0
    async with s.worker(gateway) as built:
        parse_activities = built.activities[0].__self__
        original = parse_activities._parse_document

        async def counted(data):
            nonlocal current, peak, calls
            current += 1
            calls += 1
            peak = max(peak, current)
            try:
                await asyncio.sleep(PARSE_WORK_S)
                return await original(data)
            finally:
                current -= 1

        parse_activities._parse_document = counted
        result = await asyncio.wait_for((await s.start(cid)).result(), 120)
    assert result.state == "done"
    assert calls == 5  # five distinct PDFs
    assert peak == 2
    docs = [d for d in await s.documents() if d.media_type == "application/pdf"]
    assert len(docs) == 5 and all(d.status == "parsed" for d in docs)


# -------------------------------------------------------------- kill and restart
async def test_killing_the_worker_mid_import_and_restarting_finishes_without_duplicates(
    drive_stack: DriveStack, tmp_path: Path
):
    """A SIGKILLed worker process, then a fresh worker: every file ends once, no extra rows or objects."""

    s = drive_stack
    store = DirObjectStore(tmp_path / "objects")
    cid = await s.connect("fake-synthetic-30")
    expected = expected_states(s.gateway(), "fake-synthetic-30")
    env = {
        **os.environ,
        "DRIVE_TEST_DATABASE": s.database.name,
        "DRIVE_TEST_QUEUE": s.queue,
        "DRIVE_TEST_STORE": str(store.root),
        "TEMPORAL_ADDRESS": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233").strip(),
        "TEMPORAL_NAMESPACE": "specflow-local",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    doomed = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "specflow_drive_import_support",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        handle = await s.start(cid)

        async def midway():
            assert doomed.returncode is None, "the doomed worker exited early"
            done = await s.finished_count(cid)
            return done >= 6 and done

        done_before = await wait_until(midway, max_wait_s=90, what="6 finished files")
        async with s.sessions() as session:
            in_flight = await session.scalar(
                select(func.count())
                .select_from(ConnectionItem)
                .where(
                    ConnectionItem.connection_id == cid,
                    ConnectionItem.state == "processing",
                )
            )
        doomed.kill()  # SIGKILL: no heartbeat, no cancellation
        await asyncio.wait_for(doomed.wait(), 10)
    finally:
        if doomed.returncode is None:
            doomed.kill()
    assert done_before < 30
    # crash recovery: files claimed (``processing``) at the kill are finished by the retry
    assert in_flight >= 1

    async with s.worker(s.gateway(delay=0.0), store=store):
        result = await asyncio.wait_for(handle.result(), 200)
    assert result.state == "done"
    items = await s.items(cid)
    assert {i.external_id: i.state for i in items} == expected  # one row per file, all final
    documents = await s.documents()
    assert len(documents) == len({d.content_hash for d in documents}) == 4
    stored = sum(1 for v in expected.values() if v == "stored")
    keys = store.object_keys()
    assert len(keys) == len(set(keys)) == stored + len(documents)
    assert sum(k.endswith("original.bin") for k in keys) == stored
    assert all(d.status == "parsed" for d in documents)
    connection = await s.connection(cid)
    assert (connection.state, connection.failure_reason) == ("done", None)


# ------------------------------------------------ review fixes (soft deadline, shared docs, CAN)
async def test_the_soft_deadline_is_checked_per_file_and_fails_the_import_midway(
    drive_stack: DriveStack,
):
    """Deadline passes during the import (3 s left, 1 s per file): ``fail_import`` still runs."""

    s = drive_stack
    cid = await s.connect("fake-synthetic-30")
    async with s.worker(s.gateway(delay=1.0)):
        handle = await s.client.start_workflow(
            DriveImportWorkflow.run,
            DriveImportInput(
                project_id=s.project_id,
                connection_id=cid,
                id_prefix=s.temporal.id_prefix,
                started_epoch=time.time() - 11 * 3600 + 3,
            ),
            id=f"{s.temporal.id_prefix}drive-import-{cid}",
            task_queue=s.queue,
        )
        s.temporal.track(handle)
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), 90)
    connection = await s.connection(cid)
    assert (connection.state, connection.failure_reason) == ("failed", "unknown_error")
    states = Counter((i.state, i.error_reason) for i in await s.items(cid))
    assert states[("error", "import_failed")] > 0  # the files never started
    assert sum(n for (st, _), n in states.items() if st != "error") > 0  # some finished first
    assert not any(i.state == "pending" for i in await s.items(cid))


async def test_content_held_by_another_source_is_already_in_project_without_waiting(
    drive_stack: DriveStack,
):
    """FR-SRC-0021 (Drive), SD6: a document another source holds, even one still parsing,
    ends the file as ``duplicate`` at once and links it; nothing is stored twice.
    A twin in the same folder (SD9) still shares the one stored file."""

    s = drive_stack
    doc_id = uuid.uuid4()
    async with s.session_factory_tx() as session:
        await sources.insert_connection_document(
            session,
            file=DocumentFile(
                document_id=doc_id,
                content_hash=hashlib.sha256(TINY_PDF).hexdigest(),
                filename="upload.pdf",
                size_bytes=len(TINY_PDF),
                original_key=f"projects/{s.project_id}/documents/{doc_id}/original.pdf",
            ),
            project_id=s.project_id,
            media_type="application/pdf",
            status="processing",
        )  # no holder: an orphan counts as another source
    cid = await s.connect("fake-synthetic-23")
    pdfs = sum(1 for i in s.gateway().list_all("fake-synthetic-23").items if i.mime_type == "application/pdf")
    assert pdfs >= 1
    async with s.worker():
        result = await asyncio.wait_for((await s.start(cid)).result(), 90)
    assert result.state == "done"
    items = await s.items(cid)
    linked = [i for i in items if i.document_id == doc_id]
    assert len(linked) == pdfs and all(i.state == "duplicate" for i in linked)
    assert not any(i.state in ("pending", "processing") for i in items)
    assert len(await s.documents()) == 1 + 3  # the held PDF + 3 distinct Google samples
    assert not any(k.endswith("original.pdf") for k in s.store.objects)


async def test_removing_a_source_during_an_import_fences_the_remaining_files(drive_stack: DriveStack, tmp_path: Path):
    """FR-SRC-0017, NFR-REL-0003: a worker process imports; ``db.removal.remove_source``
    removes the source mid-run; the worker goes on (it is killed only at the end) and
    stays fenced: no document row comes back, and only in-flight stored-only files
    (unfenced by design) may write one more object. The sweep then clears all of it.
    Not covered here: a worker that dies mid-file (see the SIGKILL test)."""

    s = drive_stack
    store = DirObjectStore(tmp_path / "objects")
    cid = await s.connect("fake-synthetic-30")
    env = {
        **os.environ,
        "DRIVE_TEST_DATABASE": s.database.name,
        "DRIVE_TEST_QUEUE": s.queue,
        "DRIVE_TEST_STORE": str(store.root),
        "TEMPORAL_ADDRESS": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233").strip(),
        "TEMPORAL_NAMESPACE": "specflow-local",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "specflow_drive_import_support",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        handle = await s.start(cid)

        async def midway():
            assert worker.returncode is None, "the worker exited early"
            done = await s.finished_count(cid)
            return done >= 6 and done

        await wait_until(midway, max_wait_s=90, what="6 finished files")
        async with s.session_factory_tx() as session:
            removed = await removal.remove_source(session, s.project_id, cid)
        for key in removed.object_keys:  # what the API deletes inline after the commit
            await store.delete(key)
        before = set(store.object_keys())
        # ends quietly, or fails when a child parse lost its document: both are fine
        with contextlib.suppress(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), 120)
    finally:
        worker.kill()
        await asyncio.wait_for(worker.wait(), 10)

    assert await s.documents() == []  # fenced: no row came back
    late = set(store.object_keys()) - before
    assert all(k.endswith("original.bin") for k in late)  # only stored-only in-flight
    async with s.sessions() as session:
        assert await sources.get_connection(session, s.project_id, cid) is None
    activities = SourceCleanupActivities(s.sessions, store)
    swept = await ActivityEnvironment().run(
        activities.sweep_source_objects,
        SweepInput(
            project_id=s.project_id,
            source_id=cid,
            document_ids=removed.sweep_document_ids,
            sweep=2,
        ),
    )
    assert swept.source_present is False
    leftovers = [k for k in store.object_keys() if str(s.project_id) in k]
    assert len(leftovers) <= len(late)  # at most the in-flight stored-only files remain


async def test_continue_as_new_carries_listed_and_the_start_time(temporal_live):
    """Forced continue-as-new (``max_batches_per_run=1``) with mocked activities, own queue."""

    calls = {"list": 0, "batch": 0, "item": 0, "complete": []}

    @activity.defn(name=wfm.LIST_ACTIVITY)
    async def list_(data: DriveImportInput) -> ListResult:
        calls["list"] += 1
        return ListResult(outcome="listed", files=3)

    @activity.defn(name=wfm.BATCH_ACTIVITY)
    async def batch(data: BatchRequest) -> BatchResult:
        calls["batch"] += 1
        if calls["batch"] > 3:
            return BatchResult()
        return BatchResult(pending=1, items=[BatchItem(item_id=uuid.uuid4(), kind="stored")])

    @activity.defn(name=wfm.ITEM_ACTIVITY)
    async def item(data: ImportItemInput) -> ImportItemResult:
        calls["item"] += 1
        return ImportItemResult(action="done")

    @activity.defn(name=wfm.COMPLETE_ACTIVITY)
    async def complete(data: CompleteInput) -> None:
        calls["complete"].append(data.started_epoch)

    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233").strip(),
        namespace="specflow-local",
        data_converter=pydantic_data_converter,
    )
    epoch = time.time() - 60
    async with Worker(
        client,
        task_queue=temporal_live.task_queue,
        workflows=[DriveImportWorkflow],
        activities=[list_, batch, item, complete],
    ):
        handle = await temporal_live.start(
            DriveImportWorkflow.run,
            DriveImportInput(
                project_id=uuid.uuid4(),
                connection_id=uuid.uuid4(),
                started_epoch=epoch,
                max_batches_per_run=1,
            ),
            workflow_id="can",
        )
        first_run = handle.first_execution_run_id or handle.result_run_id
        handle = client.get_workflow_handle(handle.id, result_type=ImportEnd)
        result = await asyncio.wait_for(handle.result(), 60)
    assert result.state == "done"
    assert calls["list"] == 1  # listed=True was carried across the restarts
    assert calls["batch"] == 4 and calls["item"] == 3
    assert calls["complete"] == [epoch]  # the same start time, so the soft deadline holds
    described = await handle.describe()
    assert first_run and described.run_id != first_run
