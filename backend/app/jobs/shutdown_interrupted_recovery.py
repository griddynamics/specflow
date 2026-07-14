"""Boot-time recovery (Half B): one-shot job that auto-retries sessions marked
``shutdown_interrupted`` within the recovery window, via the shared rerun path (reuses
workspaces, resumes from checkpoint). See docs/backend/graceful-shutdown-recovery.md.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from app.core.config import settings
from app.core.notifications import notifications
from app.schemas.generation_workflow_enums import GenerationStatus, WorkspaceStatus
from app.services.generation_session import GenerationSessionService
from app.services.generation_task_registry import GenerationTaskRegistry
from app.services.generation_workflow_runner import rerun_generation_session
from app.state import GenerationSessionStateMachine
from app.state.exceptions import (
    InvalidGenerationSessionStateError,
    MaxRetriesExceededError,
)
from app.state.transitions import TriggeredBy

logger = logging.getLogger(__name__)


def _compose_restart_message(restarted: list[dict]) -> str:
    """One Slack message listing the sessions auto-retried after a reboot.

    Mirrors the shutdown handler's plain-text style so the reboot alert reads as the
    natural follow-up to the "Server Shutdown — In-Flight Sessions Marked Failed" message.
    """
    lines = [
        "🔄 *Server Reboot — Interrupted Sessions Auto-Retried*",
        "",
        f"*Count:* {len(restarted)}",
        "*Sessions (resumed from checkpoint after a shutdown/eviction):*",
    ]
    for s in restarted:
        spec = s.get("spec_path") or "unknown"
        user = s.get("user_email") or "unknown"
        lines.append(f"  • `{s['generation_id']}` — {user} — {spec}")
    return "\n".join(lines)


async def _workspaces_reusable(db, workspace_ids: list, generation_id: str) -> bool:
    """True iff every workspace is ALLOCATED and still locked by this session.

    Mirrors GenerationSessionRetryService HR-2: a workspace in CLEANING/STUCK may
    still hold the code but cannot be safely reused without operator intervention.
    """
    for ws_id in workspace_ids:
        ws_doc = await db.get_workspace(ws_id)
        if not ws_doc:
            return False
        if ws_doc.get("status") != WorkspaceStatus.ALLOCATED.value:
            return False
        if ws_doc.get("locked_by") != generation_id:
            return False
    return True


async def recover_interrupted_sessions(
    db,
    generation_session_service: GenerationSessionService,
    *,
    window_minutes: int | None = None,
    task_registry: GenerationTaskRegistry | None = None,
) -> list[str]:
    """Auto-retry shutdown-interrupted sessions found within the recovery window.

    One-shot (not a loop). Returns the generation IDs it re-fired. Each candidate is
    independently guarded; one failure never blocks the others. ``task_registry`` (when
    provided) is threaded into each re-fired run so it stays cancellable.
    """
    if not settings.AUTO_RECOVER_INTERRUPTED_SESSIONS:
        logger.info("shutdown_interrupted_recovery: disabled by config — skipping")
        return []

    window = (
        window_minutes
        if window_minutes is not None
        else settings.SHUTDOWN_RECOVERY_WINDOW_MINUTES
    )
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window)

    # Two equality filters (no composite-index requirement); apply the time window in
    # Python. The marker is only ever True on a FAILED eviction casualty (reset clears it).
    candidates = await db.query_generation_sessions(
        filters=[
            ("status", "==", GenerationStatus.FAILED),
            ("shutdown_interrupted", "==", True),
        ]
    )

    esm = GenerationSessionStateMachine(db)
    recovered: list[str] = []
    recovered_info: list[dict] = []

    for session in candidates:
        gid = session["id"]
        failed_at = session.get("failed_at")
        if failed_at is None or failed_at <= cutoff:
            logger.info(
                "shutdown_interrupted_recovery: %s outside recovery window — skipping", gid
            )
            continue

        # HR-2 / Commandment V — never fresh-allocate over un-archived code.
        code_archived = session.get("code_archived", False)
        workspace_ids = session.get("workspace_ids", [])
        if not code_archived and workspace_ids:
            if not await _workspaces_reusable(db, workspace_ids, gid):
                logger.warning(
                    "shutdown_interrupted_recovery: %s workspaces not reusable — "
                    "leaving FAILED for manual handling", gid
                )
                continue

        # Atomic FAILED→PENDING. Dedup point under multi-instance boot: a lost race
        # raises InvalidGenerationSessionStateError (status no longer FAILED).
        try:
            await esm.reset_for_retry(gid, triggered_by=TriggeredBy.SHUTDOWN_RECOVERY)
        except InvalidGenerationSessionStateError:
            logger.info(
                "shutdown_interrupted_recovery: %s no longer FAILED (race) — skipping", gid
            )
            continue
        except MaxRetriesExceededError:
            logger.warning(
                "shutdown_interrupted_recovery: %s exhausted retries — leaving FAILED", gid
            )
            continue

        asyncio.create_task(
            rerun_generation_session(
                gid,
                generation_session_service=generation_session_service,
                user_email=session.get("user_email"),
                task_registry=task_registry,
            )
        )
        recovered.append(gid)
        recovered_info.append({
            "generation_id": gid,
            "user_email": session.get("user_email"),
            "spec_path": (session.get("parameters") or {}).get("spec_path"),
        })
        logger.warning("shutdown_interrupted_recovery: re-firing interrupted session %s", gid)

    if recovered:
        logger.warning(
            "shutdown_interrupted_recovery: re-fired %d interrupted session(s): %s",
            len(recovered), recovered,
        )
        # Slack: the reboot follow-up to the shutdown alert. Non-fatal — a notify
        # failure must never stop sessions from being recovered.
        try:
            await asyncio.to_thread(notifications.notify, _compose_restart_message(recovered_info))
        except Exception as notify_err:
            logger.error(
                "shutdown_interrupted_recovery: notification failed (non-fatal): %s", notify_err
            )
    return recovered
