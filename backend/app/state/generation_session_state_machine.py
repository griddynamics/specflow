"""
GenerationSessionStateMachine — the single place that writes generation status,
checkpoint, and related fields in Firestore.

No code outside backend/app/state/ may write these fields directly.
"""
from datetime import datetime, timezone
import logging
from typing import Optional

from app.schemas.generation_workflow_enums import (
    CHECKPOINT_ORDER,
    GenerationCheckpoint,
    GenerationStatus,
    WorkspaceStatus,
    parse_checkpoint,
    parse_status,
)
from app.state.exceptions import (
    ArchivePreconditionError,
    InvalidGenerationSessionStateError,
    MaxRetriesExceededError,
)
from app.services.artifact_store import ARTIFACTS_BASE
from app.state.api_key_session_concurrency import ApiKeySessionConcurrency, SessionEndReason
from app.state.transitions import GENERATION_SESSION_TRANSITIONS

logger = logging.getLogger(__name__)

# Bounded per-session log of agent error/warning events (newest kept).
# ~400 bytes/event worst case -> well under the 1 MB Firestore doc limit.
_MAX_AGENT_ERROR_EVENTS = 200


class GenerationSessionStateMachine:
    """
    One public method per legal transition. Each method:
    1. Reads the current document and validates the transition is allowed
    2. Writes the new status + status_changed_at
    3. Appends to state_history with: new status, UTC timestamp, triggered_by, metadata
    4. Executes mandatory side-effects for that transition

    Args:
        db: Database interface (Firestore, emulator, or in-memory)
        workspace_state_machine: WorkspaceStateMachine instance (injected)
        notification_service: For email/Slack (injected, can be mocked)
    """

    def __init__(
        self,
        db,
        workspace_state_machine=None,
        notification_service=None,
        api_key_sessions: ApiKeySessionConcurrency | None = None,
    ):
        self._db = db
        self._workspace_sm = workspace_state_machine
        self._notification_service = notification_service
        self._api_sessions = api_key_sessions or ApiKeySessionConcurrency(db)

    # ------------------------------------------------------------------ #
    # Public transition methods
    # ------------------------------------------------------------------ #

    async def create(self, generation_id: str, triggered_by: str,
                     metadata: dict | None = None) -> dict:
        """
        Initial creation. Writes status=PENDING and state_history entry.
        Called by GenerationSessionService.create_generation_session.
        """
        now = datetime.now(timezone.utc)
        update = {
            "status": GenerationStatus.PENDING,
            "status_changed_at": now,
            "state_history": [{
                "status": GenerationStatus.PENDING,
                "at": now,
                "triggered_by": triggered_by,
                "metadata": metadata or {},
            }],
        }
        await self._db.update_generation_session(generation_id, update)
        logger.info("generation %s created → PENDING (by %s)", generation_id, triggered_by)
        return update

    async def begin_allocation(self, generation_id: str, triggered_by: str) -> dict:
        """PENDING → INITIALIZING. Clears any stale error from a prior contract rejection."""
        return await self._transition(
            generation_id, "begin_allocation",
            triggered_by=triggered_by,
            extra_fields={"error": None},
        )

    async def allocation_succeeded(self, generation_id: str, triggered_by: str) -> dict:
        """INITIALIZING → RUNNING. Resets last_activity_at so stuck_running_detector
        does not fire on retries of old generations with a stale timestamp."""
        return await self._transition(
            generation_id, "allocation_succeeded", triggered_by=triggered_by,
            extra_fields={"last_activity_at": datetime.now(timezone.utc)},
        )

    async def allocation_failed(self, generation_id: str, triggered_by: str) -> dict:
        """INITIALIZING → PENDING"""
        return await self._transition(
            generation_id, "allocation_failed", triggered_by=triggered_by
        )

    async def advance_checkpoint(
        self,
        generation_id: str,
        checkpoint: GenerationCheckpoint,
        triggered_by: str,
        metadata: dict | None = None,
    ) -> dict:
        """
        RUNNING → RUNNING with checkpoint advance.
        Side-effects: validates checkpoint order, updates last_activity_at.
        """
        doc = await self._db.get_generation_session(generation_id)
        self._validate_transition(doc, "advance_checkpoint", generation_id)
        if parse_checkpoint(doc.get("checkpoint")) == checkpoint:
            logger.info(
                "generation %s checkpoint %s already reached, skipping (by %s)",
                generation_id, checkpoint, triggered_by,
            )
            return {}
        self._validate_checkpoint_order(doc, checkpoint, generation_id)

        now = datetime.now(timezone.utc)
        history_entry = {
            "status": GenerationStatus.RUNNING,
            "checkpoint": checkpoint,
            "at": now,
            "triggered_by": triggered_by,
            "metadata": metadata or {},
        }
        update = {
            "checkpoint": checkpoint,
            "last_activity_at": now,
            "state_history": doc.get("state_history", []) + [history_entry],
        }
        # SSOT: durable outputs are protected — flag + path set only when checkpoint
        # reaches OUTPUTS_ARCHIVED (after generation workflow archive_outputs step).
        if checkpoint == GenerationCheckpoint.OUTPUTS_ARCHIVED:
            update["outputs_archived"] = True
            update["artifact_path"] = str(ARTIFACTS_BASE / generation_id)

        await self._db.update_generation_session(generation_id, update)
        logger.info(
            "generation %s checkpoint → %s (by %s)", generation_id, checkpoint, triggered_by
        )
        return update

    async def fail(
        self,
        generation_id: str,
        reason: str,
        triggered_by: str,
        metadata: dict | None = None,
    ) -> dict:
        """
        PENDING | INITIALIZING | RUNNING → FAILED.
        Side-effects: sets failed_at. Workspaces remain ALLOCATED (Invariant 2).
        PENDING is included so that a background task crash before allocation can
        correctly fail the generation (otherwise it gets permanently stuck in PENDING).
        """
        extra_fields = {"failed_at": datetime.now(timezone.utc), "error": reason}
        update = await self._transition(
            generation_id, "fail",
            triggered_by=triggered_by,
            metadata={"reason": reason, **(metadata or {})},
            extra_fields=extra_fields,
        )
        await self._api_sessions.end(
            generation_id=generation_id,
            reason=SessionEndReason.FAILED,
        )
        return update

    async def stuck_detected(
        self, generation_id: str, triggered_by: str, metadata: dict | None = None
    ) -> dict:
        """
        RUNNING → FAILED (triggered by background job, not by code error).
        Side-effects: sets failed_at. Workspaces remain ALLOCATED (Invariant 2).
        """
        extra_fields = {"failed_at": datetime.now(timezone.utc)}
        update = await self._transition(
            generation_id, "stuck_detected",
            triggered_by=triggered_by,
            metadata=metadata or {},
            extra_fields=extra_fields,
        )
        await self._api_sessions.end(
            generation_id=generation_id,
            reason=SessionEndReason.STUCK_DETECTED,
        )
        return update

    async def interrupted_by_shutdown(
        self, generation_id: str, triggered_by: str, metadata: dict | None = None
    ) -> dict:
        """
        PENDING | INITIALIZING | RUNNING → FAILED, on graceful server shutdown
        (SIGTERM / pod eviction). Mirrors fail()/stuck_detected() but additionally
        sets the queryable ``shutdown_interrupted`` marker so the boot-recovery job
        can distinguish eviction casualties from genuine failures.

        Side-effects: sets failed_at, error, shutdown_interrupted=True; ends the
        API-key session slot. Workspaces remain ALLOCATED (Invariant 2) — the code
        is preserved for the retry that resumes from the saved checkpoint.
        """
        extra_fields = {
            "failed_at": datetime.now(timezone.utc),
            "error": "Server shutdown (pod eviction)",
            "shutdown_interrupted": True,
        }
        update = await self._transition(
            generation_id, "fail",
            triggered_by=triggered_by,
            metadata=metadata or {},
            extra_fields=extra_fields,
        )
        await self._api_sessions.end(
            generation_id=generation_id,
            reason=SessionEndReason.STUCK_DETECTED,
        )
        return update

    async def cancel(
        self, generation_id: str, triggered_by: str, metadata: dict | None = None
    ) -> dict:
        """
        PENDING | INITIALIZING | RUNNING → CANCELLED, on explicit user request.

        Mirrors interrupted_by_shutdown()/stuck_detected() but is a deliberate,
        terminal, user-initiated stop: notification-free (the user knows — no error
        Slack/email), sets the queryable ``cancelled_by_user`` marker + ``cancelled_at``
        so boot-recovery / retry / analytics can distinguish it from a genuine failure.

        Side-effects: sets cancelled_at, cancelled_by_user=True; ends the API-key session
        slot. Does NOT set failed_at. Workspaces remain ALLOCATED (Invariant 2) — the
        generated code is preserved and reclaimed by scheduled_wipe; a CANCELLED session
        is never resumed and never retried.
        """
        extra_fields = {
            "cancelled_at": datetime.now(timezone.utc),
            "cancelled_by_user": True,
        }
        update = await self._transition(
            generation_id, "cancel",
            triggered_by=triggered_by,
            metadata=metadata or {},
            extra_fields=extra_fields,
        )
        await self._api_sessions.end(
            generation_id=generation_id,
            reason=SessionEndReason.CANCELLED,
        )
        return update

    async def _record_phase_progress_for_field(
        self,
        generation_id: str,
        workspace_id: str,
        completed_phase: int,
        triggered_by: str,
        field_name: str,
        phase_label: str,
    ) -> dict:
        doc = await self._db.get_generation_session(generation_id)
        self._require_running(doc, "record_phase_progress", generation_id)

        now = datetime.now(timezone.utc)
        phases_dict = dict(doc.get(field_name) or {})
        if workspace_id not in phases_dict:
            phases_dict[workspace_id] = {}
        phases_dict[workspace_id] = dict(phases_dict[workspace_id])
        phases_dict[workspace_id]["last_completed_phase"] = completed_phase

        history_entry = {
            "status": GenerationStatus.RUNNING,
            "at": now,
            "triggered_by": triggered_by,
            "metadata": {"workspace_id": workspace_id, phase_label: completed_phase},
        }
        update = {
            field_name: phases_dict,
            "last_activity_at": now,
            "state_history": doc.get("state_history", []) + [history_entry],
        }
        await self._db.update_generation_session(generation_id, update)
        logger.info(
            "generation %s workspace %s %s %d recorded (by %s)",
            generation_id, workspace_id, phase_label, completed_phase, triggered_by,
        )
        return update

    async def record_phase_progress(
        self,
        generation_id: str,
        workspace_id: str,
        completed_phase: int,
        triggered_by: str,
    ) -> dict:
        """
        RUNNING → RUNNING: record a completed generation phase for a workspace.

        Side-effects:
          - Updates workspace_phases[workspace_id].last_completed_phase
          - Updates last_activity_at (prevents stuck_running_detector false positives)

        This is the ONLY correct way to write workspace_phases (Commandment VII).
        Never call db.update("generation_sessions", ...) with workspace_phases directly.
        """
        return await self._record_phase_progress_for_field(
            generation_id, workspace_id, completed_phase, triggered_by,
            field_name="workspace_phases", phase_label="phase",
        )

    async def record_deployment_phase_progress(
        self,
        generation_id: str,
        workspace_id: str,
        completed_phase: int,
        triggered_by: str,
    ) -> dict:
        """
        RUNNING → RUNNING: record a completed deploy phase for a workspace.

        Side-effects:
          - Updates workspace_phases_deployment[workspace_id].last_completed_phase
          - Updates last_activity_at (prevents stuck_running_detector false positives)

        This is the ONLY correct way to record deploy phase progress (Commandment VII).
        Use save_e2e_plan / init_deployment_phases to initialize workspace_phases_deployment.
        """
        return await self._record_phase_progress_for_field(
            generation_id, workspace_id, completed_phase, triggered_by,
            field_name="workspace_phases_deployment", phase_label="deploy phase",
        )

    async def record_agent_error_event(
        self,
        generation_id: str,
        event: dict,
        triggered_by: str,
    ) -> dict:
        """
        RUNNING → RUNNING: append one agent error/warning event (bounded log).

        Side-effects:
          - Appends to agent_error_events, keeping the newest _MAX_AGENT_ERROR_EVENTS
          - Updates last_activity_at (a crash-retrying workspace is alive — keeps
            stuck_running_detector honest during backoff waits)

        Not a state transition: status, checkpoint, and state_history are untouched
        (Commandment IX governs transitions; events carry their own bounded audit).
        This is the ONLY correct way to write agent_error_events (Commandment VII).

        Concurrency: the append is read-modify-write in the same consistency
        envelope as state_history appends in _record_phase_progress_for_field —
        a concurrent append may be lost under contention; acceptable for
        advisory data.
        """
        doc = await self._db.get_generation_session(generation_id)
        self._require_running(doc, "record_agent_error_event", generation_id)

        events = list(doc.get("agent_error_events") or [])
        events.append(event)
        update = {
            "agent_error_events": events[-_MAX_AGENT_ERROR_EVENTS:],
            "last_activity_at": datetime.now(timezone.utc),
        }
        await self._db.update_generation_session(generation_id, update)
        logger.info(
            "generation %s agent event %s recorded for workspace %s (by %s)",
            generation_id, event.get("kind"), event.get("workspace_id"), triggered_by,
        )
        return update

    async def set_workspace_agent_state(
        self,
        generation_id: str,
        workspace_id: str,
        state: Optional[str],
        triggered_by: str,
    ) -> dict:
        """
        RUNNING → RUNNING: set or clear the transient per-workspace agent badge.

        ``state`` is a WorkspaceAgentState value ("retrying"/"aborted"); None
        removes the key (workspace running normally). Stored on
        workspace_phases[workspace_id]["agent_state"] so the /status per-workspace
        view ships it without a new top-level field. Only writer: this method
        (Commandment VII).
        """
        doc = await self._db.get_generation_session(generation_id)
        self._require_running(doc, "set_workspace_agent_state", generation_id)

        phases_dict = dict(doc.get("workspace_phases") or {})
        entry = dict(phases_dict.get(workspace_id) or {})
        if state is None:
            entry.pop("agent_state", None)
        else:
            entry["agent_state"] = state
        phases_dict[workspace_id] = entry
        update = {"workspace_phases": phases_dict}
        await self._db.update_generation_session(generation_id, update)
        logger.info(
            "generation %s workspace %s agent_state=%s (by %s)",
            generation_id, workspace_id, state, triggered_by,
        )
        return update

    async def save_generation_plan(
        self,
        generation_id: str,
        planning_data_dict: dict,
        total_phases: int,
        triggered_by: str,
    ) -> dict:
        """
        Persist implementation plan data into workspace_phases for all workspace_ids.

        Called from contract validator after JSON conversion succeeds.
        Rejects terminal states (FAILED, COMPLETED) only.

        Always overwrites all workspace entries — safe before the generation loop starts.
        Returns empty dict if workspace_ids are not yet allocated.
        """
        doc = await self._db.get_generation_session(generation_id)
        self._require_non_terminal(doc, "save_generation_plan", generation_id)
        return await self._write_workspace_phases(
            doc, generation_id, planning_data_dict, total_phases, triggered_by
        )

    async def save_e2e_plan(
        self,
        generation_id: str,
        planning_data_dict: dict,
        total_phases: int,
        triggered_by: str,
    ) -> dict:
        """
        Persist E2E plan data into workspace_phases_deployment for all workspace_ids.

        Called from contract validator after JSON conversion succeeds.
        Rejects terminal states (FAILED, COMPLETED) only — plan refresh is checksum-gated,
        not lifecycle-gated, so PENDING/INITIALIZING/RUNNING are all accepted.

        Always overwrites all workspace entries — safe because plan changes only occur
        before the deploy loop starts (nothing in progress at call time).
        No-op only when workspace_ids are not yet allocated.
        """
        doc = await self._db.get_generation_session(generation_id)
        self._require_non_terminal(doc, "save_e2e_plan", generation_id)
        return await self._write_workspace_phases_deployment(
            doc, generation_id, planning_data_dict, total_phases, triggered_by
        )

    async def init_deployment_phases(
        self,
        generation_id: str,
        planning_data_dict: dict,
        total_phases: int,
        triggered_by: str,
    ) -> dict:
        """
        RUNNING → RUNNING: initialize workspace_phases_deployment before the deploy loop.

        No-op if the field is already populated (retry safety — preserves resume points).

        This is the ONLY correct way to initialize workspace_phases_deployment (Commandment VII).
        workspace_ids are read from the generation doc — no caller pre-fetch needed.
        """
        doc = await self._db.get_generation_session(generation_id)
        self._require_running(doc, "init_deployment_phases", generation_id)
        existing = doc.get("workspace_phases_deployment") or {}
        if existing:
            logger.info(
                "generation %s workspace_phases_deployment already initialized, skipping (by %s)",
                generation_id, triggered_by,
            )
            return {}
        return await self._write_workspace_phases_deployment(
            doc, generation_id, planning_data_dict, total_phases, triggered_by
        ) or {}

    async def _write_workspace_phases(
        self,
        doc: dict,
        generation_id: str,
        planning_data_dict: dict,
        total_phases: int,
        triggered_by: str,
    ) -> dict:
        """Write workspace_phases for all workspace_ids with the current implementation plan."""
        workspace_ids = doc.get("workspace_ids") or []
        if not workspace_ids:
            logger.warning(
                "generation %s has no workspace_ids, skipping generation phase init", generation_id
            )
            return {}

        workspace_phases = {
            ws_id: {
                "last_completed_phase": 0,
                "total_phases": total_phases,
                "planning_data": planning_data_dict,
            }
            for ws_id in workspace_ids
        }
        now = datetime.now(timezone.utc)
        history_entry = {
            "status": parse_status(doc.get("status")),
            "checkpoint": parse_checkpoint(doc.get("checkpoint")),
            "at": now,
            "triggered_by": triggered_by,
            "metadata": {"total_phases": total_phases, "workspace_count": len(workspace_ids)},
        }
        update = {
            "workspace_phases": workspace_phases,
            "last_activity_at": now,
            "state_history": doc.get("state_history", []) + [history_entry],
        }
        await self._db.update_generation_session(generation_id, update)
        logger.info(
            "generation %s workspace_phases: %d phase(s), %d workspace(s) (by %s)",
            generation_id, total_phases, len(workspace_ids), triggered_by,
        )
        return update

    async def _write_workspace_phases_deployment(
        self,
        doc: dict,
        generation_id: str,
        planning_data_dict: dict,
        total_phases: int,
        triggered_by: str,
    ) -> dict:
        """
        Write workspace_phases_deployment for all workspace_ids with the current plan.

        Always overwrites — callers are responsible for guarding against unwanted overwrites.
        Returns empty dict if workspace_ids are not yet allocated.
        """
        workspace_ids = doc.get("workspace_ids") or []
        if not workspace_ids:
            logger.warning("generation %s has no workspace_ids, skipping deploy phase init", generation_id)
            return {}

        workspace_phases_deployment = {
            ws_id: {
                "last_completed_phase": 0,
                "total_phases": total_phases,
                "planning_data": planning_data_dict,
            }
            for ws_id in workspace_ids
        }
        now = datetime.now(timezone.utc)
        history_entry = {
            "status": doc["status"],
            "at": now,
            "triggered_by": triggered_by,
            "metadata": {"total_phases": total_phases, "workspace_count": len(workspace_ids)},
        }
        update = {
            "workspace_phases_deployment": workspace_phases_deployment,
            "last_activity_at": now,
            "state_history": doc.get("state_history", []) + [history_entry],
        }
        await self._db.update_generation_session(generation_id, update)
        logger.info(
            "generation %s workspace_phases_deployment: %d phase(s), %d workspace(s) (by %s)",
            generation_id, total_phases, len(workspace_ids), triggered_by,
        )
        return update

    async def reset_for_retry(
        self,
        generation_id: str,
        triggered_by: str,
        metadata: dict | None = None,
    ) -> dict:
        """
        FAILED → PENDING.
        Side-effects: increments retry_count; clears error field; clears the
        shutdown_interrupted marker (a retried session is no longer an unhandled
        eviction casualty, so boot recovery must not pick it up again).
        Preserves checkpoint and workspace_ids (Invariant: must not clear workspace_ids).

        Raises MaxRetriesExceededError if retry_count >= max_retries (Gap 3 fix).
        The check lives here, not in the caller, so it cannot be bypassed.
        """
        doc = await self._db.get_generation_session(generation_id)
        self._validate_transition(doc, "reset_for_retry", generation_id)

        retry_count = doc.get("retry_count", 0)
        max_retries = doc.get("max_retries", 3)
        if retry_count >= max_retries:
            raise MaxRetriesExceededError(generation_id, retry_count, max_retries)

        extra_fields = {
            "retry_count": retry_count + 1,
            "error": None,
            "shutdown_interrupted": False,
        }
        return await self._transition(
            generation_id, "reset_for_retry",
            triggered_by=triggered_by,
            metadata=metadata or {},
            extra_fields=extra_fields,
            _doc=doc,
        )

    async def reject_contract(
        self,
        generation_id: str,
        reason: str,
        triggered_by: str,
    ) -> dict:
        """
        RUNNING → PENDING: contract validation failed before any real work started.

        Releases workspaces back to CLEANING via allocation_rollback and returns
        the session to PENDING so the user can fix their uploaded files and retry
        without manual cleanup. Does NOT set failed_at — this is a pre-flight
        rejection, not a generation failure.
        """
        doc = await self._db.get_generation_session(generation_id)
        workspace_ids = doc.get("workspace_ids") or []
        # Bug 3 (PR255): reset checkpoint to FILES_UPLOADED. A rejected session has done
        # no validation work, so its checkpoint must reflect files-only — never leave a
        # checkpoint at or past CONTRACT_VALIDATED, which would let a later retry skip the
        # validator (WorkflowOrchestrator._should_skip). Writing checkpoint directly here
        # intentionally bypasses the strict-forward advance_checkpoint guard: a rejection
        # is a reset, not a forward transition.
        update = await self._transition(
            generation_id, "reject_contract",
            triggered_by=triggered_by,
            metadata={"reason": reason},
            extra_fields={
                "error": reason,
                "checkpoint": GenerationCheckpoint.FILES_UPLOADED,
                # Cleared so the next run_generation sync re-allocates workspaces
                # (released below via allocation_rollback).
                "workspace_ids": [],
                "workspace_phases": {},
                "workspace_phases_deployment": {},
            },
            _doc=doc,
        )
        if self._workspace_sm:
            for ws_id in workspace_ids:
                try:
                    await self._workspace_sm.allocation_rollback(
                        ws_id, generation_id, triggered_by=triggered_by
                    )
                except Exception as exc:
                    logger.warning(
                        "reject_contract: workspace %s rollback failed (non-fatal): %s",
                        ws_id, exc,
                    )
        return update

    async def complete(
        self,
        generation_id: str,
        triggered_by: str,
        workspace_archive_service,
        result: dict | None = None,
    ) -> dict:
        """
        RUNNING → COMPLETED.

        Preconditions (raises ArchivePreconditionError if violated):
          1. outputs_archived == True on generation document
          2. All 3 workspace git archives confirmed

        Side-effects (in order):
          1. Verify preconditions
          2. Call WorkspaceStateMachine.archive_and_release on each workspace
          3. Write status=COMPLETED + result
          4. Append to state_history
          5. Send email + Slack (non-blocking — failure here does NOT roll back)
        """
        doc = await self._db.get_generation_session(generation_id)
        self._validate_transition(doc, "complete", generation_id)

        # Precondition 1: outputs archived
        if not doc.get("outputs_archived"):
            raise ArchivePreconditionError(
                f"Cannot complete generation {generation_id}: "
                f"outputs_archived is not True. Run archive_outputs step first."
            )

        # Precondition 2 (HR-7 Option B): per-workspace archive tracking
        # workspace_archive_service verifies each workspace's archive branch
        workspace_ids = doc.get("workspace_ids", [])
        archive_status = {}
        for ws_id in workspace_ids:
            confirmed = await workspace_archive_service.verify_archive_branch(
                ws_id, generation_id
            )
            archive_status[ws_id] = "confirmed" if confirmed else "failed"

        failed_archives = [ws for ws, s in archive_status.items() if s == "failed"]
        if failed_archives:
            raise ArchivePreconditionError(
                f"Cannot complete generation {generation_id}: "
                f"archive not confirmed for workspaces: {failed_archives}"
            )

        # Release workspaces (ALLOCATED → CLEANING) — only after both preconditions pass
        if self._workspace_sm:
            for ws_id in workspace_ids:
                await self._workspace_sm.archive_and_release(
                    ws_id, generation_id, triggered_by=triggered_by
                )

            # Release orphaned workspaces: ALLOCATED, locked by this generation,
            # but NOT in workspace_ids (i.e. blocked set members with no active code).
            # These exist when workspace_count < 3 — the pool allocates all 3 in a set
            # but only stores the active N IDs in workspace_ids.
            # allocation_rollback() is correct: ALLOCATED → CLEANING, clears locked_by,
            # sets last_used_by=None so cleanup skips archive (no code written).
            candidate_workspaces = await self._db.query_workspaces(
                filters=[
                    ("locked_by", "==", generation_id),
                    ("status", "==", WorkspaceStatus.ALLOCATED.value),
                ],
            )
            active_set = set(workspace_ids)
            for ws in candidate_workspaces:
                ws_id = ws["_id"]
                if ws_id not in active_set:
                    await self._workspace_sm.allocation_rollback(
                        ws_id, generation_id, triggered_by=triggered_by
                    )

        # Write COMPLETED status
        now = datetime.now(timezone.utc)
        extra_fields = {
            "completed_at": now,
            "code_archived": True,
            "archive_status": archive_status,
        }
        if result:
            extra_fields["result"] = result

        update = await self._transition(
            generation_id, "complete",
            triggered_by=triggered_by,
            extra_fields=extra_fields,
            _doc=doc,
        )

        await self._api_sessions.end(
            generation_id=generation_id,
            reason=SessionEndReason.COMPLETED,
        )

        return update

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _require_running(self, doc: dict, action: str, generation_id: str) -> None:
        """Guard: raises InvalidGenerationSessionStateError if the generation is not RUNNING."""
        current = doc.get("status")
        if current != GenerationStatus.RUNNING:
            raise InvalidGenerationSessionStateError(
                generation_id=generation_id,
                current_status=current,
                attempted_transition=action,
                allowed_from=[GenerationStatus.RUNNING],
            )

    _TERMINAL_STATUS_VALUES = {
        GenerationStatus.COMPLETED.value,
        GenerationStatus.FAILED.value,
        GenerationStatus.CANCELLED.value,
    }
    _NON_TERMINAL_STATUSES = [
        s for s in GenerationStatus
        if s not in {GenerationStatus.COMPLETED, GenerationStatus.FAILED, GenerationStatus.CANCELLED}
    ]

    def _require_non_terminal(self, doc: Optional[dict], action: str, generation_id: str) -> None:
        """Guard: raises if generation is missing or in a terminal state (COMPLETED/FAILED)."""
        if not doc:
            raise InvalidGenerationSessionStateError(
                generation_id=generation_id,
                current_status=None,
                attempted_transition=action,
                allowed_from=self._NON_TERMINAL_STATUSES,
            )
        current = doc.get("status")
        if current in self._TERMINAL_STATUS_VALUES:
            raise InvalidGenerationSessionStateError(
                generation_id=generation_id,
                current_status=current,
                attempted_transition=action,
                allowed_from=self._NON_TERMINAL_STATUSES,
            )

    async def _transition(
        self,
        generation_id: str,
        transition_name: str,
        triggered_by: str,
        metadata: dict | None = None,
        extra_fields: dict | None = None,
        _doc: dict | None = None,
    ) -> dict:
        """Generic transition executor used by most public methods."""
        transition = GENERATION_SESSION_TRANSITIONS[transition_name]
        doc = _doc or await self._db.get_generation_session(generation_id)
        self._validate_transition(doc, transition_name, generation_id, transition)

        now = datetime.now(timezone.utc)
        new_status = transition.to_state

        history_entry = {
            "status": new_status,
            "at": now,
            "triggered_by": triggered_by,
            "metadata": metadata or {},
        }
        update = {
            "status": new_status,
            "status_changed_at": now,
            "state_history": doc.get("state_history", []) + [history_entry],
            **(extra_fields or {}),
        }
        await self._db.update_generation_session(generation_id, update)
        logger.info(
            "generation %s %s → %s (by %s)",
            generation_id, doc.get("status"), new_status, triggered_by
        )
        return update

    def _validate_transition(
        self, doc: dict, transition_name: str, generation_id: str,
        transition=None
    ):
        t = transition or GENERATION_SESSION_TRANSITIONS.get(transition_name)
        if t is None:
            raise ValueError(f"Unknown transition: {transition_name}")
        current_status = parse_status(doc.get("status"))
        if t.from_states and current_status not in t.from_states:
            raise InvalidGenerationSessionStateError(
                generation_id=generation_id,
                current_status=doc.get("status"),
                attempted_transition=transition_name,
                allowed_from=list(t.from_states),
            )

    def _validate_checkpoint_order(
        self, doc: dict, new_checkpoint: GenerationCheckpoint, generation_id: str
    ):
        current = parse_checkpoint(doc.get("checkpoint"))
        if current is None:
            return
        try:
            current_idx = CHECKPOINT_ORDER.index(current)
            new_idx = CHECKPOINT_ORDER.index(new_checkpoint)
        except ValueError:
            return
        if new_idx <= current_idx:
            raise InvalidGenerationSessionStateError(
                generation_id=generation_id,
                current_status=doc.get("status"),
                attempted_transition=f"advance_checkpoint({new_checkpoint})",
                allowed_from=[f"checkpoint after {current}"],
            )
