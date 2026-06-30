"""
Generation session retry service

Allows manual retry of failed generation sessions while preserving progress.

HR-2 decision: if code_archived == False, must reuse same workspace_ids.
If those workspaces are inaccessible, raise an explicit error rather than
silently falling back to fresh workspaces (which would lose generated code).

Key Features:
- Manual retry of failed generation sessions
- Checkpoint preservation — orchestrator skips already-done steps
- Workspace reuse enforced when code has not been archived to git
- MaxRetriesExceededError propagated when retry limit is reached
"""

import logging
from typing import List

from app.database.interface import IDatabase
from app.schemas.generation_workflow_enums import GenerationStatus, WorkspaceStatus
from app.services.workspace_pool import WorkspacePoolService
from app.state import GenerationSessionStateMachine
from app.state.db_adapter import COL_GENERATION_SESSIONS, COL_WORKSPACES, StateMachineDBAdapter
from app.state.transitions import TriggeredBy

logger = logging.getLogger(__name__)


class GenerationSessionRetryError(Exception):
    """Base exception for generation session retry errors."""
    pass


class InvalidRetryStateError(GenerationSessionRetryError):
    """Raised when the session is in the wrong state for retry."""
    pass


class GenerationSessionRetryService:
    """
    Service for manually retrying failed generation sessions.

    Delegates state transitions to GenerationSessionStateMachine.reset_for_retry().
    Enforces workspace-reuse constraint when code_archived is False (HR-2).
    """

    def __init__(
        self,
        db: IDatabase,
        workspace_pool: WorkspacePoolService,
        esm: GenerationSessionStateMachine = None,
    ):
        self._db = db
        self.workspace_pool = workspace_pool
        # Build async db adapter for state machine calls
        self._db_adapter = StateMachineDBAdapter(db)
        self._esm = esm or GenerationSessionStateMachine(self._db_adapter)

    async def retry_generation_session(
        self,
        generation_id: str,
        triggered_by: str = TriggeredBy.MANUAL_RETRY,
    ) -> str:
        """
        Retry a failed generation session.

        Workspace reuse logic (HR-2 decision):
        - If code_archived == False: MUST reuse same workspace_ids.
          If those workspaces are not accessible, raise explicit error.
          (Falling back to fresh workspaces would lose generated code.)
        - If code_archived == True: fresh workspaces are acceptable.

        Args:
            generation_id: The generation session to retry.
            triggered_by: Who triggered this retry (default: manual retry).

        Raises:
            GenerationSessionRetryError: Session not found
            InvalidRetryStateError: Session not in FAILED state
            MaxRetriesExceededError: retry_count >= max_retries
            ValueError: code_archived=False but workspaces are inaccessible

        Returns:
            generation_id (same as input — we reuse the ID)
        """
        doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)

        if not doc:
            raise GenerationSessionRetryError(f"Generation session {generation_id} not found")

        current_status = doc["status"]
        if current_status not in (
            GenerationStatus.FAILED.value,
            GenerationStatus.PENDING.value,
        ):
            raise InvalidRetryStateError(
                f"Cannot retry generation session in '{current_status}' state. "
                f"Session must be in '{GenerationStatus.FAILED.value}' or "
                f"'{GenerationStatus.PENDING.value}' (stuck) state to retry."
            )

        # HR-2: enforce workspace reuse when code has not been archived
        code_archived = doc.get("code_archived", False)
        workspace_ids = doc.get("workspace_ids", [])

        if not code_archived and workspace_ids:
            accessible = await self._check_workspaces_reusable(
                workspace_ids, generation_id
            )
            if not accessible:
                raise ValueError(
                    f"Cannot retry generation session {generation_id}: "
                    f"code_archived=False but workspace_ids {workspace_ids} are not accessible. "
                    f"Data loss would occur if proceeding with fresh workspaces. "
                    f"Contact an operator to force-release the workspaces first."
                )

        if current_status == GenerationStatus.PENDING.value:
            # Session is already PENDING — a previous retry's background task
            # crashed before allocation started. Skip reset_for_retry() (which would
            # increment retry_count again) and just re-fire the workflow.
            logger.info(
                "retry_generation_session: session %s is already PENDING (stuck) — "
                "re-triggering workflow without incrementing retry_count",
                generation_id,
            )
        else:
            # Normal path: FAILED → PENDING via state machine.
            # Raises MaxRetriesExceededError if retry_count >= max_retries.
            await self._esm.reset_for_retry(
                generation_id=generation_id,
                triggered_by=triggered_by,
                metadata={
                    "code_archived": code_archived,
                },
            )

        # The orchestrator will resume from the saved checkpoint.
        await self._run_generation_session(generation_id)

        logger.info(
            "retry_generation_session: session %s queued for retry (triggered_by=%s)",
            generation_id, triggered_by,
        )
        return generation_id

    async def _run_generation_session(self, generation_id: str) -> None:
        """
        Hook called after reset_for_retry. In Phase 3 this is a no-op —
        the PENDING session is picked up by the normal allocation flow.
        Phase 4 will wire this to WorkflowOrchestrator.run().
        """

    async def _check_workspaces_reusable(
        self,
        workspace_ids: List[str],
        generation_id: str,
    ) -> bool:
        """
        Returns True if ALL workspaces in workspace_ids are currently
        ALLOCATED and locked by this session.

        A workspace in CLEANING or STUCK may still hold the generated code
        but the retry path cannot safely reuse it without manual operator
        intervention (force-release). Return False and let the caller raise.
        """
        for ws_id in workspace_ids:
            ws_doc = self._db.get(COL_WORKSPACES, ws_id)
            if not ws_doc:
                logger.debug("Workspace %s not found", ws_id)
                return False

            ws_status = ws_doc.get("status")
            locked_by = ws_doc.get("locked_by")

            if ws_status != WorkspaceStatus.ALLOCATED.value:
                logger.debug(
                    "Workspace %s status is '%s', cannot reuse (not ALLOCATED)",
                    ws_id, ws_status,
                )
                return False

            if locked_by != generation_id:
                logger.debug(
                    "Workspace %s locked_by='%s', not '%s', cannot reuse",
                    ws_id, locked_by, generation_id,
                )
                return False

        logger.info(
            "All workspaces %s are ALLOCATED and locked by %s — reusable",
            workspace_ids, generation_id,
        )
        return True
