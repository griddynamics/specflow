"""
Generation session service

Manages generation session lifecycle from creation to completion:
workspace allocation, state transitions, and progress tracking.

State Machine:
  pending → initializing → running → completed/failed

Key Features:
- Atomic state transitions via Firestore
- Progress checkpointing for retry continuity
- Auto-release of workspaces on completion/failure
"""

from dataclasses import asdict
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
import uuid

from app.core.notifications import notifications
from app.database.interface import IDatabase, ITransactionContext
from app.schemas.generation_workflow_enums import (
    GenerationCheckpoint,
    GenerationStatus,
    WorkspaceStatus,
    parse_checkpoint,
    parse_status,
)
from app.schemas.agent_error_events import AgentErrorEvent, WorkspaceAgentState
from app.schemas.model_token_usage import ModelTokenUsage
from app.schemas.workflow_usage_metrics import (
    WORKFLOW_USAGE_METRICS_FIELD,
    merge_token_usage_into_tree,
)
from app.schemas.workspace_model_usage_store import (
    SESSION_TOTAL_USD_COST_FIELD,
    WORKSPACE_MODEL_USAGE_SUBCOLLECTION,
    merge_workspace_model_usage_subdoc,
)
from app.schemas.planning import PlanningResult
from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL
from app.services.git_archive_service import GitArchiveService
from app.services.workspace_pool import NoAvailableWorkspacesError, WorkspacePoolService
from app.state import GenerationSessionStateMachine, WorkspaceStateMachine
from app.state.api_key_session_concurrency import ApiKeySessionConcurrency
from app.state.db_adapter import COL_GENERATION_SESSIONS, StateMachineDBAdapter
from app.state.exceptions import (
    InvalidGenerationSessionStateError as SMInvalidGenerationSessionStateError,
)
from app.state.transitions import TriggeredBy

logger = logging.getLogger(__name__)


class GenerationSessionError(Exception):
    """Base exception for generation session errors."""
    pass


class GenerationSessionNotFoundError(GenerationSessionError):
    """Raised when the generation session does not exist."""
    pass


class InvalidGenerationSessionStateError(GenerationSessionError):
    """Raised when the generation session is in the wrong state for the operation."""
    pass


