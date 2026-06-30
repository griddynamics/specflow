"""
Job 4 — 7-Day Wipe Scheduler.

Runs daily. Finds ALLOCATED workspaces whose owning generation has been
FAILED for more than 7 days. Schedules them for wipe with a 24h warning window.
A second pass executes confirmed wipes.

Does not wipe immediately — gives users a 24-hour window to contact support.
"""
import logging
from datetime import datetime, timezone, timedelta

from app.core.ttl_config import GenerationLifecyclePolicy
from app.state import WorkspaceStateMachine
from app.schemas.generation_workflow_enums import WorkspaceStatus, GenerationStatus

logger = logging.getLogger(__name__)

FAILED_RETENTION_DAYS = GenerationLifecyclePolicy.WORKSPACE_FAILED_RETENTION_DAYS
WIPE_WARNING_HOURS = GenerationLifecyclePolicy.WIPE_WARNING_HOURS


async def schedule_expired_workspace_wipes(db, notification_service=None):
    """
    Pass 1: identify expired workspaces and schedule them for wipe.
    
    Finds ALLOCATED workspaces whose owning generation has been FAILED for more
    than FAILED_RETENTION_DAYS (7 days). Uses the correct timestamp field:
    - FAILED generations have 'failed_at' (not 'completed_at')
    """
    wsm = WorkspaceStateMachine(db)
    cutoff = datetime.now(timezone.utc) - timedelta(days=FAILED_RETENTION_DAYS)

    allocated_workspaces = await db.query_workspaces(
        filters=[
            ("status", "==", WorkspaceStatus.ALLOCATED),
            ("scheduled_for_wipe", "==", False),
        ]
    )

    for workspace in allocated_workspaces:
        generation_id = workspace.get("locked_by")
        if not generation_id:
            continue

        generation = await db.get_generation_session(generation_id)
        if not generation:
            continue
        if generation.get("status") != GenerationStatus.FAILED:
            continue
        
        # CRITICAL FIX: FAILED generations have 'failed_at', NOT 'completed_at'
        failed_at = generation.get("failed_at")
        if not failed_at or failed_at > cutoff:
            continue

        wipe_at = datetime.now(timezone.utc) + timedelta(hours=WIPE_WARNING_HOURS)
        await wsm.schedule_wipe(workspace["id"], wipe_at=wipe_at,
                                triggered_by="scheduled_wipe_job")

        if notification_service:
            try:
                await notification_service.send_wipe_warning(
                    generation_id=generation_id,
                    workspace_id=workspace["id"],
                    wipe_at=wipe_at,
                )
            except Exception as e:
                logger.error("Failed to send wipe warning for %s: %s", workspace["id"], e)

        logger.info(
            "Workspace %s (generation %s) scheduled for wipe at %s",
            workspace["id"], generation_id, wipe_at
        )


async def execute_confirmed_wipes(db):
    """Pass 2: execute wipes whose 24h window has expired."""
    wsm = WorkspaceStateMachine(db)
    now = datetime.now(timezone.utc)

    ready = await db.query_workspaces(
        filters=[
            ("scheduled_for_wipe", "==", True),
            ("scheduled_for_wipe_at", "<", now),
        ]
    )

    for workspace in ready:
        try:
            await wsm.execute_scheduled_wipe(workspace["id"],
                                              triggered_by="scheduled_wipe_job")
            logger.info("Workspace %s wipe executed → CLEANING", workspace["id"])
        except Exception as e:
            logger.error("Failed to execute wipe for %s: %s", workspace["id"], e)
