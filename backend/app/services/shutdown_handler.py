"""Graceful-shutdown handler (Half A): on SIGTERM, mark in-flight sessions FAILED with the
``shutdown_interrupted`` marker and post one Slack message. Non-fatal; workspaces are preserved.
Single-replica deployment — see docs/backend/graceful-shutdown-recovery.md.
"""
import asyncio
import logging

from app.core.notifications import notifications
from app.schemas.generation_workflow_enums import GenerationStatus
from app.state.generation_session_state_machine import GenerationSessionStateMachine
from app.state.transitions import TriggeredBy

logger = logging.getLogger(__name__)

# In-flight statuses to fail on shutdown. PENDING is excluded: a created-but-not-launched
# session must not be auto-resumed on the next boot.
_IN_FLIGHT_STATUSES = (GenerationStatus.RUNNING, GenerationStatus.INITIALIZING)


def _compose_slack_message(failed: list[dict]) -> str:
    """One Slack message listing every session marked FAILED on shutdown."""
    lines = [
        "🟠 *Server Shutdown — In-Flight Sessions Marked Failed*",
        "",
        f"*Count:* {len(failed)}",
        "*Sessions (interrupted by pod eviction; retry to resume):*",
    ]
    for doc in failed:
        spec = (doc.get("parameters") or {}).get("spec_path") or "unknown"
        user = doc.get("user_email") or "unknown"
        lines.append(f"  • `{doc['id']}` — {user} — {spec}")
    return "\n".join(lines)


async def mark_active_sessions_interrupted(db) -> list[str]:
    """Mark in-flight (RUNNING/INITIALIZING) sessions FAILED and Slack-notify the IDs.

    Returns the generation IDs successfully marked FAILED. Never raises — a failure to
    notify (or to fail an already-terminal session) must not block the shutdown sequence.
    Scope is a Firestore query: correct for a single-replica deployment, where the in-flight
    set is exactly this pod's sessions.
    """
    esm = GenerationSessionStateMachine(db)

    candidates: list[dict] = []
    for status in _IN_FLIGHT_STATUSES:
        candidates.extend(await db.query_generation_sessions(filters=[("status", "==", status)]))

    failed: list[dict] = []
    for doc in candidates:
        gid = doc["id"]
        try:
            await esm.interrupted_by_shutdown(gid, triggered_by=TriggeredBy.SHUTDOWN)
            failed.append(doc)
            logger.warning("shutdown_handler: marked generation %s as FAILED", gid)
        except Exception as exc:
            # Already terminal (another path won the race) — idempotent, skip.
            logger.info("shutdown_handler: skipped %s (%s)", gid, exc)

    if failed:
        try:
            await asyncio.to_thread(notifications.notify, _compose_slack_message(failed))
        except Exception as notify_err:
            logger.error("shutdown_handler: notification failed (non-fatal): %s", notify_err)

    return [doc["id"] for doc in failed]
