"""Tier 2 (``SPECFLOW_TEST_TEMPORAL=1``): the API's workflow starts against the REAL local Temporal.

Requirements: NFR-REL-0003, FR-SRC-0017, INT-SRC-0003. Review finding M1: the offline tests
use ``FakeTemporal``, so the payload of ``SourceCleanupWorkflow`` (pydantic data converter,
``list[UUID]``), the ``FAIL`` + ``REJECT_DUPLICATE`` policies and the ``terminate`` error
codes were never seen by a server. Here they are.

Every workflow id and task queue carries the test's unique ``pytest-<run>-`` prefix and no
worker polls the cleanup queue (the cleanup workflows stay in their first sleep and are
terminated at teardown); the owner's ``parse`` queue is never used.

Run (owner's local stack, gate H1(04)): see ``test_specflow_api_batches_live.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from api import workflows
from logger_harness import events, fields
from specflow_api_temporal_support import (
    api_runtime,
    connect_with_converter,
    handle_of,
    terminate_prefixed,
)
from specflow_live import requires_live
from specflow_quick_workflow import QuickWorkflow
from temporal.cleanup.workflows import WORKFLOW_NAME as CLEANUP_WORKFLOW
from temporal.ids import CLEANUP_CHUNK_SIZE, cleanup_workflow_id, drive_import_workflow_id
from temporal.queues import queue_name

pytestmark = requires_live("TEMPORAL")


@pytest_asyncio.fixture
async def real(temporal_live, valid_env):
    client = await connect_with_converter()
    runtime = api_runtime(client, temporal_live, valid_env)
    parse_queue = queue_name(runtime.settings.temporal, "parse", prefix=temporal_live.id_prefix)
    assert parse_queue != "parse" and parse_queue.startswith("pytest-")
    yield SimpleNamespace(client=client, runtime=runtime, live=temporal_live, parse_queue=parse_queue)
    await terminate_prefixed(client, temporal_live.id_prefix)


# ---- start_cleanup / start_cleanups ----------------------------------------------------


async def test_start_cleanup_starts_a_real_cleanup_workflow_with_the_ids_payload(real):
    removal_id, project_id, source_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    documents = [uuid.uuid4() for _ in range(3)]
    workflow_id = await workflows.start_cleanup(real.runtime, project_id, source_id, documents, removal_id=removal_id)
    assert workflow_id == cleanup_workflow_id(removal_id, 0, prefix=real.live.id_prefix)
    handle = handle_of(real.client, real.live, workflow_id)
    described = await handle.describe()
    assert described.status == WorkflowExecutionStatus.RUNNING
    assert described.workflow_type == CLEANUP_WORKFLOW
    assert described.task_queue == real.parse_queue


async def test_a_second_start_with_the_same_removal_id_is_rejected(real):
    removal_id = uuid.uuid4()
    args = (real.runtime, uuid.uuid4(), uuid.uuid4(), [uuid.uuid4()])
    workflow_id = await workflows.start_cleanup(*args, removal_id=removal_id)
    handle_of(real.client, real.live, workflow_id)
    # FAIL: a running one with the id is an error, not a second execution (the API turns
    # it into CleanupStartError -> rollback -> 503, and the retry uses a NEW removal id)
    with pytest.raises(WorkflowAlreadyStartedError):
        await workflows.start_cleanup(*args, removal_id=removal_id)
    # REJECT_DUPLICATE: a closed one with the id is rejected too
    await real.client.get_workflow_handle(workflow_id).terminate("pytest")
    with pytest.raises(WorkflowAlreadyStartedError):
        await workflows.start_cleanup(*args, removal_id=removal_id)
    # a new removal id (the retry of the API) is accepted
    again = await workflows.start_cleanup(*args, removal_id=uuid.uuid4())
    handle_of(real.client, real.live, again)
    described = await real.client.get_workflow_handle(again).describe()
    assert described.status == WorkflowExecutionStatus.RUNNING


async def test_start_cleanups_starts_one_real_workflow_per_chunk(real):
    removal_id = uuid.uuid4()
    documents = [uuid.uuid4() for _ in range(CLEANUP_CHUNK_SIZE + 1)]
    started = await workflows.start_cleanups(real.runtime, uuid.uuid4(), uuid.uuid4(), documents, removal_id)
    assert started == [cleanup_workflow_id(removal_id, number, prefix=real.live.id_prefix) for number in (0, 1)]
    for workflow_id in started:
        described = await handle_of(real.client, real.live, workflow_id).describe()
        assert described.status == WorkflowExecutionStatus.RUNNING
    assert (
        await workflows.start_cleanups(real.runtime, uuid.uuid4(), uuid.uuid4(), [], uuid.uuid4()) == []
    )  # no ids, no workflow


# ---- terminate_drive_import ------------------------------------------------------------


async def test_terminate_of_a_missing_workflow_does_not_raise_and_logs_not_running(real, caplog):
    caplog.set_level("INFO")
    await workflows.terminate_drive_import(real.runtime, uuid.uuid4())
    [record] = events(caplog, "api.source.drive_terminate")
    assert fields(record)["outcome"] == "not_running" and record.levelname == "INFO"


async def test_terminate_of_a_running_import_stops_it_and_logs_terminated(real, caplog):
    caplog.set_level("INFO")
    connection_id = uuid.uuid4()
    workflow_id = await workflows.start_drive_import(real.runtime, uuid.uuid4(), connection_id)
    assert workflow_id == drive_import_workflow_id(connection_id, prefix=real.live.id_prefix)
    handle = handle_of(real.client, real.live, workflow_id)
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING  # no worker: scheduled
    await workflows.terminate_drive_import(real.runtime, connection_id)
    assert (await handle.describe()).status == WorkflowExecutionStatus.TERMINATED
    [record] = events(caplog, "api.source.drive_terminate")
    assert fields(record)["outcome"] == "terminated" and record.levelname == "INFO"
    assert fields(record)["source_id"] == str(connection_id)


async def test_terminate_of_a_completed_workflow_does_not_raise_and_logs_not_running(real, caplog):
    """What Temporal answers for a closed workflow: the code assumes ``NOT_FOUND`` (review M1c)."""

    caplog.set_level("INFO")
    connection_id = uuid.uuid4()
    workflow_id = drive_import_workflow_id(connection_id, prefix=real.live.id_prefix)
    async with Worker(real.client, task_queue=real.live.task_queue, workflows=[QuickWorkflow]):
        handle = await real.client.start_workflow(QuickWorkflow.run, id=workflow_id, task_queue=real.live.task_queue)
        assert await handle.result() == "done"
    await workflows.terminate_drive_import(real.runtime, connection_id)
    [record] = events(caplog, "api.source.drive_terminate")
    assert fields(record)["outcome"] == "not_running", fields(record)  # else: record the code
    assert record.levelname == "INFO"
    assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED
