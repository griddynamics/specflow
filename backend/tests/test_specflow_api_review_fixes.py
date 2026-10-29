"""Tier 1: the phase-7 fixes of the session 04 review (M1 offline side, m1..m4, m7).

Requirements: NFR-REL-0003, NFR-PERF-0003, NFR-SEC-0002, NFR-MAIN-0002, INT-SRC-0003,
INT-SRC-0004. ``FakeBatches`` stands in for the database; what is tested is the router:
the bounds, the translation of commit-time errors and the order of the checks.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import OperationalError

from api import batch_files
from api.routers import source_removal
from db import batches, removal
from db.batches import BatchEndedError, SourceNotFoundError
from db.removal import Removal
from logger_harness import events, fields
from specflow_api_support import PDF
from test_specflow_api_batches import add, code, end, project, start
from test_specflow_api_remove import batch_of, delete, trace

DEADLOCK, TIMEOUT, FOREIGN_KEY = "40P01", "55P03", "23503"


class _OrigError(Exception):
    def __init__(self, sqlstate: str, text: str = "") -> None:
        super().__init__(text)
        self.sqlstate = sqlstate


def db_error(sqlstate: str, text: str = "") -> OperationalError:
    return OperationalError("statement", {}, _OrigError(sqlstate, text))


def fail_at_commit(harness, monkeypatch, module, name, errors):
    """After ``module.name`` ran, the next commit raises the next of ``errors``."""

    armed = {"on": False, "calls": 0}
    real_tx, real_call = harness.runtime.transaction, getattr(module, name)

    async def called(*args, **kwargs):
        armed["on"] = True
        armed["calls"] += 1
        return await real_call(*args, **kwargs)

    @asynccontextmanager
    async def tx():
        async with real_tx() as session:
            yield session
        if armed["on"] and errors:
            armed["on"] = False
            raise errors.pop(0)  # what the COMMIT raises

    monkeypatch.setattr(module, name, called)
    monkeypatch.setattr(harness.runtime, "transaction", tx)
    return armed


# ---- m7: translate is total ------------------------------------------------------------


def test_translate_answers_every_exception_with_an_api_error():
    assert batch_files.translate(SourceNotFoundError()).status == 404
    assert batch_files.translate(BatchEndedError()).status == 409
    other = batch_files.translate(ValueError("x"))
    assert (other.status, other.code) == (500, "internal_error")


# ---- m1: the cleanup start under the project lock is bounded ----------------------------


async def test_a_hanging_cleanup_start_times_out_rolls_back_and_is_503(harness, monkeypatch, caplog):
    pid = await project(harness)
    source = await batch_of(harness, pid, 2)
    order = trace(harness, monkeypatch)
    real_start = harness.temporal.start_workflow

    async def hanging(workflow, arg, **options):
        if workflow == "SourceCleanupWorkflow":
            await asyncio.Event().wait()
        return await real_start(workflow, arg, **options)

    monkeypatch.setattr(harness.temporal, "start_workflow", hanging)
    monkeypatch.setattr(source_removal, "CLEANUP_START_TIMEOUT", 0.1)
    with caplog.at_level("INFO"):
        response = await asyncio.wait_for(delete(harness, pid, source.id), 5)
    assert response.status_code == 503 and code(response) == "removal_unavailable"
    assert order[-1] == "rollback" and "commit" not in order
    assert fields(events(caplog, "api.source.remove_failed")[0])["outcome"] == "cleanup_start_failed"
    assert harness.store.objects  # nothing was deleted


def test_the_cleanup_start_bound_is_ten_seconds():
    assert source_removal.CLEANUP_START_TIMEOUT == 10.0


# ---- m2: errors raised at COMMIT are typed like the ones inside the transaction ----------


async def test_a_deadlock_at_commit_of_a_removal_is_retried_once(harness, monkeypatch):
    pid = await project(harness)
    source = await batch_of(harness, pid, 1)
    fail_at_commit(harness, monkeypatch, removal, "remove_source", [db_error(DEADLOCK)])
    await delete(harness, pid, source.id)
    assert harness.batches.calls.count("remove_source") == 2


@pytest.mark.parametrize(
    ("errors", "calls"),
    [
        ([db_error(DEADLOCK), db_error(DEADLOCK)], 2),  # the second conflict
        ([db_error(TIMEOUT)], 1),
        ([db_error(FOREIGN_KEY, "previous_revision")], 1),  # never retried
    ],
)
async def test_a_commit_error_of_a_removal_is_503_removal_unavailable(harness, monkeypatch, errors, calls):
    pid = await project(harness)
    source = await batch_of(harness, pid, 1)
    reparse_removal(monkeypatch, 0)  # the fake keeps no rollback: a fixed result, no rows
    seen = fail_at_commit(harness, monkeypatch, removal, "remove_source", errors)
    response = await delete(harness, pid, source.id)
    assert response.status_code == 503 and code(response) == "removal_unavailable"
    assert seen["calls"] == calls


async def test_a_plain_foreign_key_error_at_commit_is_a_conflict_retried_once(harness, monkeypatch):
    pid = await project(harness)
    source = await batch_of(harness, pid, 1)
    fail_at_commit(harness, monkeypatch, removal, "remove_source", [db_error(FOREIGN_KEY, "documents_fkey")])
    await delete(harness, pid, source.id)
    assert harness.batches.calls.count("remove_source") == 2


async def test_an_untyped_commit_error_stays_a_500(harness, monkeypatch):
    pid = await project(harness)
    source = await batch_of(harness, pid, 1)
    fail_at_commit(harness, monkeypatch, removal, "remove_source", [db_error("22P02")])
    assert (await delete(harness, pid, source.id)).status_code == 500


async def test_a_deadlock_at_commit_of_a_batch_start_is_retried_once(harness, monkeypatch):
    pid = await project(harness)
    fail_at_commit(harness, monkeypatch, batches, "start_batch", [db_error(DEADLOCK)])
    assert (await start(harness, pid)).status_code == 201
    assert harness.batches.calls.count("start_batch") == 2


@pytest.mark.parametrize("errors", [[db_error(TIMEOUT)], [db_error(DEADLOCK), db_error(DEADLOCK)]])
async def test_a_commit_error_of_a_batch_start_is_503_database_unavailable(harness, monkeypatch, errors):
    pid = await project(harness)
    fail_at_commit(harness, monkeypatch, batches, "start_batch", errors)
    response = await start(harness, pid)
    assert response.status_code == 503 and code(response) == "database_unavailable"


async def test_a_commit_error_of_an_add_is_503_and_the_streamed_object_is_deleted(harness, monkeypatch):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    fail_at_commit(harness, monkeypatch, batches, "add_batch_file", [db_error(TIMEOUT)])
    response = await add(harness, pid, sid)
    assert response.status_code == 503 and code(response) == "database_unavailable"
    assert not harness.store.objects


async def test_a_deadlock_at_commit_of_an_add_is_retried_once(harness, monkeypatch):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    fail_at_commit(harness, monkeypatch, batches, "add_batch_file", [db_error(DEADLOCK)])
    assert (await add(harness, pid, sid)).status_code in (200, 201)
    assert harness.batches.calls.count("add_batch_file") == 2


async def test_a_deadlock_at_commit_of_an_end_is_retried_once(harness, monkeypatch):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    await add(harness, pid, sid)
    fail_at_commit(harness, monkeypatch, batches, "end_batch", [db_error(DEADLOCK)])
    assert (await end(harness, pid, sid)).status_code == 200
    assert harness.batches.calls.count("end_batch") == 2


# ---- m3: the re-reads start concurrently inside the same budget --------------------------


def reparse_removal(monkeypatch, count, keys=()):
    ids = [uuid.uuid4() for _ in range(count)]

    async def remove(session, project_id, source_id):
        return Removal("gdrive", count, [], ids, [], list(keys), drive_import_open=False)

    monkeypatch.setattr(removal, "remove_source", remove)
    return ids


async def test_the_reparse_starts_run_concurrently_at_most_sixteen_at_once(harness, monkeypatch):
    pid = await project(harness)
    ids = reparse_removal(monkeypatch, 40)
    running = peak = 0
    real = harness.temporal.start_workflow

    async def slow(workflow, arg, **options):
        nonlocal running, peak
        if workflow == "ParseDocumentWorkflow":
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            running -= 1
        return await real(workflow, arg, **options)

    monkeypatch.setattr(harness.temporal, "start_workflow", slow)
    assert (await delete(harness, pid, uuid.uuid4())).status_code == 204
    started = [c["id"] for c in harness.temporal.calls if c["workflow"] == "ParseDocumentWorkflow"]
    assert sorted(started) == sorted(f"doc-parse-{i}" for i in ids)
    assert 1 < peak <= source_removal.REPARSE_CONCURRENCY == 16


async def test_hanging_reparse_starts_stop_at_the_budget_are_logged_and_the_answer_is_204(harness, monkeypatch, caplog):
    pid = await project(harness)
    key = "projects/p/documents/d/original.pdf"
    harness.store.objects[key] = PDF
    ids = reparse_removal(monkeypatch, 5, keys=[key])
    monkeypatch.setattr(source_removal, "DELETE_BUDGET_SECONDS", 0.2)

    async def hanging(workflow, arg, **options):
        await asyncio.Event().wait()

    async def stuck_delete(key):
        await asyncio.Event().wait()

    monkeypatch.setattr(harness.temporal, "start_workflow", hanging)
    monkeypatch.setattr(harness.store, "delete", stuck_delete)
    with caplog.at_level("INFO"):
        response = await asyncio.wait_for(delete(harness, pid, uuid.uuid4()), 5)
    assert response.status_code == 204
    [left] = events(caplog, "api.source.reparse_left")
    assert fields(left)["failed_count"] == 5
    assert set(fields(left)["document_ids"]) == {str(i) for i in ids}
    # the budget is shared: the deletes got what was left (nothing), the sweeps take the rest
    [objects] = events(caplog, "api.source.objects_left")
    assert fields(objects)["failed_count"] == 1
    assert len(events(caplog, "api.source.removed")) == 1


async def test_a_failing_reparse_start_does_not_stop_the_others(harness, monkeypatch):
    pid = await project(harness)
    ids = reparse_removal(monkeypatch, 3)
    real = harness.temporal.start_workflow
    bad = f"doc-parse-{ids[0]}"

    async def flaky(workflow, arg, **options):
        if options.get("id") == bad:
            raise RuntimeError("temporal down")
        return await real(workflow, arg, **options)

    monkeypatch.setattr(harness.temporal, "start_workflow", flaky)
    assert (await delete(harness, pid, uuid.uuid4())).status_code == 204
    started = [c["id"] for c in harness.temporal.calls if c["workflow"] == "ParseDocumentWorkflow"]
    assert sorted(started) == sorted(f"doc-parse-{i}" for i in ids[1:])


async def test_run_bounded_returns_the_items_that_did_not_complete():
    async def work(item):
        if item == "bad":
            raise OSError("x")
        if item == "slow":
            await asyncio.Event().wait()

    left = await source_removal.run_bounded(["ok", "bad", "slow"], work, budget=0.1, concurrency=3)
    assert left == ["bad", "slow"]
    assert await source_removal.run_bounded([], work, budget=0.1, concurrency=3) == []


# ---- m4: the batch is checked before the body is streamed --------------------------------


def watched_body():
    read: list[int] = []

    async def chunks():
        read.append(1)
        yield PDF

    return chunks(), read


async def test_an_unknown_batch_answers_404_without_reading_the_body(harness):
    pid = await project(harness)
    body, read = watched_body()
    response = await add(harness, pid, uuid.uuid4(), content=body)
    assert response.status_code == 404 and code(response) == "source_not_found"
    assert not read and not harness.store.objects


async def test_an_ended_batch_answers_409_without_reading_the_body(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    await add(harness, pid, sid)
    await end(harness, pid, sid)
    harness.store.objects.clear()
    body, read = watched_body()
    response = await add(harness, pid, sid, content=body)
    assert response.status_code == 409 and code(response) == "batch_ended"
    assert not read and not harness.store.objects


async def test_a_drive_source_answers_409_without_reading_the_body(harness):
    pid = await project(harness)
    drive = harness.batches.add_source(uuid.UUID(pid), kind="gdrive")
    body, read = watched_body()
    response = await add(harness, pid, drive.id, content=body)
    assert response.status_code == 409 and not read


async def test_another_projects_batch_answers_404_without_reading_the_body(harness):
    mine, theirs = await project(harness), await project(harness)
    sid = (await start(harness, theirs)).json()["source"]["id"]
    body, read = watched_body()
    response = await add(harness, mine, sid, content=body)
    assert response.status_code == 404 and not read


async def test_a_non_member_never_gets_to_the_batch_check(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    body, read = watched_body()
    response = await add(harness, pid, sid, content=body, email="mallory@example.com")
    assert response.status_code == 404 and code(response) == "project_not_found" and not read


async def test_an_open_batch_still_streams_and_adds(harness):
    pid = await project(harness)
    sid = (await start(harness, pid)).json()["source"]["id"]
    body, read = watched_body()
    response = await add(harness, pid, sid, content=body)
    assert response.status_code == 201 and read
