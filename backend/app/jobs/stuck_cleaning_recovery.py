"""
Job 3 — Stuck CLEANING Recovery.

Queries for workspaces in CLEANING state where cleaning_started_at is
older than the threshold. Transitions them to STUCK, then triggers recovery
by calling cleanup_workspace() to actually perform the git reset.

HR-3 decision: uses cleaning_started_at timestamp (Option A).
"""
import logging
from datetime import datetime, timezone, timedelta

from app.core.ttl_config import GenerationLifecyclePolicy
from app.state import WorkspaceStateMachine
from app.state.transitions import TriggeredBy
from app.schemas.generation_workflow_enums import WorkspaceStatus

logger = logging.getLogger(__name__)

STALE_CLEANING_THRESHOLD_HOURS = GenerationLifecyclePolicy.STUCK_CLEANING_HOURS


async def recover_stuck_cleaning(
    db, workspace_pool_service=None, threshold_hours: int = STALE_CLEANING_THRESHOLD_HOURS
):
    """
    Recovers workspaces stuck in CLEANING state.
    
    Handles two cases:
    1. Workspaces with cleaning_started_at > threshold (normal stuck case)
    2. Workspaces with missing/None cleaning_started_at (legacy stuck workspaces)
    
    For case 2, we treat them as immediately stuck and attempt recovery.
    """
    wsm = WorkspaceStateMachine(db)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=threshold_hours)

    # Single query for all CLEANING workspaces; split client-side.
    # Conditions are mutually exclusive (None vs. old timestamp), so no dedup needed.
    # Case 1: cleaning_started_at is old → stuck normally
    # Case 2: cleaning_started_at is None → legacy doc from before the field existed
    all_cleaning = await db.query_workspaces(
        filters=[("status", "==", WorkspaceStatus.CLEANING)]
    )
    all_stuck = [
        ws for ws in all_cleaning
        if ws.get("cleaning_started_at") is None
        or ws.get("cleaning_started_at") < cutoff
    ]

    for workspace in all_stuck:
        wid = workspace["id"]
        cleaning_started = workspace.get("cleaning_started_at")
        reason = (
            f"Stuck in CLEANING since {cleaning_started}" if cleaning_started
            else "Stuck in CLEANING with missing cleaning_started_at (legacy inconsistent state)"
        )
        
        try:
            await wsm.mark_stuck(
                wid, triggered_by=TriggeredBy.STUCK_CLEANING,
                reason=reason
            )
            await wsm.begin_recovery(wid, triggered_by=TriggeredBy.STUCK_CLEANING)
            logger.warning("stuck_cleaning_recovery: workspace %s → STUCK → CLEANING (%s)", wid, reason)

            if workspace_pool_service is not None:
                try:
                    await workspace_pool_service.cleanup_workspace(wid)
                    logger.info(
                        "stuck_cleaning_recovery: workspace %s cleaned → AVAILABLE", wid
                    )
                except Exception as cleanup_err:
                    logger.error(
                        "stuck_cleaning_recovery: cleanup failed for %s (%s)",
                        wid, cleanup_err,
                    )
        except Exception as e:
            logger.info("stuck_cleaning_recovery: skipped %s (%s)", wid, e)
