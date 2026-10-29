"""Start the parse and summarize workflows by NAME (only the workflow timeouts are imported).

Status ownership (AN 3.3): the caller has already written the "started" state;
here only the start call happens, with ``USE_EXISTING`` so a repeat click
returns the running handle.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from api.contained import Contained
from api.errors import required
from api.runtime import Runtime
from db.sources import DRIVE_IMPORT_EXECUTION_TIMEOUT
from logger.logger import INFO, WARNING, get_logger, log_event
from temporal.agents.models import SummarizeDocumentInput
from temporal.agents.workflows import (
    EXECUTION_TIMEOUT as SUMMARIZE_EXECUTION_TIMEOUT,
)
from temporal.cleanup.models import SourceCleanupInput
from temporal.cleanup.workflows import (
    EXECUTION_TIMEOUT as CLEANUP_EXECUTION_TIMEOUT,
)
from temporal.cleanup.workflows import (
    WORKFLOW_NAME as CLEANUP_WORKFLOW,
)
from temporal.documents.models import ParseDocumentInput
from temporal.documents.workflows import (
    EXECUTION_TIMEOUT as PARSE_EXECUTION_TIMEOUT,
)
from temporal.ids import (
    CLEANUP_CHUNK_SIZE,
    chunk_ids,
    cleanup_workflow_id,
    drive_import_workflow_id,
    parse_workflow_id,
    summary_workflow_id,
)
from temporal.queues import queue_name
from temporal.sources.models import DriveImportInput
from temporal.sources.workflows import WORKFLOW_NAME as DRIVE_IMPORT_WORKFLOW

logger = get_logger("api.workflows")

PARSE_WORKFLOW: Final = "ParseDocumentWorkflow"
SUMMARIZE_WORKFLOW: Final = "SummarizeDocumentWorkflow"


class CleanupChunkError(ValueError):
    """A cleanup workflow was asked to take more ids than one chunk holds."""


async def start_parse(runtime: Runtime, project_id: uuid.UUID, document_id: uuid.UUID) -> str:
    temporal = required(runtime.settings.temporal, "the api role requires temporal settings")
    handle = await runtime.temporal.start_workflow(
        PARSE_WORKFLOW,
        ParseDocumentInput(project_id=project_id, document_id=document_id),
        id=parse_workflow_id(document_id, prefix=runtime.id_prefix),
        task_queue=queue_name(temporal, "parse", prefix=runtime.id_prefix),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        execution_timeout=PARSE_EXECUTION_TIMEOUT,
    )
    return str(handle.id)


async def start_summarize(
    runtime: Runtime,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    requested_by: uuid.UUID,
) -> str:
    """The model is always the server's ``AGENT_MODEL``; a client string never gets here."""

    temporal = required(runtime.settings.temporal, "the api role requires temporal settings")
    run = required(runtime.settings.agent_run, "summarize requires the agent run settings")
    handle = await runtime.temporal.start_workflow(
        SUMMARIZE_WORKFLOW,
        SummarizeDocumentInput(
            project_id=project_id,
            document_id=document_id,
            requested_by=requested_by,
            model=run.model,
            trace_sensitive_data=run.trace_sensitive_data,
        ),
        id=summary_workflow_id(document_id, prefix=runtime.id_prefix),
        task_queue=queue_name(temporal, "agents", prefix=runtime.id_prefix),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        execution_timeout=SUMMARIZE_EXECUTION_TIMEOUT,
    )
    return str(handle.id)