class GenerationSessionService:
    """
    Service for managing generation session lifecycle.

    Handles:
    - Creating sessions
    - Allocating workspaces
    - State transitions (pending → initializing → running → completed/failed)
    - Heartbeat (extends lease to prevent auto-recovery)
    - Progress tracking
    - Workspace cleanup on completion
    """
    
    DEFAULT_MAX_RETRIES = 3
    
    def __init__(
        self,
        db: IDatabase,
        workspace_pool: WorkspacePoolService
    ):
        """
        Initialize generation session service.
        
        Args:
            db: Database interface
            workspace_pool: Workspace pool service
        """
        self._db = db
        self.workspace_pool = workspace_pool
        # State machines — wired in Phase 2
        self._db_adapter = StateMachineDBAdapter(db)
        self._api_sessions = ApiKeySessionConcurrency(self._db_adapter)
        _workspace_sm = WorkspaceStateMachine(
            self._db_adapter,
            api_key_sessions=self._api_sessions,
        )
        self._esm = GenerationSessionStateMachine(
            db=self._db_adapter,
            workspace_state_machine=_workspace_sm,
            api_key_sessions=self._api_sessions,
        )
        self._workspace_sm = _workspace_sm
        self._archive_svc = GitArchiveService(
            workspace_root=str(workspace_pool.workspace_base_path)
        )

    # ------------------------------------------------------------------
    # Public data-access helpers — callers must not reach through _db
    # ------------------------------------------------------------------

    @property
    def db_adapter(self) -> "StateMachineDBAdapter":
        """Async-capable db adapter for constructing state machines or ArtifactStore."""
        return self._db_adapter

    @property
    def api_key_sessions(self) -> ApiKeySessionConcurrency:
        """Generation-session concurrency facade (shared by state machines)."""
        return self._api_sessions

    @property
    def generation_session_sm(self) -> GenerationSessionStateMachine:
        """Generation session state machine used for transitions (single shared instance)."""
        return self._esm

    @property
    def db(self) -> "IDatabase":
        """Raw database interface (for legacy callers that require IDatabase, e.g. notifications)."""
        return self._db

    async def begin_allocation(self, generation_id: str, triggered_by: str) -> None:
        await self._esm.begin_allocation(generation_id, triggered_by=triggered_by)

    async def allocation_succeeded(self, generation_id: str, triggered_by: str) -> None:
        await self._esm.allocation_succeeded(generation_id, triggered_by=triggered_by)

    async def reject_generation_session(
        self,
        generation_id: str,
        reason: str,
        triggered_by: str,
    ) -> None:
        await self._esm.reject_contract(
            generation_id=generation_id,
            reason=reason,
            triggered_by=triggered_by,
        )

    def get_generation_session(self, generation_id: str) -> Optional[Dict[str, Any]]:
        """Return the raw Firestore session document, or None if not found."""
        return self._db.get(COL_GENERATION_SESSIONS, generation_id)

    def set_workspace_ids(self, generation_id: str, workspace_ids: list) -> None:
        """Persist workspace_ids on an existing session document."""
        self._db.update(COL_GENERATION_SESSIONS, generation_id, {"workspace_ids": workspace_ids})

    @staticmethod
    def _serialize_planning_data(planning_data: Any) -> dict:
        if isinstance(planning_data, PlanningResult):
            d = asdict(planning_data)
            for p in d.get("phases", []):
                am = p.get("applicable_agent_mcps")
                if isinstance(am, tuple):
                    p["applicable_agent_mcps"] = list(am)
            return d
        if hasattr(planning_data, 'model_dump'):
            return planning_data.model_dump()
        if hasattr(planning_data, 'dict'):
            return planning_data.dict()
        if isinstance(planning_data, dict):
            return planning_data
        return {
            "phase_count": getattr(planning_data, 'phase_count', None),
            "phases": [
                {
                    "number": getattr(p, 'number', None),
                    "name": getattr(p, 'name', None),
                    "description": getattr(p, 'description', None),
                    "estimated_commits": getattr(p, 'estimated_commits', None),
                    **(
                        {"applicable_agent_mcps": list(getattr(p, "applicable_agent_mcps"))}
                        if getattr(p, "applicable_agent_mcps", None) is not None
                        else {}
                    ),
                }
                for p in getattr(planning_data, 'phases', [])
            ],
            "plan_file_path": getattr(planning_data, 'plan_file_path', None),
        }

    async def create_generation_session(
        self,
        user_email: str,
        parameters: Dict[str, Any],
        max_retries: Optional[int] = None,
        *,
        key_uid: Optional[str] = None,
        workspace_pool: Optional[str] = None,
    ) -> str:
        """
        Create a new generation session in pending state.

        Args:
            user_email: Email of user who requested the session (primary user identification)
            parameters: Session parameters (spec_file, model, etc.)
            max_retries: Maximum retry attempts (default: 3)

        Returns:
            generation_id: Unique ID for the session
            
        Example:
            >>> service = GenerationSessionService(db, workspace_pool)
            >>> est_id = await service.create_generation_session(
            ...     user_email="user@example.com",
            ...     parameters={
            ...         "spec_file": "path/to/spec.md",
            ...         "model": "claude-sonnet-4-20250514",
            ...         "notification_email": "user@example.com"
            ...     }
            ... )
        """
        if max_retries is None:
            max_retries = self.DEFAULT_MAX_RETRIES
        
        generation_id = f"est-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)

        # Build doc without status/state_history — state machine writes those
        pool = workspace_pool or DEFAULT_WORKSPACE_POOL

        session_doc = {
            # Core fields
            "generation_id": generation_id,  # Add generation_id to document
            "user_email": user_email,
            "key_uid": key_uid,
            "workspace_pool": pool,
            "workspace_ids": [],
            "parameters": parameters,

            # Timestamps
            "created_at": now,
            "started_at": None,
            "completed_at": None,


            # Retry handling
            "retry_count": 0,
            "max_retries": max_retries,

            # Checkpoint tracking
            "checkpoint": GenerationCheckpoint.FILES_UPLOADED.value,
            "specification_dir": None,
            "workspace_phases": {},
            "workspace_phases_deployment": {},

            # Progress tracking
            "progress": {
                "phase": "pending",
                "completed_phases": [],
                "checkpoint": GenerationCheckpoint.FILES_UPLOADED.value,
                "workspace_progress": {},
            },

            # Results
            "result": None,
            "error": None,
        }

        self._db.set(COL_GENERATION_SESSIONS, generation_id, session_doc)
        await self._esm.create(generation_id, triggered_by=TriggeredBy.CREATE)

        logger.info(f"Created generation session {generation_id} for user {user_email}")

        return generation_id
    
    def merge_generation_session_parameters(
        self,
        generation_id: str,
        parameter_updates: Dict[str, Any],
        top_level_updates: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Atomically merge parameter_updates into ``parameters`` on the session document.

        Reads the current document inside a transaction, merges parameter_updates
        into the existing parameters dict (later keys win), then writes back.
        top_level_updates adds extra top-level fields to the same write (e.g. max_retries).

        Raises ValueError if the session document does not exist.
        """
        def _txn(tx) -> None:
            doc = tx.get(COL_GENERATION_SESSIONS, generation_id)
            if doc is None:
                raise ValueError(f"Generation session {generation_id} not found when persisting parameters")
            params = dict(doc.get("parameters") or {})
            params.update(parameter_updates)
            write: Dict[str, Any] = {"parameters": params}
            if top_level_updates:
                write.update(top_level_updates)
            tx.update(COL_GENERATION_SESSIONS, generation_id, write)

        self._db.run_transaction(_txn)

    async def start_generation_session(
        self,
        generation_id: str,
        workspace_count: int | None = None,
    ) -> List[str]:
        """
        Start a generation session (allocate workspaces and transition to running).

        Steps:
        1. Verify session is in 'pending' state
        2. Transition to 'initializing'
        3. Allocate workspace set
        4. Transition to 'running'

        Args:
            generation_id: The session to start
            workspace_count: Number of active workspaces (1, 2, or 3).
                If None, reads stored value from generation session parameters (SSOT),
                which preserves the original count on retry without re-passing it.

        Returns:
            List of workspace IDs allocated

        Raises:
            GenerationSessionNotFoundError: Session doesn't exist
            InvalidGenerationSessionStateError: Session not in 'pending' state
            NoAvailableWorkspacesError: No workspaces available

        Example:
            >>> workspace_ids = await service.start_generation_session("est-123")
            >>> # Start doing work with workspaces
        """
        # 1. Verify session exists
        session_doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)
        if not session_doc:
            raise GenerationSessionNotFoundError(f"Generation session {generation_id} not found")

        # Workspace count is frozen at first allocation (SSOT in Firestore parameters).
        # If a stored value exists, always use it — ignore any caller-supplied value.
        # This prevents subsequent calls (retry, re-run) from changing the count and
        # producing inconsistent orphaned-workspace state.
        stored_workspace_count = session_doc.get("parameters", {}).get("workspace_count")
        if stored_workspace_count is not None:
            if workspace_count is not None and workspace_count != stored_workspace_count:
                logger.warning(
                    "Ignoring workspace_count=%s for generation session %s — frozen at %s from first run",
                    workspace_count, generation_id, stored_workspace_count,
                )
            workspace_count = stored_workspace_count

        now = datetime.now(timezone.utc)

        # 2. PENDING → INITIALIZING via state machine (raises InvalidGenerationSessionStateError
        #    if current status is not PENDING)
        try:
            await self._esm.begin_allocation(
                generation_id, triggered_by=TriggeredBy.START
            )
        except SMInvalidGenerationSessionStateError as e:
            raise InvalidGenerationSessionStateError(str(e)) from e

        try:
            # 3. Allocate workspaces — or reuse existing ones (HR-2 / Commandment V).
            # If the session already has workspace_ids that are ALLOCATED and locked
            # to this session (e.g. a retry of a crashed-before-allocation run),
            # skip allocation entirely. Overwriting workspace_ids here would orphan the
            # existing ALLOCATED workspaces with no recovery path.
            existing_workspace_ids = session_doc.get("workspace_ids", [])
            if existing_workspace_ids and self._workspaces_still_allocated(
                existing_workspace_ids, generation_id
            ):
                workspace_ids = existing_workspace_ids
                self._db.update(COL_GENERATION_SESSIONS, generation_id, {"started_at": now})
                logger.info(
                    "Reusing existing allocated workspaces %s for generation session %s",
                    workspace_ids, generation_id,
                )
            else:
                workspace_ids = await self.workspace_pool.allocate_workspace_set(
                    generation_id=generation_id,
                    count=workspace_count,
                )
                self._db.update(COL_GENERATION_SESSIONS, generation_id, {
                    "workspace_ids": workspace_ids,
                    "started_at": now,
                })

            # 4. INITIALIZING → RUNNING via state machine
            await self._esm.allocation_succeeded(
                generation_id, triggered_by=TriggeredBy.START
            )

            logger.info(
                f"Started generation session {generation_id} with workspaces {workspace_ids}"
            )

            return workspace_ids

        except NoAvailableWorkspacesError as e:
            # Allocation failed — INITIALIZING → PENDING
            logger.warning(f"Workspace allocation failed for {generation_id}: {e}")
            await self._esm.allocation_failed(
                generation_id, triggered_by=TriggeredBy.START
            )
            raise

        except Exception as e:
            # Other error — mark as failed (no workspace release per Commandment II)
            logger.error(
                f"Failed to start generation session {generation_id}: {e}",
                exc_info=True
            )
            await self._esm.fail(
                generation_id,
                reason=str(e),
                triggered_by=TriggeredBy.START,
            )
            raise

    async def get_workspace_repo_url(self, workspace_id: str) -> Optional[str]:
        """Return the repo_url stored in the Firestore workspace document, or None."""
        ws_doc = await self._db_adapter.get_workspace(workspace_id)
        if not ws_doc:
            return None
        return ws_doc.get("repo_url")

    def _workspaces_still_allocated(
        self, workspace_ids: list, generation_id: str
    ) -> bool:
        """
        Returns True if ALL workspace_ids are currently ALLOCATED and locked by
        this session. Used by start_generation_session to decide whether to reuse or
        reallocate (HR-2 / Commandment V).
        """
        if not workspace_ids:
            return False
        for ws_id in workspace_ids:
            ws_doc = self._db.get("workspaces", ws_id)
            if not ws_doc:
                return False
            if ws_doc.get("status") != WorkspaceStatus.ALLOCATED.value:
                return False
            if ws_doc.get("locked_by") != generation_id:
                return False
        return True

    async def ensure_workspaces_for_sync(
        self,
        generation_id: str,
        workspace_count: int | None = None,
    ) -> list[str]:
        """Return workspace IDs ready for ``POST /workspace/sync`` file upload.

        Reuses ALLOCATED workspaces when still locked by this session. After
        ``reject_contract`` (workspaces released, ``workspace_ids`` cleared), allocates
        a fresh set so ``run_generation`` can upload again on the same ``generation_id``.
        """
        session_doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)
        if not session_doc:
            raise GenerationSessionNotFoundError(f"Generation session {generation_id} not found")

        current_status = parse_status(session_doc.get("status"))
        if current_status not in (GenerationStatus.PENDING, None):
            raise InvalidGenerationSessionStateError(
                f"Generation session {generation_id} is in '{session_doc.get('status')}' state; "
                f"sync reuse is only allowed while pending."
            )

        existing_workspace_ids = session_doc.get("workspace_ids") or []
        if existing_workspace_ids and self._workspaces_still_allocated(
            existing_workspace_ids, generation_id
        ):
            return existing_workspace_ids

        stored_workspace_count = session_doc.get("parameters", {}).get("workspace_count")
        effective_count = (
            stored_workspace_count
            if stored_workspace_count is not None
            else workspace_count
        )
        if workspace_count is not None and stored_workspace_count is not None:
            if workspace_count != stored_workspace_count:
                logger.warning(
                    "Ignoring workspace_count=%s for sync on %s — frozen at %s",
                    workspace_count,
                    generation_id,
                    stored_workspace_count,
                )

        workspace_ids = await self.workspace_pool.allocate_workspace_set(
            generation_id=generation_id,
            count=effective_count,
        )
        self.set_workspace_ids(generation_id, workspace_ids)
        logger.info(
            "Allocated workspaces %s for sync on generation session %s (re-upload after reject or stale ids)",
            workspace_ids,
            generation_id,
        )
        return workspace_ids

    async def complete_generation_session(
        self,
        generation_id: str,
        result: Dict[str, Any]
    ) -> None:
        """
        Mark the generation session as completed and release workspaces.

        Args:
            generation_id: The session to complete
            result: P10Y / workflow result payload to store
            
        Example:
            >>> await service.complete_generation_session(
            ...     "est-123",
            ...     result={"p10y_scores": [...], "commit_count": 5}
            ... )
        """
        session_doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)

        if not session_doc:
            raise GenerationSessionNotFoundError(f"Generation session {generation_id} not found")

        if session_doc.get("status") == GenerationStatus.COMPLETED.value:
            logger.warning(
                f"Generation session {generation_id} is already completed. Skipping."
            )
            return

        # outputs_archived + artifact_path are set in GenerationSessionStateMachine.advance_checkpoint
        # when checkpoint reaches OUTPUTS_ARCHIVED (generation archive_outputs step), or by
        # ArtifactStore.emergency_archive for partial download paths.

        # RUNNING → COMPLETED via state machine.
        # Verifies archive preconditions, releases workspaces (ALLOCATED → CLEANING),
        # writes status=COMPLETED + result.
        try:
            await self._esm.complete(
                generation_id,
                triggered_by=TriggeredBy.orchestrator_step("complete"),
                workspace_archive_service=self._archive_svc,
                result=result,
            )
        except SMInvalidGenerationSessionStateError as e:
            raise InvalidGenerationSessionStateError(str(e)) from e


        logger.info(f"Completed generation session {generation_id}")

    async def update_completed_generation_session_result(
        self,
        generation_id: str,
        result: Dict[str, Any],
    ) -> None:
        """
        Replace stored ``result`` on a **completed** session (e.g. P10Y metrics refresh).

        Does not change status or checkpoints — only the result payload used for
        email and API consumers.
        """
        session_doc = await self._db_adapter.get_generation_session(generation_id)
        if not session_doc:
            raise GenerationSessionNotFoundError(f"Generation session {generation_id} not found")
        if session_doc.get("status") != GenerationStatus.COMPLETED.value:
            raise InvalidGenerationSessionStateError(
                f"Generation session {generation_id} is not completed; cannot update stored result."
            )
        await self._db_adapter.update_generation_session(generation_id, {"result": result})

    async def add_agent_query_token_usage(
        self,
        generation_id: str,
        workflow_key: str,
        workspace_key: str,
        delta: ModelTokenUsage,
        cost_usd: float = 0.0,
    ) -> None:
        """Persist one agent_query worth of usage into nested workflow metrics + flat totals.

        Firestore layout: ``workflow_usage_metrics[workflow][workspaces][ws][models][model_name]``
        (see :mod:`app.schemas.workflow_usage_metrics`). Also updates legacy flat
        ``model_usage`` as the sum of all nested rows for API backward compatibility.

        Per-workspace cumulative tokens and ``total_usd_cost`` are stored in the
        ``workspace_model_usage`` subcollection; the parent session document stores
        ``total_usd_cost`` (cumulative spend for the whole run).
        """
        cost_usd = float(cost_usd or 0.0)
        if delta.is_empty() and cost_usd <= 0.0:
            return

        wf_key = workflow_key or "unknown"
        ws_key = workspace_key or "_"

        def _txn(tx: ITransactionContext) -> None:
            doc = tx.get(COL_GENERATION_SESSIONS, generation_id)
            if not doc:
                return
            tree = doc.get(WORKFLOW_USAGE_METRICS_FIELD) or {}
            if not isinstance(tree, dict):
                tree = {}
            if not delta.is_empty():
                tree = merge_token_usage_into_tree(tree, wf_key, ws_key, delta)

            existing_sub = tx.get_subdocument(
                COL_GENERATION_SESSIONS,
                generation_id,
                WORKSPACE_MODEL_USAGE_SUBCOLLECTION,
                ws_key,
            )
            ws_payload = merge_workspace_model_usage_subdoc(existing_sub, delta, cost_usd)
            tx.set_subdocument(
                COL_GENERATION_SESSIONS,
                generation_id,
                WORKSPACE_MODEL_USAGE_SUBCOLLECTION,
                ws_key,
                ws_payload,
            )

            prev_session_cost = float(doc.get(SESSION_TOTAL_USD_COST_FIELD) or 0.0)
            session_cost = prev_session_cost + cost_usd

            update_fields: Dict[str, Any] = {
                WORKFLOW_USAGE_METRICS_FIELD: tree,
                SESSION_TOTAL_USD_COST_FIELD: session_cost,
            }
            if not delta.is_empty():
                flat = ModelTokenUsage.from_dict(doc.get("model_usage") or {}) + delta
                update_fields["model_usage"] = flat.to_flat_dict()

            tx.update(COL_GENERATION_SESSIONS, generation_id, update_fields)

        await self._db_adapter.run_sync_transaction(_txn)

    def update_spec_analysis_snapshot(
        self,
        generation_id: str,
        *,
        readiness: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> None:
        """Persist last spec check outcome for clients polling status."""
        payload: Dict[str, Any] = {}
        if readiness is not None:
            payload["last_spec_readiness"] = readiness
        if summary is not None:
            payload["last_spec_summary"] = summary
        if payload:
            self._db.update(COL_GENERATION_SESSIONS, generation_id, payload)

    async def fail_generation_session(
        self,
        generation_id: str,
        error: str,
        *,
        triggered_by: Optional[str] = None,
    ) -> None:
        """
        Mark the generation session as failed. Workspaces stay ALLOCATED (Commandment II).

        Args:
            generation_id: The session to fail
            error: Error message
            
        Example:
            >>> await service.fail_generation_session("est-123", "Timeout after 1 hour")
        """
        session_doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)

        if not session_doc:
            raise GenerationSessionNotFoundError(f"Generation session {generation_id} not found")

        # RUNNING | INITIALIZING | ANALYSIS | PENDING → FAILED via state machine.
        # Commandment II: workspaces stay ALLOCATED. No workspace release. Code preserved.
        tb = triggered_by if triggered_by is not None else TriggeredBy.orchestrator_step("fail")
        await self._esm.fail(
            generation_id,
            reason=error,
            triggered_by=tb,
        )


        logger.info(f"Generation session {generation_id} marked failed: {error}")
        
        # Send Slack notification about the failure
        try:
            user_email = session_doc.get("user_email", "unknown")
            parameters = session_doc.get("parameters", {})
            spec_path = parameters.get("spec_file", "unknown")
            
            # Format error message (truncate if too long)
            error_preview = error[:500] if len(error) > 500 else error
            if len(error) > 500:
                error_preview += "..."
            
            notification_message = (
                f"❌ Run Failed\n\n"
                f"*Run ID:* `{generation_id}`\n"
                f"*User:* {user_email}\n"
                f"*Spec File:* {spec_path}\n"
                f"*Error:* {error_preview}"
            )
            
            notifications.notify(
                notification_message,
                recipient_email=user_email
            )
        except Exception as e:
            # Don't fail the session failure handling if notification fails
            logger.error(f"Failed to send Slack notification for generation session {generation_id}: {e}", exc_info=True)

    async def cancel_generation_session(
        self,
        generation_id: str,
        *,
        triggered_by: str = TriggeredBy.CANCEL,
    ) -> None:
        """
        Mark the generation session CANCELLED (user-initiated). Notification-free.

        Deliberately does NOT route through ``fail_generation_session`` — a user
        cancellation must not emit the "❌ Run Failed" Slack/email notification, and
        must land in the distinct terminal CANCELLED state (never resumed, never
        retried). Workspaces stay ALLOCATED (Commandment II); the state machine is the
        single writer of status.

        Args:
            generation_id: The session to cancel
            triggered_by: TriggeredBy constant (defaults to CANCEL)

        Raises:
            GenerationSessionNotFoundError: Session doesn't exist
            InvalidGenerationSessionStateError: Session is already terminal
        """
        session_doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)
        if not session_doc:
            raise GenerationSessionNotFoundError(f"Generation session {generation_id} not found")

        await self._esm.cancel(generation_id, triggered_by=triggered_by)
        logger.info("Generation session %s cancelled by user", generation_id)

    async def get_generation_session_status(self, generation_id: str) -> Dict[str, Any]:
        """
        Get current generation session status.

        Args:
            generation_id: The session to query

        Returns:
            Session document

        Raises:
            GenerationSessionNotFoundError: Session doesn't exist
        """
        session_doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)
        
        if not session_doc:
            raise GenerationSessionNotFoundError(f"Generation session {generation_id} not found")
        
        return session_doc
    
    async def update_progress(
        self,
        generation_id: str,
        phase: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Update session progress (for retry continuity).
        
        Args:
            generation_id: The session to update
            phase: Current phase (e.g., "analysis", "implementation", "testing")
            metadata: Additional progress metadata
            
        Example:
            >>> await service.update_progress(
            ...     "est-123",
            ...     phase="implementation",
            ...     metadata={"last_commit_sha": "abc123", "files_generated": ["src/main.py"]}
            ... )
        """
        session_doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)
        
        if not session_doc:
            logger.warning(f"Cannot update progress for non-existent generation session {generation_id}")
            return
        
        # Update progress
        progress = session_doc.get("progress", {})
        
        # Add current phase to completed phases if new
        completed_phases = progress.get("completed_phases", [])
        current_phase = progress.get("phase")
        
        if current_phase and current_phase not in completed_phases:
            completed_phases.append(current_phase)
        
        # Update progress
        new_progress = {
            "phase": phase,
            "completed_phases": completed_phases,
        }
        
        if metadata:
            new_progress.update(metadata)
        
        self._db.update(COL_GENERATION_SESSIONS, generation_id, {
            "progress": new_progress
        })
        
        logger.info(f"Updated progress for generation session {generation_id}: phase={phase}")
    
    async def update_checkpoint(
        self,
        generation_id: str,
        checkpoint: GenerationCheckpoint,
        specification_dir: Optional[str] = None
    ) -> None:
        """
        Update the current checkpoint.
        
        Args:
            generation_id: The session to update
            checkpoint: The checkpoint to set
            specification_dir: Optional specification directory path (only set when checkpoint is FILES_UPLOADED)

        Raises:
            GenerationSessionNotFoundError: If generation session doesn't exist
            ValueError: If checkpoint transition is invalid

        Example:
            >>> await service.update_checkpoint(
            ...     "est-123",
            ...     GenerationCheckpoint.FILES_UPLOADED,
            ... )
        """
        await self._esm.advance_checkpoint(
            generation_id,
            checkpoint=checkpoint,
            triggered_by=TriggeredBy.orchestrator_step("update_checkpoint"),
            metadata={"specification_dir": specification_dir} if specification_dir is not None else None,
        )
    
    async def update_planning_data(
        self,
        generation_id: str,
        planning_data: Any,  # PlanningResult type
        total_phases: int,
    ) -> bool:
        """
        Store planning data and initialize workspace phase tracking.
        
        Args:
            generation_id: The session to update
            planning_data: Planning result data (will be serialized)
            total_phases: Total number of execution phases
            
        Raises:
            GenerationSessionNotFoundError: If generation session doesn't exist
            
        Example:
            >>> await service.update_planning_data(
            ...     "est-123",
            ...     planning_data=planning_result,
            ...     total_phases=5
            ... )
        """
        planning_data_dict = self._serialize_planning_data(planning_data)
        written = await self._esm.save_generation_plan(
            generation_id=generation_id,
            planning_data_dict=planning_data_dict,
            total_phases=total_phases,
            triggered_by=TriggeredBy.orchestrator_step("update_planning_data"),
        )
        if not written:
            logger.warning(
                "update_planning_data: no workspace_ids for %s — Firestore write skipped",
                generation_id,
            )
            return False

        logger.info(
            "Updated planning data for generation session %s: %d phases",
            generation_id,
            total_phases,
        )
        return True

    async def update_workspace_phase(
        self,
        generation_id: str,
        workspace_id: str,
        completed_phase: int
    ) -> None:
        """
        Update the last completed phase for a workspace.
        
        Args:
            generation_id: The session to update
            workspace_id: The workspace to update
            completed_phase: The phase number that was completed (1-based for execution phases)
            
        Raises:
            GenerationSessionNotFoundError: If generation session doesn't exist
            
        Example:
            >>> await service.update_workspace_phase(
            ...     "est-123",
            ...     workspace_id="ws-1",
            ...     completed_phase=3
            ... )
        """
        session_doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)

        if not session_doc:
            raise GenerationSessionNotFoundError(f"Generation session {generation_id} not found")

        await self._esm.record_phase_progress(
            generation_id,
            workspace_id=workspace_id,
            completed_phase=completed_phase,
            triggered_by=TriggeredBy.orchestrator_step("update_workspace_phase"),
        )

        logger.info(
            f"Updated workspace phase for generation session {generation_id}, "
            f"workspace {workspace_id}: phase {completed_phase}"
        )

    async def record_agent_error_event(self, generation_id: str, event: AgentErrorEvent) -> None:
        """Append one durable agent error/warning event (bounded; see state machine)."""
        await self._esm.record_agent_error_event(
            generation_id,
            event.model_dump(mode="json", exclude_none=True),
            triggered_by=TriggeredBy.orchestrator_step("record_agent_error_event"),
        )

    async def set_workspace_agent_state(
        self,
        generation_id: str,
        workspace_id: str,
        state: Optional[WorkspaceAgentState],
    ) -> None:
        """Set or clear the per-workspace agent-state badge (retrying/aborted)."""
        await self._esm.set_workspace_agent_state(
            generation_id,
            workspace_id=workspace_id,
            state=state.value if state is not None else None,
            triggered_by=TriggeredBy.orchestrator_step("set_workspace_agent_state"),
        )

    async def save_e2e_plan(
        self,
        generation_id: str,
        planning_data: Any,
        total_phases: int,
    ) -> dict:
        """
        Persist E2E plan snapshot immediately after plan conversion (any active status).
        Writes to workspace_phases_deployment. Returns the Firestore update dict,
        or empty dict if workspace_ids are not yet allocated (no-op).
        """
        planning_data_dict = self._serialize_planning_data(planning_data)
        return await self._esm.save_e2e_plan(
            generation_id=generation_id,
            planning_data_dict=planning_data_dict,
            total_phases=total_phases,
            triggered_by=TriggeredBy.orchestrator_step("save_e2e_plan"),
        )

    async def update_deployment_planning_data(
        self,
        generation_id: str,
        planning_data: Any,
        total_phases: int,
    ) -> None:
        """
        Initialize workspace_phases_deployment when the deploy & QA loop starts.

        No-op if already initialized (retry safety — preserves resume points).
        For LOCAL_ONLY sessions this is never called.
        """
        planning_data_dict = self._serialize_planning_data(planning_data)

        await self._esm.init_deployment_phases(
            generation_id=generation_id,
            planning_data_dict=planning_data_dict,
            total_phases=total_phases,
            triggered_by=TriggeredBy.orchestrator_step("update_deployment_planning_data"),
        )

        logger.info(
            f"Initialized deployment planning data for generation session {generation_id}: "
            f"{total_phases} deploy phases"
        )

    async def update_deployment_workspace_phase(
        self,
        generation_id: str,
        workspace_id: str,
        completed_phase: int,
    ) -> None:
        """Record a completed deploy phase for a workspace."""
        await self._esm.record_deployment_phase_progress(
            generation_id=generation_id,
            workspace_id=workspace_id,
            completed_phase=completed_phase,
            triggered_by=TriggeredBy.orchestrator_step("update_deployment_workspace_phase"),
        )

        logger.info(
            f"Updated deployment workspace phase for generation session {generation_id}, "
            f"workspace {workspace_id}: deploy phase {completed_phase}"
        )

    def get_checkpoint(self, generation_id: str) -> GenerationCheckpoint:
        """
        Get the current checkpoint for a generation session.
        
        Args:
            generation_id: The session to query
            
        Returns:
            Current checkpoint, or FILES_UPLOADED if not set
            
        Raises:
            GenerationSessionNotFoundError: If generation session doesn't exist
        """
        session_doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)
        
        if not session_doc:
            raise GenerationSessionNotFoundError(f"Generation session {generation_id} not found")
        
        checkpoint = parse_checkpoint(session_doc.get("checkpoint"))
        if checkpoint is not None:
            return checkpoint

        return GenerationCheckpoint.FILES_UPLOADED
    
    def get_current_phase_name(self, generation_id: str) -> str:
        """
        Compute the current phase name based on checkpoint and workspace progress.
        
        Args:
            generation_id: The session to query
            
        Returns:
            Current phase name (e.g., "plan_synced", "Phase 5: Core Backend API", etc.)
            
        Raises:
            GenerationSessionNotFoundError: If generation session doesn't exist
        """
        session_doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)
        
        if not session_doc:
            raise GenerationSessionNotFoundError(f"Generation session {generation_id} not found")

        checkpoint = self.get_checkpoint(generation_id)

        # For checkpoints that indicate generation is happening, check workspace progress
        if checkpoint in [GenerationCheckpoint.KB_INIT_DONE, GenerationCheckpoint.GENERATION_STARTED]:
            workspace_phases = session_doc.get("workspace_phases", {})
            
            if not workspace_phases:
                return checkpoint.value
            
            # Get planning data from any workspace (they should all have the same)
            planning_data = None
            for ws_data in workspace_phases.values():
                planning_data = ws_data.get("planning_data")
                if planning_data:
                    break
            
            if not planning_data:
                return checkpoint.value
            
            # Extract phases list from planning data
            phases = planning_data.get("phases", [])
            
            if not phases:
                return checkpoint.value
            
            # Get the current phase being worked on from workspace progress
            # Use the first workspace's last_completed_phase to determine current phase
            # (last_completed_phase is the phase that was completed, so current is +1)
            first_ws_data = next(iter(workspace_phases.values()))
            last_completed = first_ws_data.get("last_completed_phase", 0)
            
            # If no execution phases have been completed yet (last_completed_phase = 0),
            # we're still at the checkpoint stage, so return checkpoint value
            if last_completed == 0:
                return checkpoint.value
            
            current_phase_num = last_completed + 1
            
            if current_phase_num > len(phases):
                # All phases completed
                return checkpoint.value
            
            # Get phase name (phases are 1-indexed in the list)
            if current_phase_num <= len(phases):
                phase_info = phases[current_phase_num - 1]
                phase_name = phase_info.get("name", f"Phase {current_phase_num}")
                return phase_name
            
            return checkpoint.value
        
        # For all other checkpoints, just return the checkpoint value
        return checkpoint.value
    
    async def validate_and_clean_workspace_ids(
        self,
        generation_id: str,
        workspace_ids: List[str],
        raise_if_empty: bool = True
    ) -> List[str]:
        """
        Validate that workspace_ids are actually allocated to this session.
        
        Clears stale workspace_ids from the session document if workspaces are not
        actually owned by this session (e.g., released during maintenance).
        
        Args:
            generation_id: The session to validate workspaces for
            workspace_ids: List of workspace IDs to validate
            raise_if_empty: If True, raise an exception if no valid workspaces remain
            
        Returns:
            List of valid workspace IDs that are actually allocated to this session
            
        Raises:
            GenerationSessionNotFoundError: If generation session doesn't exist
            InvalidGenerationSessionStateError: If no valid workspaces remain and raise_if_empty=True
            
        Example:
            >>> valid_workspaces = await service.validate_and_clean_workspace_ids(
            ...     "est-123",
            ...     ["ws-01-1", "ws-01-2", "ws-01-3"]
            ... )
        """
        session_doc = self._db.get(COL_GENERATION_SESSIONS, generation_id)
        
        if not session_doc:
            raise GenerationSessionNotFoundError(f"Generation session {generation_id} not found")
        
        valid_workspace_ids = []
        stale_workspace_ids = []
        
        for ws_id in workspace_ids:
            ws_doc = self._db.get("workspaces", ws_id)
            if ws_doc and ws_doc.get("locked_by") == generation_id and ws_doc.get("status") == WorkspaceStatus.ALLOCATED.value:
                valid_workspace_ids.append(ws_id)
            else:
                stale_workspace_ids.append(ws_id)
                logger.warning(
                    f"Workspace {ws_id} in generation session {generation_id} is not actually allocated "
                    f"to this session (status={ws_doc.get('status') if ws_doc else 'not_found'}, "
                    f"locked_by={ws_doc.get('locked_by') if ws_doc else 'not_found'}). "
                    f"This indicates stale workspace_ids."
                )
        
        if stale_workspace_ids:
            # Clear stale workspace_ids from generation session document
            self._db.update(COL_GENERATION_SESSIONS, generation_id, {
                "workspace_ids": valid_workspace_ids
            })
            logger.warning(
                f"Cleared {len(stale_workspace_ids)} stale workspace_ids from generation session {generation_id}: "
                f"{stale_workspace_ids}"
            )
        
        if not valid_workspace_ids and raise_if_empty:
            raise InvalidGenerationSessionStateError(
                f"Generation session {generation_id} has no valid allocated workspaces. "
                f"All workspace_ids were stale (possibly released during maintenance)."
            )
        
        return valid_workspace_ids
