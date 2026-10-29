"""Tier 1: ``DELETE /projects/{pid}/sources/{sid}`` (INT-SRC-0003, SPECS 4.3 and 5).

Requirements: INT-SRC-0003, FR-SRC-0016/0017/0019, NFR-REL-0003, NFR-PERF-0003,
NFR-SEC-0002, NFR-MAIN-0002. The removal rows are ``FakeBatches`` (the real hand-over
and key rules are covered by ``test_db_removal_*``); what is tested here is the router:
the order of the steps, the retry, the budget and the answers.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from temporalio.common import WorkflowIDReusePolicy

from api import workflows
from api.routers import source_removal
from db import removal
from db.batches import LockTimeoutError
from db.removal import Removal, RevisionChainError
from logger_harness import events, fields
from specflow_api_support import PDF, bearer
from test_specflow_api_batches import code, end, project, start, url

ALICE = "alice@example.com"


async def delete(harness, pid, sid, email=ALICE):
    return await harness.client.delete(url(pid, f"/{sid}"), headers=bearer(email))


def put_doc(harness, pid, source, name, *, status="parsed", content=None):
    """A document held by ``source`` with its object in the store and an item for it."""

    did = uuid.uuid4()
    key = f"projects/{pid}/documents/{did}/original.pdf"
    doc = harness.repo.documents[did] = SimpleNamespace(
        id=did,
        project_id=uuid.UUID(pid),
        content_hash=content or str(did),
        original_filename=name,
        size_bytes=5,
        original_key=key,
        uploaded_by=None,
        status=status,
        error=None,
        markdown=None,
        summarization=None,
        summarization_status="none",
        summarization_model=None,
        summarization_error=None,
        summarized_at=None,
        created_at=None,
        media_type="application/pdf",
        source_id=source.id,
        updated_at=datetime.now(UTC),
    )
    harness.store.objects[key] = PDF
    harness.batches.add_item(source, name, document=doc)
    return doc


async def batch_of(harness, pid, files=3):
    """An ended upload batch with ``files`` parsed documents (through the API)."""

    sid = (await start(harness, pid, files)).json()["source"]["id"]
    source = harness.batches.sources[uuid.UUID(sid)]
    for n in range(files):
        put_doc(harness, pid, source, f"f{n}.pdf")
    await end(harness, pid, sid)
    return source


def trace(harness, monkeypatch):
    """Record the order of the steps around the commit."""

    order: list[str] = []
    armed = {"on": False}  # the membership check's own transaction is not part of the order
    runtime, temporal, store = harness.runtime, harness.temporal, harness.store
    real_tx = runtime.transaction

    @asynccontextmanager
    async def tx():
        async with real_tx() as session:
            try:
                yield session
            except BaseException:
                if armed["on"]:
                    order.append("rollback")
                raise
        if armed["on"]:
            order.append("commit")

    start_workflow, handle, delete_object = temporal.start_workflow, temporal.get_workflow_handle, store.delete

    async def started(workflow, arg, **options):
        order.append(f"start:{workflow}")
        return await start_workflow(workflow, arg, **options)

    def handled(workflow_id, **kw):
        found = handle(workflow_id, **kw)
        real = found.terminate

        async def terminate(*a, **k):
            order.append("terminate")
            return await real(*a, **k)

        found.terminate = terminate
        return found

    async def deleted(key):
        order.append("delete")
        return await delete_object(key)

    remove_source = harness.batches.remove_source

    async def removing(*a, **k):
        armed["on"] = True
        order.append("db")
        return await remove_source(*a, **k)

    monkeypatch.setattr(runtime, "transaction", tx)
    monkeypatch.setattr(temporal, "start_workflow", started)
    monkeypatch.setattr(temporal, "get_workflow_handle", handled)
    monkeypatch.setattr(store, "delete", deleted)

    monkeypatch.setattr(removal, "remove_source", removing)
    return order


# ---- success ------------------------------------------------------------------------


async def test_removing_a_batch_is_204_and_leaves_no_row_or_object(harness, caplog):
    pid = await project(harness)
    source = await batch_of(harness, pid, 3)
    keys = {d.original_key for d in harness.repo.documents.values()}
    assert keys <= set(harness.store.objects)
    with caplog.at_level("INFO"):
        response = await delete(harness, pid, source.id)
    assert response.status_code == 204 and response.content == b""
    assert not harness.repo.documents and not harness.store.objects
    assert source.id not in harness.batches.sources and not harness.batches.items
    assert harness.batches.removed[uuid.UUID(pid)] == 3  # FR-SRC-0018: the counter
    status = (await harness.client.get(url(pid, "?batches=1"), headers=bearer())).json()
    assert (status["sources"], status["removed_files"]) == ([], 3)
    [started] = events(caplog, "api.source.remove_started")
    [done] = events(caplog, "api.source.removed")
    assert set(fields(started)) >= {"user_id", "project_id", "source_id"}
    logged = fields(done)
    assert (logged["kind"], logged["file_count"], logged["handed_over"]) == ("upload", 3, 0)
    assert logged["outcome"] == "removed" and "duration_ms" in logged
    assert logged["source_id"] == str(source.id) and logged["project_id"] == pid


async def test_the_cleanup_workflow_starts_with_ids_only_before_the_commit(harness, monkeypatch):
    pid = await project(harness)
    source = await batch_of(harness, pid, 2)
    doc_ids = {d.id for d in harness.repo.documents.values()}
    order = trace(harness, monkeypatch)
    assert (await delete(harness, pid, source.id)).status_code == 204
    [call] = [c for c in harness.temporal.calls if c["workflow"] == "SourceCleanupWorkflow"]
    assert call["id"].startswith("source-cleanup-") and call["id"].endswith("-0")
    assert call["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert call["task_queue"].endswith("parse") or "parse" in call["task_queue"]
    arg = call["arg"]
    assert (arg.project_id, arg.source_id) == (uuid.UUID(pid), source.id)
    assert set(arg.document_ids) == doc_ids
    assert order.index("start:SourceCleanupWorkflow") < order.index("commit")
    assert order[0] == "db"


async def test_the_after_commit_steps_run_in_the_spec_order(harness, monkeypatch):
    """R09: terminate the import -> start the re-reads -> delete the objects -> 204."""

    pid = await project(harness)
    drive = harness.batches.add_source(uuid.UUID(pid), kind="gdrive")
    other = harness.batches.add_source(uuid.UUID(pid), kind="gdrive")
    gone = put_doc(harness, pid, drive, "gone.pdf")
    shared = put_doc(harness, pid, drive, "shared.pdf", status="uploaded")
    harness.batches.add_item(other, "shared.pdf", document=shared)  # the second lister
    harness.temporal.calls.append({"workflow": "DriveImportWorkflow", "id": f"drive-import-{drive.id}"})
    order = trace(harness, monkeypatch)
    response = await delete(harness, pid, drive.id)
    assert response.status_code == 204
    assert order == [
        "db",
        "start:SourceCleanupWorkflow",
        "commit",
        "terminate",
        "start:ParseDocumentWorkflow",
        "delete",
    ]
    assert harness.temporal.terminated == [f"drive-import-{drive.id}"]
    assert gone.original_key not in harness.store.objects
    assert shared.original_key in harness.store.objects  # handed over: its object survives
    assert (shared.source_id, shared.status) == (other.id, "processing")
    assert [c["id"] for c in harness.temporal.calls if c["workflow"] == "ParseDocumentWorkflow"] == [
        f"doc-parse-{shared.id}"
    ]


async def test_an_upload_batch_terminates_nothing(harness):
    pid = await project(harness)
    source = await batch_of(harness, pid, 1)
    assert (await delete(harness, pid, source.id)).status_code == 204
    assert harness.temporal.terminated == []


@pytest.mark.parametrize("failure", ["terminate", "not_running"])
async def test_a_failed_or_missing_terminate_never_changes_the_answer(harness, caplog, failure):
    pid = await project(harness)
    drive = harness.batches.add_source(uuid.UUID(pid), kind="gdrive")
    put_doc(harness, pid, drive, "a.pdf")
    if failure == "terminate":
        harness.temporal.calls.append({"workflow": "DriveImportWorkflow", "id": f"drive-import-{drive.id}"})
        harness.temporal.terminate_error = RuntimeError("temporal down")
    with caplog.at_level("INFO"):
        response = await delete(harness, pid, drive.id)
    assert response.status_code == 204 and not harness.repo.documents
    [record] = events(caplog, "api.source.drive_terminate")
    assert fields(record)["outcome"] == ("failed" if failure == "terminate" else "not_running")
    assert not harness.store.objects  # the objects were still deleted


async def test_a_reparse_start_that_fails_marks_the_document_failed_and_still_204(harness):
    pid = await project(harness)
    drive = harness.batches.add_source(uuid.UUID(pid), kind="gdrive")
    other = harness.batches.add_source(uuid.UUID(pid), kind="gdrive")
    shared = put_doc(harness, pid, drive, "shared.pdf", status="failed")
    harness.batches.add_item(other, "shared.pdf", document=shared)
    # the cleanup workflow has no ids here (the document is handed over), so the start
    # failure only hits the parse
    harness.temporal.fail = RuntimeError("temporal down")
    response = await delete(harness, pid, drive.id)
    assert response.status_code == 204
    assert (shared.status, shared.error) == ("failed", "workflow_start_failed")


# ---- the 20 second budget -------------------------------------------------------------


def test_the_budget_and_the_concurrency_are_the_specified_numbers():
    assert source_removal.DELETE_BUDGET_SECONDS == 20.0 and source_removal.DELETE_CONCURRENCY == 16


async def test_slow_deletes_stop_at_the_budget_log_the_rest_and_answer_204(harness, monkeypatch, caplog):
    pid = await project(harness)
    source = await batch_of(harness, pid, 6)
    monkeypatch.setattr(source_removal, "DELETE_BUDGET_SECONDS", 0.2)
    real = harness.store.delete
    slow = sorted(d.original_key for d in harness.repo.documents.values())[:2]

    async def delete_object(key):
        if key in slow:
            await asyncio.Event().wait()  # never ends inside the budget
        await real(key)

    monkeypatch.setattr(harness.store, "delete", delete_object)
    with caplog.at_level("INFO"):
        response = await asyncio.wait_for(delete(harness, pid, source.id), 5)
    assert response.status_code == 204
    assert set(harness.store.objects) == set(slow)
    [left] = events(caplog, "api.source.objects_left")
    assert (fields(left)["failed_count"], fields(left)["source_id"]) == (2, str(source.id))
    assert source.id not in harness.batches.sources  # the rows are gone: the sweeps take the rest
    assert len(events(caplog, "api.source.removed")) == 1


async def test_a_failing_object_delete_is_counted_not_raised(harness, monkeypatch, caplog):
    pid = await project(harness)
    source = await batch_of(harness, pid, 3)
    real = harness.store.delete
    first = min(d.original_key for d in harness.repo.documents.values())

    async def delete_object(key):
        if key == first:
            raise OSError("gcs 503")
        await real(key)

    monkeypatch.setattr(harness.store, "delete", delete_object)
    with caplog.at_level("INFO"):
        assert (await delete(harness, pid, source.id)).status_code == 204
    [left] = events(caplog, "api.source.objects_left")
    assert fields(left)["failed_count"] == 1 and "gcs 503" not in caplog.text


async def test_deletes_run_sixteen_at_a_time(harness):
    running = peak = 0

    class Store:
        async def delete(self, _key):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            running -= 1

    left = await source_removal.delete_objects(Store(), [f"k{n}" for n in range(60)], budget=5, concurrency=16)
    assert (left, peak) == (0, 16)
    assert await source_removal.delete_objects(Store(), [], budget=1, concurrency=16) == 0


# ---- errors ----------------------------------------------------------------------------


async def test_an_unknown_source_is_404_source_not_found(harness, caplog):
    pid = await project(harness)
    with caplog.at_level("INFO"):
        response = await delete(harness, pid, uuid.uuid4())
    assert response.status_code == 404 and code(response) == "source_not_found"
    assert fields(events(caplog, "api.source.remove_failed")[0])["outcome"] == "not_found"


async def test_two_concurrent_deletes_give_one_204_and_one_404(harness):
    pid = await project(harness)
    source = await batch_of(harness, pid, 2)
    first, second = await asyncio.gather(delete(harness, pid, source.id), delete(harness, pid, source.id))
    assert sorted([first.status_code, second.status_code]) == [204, 404]
    failed = first if first.status_code == 404 else second
    assert code(failed) == "source_not_found"
    assert harness.batches.removed[uuid.UUID(pid)] == 2  # counted once


@pytest.mark.parametrize(
    ("error", "outcome"),
    [(LockTimeoutError("slow"), "lock_timeout"), (RevisionChainError("fk"), "revision_fk")],
)
async def test_a_lock_timeout_or_a_revision_chain_is_503_removal_unavailable_without_retry(
    harness, caplog, error, outcome
):
    pid = await project(harness)
    source = await batch_of(harness, pid, 1)
    harness.batches.raises["remove_source"] = [error, error]
    with caplog.at_level("INFO"):
        response = await delete(harness, pid, source.id)
    assert response.status_code == 503 and code(response) == "removal_unavailable"
    assert harness.batches.calls.count("remove_source") == 1  # R14: the FK is never retried
    assert fields(events(caplog, "api.source.remove_failed")[0])["outcome"] == outcome
    assert not events(caplog, "api.source.removed")
    assert source.id in harness.batches.sources  # unchanged


async def test_a_lock_conflict_is_retried_once(harness, caplog):
    pid = await project(harness)
    source = await batch_of(harness, pid, 1)
    harness.batches.conflicts = 1
    with caplog.at_level("INFO"):
        response = await delete(harness, pid, source.id)
    assert response.status_code == 204
    assert harness.batches.calls.count("remove_source") == 2
    assert not events(caplog, "api.source.remove_failed")


async def test_a_second_lock_conflict_is_503_removal_unavailable(harness, caplog):
    pid = await project(harness)
    source = await batch_of(harness, pid, 1)
    harness.batches.conflicts = 2
    with caplog.at_level("INFO"):
        response = await delete(harness, pid, source.id)
    assert response.status_code == 503 and code(response) == "removal_unavailable"
    assert harness.batches.calls.count("remove_source") == 2
    assert fields(events(caplog, "api.source.remove_failed")[0])["outcome"] == "error"


async def test_a_cleanup_start_failure_rolls_back_and_does_nothing_after(harness, monkeypatch, caplog):
    pid = await project(harness)
    drive = harness.batches.add_source(uuid.UUID(pid), kind="gdrive")
    put_doc(harness, pid, drive, "a.pdf")
    harness.temporal.fail = RuntimeError("temporal down")
    order = trace(harness, monkeypatch)
    with caplog.at_level("INFO"):
        response = await delete(harness, pid, drive.id)
    assert response.status_code == 503 and code(response) == "removal_unavailable"
    # no terminate, no re-read, no object delete after the rollback
    assert order == ["db", "start:SourceCleanupWorkflow", "rollback"]
    assert fields(events(caplog, "api.source.remove_failed")[0])["outcome"] == "cleanup_start_failed"
    assert "temporal down" not in caplog.text and harness.temporal.terminated == []


async def test_a_large_source_starts_one_cleanup_workflow_per_chunk_of_5000_ids(harness, monkeypatch):

    pid = await project(harness)
    ids = [uuid.uuid4() for _ in range(12_000)]

    async def big(session, project_id, source_id):
        return Removal("gdrive", 12_000, [], [], ids, [], drive_import_open=False)

    monkeypatch.setattr(removal, "remove_source", big)
    assert (await delete(harness, pid, uuid.uuid4())).status_code == 204
    calls = [c for c in harness.temporal.calls if c["workflow"] == "SourceCleanupWorkflow"]
    assert [len(c["arg"].document_ids) for c in calls] == [5000, 5000, 2000]
    assert [c["id"].rsplit("-", 1)[1] for c in calls] == ["0", "1", "2"]
    assert len({c["id"].rsplit("-", 1)[0] for c in calls}) == 1  # one removal id
    assert [i for c in calls for i in c["arg"].document_ids] == ids


async def test_a_failing_second_chunk_rolls_back_the_removal(harness, monkeypatch):

    pid = await project(harness)
    ids = [uuid.uuid4() for _ in range(6000)]

    async def big(session, project_id, source_id):
        return Removal("upload", 6000, [], [], ids, [], drive_import_open=False)

    monkeypatch.setattr(harness.batches, "remove_source", big)
    order = trace(harness, monkeypatch)
    real = harness.temporal.start_workflow
    seen = []

    async def flaky(workflow, arg, **options):
        seen.append(workflow)
        if len(seen) == 2:
            raise RuntimeError("second chunk")
        return await real(workflow, arg, **options)

    monkeypatch.setattr(harness.temporal, "start_workflow", flaky)
    response = await delete(harness, pid, uuid.uuid4())
    assert response.status_code == 503 and code(response) == "removal_unavailable"
    assert "rollback" in order and "commit" not in order


async def test_an_unexpected_error_is_500_and_an_unreachable_database_503(harness, caplog):
    pid = await project(harness)
    source = await batch_of(harness, pid, 1)
    harness.batches.raises["remove_source"] = [RuntimeError("boom"), ConnectionError("db gone")]
    with caplog.at_level("INFO"):
        crashed = await delete(harness, pid, source.id)
        unreachable = await delete(harness, pid, source.id)
    assert crashed.status_code == 500 and code(crashed) == "internal_error"
    assert unreachable.status_code == 503 and code(unreachable) == "database_unavailable"
    assert [fields(r)["outcome"] for r in events(caplog, "api.source.remove_failed")] == ["error", "error"]


# ---- membership, cross-project, logs ----------------------------------------------------


async def test_a_non_member_and_another_projects_id_cannot_remove(harness):
    mine, theirs = await project(harness), await project(harness)
    source = await batch_of(harness, theirs, 2)
    wrong_project = await delete(harness, mine, source.id)
    assert wrong_project.status_code == 404 and code(wrong_project) == "source_not_found"
    await project(harness, "bob@example.com")
    stranger = await delete(harness, theirs, source.id, "bob@example.com")
    assert stranger.status_code == 404 and code(stranger) == "project_not_found"
    assert source.id in harness.batches.sources and len(harness.repo.documents) == 2
    assert len(harness.store.objects) == 2 and not harness.batches.removed
    assert not [c for c in harness.temporal.calls if c["workflow"] == "SourceCleanupWorkflow"]


async def test_the_removal_logs_carry_no_name_or_email(harness, caplog):
    pid = await project(harness, "carol.secret@example.com")
    source = harness.batches.add_source(uuid.UUID(pid), kind="gdrive", name="Confidential Folder")
    put_doc(harness, pid, source, "Secret Plan.pdf")
    harness.temporal.calls.append({"workflow": "DriveImportWorkflow", "id": f"drive-import-{source.id}"})
    with caplog.at_level("DEBUG"):
        response = await delete(harness, pid, source.id, "carol.secret@example.com")
    assert response.status_code == 204
    assert "Confidential" not in caplog.text and "Secret Plan" not in caplog.text
    assert "carol.secret" not in caplog.text
    cleanup = [c for c in harness.temporal.calls if c["workflow"] == "SourceCleanupWorkflow"]
    assert "Secret" not in repr(cleanup[0]["arg"])  # the workflow input holds ids only


# ---- API helpers -------------------------------------------------------------------------


async def test_start_cleanup_refuses_more_than_one_chunk(harness):
    with pytest.raises(ValueError, match="at most one chunk"):
        await workflows.start_cleanup(
            harness.runtime,
            uuid.uuid4(),
            uuid.uuid4(),
            [uuid.uuid4() for _ in range(5001)],
            removal_id=uuid.uuid4(),
        )


async def test_start_cleanups_without_ids_starts_nothing(harness):
    assert await workflows.start_cleanups(harness.runtime, uuid.uuid4(), uuid.uuid4(), [], uuid.uuid4()) == []
    assert harness.temporal.calls == []
