"""
WorkspaceStateMachine — the single place that writes workspace status fields.
All writes that require atomicity use Firestore transactions.
"""
import logging
from datetime import datetime, timezone

from app.state.transitions import WORKSPACE_TRANSITIONS
from app.state.exceptions import (
    InvalidWorkspaceStateError, WorkspaceOwnershipError, CleanupTargetError
)
from app.schemas.generation_workflow_enums import WorkspaceStatus
from app.state.api_key_session_concurrency import ApiKeySessionConcurrency, SessionEndReason

logger = logging.getLogger(__name__)


class WorkspaceStateMachine:

    def __init__(self, db, api_key_sessions: ApiKeySessionConcurrency | None = None):
        self._db = db
        self._api_sessions = api_key_sessions or ApiKeySessionConcurrency(db)

    async def allocate(self, workspace_id: str, generation_id: str,
                       triggered_by: str) -> dict:
        """
        AVAILABLE → ALLOCATED. Must run inside a Firestore transaction
        to prevent double-allocation.
        """
        async def txn_fn(transaction):
            doc = await self._db.get_workspace_in_transaction(workspace_id, transaction)
            self._validate_transition(doc, "allocate", workspace_id)

            update = {
                "status": WorkspaceStatus.ALLOCATED,
                "locked_by": generation_id,
                "clean_verified": False,
            }
            await self._db.update_workspace_in_transaction(
                workspace_id, update, transaction
            )
            return update

        result = await self._db.run_transaction(txn_fn)
        logger.info("workspace %s allocated to %s (by %s)",
                    workspace_id, generation_id, triggered_by)
        return result

    async def allocation_rollback(self, workspace_id: str, generation_id: str,
                                  triggered_by: str) -> dict:
        """
        ALLOCATED → CLEANING (HR-4 Option A: go through CLEANING, not directly AVAILABLE).
        Used when git clone fails before any work begins.
        The cleanup job will verify the workspace is empty then mark it AVAILABLE.
        """
        doc = await self._db.get_workspace(workspace_id)
        self._validate_transition(doc, "allocation_rollback", workspace_id)
        self._check_ownership(doc, workspace_id, generation_id)

        now = datetime.now(timezone.utc)
        update = {
            "status": WorkspaceStatus.CLEANING,
            "locked_by": None,
            "last_used_by": None,   # clone failed — no code on filesystem, nothing to preserve
            "cleaning_started_at": now,
        }
        await self._db.update_workspace(workspace_id, update)
        logger.info("workspace %s allocation rolled back → CLEANING (by %s)",
                    workspace_id, triggered_by)
        return update

    async def archive_and_release(self, workspace_id: str, generation_id: str,
                                   triggered_by: str) -> dict:
        """
        ALLOCATED → CLEANING.
        Must run inside a Firestore transaction.
        Verifies locked_by == generation_id (Invariant 1) before writing.
        ONLY called by GenerationSessionStateMachine.complete after archiving confirms.
        """
        now = datetime.now(timezone.utc)

        async def txn_fn(transaction):
            doc = await self._db.get_workspace_in_transaction(workspace_id, transaction)
            self._validate_transition(doc, "archive_and_release", workspace_id)

            # Invariant 1: ownership check
            if doc.get("locked_by") != generation_id:
                raise WorkspaceOwnershipError(
                    workspace_id, generation_id, doc.get("locked_by")
                )

            update = {
                "status": WorkspaceStatus.CLEANING,
                "locked_by": None,
                "last_used_by": generation_id,
                "cleaning_started_at": now,
            }
            await self._db.update_workspace_in_transaction(
                workspace_id, update, transaction
            )
            return update

        result = await self._db.run_transaction(txn_fn)
        logger.info("workspace %s archive_and_release → CLEANING (generation %s, by %s)",
                    workspace_id, generation_id, triggered_by)
        return result

    async def mark_clean(self, workspace_id: str, triggered_by: str) -> dict:
        """CLEANING → AVAILABLE"""
        doc = await self._db.get_workspace(workspace_id)
        self._validate_transition(doc, "mark_clean", workspace_id)

        update = {
            "status": WorkspaceStatus.AVAILABLE,
            "clean_verified": True,
            "cleaning_started_at": None,
        }
        await self._db.update_workspace(workspace_id, update)
        logger.info("workspace %s → AVAILABLE (by %s)", workspace_id, triggered_by)
        return update

    async def mark_stuck(self, workspace_id: str, triggered_by: str,
                         reason: str = "") -> dict:
        """
        CLEANING → STUCK.
        ALLOCATED → STUCK is NOT a legal transition (Invariant 2).
        """
        doc = await self._db.get_workspace(workspace_id)

        # Invariant 2: explicitly block ALLOCATED → STUCK
        if doc.get("status") == WorkspaceStatus.ALLOCATED:
            raise CleanupTargetError(
                f"Cannot mark_stuck workspace {workspace_id}: "
                f"status is ALLOCATED. Invariant 2: ALLOCATED → STUCK is not a legal transition. "
                f"Workspaces must stay ALLOCATED on failure paths."
            )

        self._validate_transition(doc, "mark_stuck", workspace_id)
        update = {
            "status": WorkspaceStatus.STUCK,
            "stuck_reason": reason,
        }
        await self._db.update_workspace(workspace_id, update)
        logger.warning("workspace %s → STUCK (by %s): %s", workspace_id, triggered_by, reason)
        return update

    async def begin_recovery(self, workspace_id: str, triggered_by: str) -> dict:
        """STUCK → CLEANING"""
        doc = await self._db.get_workspace(workspace_id)
        self._validate_transition(doc, "begin_recovery", workspace_id)

        now = datetime.now(timezone.utc)
        update = {
            "status": WorkspaceStatus.CLEANING,
            "cleaning_started_at": now,
        }
        await self._db.update_workspace(workspace_id, update)
        logger.info("workspace %s begin_recovery → CLEANING (by %s)", workspace_id, triggered_by)
        return update

    async def schedule_wipe(self, workspace_id: str, wipe_at: datetime,
                             triggered_by: str) -> dict:
        """
        Marks a workspace for scheduled wipe (status stays ALLOCATED).
        Used by Job 4 (7-day scheduler).

        Invariant check: only allowed when owning generation is FAILED.
        """
        doc = await self._db.get_workspace(workspace_id)
        # Invariants 3 & 4: cleanup processes may not touch ALLOCATED unless scheduling wipe
        if doc.get("status") != WorkspaceStatus.ALLOCATED:
            raise InvalidWorkspaceStateError(
                workspace_id, doc.get("status"), "schedule_wipe",
                [WorkspaceStatus.ALLOCATED]
            )

        update = {
            "scheduled_for_wipe": True,
            "scheduled_for_wipe_at": wipe_at,
        }
        await self._db.update_workspace(workspace_id, update)
        logger.info("workspace %s scheduled for wipe at %s (by %s)",
                    workspace_id, wipe_at, triggered_by)
        return update

    async def execute_scheduled_wipe(self, workspace_id: str,
                                      triggered_by: str) -> dict:
        """
        ALLOCATED → CLEANING when scheduled_for_wipe window has passed.
        Confirms that the window has actually elapsed before writing.
        """
        doc = await self._db.get_workspace(workspace_id)
        if not doc.get("scheduled_for_wipe"):
            raise InvalidWorkspaceStateError(
                workspace_id, doc.get("status"), "execute_scheduled_wipe",
                ["scheduled_for_wipe == True"]
            )
        now = datetime.now(timezone.utc)
        wipe_at = doc.get("scheduled_for_wipe_at")
        if wipe_at and wipe_at > now:
            raise InvalidWorkspaceStateError(
                workspace_id, doc.get("status"), "execute_scheduled_wipe",
                [f"scheduled_for_wipe_at ({wipe_at}) must be in the past"]
            )

        update = {
            "status": WorkspaceStatus.CLEANING,
            "scheduled_for_wipe": False,
            "scheduled_for_wipe_at": None,
            "cleaning_started_at": now,
        }
        await self._db.update_workspace(workspace_id, update)
        logger.info("workspace %s executed scheduled wipe → CLEANING (by %s)",
                    workspace_id, triggered_by)
        return update

    async def force_release(self, workspace_id: str, reason: str,
                              confirmed_by: str) -> dict:
        """
        Operator escape hatch: ALLOCATED → CLEANING bypassing archive check.
        Requires explicit reason and human name. Both are written to audit trail.
        
        Also releases the API key lock if the owning generation holds one.
        """
        doc = await self._db.get_workspace(workspace_id)
        if doc.get("status") != WorkspaceStatus.ALLOCATED:
            raise InvalidWorkspaceStateError(
                workspace_id, doc.get("status"), "force_release",
                [WorkspaceStatus.ALLOCATED]
            )

        generation_id = doc.get("locked_by")
        if generation_id:
            await self._api_sessions.end(
                generation_id=generation_id,
                reason=SessionEndReason.FORCE_RELEASED,
            )

        now = datetime.now(timezone.utc)
        update = {
            "status": WorkspaceStatus.CLEANING,
            "locked_by": None,
            "cleaning_started_at": now,
            "force_released": True,
            "force_release_reason": reason,
            "force_released_by": confirmed_by,
            "force_released_at": now,
        }
        await self._db.update_workspace(workspace_id, update)
        logger.warning(
            "workspace %s FORCE RELEASED → CLEANING by '%s': %s",
            workspace_id, confirmed_by, reason
        )
        return update

    async def admin_clean_available(self, workspace_id: str,
                                     reason: str, triggered_by: str) -> dict:
        """
        AVAILABLE → CLEANING (admin cleanup path).
        Used when a workspace has stale data on disk but no owning generation.
        Does NOT touch locked_by or last_used_by — no generation is associated.
        """
        doc = await self._db.get_workspace(workspace_id)
        self._validate_transition(doc, "admin_clean_available", workspace_id)

        now = datetime.now(timezone.utc)
        update = {
            "status": WorkspaceStatus.CLEANING,
            "cleaning_started_at": now,
        }
        await self._db.update_workspace(workspace_id, update)
        logger.info("workspace %s admin_clean_available → CLEANING (reason: %s, by %s)",
                    workspace_id, reason, triggered_by)
        return update

    async def admin_deallocate(self, workspace_id: str,
                                reason: str, triggered_by: str) -> dict:
        """
        ALLOCATED → AVAILABLE (operator escape hatch — skips CLEANING pipeline).
        Use ONLY when confirmed no generation code was written to the workspace.
        Sets clean_verified=True as an operator assertion.
        """
        doc = await self._db.get_workspace(workspace_id)
        self._validate_transition(doc, "admin_deallocate", workspace_id)

        previous_locked_by = doc.get("locked_by")
        update = {
            "status": WorkspaceStatus.AVAILABLE,
            "locked_by": None,
            "clean_verified": True,
            "error": None,
        }
        await self._db.update_workspace(workspace_id, update)
        logger.warning("workspace %s ADMIN DEALLOCATED → AVAILABLE by '%s': %s "
                       "(was locked_by=%s)",
                       workspace_id, triggered_by, reason, previous_locked_by)
        return update

    async def admin_release_stuck(self, workspace_id: str,
                                   reason: str, triggered_by: str,
                                   clean_verified: bool = False) -> dict:
        """
        STUCK → AVAILABLE (operator escape hatch after manual cleanup verification).
        clean_verified should only be True if the caller confirmed workspace is clean.
        """
        doc = await self._db.get_workspace(workspace_id)
        self._validate_transition(doc, "admin_release_stuck", workspace_id)

        now = datetime.now(timezone.utc)
        update = {
            "status": WorkspaceStatus.AVAILABLE,
            "clean_verified": clean_verified,
            "locked_by": None,
            "error": None,
            "stuck_at": None,
            "last_cleaned_at": now,
        }
        await self._db.update_workspace(workspace_id, update)
        logger.warning("workspace %s ADMIN RELEASE STUCK → AVAILABLE by '%s': %s "
                       "(clean_verified=%s)",
                       workspace_id, triggered_by, reason, clean_verified)
        return update

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _validate_transition(self, doc: dict, transition_name: str, workspace_id: str):
        t = WORKSPACE_TRANSITIONS.get(transition_name)
        if t is None:
            raise ValueError(f"Unknown workspace transition: {transition_name}")
        current = doc.get("status")
        if current not in t.from_states:
            raise InvalidWorkspaceStateError(
                workspace_id=workspace_id,
                current_status=current,
                attempted_transition=transition_name,
                allowed_from=list(t.from_states),
            )

    def _check_ownership(self, doc: dict, workspace_id: str, generation_id: str):
        if doc.get("locked_by") != generation_id:
            raise WorkspaceOwnershipError(
                workspace_id, generation_id, doc.get("locked_by")
            )
