"""Tier 1: the source cleanup without Temporal or Postgres.

The workflow body runs only live (``test_source_cleanup_live.py``): a workflow test
environment may not be downloaded. Here: module hygiene, constants, the sweep
activity over a fake read model and the in-memory store, registration, and the
log fields. Requirements: FR-SRC-0017, NFR-REL-0003, NFR-MAIN-0002.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from db import source_files
from logger_harness import events, fields
from storage import ObjectStoreError
from storage.keys import (
    document_original,
    document_original_bin,
    document_original_json,
)
from storage.memory import InMemoryObjectStore
from temporal.cleanup import activities as acts
from temporal.cleanup import workflows as wf
from temporal.cleanup.models import SourceCleanupInput, SweepInput
from temporal.documents import registration
from temporal.sources import workflows as imports

PID, SID = uuid.uuid4(), uuid.uuid4()


# ------------------------------------------------------------------- hygiene
def test_the_workflow_module_imports_no_infrastructure_packages():
    code = (
        "import sys\n"
        "import temporal.cleanup.workflows\n"
        "bad = [m for m in ('sqlalchemy', 'asyncpg', 'google.cloud', 'pymupdf',"
        " 'settings', 'db', 'storage', 'drive')"
        " if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_the_workflow_is_named_and_sweeps_at_one_and_forty_five_minutes():
    definition = workflow._Definition.must_from_class(wf.SourceCleanupWorkflow)
    assert definition.name == "SourceCleanupWorkflow"
    assert wf.SWEEP_ACTIVITY == "sweep_source_objects"
    assert timedelta(seconds=60) == wf.FIRST_SWEEP_DELAY
    assert timedelta(minutes=44) == wf.SECOND_SWEEP_DELAY
    # R21: the second sweep is after the 30 min file limit of an import
    assert wf.FIRST_SWEEP_DELAY + wf.SECOND_SWEEP_DELAY > imports.ITEM_START_TO_CLOSE
    assert timedelta(minutes=10) == wf.SWEEP_START_TO_CLOSE
    assert wf.SWEEP_RETRY.maximum_attempts == 5
    # worst case 60 s + 44 min + 2 sweeps x 5 attempts x 10 min fits the timeout
    worst = wf.FIRST_SWEEP_DELAY + wf.SECOND_SWEEP_DELAY + 2 * wf.SWEEP_RETRY.maximum_attempts * wf.SWEEP_START_TO_CLOSE
    assert wf.EXECUTION_TIMEOUT == timedelta(hours=3) > worst


def test_the_input_defaults_are_the_production_delays():
    data = SourceCleanupInput(project_id=PID, source_id=SID)
    assert (data.first_delay_s, data.second_delay_s) == (60, 44 * 60)
    assert data.first_delay_s == wf.FIRST_SWEEP_DELAY.total_seconds()
    assert data.second_delay_s == wf.SECOND_SWEEP_DELAY.total_seconds()


def test_models_carry_ids_and_counts_only():
    names = set(SweepInput.model_fields) | set(SourceCleanupInput.model_fields)
    assert names == {
        "project_id",
        "source_id",
        "document_ids",
        "id_prefix",
        "first_delay_s",
        "second_delay_s",
        "sweep",
    }
    with pytest.raises(ValueError, match="sweep"):
        SweepInput(project_id=PID, source_id=SID, sweep=3)


def test_registration_adds_the_workflow_and_the_activity(monkeypatch):
    pool = ThreadPoolExecutor(1)
    try:
        reg = registration.build_registration(
            SimpleNamespace(dispose=None),
            InMemoryObjectStore(app_env="local"),
            executor=pool,
            drive_executor=pool,
        )
    finally:
        pool.shutdown(wait=False)
    assert wf.SourceCleanupWorkflow in reg.workflows
    names = {activity._Definition.must_from_callable(fn).name for fn in reg.activities}
    assert wf.SWEEP_ACTIVITY in names


# --------------------------------------------------------------- the activity
class Session:
    pass


class Sessions:
    def __call__(self):
        @contextlib.asynccontextmanager
        async def manager():
            yield Session()

        return manager()


@pytest.fixture
def store():
    return InMemoryObjectStore(app_env="local")


def keys_of(document_id):
    return [
        document_original(str(PID), str(document_id)),
        document_original_json(str(PID), str(document_id)),
        document_original_bin(str(PID), str(document_id)),
    ]


def seed(store, document_ids):
    for document_id in document_ids:
        for key in keys_of(document_id):
            store.objects[key] = b"x"


def fake_read_model(monkeypatch, *, present, missing):
    calls = []

    async def sweep_candidates(session, project_id, source_id, document_ids):
        calls.append((project_id, source_id, list(document_ids)))
        return present, [i for i in document_ids if i in missing]

    monkeypatch.setattr(source_files, "sweep_candidates", sweep_candidates)
    return calls


async def sweep(activities, document_ids, number=1):
    arg = SweepInput(project_id=PID, source_id=SID, document_ids=document_ids, sweep=number)
    return await ActivityEnvironment().run(activities.sweep_source_objects, arg)


async def test_a_present_source_is_a_no_op(monkeypatch, store, caplog):
    caplog.set_level("INFO")
    ids = [uuid.uuid4() for _ in range(3)]
    seed(store, ids)
    calls = fake_read_model(monkeypatch, present=True, missing=set(ids))
    activities = acts.SourceCleanupActivities(Sessions(), store)

    result = await sweep(activities, ids)

    assert (result.source_present, result.deleted, result.kept) == (True, 0, 3)
    assert len(store.objects) == 9  # nothing deleted
    assert calls == [(PID, SID, ids)]
    [logged] = events(caplog, "source.cleanup.swept")
    assert fields(logged) == {
        "event": "source.cleanup.swept",
        "project_id": str(PID),
        "source_id": str(SID),
        "sweep": 1,
        "deleted": 0,
        "kept": 3,
        "source_present": True,
    }


async def test_ids_with_a_document_row_keep_their_objects(monkeypatch, store):
    ids = [uuid.uuid4() for _ in range(2)]
    seed(store, ids)
    fake_read_model(monkeypatch, present=False, missing=set())  # rows everywhere
    activities = acts.SourceCleanupActivities(Sessions(), store)

    result = await sweep(activities, ids)

    assert (result.source_present, result.deleted, result.kept) == (False, 0, 2)
    assert len(store.objects) == 6


async def test_ids_without_a_row_lose_the_pdf_json_and_bin_objects(monkeypatch, store, caplog):
    caplog.set_level("INFO")
    gone, kept = uuid.uuid4(), uuid.uuid4()
    seed(store, [gone, kept])
    fake_read_model(monkeypatch, present=False, missing={gone})
    activities = acts.SourceCleanupActivities(Sessions(), store)
    deletes = []
    original = store.delete

    async def counting_delete(key):
        deletes.append(key)
        await original(key)

    monkeypatch.setattr(store, "delete", counting_delete)

    result = await sweep(activities, [gone, kept], number=2)

    assert sorted(deletes) == sorted(keys_of(gone))  # 3 deletes, for the missing id
    assert set(store.objects) == set(keys_of(kept))
    assert (result.deleted, result.kept) == (1, 1)
    [logged] = events(caplog, "source.cleanup.swept")
    assert fields(logged)["sweep"] == 2 and fields(logged)["deleted"] == 1


async def test_a_sweep_is_idempotent_and_survives_missing_objects(monkeypatch, store):
    ids = [uuid.uuid4()]
    fake_read_model(monkeypatch, present=False, missing=set(ids))
    activities = acts.SourceCleanupActivities(Sessions(), store)
    first = await sweep(activities, ids)  # nothing stored: delete is idempotent
    second = await sweep(activities, ids, number=2)
    assert first == second and first.deleted == 1


async def test_many_ids_are_deleted_within_the_concurrency_bound(monkeypatch, store):
    ids = [uuid.uuid4() for _ in range(200)]
    seed(store, ids)
    fake_read_model(monkeypatch, present=False, missing=set(ids))
    active, peak = 0, 0
    original = store.delete

    async def tracked(key):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await original(key)
        active -= 1

    monkeypatch.setattr(store, "delete", tracked)
    activities = acts.SourceCleanupActivities(Sessions(), store)
    result = await sweep(activities, ids)
    assert result.deleted == 200 and store.objects == {}
    assert peak <= acts.DELETE_CONCURRENCY


async def test_a_store_failure_is_a_fixed_retryable_code_without_key_text(monkeypatch, store, caplog):
    ids = [uuid.uuid4()]
    fake_read_model(monkeypatch, present=False, missing=set(ids))

    async def boom(key):
        raise ObjectStoreError(f"cannot delete {key}")

    monkeypatch.setattr(store, "delete", boom)
    activities = acts.SourceCleanupActivities(Sessions(), store)
    with pytest.raises(ApplicationError) as raised:
        await sweep(activities, ids)
    assert (raised.value.message, raised.value.type) == (
        "internal_error",
        "SourceCleanupError",
    )
    assert not raised.value.non_retryable
    assert str(ids[0]) not in caplog.text
    [logged] = events(caplog, "source.cleanup.failed")
    assert fields(logged)["error_type"] == "ObjectStoreError"