async def start_drive_import(runtime: Runtime, project_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    """Start the Drive import of a saved source; returns the workflow id (INT-GDUI-0003).

    ``DriveImportWorkflow`` on the ``parse`` queue, id ``drive-import-{connection_id}``,
    ``USE_EXISTING``, execution timeout 12 h. An ambiguous start (the call raised, but
    the workflow may exist) looks the workflow up by its deterministic id first: when
    it is there the id is returned and the caller must not compensate; only a proven
    "not started" re-raises.
    """

    temporal = required(runtime.settings.temporal, "the api role requires temporal settings")
    workflow_id = drive_import_workflow_id(connection_id, prefix=runtime.id_prefix)
    try:
        handle = await runtime.temporal.start_workflow(
            DRIVE_IMPORT_WORKFLOW,
            DriveImportInput(
                project_id=project_id,
                connection_id=connection_id,
                id_prefix=runtime.id_prefix,
            ),
            id=workflow_id,
            task_queue=queue_name(temporal, "parse", prefix=runtime.id_prefix),
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            execution_timeout=DRIVE_IMPORT_EXECUTION_TIMEOUT,
        )
    except Exception:
        if await _workflow_exists(runtime, workflow_id):
            return workflow_id
        raise
    return str(handle.id)


async def _workflow_exists(runtime: Runtime, workflow_id: str) -> bool:
    """``True`` only when Temporal confirms a RUNNING workflow; closed or any doubt is ``False``."""

    described: Any = None
    with Contained() as lookup:
        described = await runtime.temporal.get_workflow_handle(workflow_id).describe()
    if lookup.failed:
        return False
    return getattr(described.status, "name", None) == "RUNNING"


async def start_cleanup(
    runtime: Runtime,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    document_ids: list[uuid.UUID],
    *,
    removal_id: uuid.UUID,
    chunk: int = 0,
) -> str:
    """Start ONE ``SourceCleanupWorkflow`` for at most ``CLEANUP_CHUNK_SIZE`` ids (R07).

    Called inside the removal transaction, before the commit (NFR-REL-0003): the id is
    ``source-cleanup-{removal_id}-{chunk}``, ``REJECT_DUPLICATE``, on the ``parse`` queue.
    The input holds ids only. A failure propagates: the caller rolls the removal back.
    """

    if len(document_ids) > CLEANUP_CHUNK_SIZE:
        raise CleanupChunkError("one cleanup workflow takes at most one chunk of ids")
    temporal = required(runtime.settings.temporal, "the api role requires temporal settings")
    handle = await runtime.temporal.start_workflow(
        CLEANUP_WORKFLOW,
        SourceCleanupInput(
            project_id=project_id,
            source_id=source_id,
            document_ids=document_ids,
            id_prefix=runtime.id_prefix,
        ),
        id=cleanup_workflow_id(removal_id, chunk, prefix=runtime.id_prefix),
        task_queue=queue_name(temporal, "parse", prefix=runtime.id_prefix),
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        execution_timeout=CLEANUP_EXECUTION_TIMEOUT,
    )
    return str(handle.id)


async def start_cleanups(
    runtime: Runtime,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    document_ids: list[uuid.UUID],
    removal_id: uuid.UUID,
) -> list[str]:
    """One cleanup workflow per chunk of ids, in order (none for no ids)."""

    return [
        await start_cleanup(runtime, project_id, source_id, ids, removal_id=removal_id, chunk=number)
        for number, ids in enumerate(chunk_ids(document_ids))
    ]


async def terminate_drive_import(runtime: Runtime, connection_id: uuid.UUID) -> None:
    """Best-effort stop of a removed source's Drive import; never raises (SPECS 6.2).

    A workflow that is not running (finished, never started) is the normal case. Late
    writes of a CPU parse thread that outlives the terminate are fenced in the database.
    """

    workflow_id = drive_import_workflow_id(connection_id, prefix=runtime.id_prefix)
    outcome, error_type = "terminated", None
    with Contained() as terminate:
        try:
            await runtime.temporal.get_workflow_handle(workflow_id).terminate(reason="source removed")
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                outcome = "not_running"
            else:
                outcome, error_type = "failed", type(error).__name__
    if terminate.failed:
        outcome, error_type = "failed", terminate.error_type
    log_event(
        logger,
        WARNING if outcome == "failed" else INFO,
        "api.source.drive_terminate",
        source_id=str(connection_id),
        outcome=outcome,
        error_type=error_type,
    )
