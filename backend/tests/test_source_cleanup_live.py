"""Tier 2 (``SPECFLOW_TEST_POSTGRES=1`` + ``SPECFLOW_TEST_TEMPORAL=1``): ``SourceCleanupWorkflow``.

Real local Temporal (``specflow-local``; a unique task queue and id prefix), a scratch
database and the in-memory object store. The two real sleeps (60 s, 44 min) are
shortened through the start arguments ``first_delay_s`` / ``second_delay_s`` (the
sandboxed workflow does not see patched module constants). Requirements: FR-SRC-0017,
NFR-REL-0003, R21, R07.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio import activity
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from db import sources
from db.sources import DocumentFile
from specflow_drive_import_support import (
    DriveStack,
)
from specflow_live import requires_live
from storage.keys import (
    document_original,
    document_original_bin,
    document_original_json,
)
from temporal import ids
from temporal.cleanup import workflows as wfm
from temporal.cleanup.models import SourceCleanupInput, SweepInput, SweepResult
from temporal.cleanup.workflows import SourceCleanupWorkflow

pytest_plugins = ["specflow_drive_import_support"]
pytestmark = [requires_live("POSTGRES"), requires_live("TEMPORAL")]


def keys_of(project_id: uuid.UUID, document_id: uuid.UUID) -> list[str]:
    return [
        document_original(str(project_id), str(document_id)),
        document_original_json(str(project_id), str(document_id)),
        document_original_bin(str(project_id), str(document_id)),
    ]


async def run_cleanup(s: DriveStack, source_id: uuid.UUID, document_ids, monkeypatch):
    removal = uuid.uuid4()
    async with s.worker():
        handle = await s.client.start_workflow(
            SourceCleanupWorkflow.run,
            SourceCleanupInput(
                project_id=s.project_id,
                source_id=source_id,
                document_ids=document_ids,
                id_prefix=s.temporal.id_prefix,
                first_delay_s=1,
                second_delay_s=1,
            ),
            id=ids.cleanup_workflow_id(removal, 0, prefix=s.temporal.id_prefix),
            task_queue=s.queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            execution_timeout=wfm.EXECUTION_TIMEOUT,
        )
        s.temporal.track(handle)
        return await asyncio.wait_for(handle.result(), 60)


async def test_a_removed_source_loses_the_objects_that_have_no_document_row(drive_stack: DriveStack, monkeypatch):
    s = drive_stack
    gone, held = uuid.uuid4(), uuid.uuid4()
    for document_id in (gone, held):
        for key in keys_of(s.project_id, document_id):
            s.store.objects[key] = b"x"
    async with s.session_factory_tx() as session:
        await sources.insert_connection_document(
            session,
            file=DocumentFile(
                document_id=held,
                content_hash="a" * 64,
                filename="kept.pdf",
                size_bytes=1,
                original_key=keys_of(s.project_id, held)[0],
            ),
            project_id=s.project_id,
            media_type="application/pdf",
            status="parsed",
        )

    result = await run_cleanup(s, uuid.uuid4(), [gone, held], monkeypatch)

    assert (result.source_present, result.deleted, result.kept) == (False, 1, 1)
    assert set(s.store.objects) == set(keys_of(s.project_id, held))


async def test_a_present_source_is_left_alone_by_both_sweeps(drive_stack: DriveStack, monkeypatch):
    s = drive_stack
    cid = await s.connect("fake-synthetic-23")  # the removal rolled back: the row exists
    orphan = uuid.uuid4()
    for key in keys_of(s.project_id, orphan):
        s.store.objects[key] = b"x"

    result = await run_cleanup(s, cid, [orphan], monkeypatch)

    assert (result.source_present, result.deleted, result.kept) == (True, 0, 1)
    assert len(s.store.objects) == 3


async def test_the_workflow_id_rejects_a_second_start_of_the_same_chunk(drive_stack: DriveStack, monkeypatch):
    s = drive_stack
    removal = uuid.uuid4()
    workflow_id = ids.cleanup_workflow_id(removal, 1, prefix=s.temporal.id_prefix)
    arg = SourceCleanupInput(
        project_id=s.project_id,
        source_id=uuid.uuid4(),
        id_prefix=s.temporal.id_prefix,
        first_delay_s=30,
    )
    first = await s.client.start_workflow(
        SourceCleanupWorkflow.run,
        arg,
        id=workflow_id,
        task_queue=s.queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )
    s.temporal.track(first)
    with pytest.raises(WorkflowAlreadyStartedError):
        await s.client.start_workflow(
            SourceCleanupWorkflow.run,
            arg,
            id=workflow_id,
            task_queue=s.queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    await first.terminate()


async def test_sweep_two_still_runs_when_sweep_one_fails_for_good(
    drive_stack: DriveStack,
):
    """A stub activity fails every attempt of sweep 1 (5 attempts, about 75 s of backoff)
    and answers sweep 2: the workflow must still run sweep 2 and return its result."""

    s = drive_stack
    seen: list[int] = []

    @activity.defn(name=wfm.SWEEP_ACTIVITY)
    async def stub(data: SweepInput) -> SweepResult:
        seen.append(data.sweep)
        if data.sweep == 1:
            raise RuntimeError("outage")
        return SweepResult(source_present=False, deleted=2, kept=0)

    worker = Worker(
        s.client,
        task_queue=s.queue,
        workflows=[SourceCleanupWorkflow],
        activities=[stub],
    )
    async with worker:
        handle = await s.client.start_workflow(
            SourceCleanupWorkflow.run,
            SourceCleanupInput(
                project_id=s.project_id,
                source_id=uuid.uuid4(),
                id_prefix=s.temporal.id_prefix,
                first_delay_s=1,
                second_delay_s=1,
            ),
            id=ids.cleanup_workflow_id(uuid.uuid4(), 0, prefix=s.temporal.id_prefix),
            task_queue=s.queue,
        )
        s.temporal.track(handle)
        result = await asyncio.wait_for(handle.result(), 180)
    assert result.deleted == 2
    assert seen[-1] == 2 and seen.count(1) == 5
