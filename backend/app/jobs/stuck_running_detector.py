"""
Job 1 — Stuck RUNNING Detector.

Queries for generations with status=RUNNING and last_activity_at older
than the configured threshold. Calls GenerationSessionStateMachine.stuck_detected
for each one.

Does NOT touch workspaces (Invariant 4).

Also ends the generation session for the stuck generation (prevents slot leaks
after OOM/eviction before terminal paths run).
"""
import logging
from datetime import datetime, timezone, timedelta

from app.core.ttl_config import GenerationLifecyclePolicy
from app.state import GenerationSessionStateMachine
from app.state.transitions import TriggeredBy
from app.schemas.generation_workflow_enums import GenerationStatus
from app.core.notifications import notifications

logger = logging.getLogger(__name__)

STALE_RUNNING_THRESHOLD_MINUTES = GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES


async def detect_stuck_running(db, threshold_minutes: int = STALE_RUNNING_THRESHOLD_MINUTES):
    """
    Idempotent. Safe to call from multiple instances (state machine validates
    current status; if already FAILED, transition is rejected silently).
    """
    esm = GenerationSessionStateMachine(db)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)

    by_activity = await db.query_generation_sessions(
        filters=[
            ("status", "==", GenerationStatus.RUNNING),
            ("last_activity_at", "<", cutoff),
        ]
    )

    # Fallback: generations that transitioned to RUNNING but never had last_activity_at
    # written (crash between allocation_succeeded and first checkpoint update).
    # last_activity_at=None is invisible to the < filter above; use status_changed_at
    # as a proxy — if the status transition itself is stale, the job is stuck.
    by_status_change = await db.query_generation_sessions(
        filters=[
            ("status", "==", GenerationStatus.RUNNING),
            ("status_changed_at", "<", cutoff),
        ]
    )
    seen_ids = {e["id"] for e in by_activity}
    stuck = list(by_activity) + [
        e for e in by_status_change
        if e["id"] not in seen_ids and e.get("last_activity_at") is None
    ]

    for generation in stuck:
        eid = generation["id"]
        try:
            await esm.stuck_detected(
                generation_id=eid,
                triggered_by=TriggeredBy.STUCK_RUNNING,
                metadata={"last_activity_at": str(generation.get("last_activity_at"))},
            )
            logger.warning("stuck_running_detector: marked generation %s as FAILED", eid)

            # Notify — same pattern as GenerationSessionService.fail_generation_session()
            try:
                user_email = generation.get("user_email", "unknown")
                parameters = generation.get("parameters", {})
                spec_path = parameters.get("spec_file", "unknown")
                last_activity = generation.get("last_activity_at")
                notifications.notify(
                    f"❌ Run Stopped (Stuck Detector)\n\n"
                    f"*Run ID:* `{eid}`\n"
                    f"*User:* {user_email}\n"
                    f"*Spec File:* {spec_path}\n"
                    f"*Last activity:* {last_activity}\n"
                    f"*Threshold:* {threshold_minutes} minutes",
                    recipient_email=user_email,
                )
            except Exception as notify_err:
                logger.error(
                    "stuck_running_detector: notification failed for %s: %s", eid, notify_err
                )
        except Exception as e:
            # Already FAILED or COMPLETED by another process — this is correct behaviour.
            # The state machine rejects the transition; the job logs and moves on.
            logger.info("stuck_running_detector: skipped %s (%s)", eid, e)
