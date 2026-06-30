"""
Job 2 — Stuck INITIALIZING Detector + Orphaned PENDING Workspace Recovery.

Part A: INITIALIZING means allocation is in progress but no code generation has started.
It is safe to roll back to PENDING: no user work exists on the filesystem yet.

Issue 4 fix: after rolling the generation back to PENDING, also call
allocation_rollback() on each workspace that was allocated. Without this,
those workspaces stay ALLOCATED forever (orphaned), draining the pool.

Part B: Detects PENDING generations that still have ALLOCATED workspaces.
This happens when allocation_failed() succeeded (generation → PENDING) but
allocation_rollback() failed or was never called for some workspaces. The
workspaces are orphaned — ALLOCATED with no active generation to release them.
Since PENDING means no code generation occurred, these are safe to roll back.
"""
import logging
from datetime import datetime, timezone, timedelta

from app.core.ttl_config import GenerationLifecyclePolicy
from app.state import GenerationSessionStateMachine
from app.state.workspace_state_machine import WorkspaceStateMachine
from app.state.transitions import TriggeredBy
from app.schemas.generation_workflow_enums import (
    GenerationCheckpoint,
    GenerationStatus,
    WorkspaceStatus,
    parse_checkpoint,
)

logger = logging.getLogger(__name__)

STALE_INITIALIZING_THRESHOLD_MINUTES = GenerationLifecyclePolicy.STUCK_INITIALIZING_MINUTES
ORPHANED_WORKSPACE_THRESHOLD_MINUTES = GenerationLifecyclePolicy.ORPHANED_WORKSPACE_MINUTES


async def detect_stuck_initializing(
    db, threshold_minutes: int = STALE_INITIALIZING_THRESHOLD_MINUTES
):
    esm = GenerationSessionStateMachine(db)
    workspace_sm = WorkspaceStateMachine(db)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)

    # Part A: INITIALIZING generations stuck for > threshold
    stuck = await db.query_generation_sessions(
        filters=[
            ("status", "==", GenerationStatus.INITIALIZING),
            ("status_changed_at", "<", cutoff),
        ]
    )

    for generation in stuck:
        eid = generation["id"]
        workspace_ids = generation.get("workspace_ids", [])
        try:
            await esm.allocation_failed(
                generation_id=eid,
                triggered_by=TriggeredBy.STUCK_INITIALIZING,
            )
            logger.warning("stuck_initializing_detector: rolled back generation %s → PENDING", eid)
        except Exception as e:
            logger.info("stuck_initializing_detector: skipped %s (%s)", eid, e)
            continue

        for ws_id in workspace_ids:
            try:
                await workspace_sm.allocation_rollback(
                    workspace_id=ws_id,
                    generation_id=eid,
                    triggered_by=TriggeredBy.STUCK_INITIALIZING,
                )
                logger.info(
                    "stuck_initializing_detector: workspace %s → CLEANING (generation %s)",
                    ws_id, eid,
                )
            except Exception as ws_err:
                logger.warning(
                    "stuck_initializing_detector: could not roll back workspace %s "
                    "for generation %s: %s (skipping)",
                    ws_id, eid, ws_err,
                )

    # Part B: Orphaned ALLOCATED workspaces with PENDING generations.
    # This catches workspaces that survived Part A rollback failures — the generation
    # went back to PENDING but workspace rollback failed or was never called.
    # Safe to clean: PENDING means no code generation ever ran on these workspaces.
    await _recover_orphaned_pending_workspaces(db, workspace_sm)


async def _recover_orphaned_pending_workspaces(db, workspace_sm: WorkspaceStateMachine):
    """
    Finds ALLOCATED workspaces whose owning generation is PENDING (orphaned).

    PENDING + ALLOCATED is an inconsistent state ONLY if the generation hasn't
    progressed past initial file upload. If the generation has passed CONTRACT_VALIDATED
    checkpoint, the workspaces are legitimately allocated and waiting for the next
    phase. These should NOT be cleaned.

    Uses a threshold to avoid racing with normal allocation flow.
    """
    orphan_cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=ORPHANED_WORKSPACE_THRESHOLD_MINUTES
    )

    allocated_workspaces = await db.query_workspaces(
        filters=[("status", "==", WorkspaceStatus.ALLOCATED)]
    )

    for ws in allocated_workspaces:
        generation_id = ws.get("locked_by")
        if not generation_id:
            continue

        generation = await db.get_generation_session(generation_id)
        if not generation:
            continue

        if generation.get("status") != GenerationStatus.PENDING:
            continue

        # PENDING generations that have already advanced past upload should not be
        # treated as orphans — they're in mid-flight and a different recovery path
        # will handle them.
        checkpoint_enum = parse_checkpoint(generation.get("checkpoint"))
        if checkpoint_enum and GenerationCheckpoint.is_at_or_after(
            checkpoint_enum,
            GenerationCheckpoint.CONTRACT_VALIDATED,
        ):
            continue

        # Grace period: don't touch workspaces allocated very recently —
        # might be mid-initialization and the generation status hasn't caught up.
        allocated_at = ws.get("allocated_at") or generation.get("status_changed_at")
        if allocated_at and allocated_at > orphan_cutoff:
            continue

        try:
            await workspace_sm.allocation_rollback(
                workspace_id=ws["id"],
                generation_id=generation_id,
                triggered_by=TriggeredBy.STUCK_INITIALIZING,
            )
            logger.warning(
                "stuck_initializing_detector: orphaned workspace %s → CLEANING "
                "(generation %s is PENDING with checkpoint=%s)",
                ws["id"], generation_id, generation.get("checkpoint"),
            )
        except Exception as e:
            logger.warning(
                "stuck_initializing_detector: could not roll back orphaned workspace %s "
                "(generation %s): %s (skipping)",
                ws["id"], generation_id, e,
            )
