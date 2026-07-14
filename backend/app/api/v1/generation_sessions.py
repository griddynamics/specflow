"""
Generation session API endpoints.

Provides REST API for orchestration runs stored in ``generation_sessions``:
- Run full workflow (codegen/deploy + P10Y per-workspace estimation, persisted as one session)
- Get session status and details
- Retry a failed session
- Cancel a running session
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from app.core.config import settings
from app.core.mcp_config import enabled_mcps_to_parameter_string
from app.core.logging import create_agent_logger
from app.core.notifications import notifications
from app.core.telemetry_decorator import track_event
from app.core.telemetry_context import TelemetryContext
from app.api.dependencies import (
    require_generation_session_owner,
    require_generation_session_owner_form,
    require_generation_session_owner_or_admin,
)
from app.database.dependencies import get_db
from app.database.interface import IDatabase
from app.schemas.estimate import (
    ResendEmailResponse,
    MultiWorkspaceEstimationResponse,
    EstimationSummary,
    WorkspaceEstimation,
    ComparativeAnalysis,
    ComponentComparison,
    RiskAssessment,
    SkippedWorkspaceP10Y,
)
from app.schemas.generation_workflow_enums import (
    GenerationCheckpoint,
    GenerationStatus,
    parse_checkpoint,
    parse_status,
)
from app.schemas.llm_tier import (
    LLMTier,
    WorkflowName,
    WORKFLOW_TIER_MAP,
    assign_model_to_workspace,
    build_llm_overrides,
    parse_model_list,
    resolve_model,
)
from app.schemas.telemetry_workflow import PhaseKind
from app.schemas.model_validation import TierValidation, TierValidationStatus
from app.schemas.specification import GenerationWorkflowRequest
from app.services.contract_validator import RejectionCode
from app.services.model_validation import validate_models_config
from app.services.generation_session import (
    GenerationSessionNotFoundError,
    GenerationSessionService,
    InvalidGenerationSessionStateError,
)
from app.services.generation_session_retry import GenerationSessionRetryService, InvalidRetryStateError
from app.services.generation_workflow_runner import (
    rerun_generation_session,
    run_generation_and_estimation,
    _build_simplified_response,
    _multi_workspace_result_to_store,
    _handle_workflow_exception,
)
from app.core.artifact_files import MULTI_WORKSPACE_REPORT_HTML_FILE
from app.core.artifact_subdirs import REPORT_SUBDIR
from app.services.workspace_pool import WorkspacePoolService
from app.services.artifact_store import ARTIFACTS_BASE, ArtifactStore
from app.services.agent_stream_broker import get_agent_stream_broker
from app.services.mcp_prune import resolve_enabled_mcps_and_set_telemetry
from app.services.generation_task_registry import GenerationTaskRegistry
from app.schemas.model_token_usage import ModelTokenUsage
from app.schemas.workflow_usage_metrics import (
    aggregate_model_usage_by_workspace,
    model_names_by_workspace,
)
from app.utils.formatting import format_token_count
from app.state.api_key_session_concurrency import (
    ApiKeyDocMissingError,
    ApiKeySessionConcurrency,
    OperationKind,
    SessionAtCapacityError,
    SessionBeginOutcome,
)
from app.state.db_adapter import COL_GENERATION_SESSIONS, StateMachineDBAdapter
from app.state.workspace_models import resolve_workspace_model
from app.state.transitions import TriggeredBy
from app.utils.file_utils import normalize_path_parameter
from app.workflows.multi_workspace_estimation_p10y import (
    multi_workspace_estimation_p10y_workflow,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/generation-sessions", tags=["generation_sessions"])


def _model_unavailable_message(tier: TierValidation, provider: str) -> str:
    """Human-readable MODEL_UNAVAILABLE message for the first blocking tier."""
    invalid = [m for m in tier.models if m.status == TierValidationStatus.INVALID]
    names = ", ".join(m.configured for m in invalid)
    suggestion = next((m.suggestion for m in invalid if m.suggestion), None)
    msg = (
        f"The model(s) configured for {tier.tier.value} aren't available on "
        f"{provider}: {names}."
    )
    if suggestion:
        msg += f" Did you mean '{suggestion}'?"
    msg += f" Fix {tier.tier.value} in your MCP config and try again."
    return msg


# ============================================================
# Request/Response Models
# ============================================================

class GenerationStatusResponse(BaseModel):
    """Response with generation session status and details."""
    generation_id: str
    user_email: str
    status: str
    workspace_ids: list[str]
    parameters: Dict[str, Any]
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    retry_count: int
    max_retries: int
    progress: Dict[str, Any]
    result: Optional[Dict[str, Any]]
    error: Optional[str]


class WorkspacePhaseSummary(BaseModel):
    """Per-workspace phase progress, slimmed for the sessions list (no planning_data)."""
    last_completed_phase: int = 0
    phase_name: str = ""


class GenerationSessionListItem(BaseModel):
    """One row of the sessions list — active or completed."""
    generation_id: str
    status: str = "unknown"
    checkpoint: str = ""
    created_at: str = ""
    current_phase: str = ""
    workspace_phases: Dict[str, WorkspacePhaseSummary] = Field(default_factory=dict)


def _format_timestamp(val: Any) -> str:
    """ISO-format a Firestore timestamp/datetime; '' when absent."""
    if val is None:
        return ""
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _slim_workspace_phases(raw: Any) -> Dict[str, WorkspacePhaseSummary]:
    """Project per-workspace phase progress down to the fields the list needs."""
    if not isinstance(raw, dict):
        return {}
    return {
        ws_id: WorkspacePhaseSummary(
            last_completed_phase=(data or {}).get("last_completed_phase", 0),
            phase_name=(data or {}).get("phase_name", ""),
        )
        for ws_id, data in raw.items()
    }


def _session_list_item(session: Dict[str, Any]) -> GenerationSessionListItem:
    """Build a list item from a raw ``generation_sessions`` document.

    Single source of truth for both the ``key_uid`` query path and the
    no-``key_uid`` fallback, so the two paths cannot drift in shape.
    """
    return GenerationSessionListItem(
        generation_id=session.get("generation_id") or session.get("_id", ""),
        status=session.get("status", "unknown"),
        checkpoint=session.get("checkpoint", ""),
        created_at=_format_timestamp(session.get("created_at")),
        current_phase=session.get("current_phase", ""),
        workspace_phases=_slim_workspace_phases(session.get("workspace_phases")),
    )


class RetryGenerationSessionResponse(BaseModel):
    """Response after retrying a generation session."""
    generation_id: str
    status: str
    retry_count: int


class CancelGenerationSessionResponse(BaseModel):
    """Response after cancelling a generation session."""
    generation_id: str
    status: str
    message: str


class RunGenerationSessionRequest(BaseModel):
    """Request to run the full generation session workflow (codegen + P10Y estimation)."""
    spec_path: str = Field(..., description="Path to specification files")
    outputs_dir: str = Field(default="specflow", description="Output directory")
    user_email: str = Field(..., description="User email (primary user identification)")
    notification_email: Optional[str] = Field(None, description="Email for completion notification")
    session_id: Optional[str] = Field(None, description="Optional session ID")
    max_retries: Optional[int] = Field(3, description="Maximum retry attempts")


class RunGenerationSessionResponse(BaseModel):
    """Response after starting a generation session workflow."""
    generation_id: str = Field(..., description="Generation session ID for tracking")
    status: str = Field(..., description="Current status (pending)")
    message: str = Field(..., description="Status message")


# ============================================================
# Dependency Injection
# ============================================================

def get_workspace_pool(db: IDatabase = Depends(get_db)) -> WorkspacePoolService:
    """Get workspace pool service instance."""
    return WorkspacePoolService(db)


def get_generation_session_service(
    db: IDatabase = Depends(get_db),
    workspace_pool: WorkspacePoolService = Depends(get_workspace_pool)
) -> GenerationSessionService:
    """Get generation session service instance."""
    return GenerationSessionService(db, workspace_pool)


def get_generation_session_retry_service(
    db: IDatabase = Depends(get_db),
    workspace_pool: WorkspacePoolService = Depends(get_workspace_pool)
) -> GenerationSessionRetryService:
    """Get generation session retry service instance."""
    return GenerationSessionRetryService(db, workspace_pool)


def get_generation_task_registry(request: Request) -> GenerationTaskRegistry:
    """Provide the process-local task registry created once at app startup."""
    return request.app.state.generation_task_registry


# ============================================================
# Endpoints
# ============================================================


@router.get(
    "/",
    response_model=list[GenerationSessionListItem],
    summary="List recent generation sessions for the authenticated API key",
)
async def list_generation_sessions(
    request: Request,
    db: IDatabase = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
) -> list[GenerationSessionListItem]:
    """Return up to ``limit`` recent sessions for the caller's API key, newest first.

    Queries the ``generation_sessions`` collection by ``key_uid`` so completed and
    failed sessions are included (the ``active_generation_sessions`` list on the API
    key document only tracks in-flight leases and is cleared on completion).

    Falls back to the active-sessions snapshot when the key has no ``key_uid``
    (older keys created before that field was added). The fallback resolves each
    active id's real status from its session document — never a flat "unknown".
    """
    api_key = getattr(request.state, "api_key", None)
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    doc = db.get("api_keys", api_key)
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    key_uid = doc.get("key_uid")
    if key_uid:
        sessions = db.query(
            COL_GENERATION_SESSIONS,
            filters=[("key_uid", "==", key_uid)],
            order_by="-created_at",
            limit=limit,
        )
        return [_session_list_item(s) for s in sessions]

    # Fallback: key has no key_uid (created before that field existed). The lease
    # snapshot only knows active generation ids, so resolve each one's real status
    # from its session document and reuse the same builder as the primary path.
    adapter = StateMachineDBAdapter(db)
    session_concurrency = ApiKeySessionConcurrency(adapter)
    try:
        snap = await session_concurrency.snapshot(api_key_doc_id=api_key)
    except ApiKeyDocMissingError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    items = [
        _session_list_item(session)
        for active in snap.active
        if (session := db.get(COL_GENERATION_SESSIONS, active.generation_id))
    ]
    items.sort(key=lambda it: it.created_at, reverse=True)
    return items[:limit]


@router.get("/{generation_id}", response_model=GenerationStatusResponse)
@track_event(event_name="get_generation_session_triggered")
async def get_generation_session(
    generation_id: str,
    _: None = Depends(require_generation_session_owner),
    generation_session_service: GenerationSessionService = Depends(get_generation_session_service)
):
    """
    Get generation session details and current status.

    Returns the full Firestore-backed session document including:
    - Current status
    - Workspace IDs (if allocated)
    - Progress information
    - Results (if completed)
    - Error message (if failed)
    
    Example:
        ```
        GET /api/v1/generation-sessions/est-abc123
        ```
    """
    try:
        session_doc = await generation_session_service.get_generation_session_status(generation_id)
        
        return GenerationStatusResponse(
            generation_id=generation_id,
            user_email=session_doc["user_email"],
            status=session_doc["status"],
            workspace_ids=session_doc.get("workspace_ids", []),
            parameters=session_doc.get("parameters", {}),
            created_at=session_doc["created_at"].isoformat(),
            started_at=session_doc["started_at"].isoformat() if session_doc.get("started_at") else None,
            completed_at=session_doc["completed_at"].isoformat() if session_doc.get("completed_at") else None,
            retry_count=session_doc.get("retry_count", 0),
            max_retries=session_doc.get("max_retries", 3),
            progress=session_doc.get("progress", {}),
            result=session_doc.get("result"),
            error=session_doc.get("error")
        )
    
    except GenerationSessionNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    
    except Exception as e:
        logger.error(f"Failed to get generation session {generation_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get generation session: {str(e)}"
        )


def _ws_usage_view(usage: Optional[ModelTokenUsage]) -> Optional[Dict[str, int]]:
    """Compact per-workspace token/turn dict for the status payload, or None.

    Returns None when there is no usage yet so the TUI stats panel can render a
    placeholder rather than a row of zeros.
    """
    if usage is None or usage.is_empty():
        return None
    return {
        "num_turns": usage.num_turns,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "total_tokens": usage.total_tokens,
    }


# Phase-name shown while the workspace is still in the KB-init/setup stage
# (checkpoint before KB_INIT_DONE). The stored planning_data phases describe the
# code-generation phases, which only begin after KB init — surfacing phases[0]
# here would mislabel the KB-init step as "Phase 1".
KB_INIT_PHASE_NAME = "Knowledge Base Initialization with Rosetta"


def _ws_phase_view_entry(
    ws_data: dict,
    usage: Optional[ModelTokenUsage],
    models: list[str],
    kb_init_in_progress: bool = False,
) -> Dict[str, Any]:
    """Lean per-workspace status entry: phase counters + optional usage/models.

    ``usage`` and ``models`` are only added when present so the polled response
    stays small (and existing phase-only consumers keep their exact shape) while
    the TUI workspace drill-in gets per-variant stats when available.

    While ``kb_init_in_progress`` (session checkpoint before KB_INIT_DONE) the
    phase name reports the KB-init step rather than the first code-gen phase.
    """
    last = ws_data.get("last_completed_phase", 0) or 0
    phase_name = (
        KB_INIT_PHASE_NAME if kb_init_in_progress else _current_phase_name(ws_data, last)
    )
    entry: Dict[str, Any] = {
        "last_completed_phase": last,
        "total_phases": ws_data.get("total_phases"),
        "phase_name": phase_name,
    }
    usage_view = _ws_usage_view(usage)
    if usage_view is not None:
        entry["usage"] = usage_view
    if models:
        entry["models"] = models
    return entry


def _current_phase_name(ws_data: dict, last_completed_phase: int) -> str:
    """Name of the phase a workspace is currently working on.

    Phases complete in order, so the in-flight phase is the one at index
    ``last_completed_phase`` (0-based) in the stored planning data; once every
    phase is done we report the last phase's name. Returns "" when no planning
    data is available.
    """
    phases = (ws_data.get("planning_data") or {}).get("phases") or []
    if not phases:
        return ""
    index = min(last_completed_phase, len(phases) - 1)
    phase = phases[index]
    if isinstance(phase, dict):
        return phase.get("name") or phase.get("description") or ""
    return ""


def _llm_overrides_from_session_parameters(parameters: dict) -> Dict[str, str]:
    """Rebuild per-tier overrides stored flat on the generation session parameters."""
    return {
        tier.value: parameters[tier.value]
        for tier in LLMTier
        if parameters.get(tier.value)
    }


_GENERATION_PHASE_PREFIX = f"{PhaseKind.GENERATION.value}_phase_"


def _active_workflow_prefix(
    ws_data: dict,
    kb_init_in_progress: bool,
) -> str:
    """Workflow key prefix for the step currently active on a workspace.

    Used to scope the model list in the TUI workspace drill-in. Usage tokens stay
    on the full lifetime aggregate (see ``_ws_usage_and_models``).
    """
    if kb_init_in_progress:
        return WorkflowName.KB_INIT.value
    last = int(ws_data.get("last_completed_phase") or 0)
    return f"{_GENERATION_PHASE_PREFIX}{last + 1}_"


def _configured_model_for_active_step(
    session_doc: dict,
    ws_id: str,
    workflow_prefix: str,
) -> Optional[str]:
    """Best-effort model configured for the active workflow on this workspace.

    Fills the gap while an ``agent_query`` is still in flight (usage is only
    flushed on the terminal SDK message, so the scoped metrics tree is empty).
    """
    parameters = session_doc.get("parameters") or {}
    overrides = _llm_overrides_from_session_parameters(parameters)
    ws_overrides = session_doc.get("workspace_model_overrides") or {}

    if workflow_prefix == WorkflowName.KB_INIT.value:
        base = resolve_model(
            WORKFLOW_TIER_MAP[WorkflowName.KB_INIT],
            settings,
            overrides,
            return_first=True,
        )
    elif workflow_prefix.startswith(_GENERATION_PHASE_PREFIX):
        workspace_ids = session_doc.get("workspace_ids") or []
        try:
            ws_index = workspace_ids.index(ws_id)
        except ValueError:
            ws_index = 0
        tier_value = resolve_model(
            WORKFLOW_TIER_MAP[WorkflowName.GENERATE_APP],
            settings,
            overrides,
            return_first=False,
        )
        try:
            models = parse_model_list(tier_value)
        except ValueError:
            logger.warning("Could not parse model list for active step display (ws=%s): %r", ws_id, tier_value)
            return None
        base = assign_model_to_workspace(ws_index, models)
    else:
        return None

    return resolve_workspace_model(ws_id, base, ws_overrides, logger)


def _ws_models_for_view(
    usage_tree: Optional[Dict[str, Any]],
    ws_id: str,
    session_doc: dict,
    ws_data: dict,
    kb_init_in_progress: bool,
) -> list[str]:
    """Model name(s) for the TUI workspace drill-in — active step only.

    Scopes to the workflow currently running on the workspace so earlier steps do not leak.
    When the active step has not finished an ``agent_query`` yet, falls back to
    the configured tier model for that step (including per-workspace overrides).
    """
    prefix = _active_workflow_prefix(ws_data, kb_init_in_progress)
    models = model_names_by_workspace(usage_tree, prefix).get(ws_id, [])
    if models:
        return models
    configured = _configured_model_for_active_step(session_doc, ws_id, prefix)
    return [configured] if configured else []


def _ws_usage_and_models(
    usage_tree: Optional[Dict[str, Any]],
    ws_id: str,
    session_doc: dict,
    ws_data: dict,
    kb_init_in_progress: bool,
) -> "tuple[Optional[ModelTokenUsage], list[str]]":
    """Per-workspace usage (lifetime) + model names (active step).

    Usage is the workspace's cumulative total across every workflow that has run
    on it — converters, KB init, and each completed generation phase. Usage only
    lands in ``workflow_usage_metrics`` when an ``agent_query`` returns its
    terminal message, so the aggregate updates step-by-step and stays visible
    while the current step is still in flight.

    Models are scoped to the active workflow (with configured-model fallback) so
    the TUI shows what is running now, not every model used earlier in the run.
    """
    usage = aggregate_model_usage_by_workspace(usage_tree).get(ws_id)
    models = _ws_models_for_view(
        usage_tree, ws_id, session_doc, ws_data, kb_init_in_progress
    )
    return usage, models


@router.get("/{generation_id}/status")
@track_event(event_name="check_status_triggered")
async def get_generation_session_status(
    generation_id: str,
    request: Request,
    _: None = Depends(require_generation_session_owner),
    generation_session_service: GenerationSessionService = Depends(get_generation_session_service)
):
    """
    Get generation session status (lightweight endpoint).
    
    Returns only the essential status information:
    - Current status
    - Progress (with dynamically computed current phase)
    - Error (if failed)
    
    This is a lighter-weight alternative to GET /generation-sessions/{id}
    for polling status without fetching full details.
    
    Example:
        ```
        GET /api/v1/generation-sessions/est-abc123/status
        ```
    """
    try:
        session_doc = await generation_session_service.get_generation_session_status(generation_id)
        
        # Get progress and compute current phase dynamically
        progress = session_doc.get("progress", {}).copy()
        
        # Compute current phase based on checkpoint and workspace progress
        current_phase_name = "Unknown"
        try:
            current_phase_name = generation_session_service.get_current_phase_name(generation_id)
            progress["phase"] = current_phase_name
        except Exception as e:
            logger.warning(f"Failed to compute current phase for {generation_id}: {e}")
            # Fall back to stored phase if computation fails
            if "phase" in progress:
                current_phase_name = progress["phase"]
        
        # Get checkpoint for response
        checkpoint = generation_session_service.get_checkpoint(generation_id)
        
        # Compute completed phases info from workspace_phases
        workspace_phases = session_doc.get("workspace_phases", {})
        completed_phases_info = []
        if workspace_phases:
            # Find minimum completed phase across all workspaces
            min_completed = min(
                (ws_data.get("last_completed_phase", 0) for ws_data in workspace_phases.values()),
                default=0
            )
            total_phases = next(
                (ws_data.get("total_phases") for ws_data in workspace_phases.values() if ws_data.get("total_phases")),
                None
            )

            if total_phases is not None:
                completed_phases_info = [f"Phase {i}" for i in range(1, min_completed + 1)]

        # Lean per-workspace view for clients (e.g. the TUI dashboard). The stored
        # workspace_phases carries the full planning_data per workspace; we surface
        # only the progress counters plus the name of the phase currently in flight
        # (derived from planning_data.phases) so the response stays small enough to
        # poll on a few-second cadence. Per-workspace usage/models are derived from
        # the existing workflow_usage_metrics tree (no new endpoint) so the TUI
        # workspace drill-in can show per-variant stats; both stay optional.
        usage_tree = session_doc.get("workflow_usage_metrics")
        # Before KB_INIT_DONE the workspace is running KB init (not a code-gen
        # phase), so the per-workspace phase name reports the KB-init step.
        kb_init_in_progress = not GenerationCheckpoint.is_at_or_after(
            checkpoint, GenerationCheckpoint.KB_INIT_DONE
        )
        # Per-workspace usage is the full lifetime aggregate; models are scoped to
        # the active workflow (with configured-model fallback while a step is in
        # flight). Usage flushes only when an agent_query finishes.
        workspace_phases_view: Dict[str, Any] = {}
        for ws_id, ws_data in workspace_phases.items():
            ws_usage, ws_models = _ws_usage_and_models(
                usage_tree,
                ws_id,
                session_doc,
                ws_data,
                kb_init_in_progress=kb_init_in_progress,
            )
            workspace_phases_view[ws_id] = _ws_phase_view_entry(
                ws_data,
                ws_usage,
                ws_models,
                kb_init_in_progress=kb_init_in_progress,
            )

        usage = ModelTokenUsage.from_generation_session_doc(session_doc)
        tok_used = usage.total_tokens
        response = {
            "generation_id": generation_id,
            "status": session_doc["status"],
            "current_phase": current_phase_name,
            "checkpoint": checkpoint.value,
            "completed_phases": completed_phases_info if completed_phases_info else progress.get("completed_phases", []),
            "progress": progress,
            "retry_count": session_doc.get("retry_count", 0),
            "error": session_doc.get("error"),
            "workspace_count": session_doc.get("parameters", {}).get("workspace_count", 3),
            "num_turns": usage.num_turns,
            "total_tokens_used": tok_used,
            "total_tokens_used_display": format_token_count(tok_used),
            "last_spec_readiness": session_doc.get("last_spec_readiness"),
            "last_spec_summary": session_doc.get("last_spec_summary"),
            "workspace_phases": workspace_phases_view,
        }

        # 2.5a: include result fields when COMPLETED (Gap 1 fix)
        if session_doc.get("status") == GenerationStatus.COMPLETED.value:
            response["result"] = session_doc.get("result")
            response["artifact_path"] = session_doc.get("artifact_path")
            response["code_archived"] = session_doc.get("code_archived", False)

        return response
    
    except GenerationSessionNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    
    except Exception as e:
        logger.error(f"Failed to get generation session status {generation_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get generation session status: {str(e)}"
        )


# Heartbeat cadence for the live message SSE stream. A comment line is emitted
# at least this often so idle clients (and proxies) can detect liveness.
_STREAM_HEARTBEAT_SECONDS = 15.0


@router.get("/{generation_id}/workspaces/{workspace_id}/messages/stream")
async def stream_workspace_messages(
    generation_id: str,
    workspace_id: str,
    request: Request,
    _: None = Depends(require_generation_session_owner),
    generation_session_service: GenerationSessionService = Depends(get_generation_session_service),
):
    """Live-tail the simplified Claude message stream for one workspace (SSE).

    Streams ``text/event-stream`` events derived from the live Claude SDK message
    stream via an in-memory broker. Only events produced **after** subscription
    are delivered (no history replay), and the feed is best-effort: under
    backpressure events are dropped rather than slowing generation.

    Auth reuses the standard generation-session ownership check. ``workspace_id``
    must belong to the session, else 404. This endpoint never reads ``/agent_logs``
    and never touches status/checkpoint state.

    Multi-replica caveat: a client only sees events from the backend replica that
    is running the agent. V1 is process-local; documented as self-host/local-best-
    effort until shared pub/sub or sticky routing is added.
    """
    session_doc = await generation_session_service.get_generation_session_status(generation_id)
    workspace_ids = session_doc.get("workspace_ids", [])
    if workspace_id not in workspace_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Workspace {workspace_id} does not belong to generation session {generation_id}."
            ),
        )

    broker = get_agent_stream_broker()

    async def event_source():
        # Subscribe inside the generator so subscribe/unsubscribe are strictly
        # paired: if the response body is never iterated, no queue is registered
        # and so none can leak.
        queue = broker.subscribe(generation_id, workspace_id)
        try:
            # Initial comment so the client connection resolves immediately.
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=_STREAM_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # No events this interval — heartbeat keeps the stream alive.
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {event.model_dump_json()}\n\n"
        finally:
            broker.unsubscribe(generation_id, workspace_id, queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{generation_id}/retry", response_model=RetryGenerationSessionResponse)
@track_event(event_name="retry_generation_session_triggered")
async def retry_generation_session(
    generation_id: str,
    request: Request,
    _: None = Depends(require_generation_session_owner_or_admin),
    retry_service: GenerationSessionRetryService = Depends(get_generation_session_retry_service),
    generation_session_service: GenerationSessionService = Depends(get_generation_session_service),
    workspace_pool: WorkspacePoolService = Depends(get_workspace_pool),
    task_registry: GenerationTaskRegistry = Depends(get_generation_task_registry),
):
    """
    Retry a failed generation session.

    Resets the session to PENDING and spawns an async workflow that
    allocates workspaces and runs the same workflow as the initial run.
    generate_app_workflow resumes from the saved checkpoint via its inner
    WorkflowOrchestrator.

    Retry always reuses the same workspaces. To start fresh, create a new
    session from the same spec — the old FAILED session's workspaces
    are cleaned up by scheduled_wipe after 7 days.

    Example:
        ```
        POST /api/v1/generation-sessions/est-abc123/retry
        ```
    """
    try:
        # FAILED → PENDING via state machine (raises on bad state or max retries)
        await retry_service.retry_generation_session(
            generation_id=generation_id,
        )

        session_doc = await generation_session_service.get_generation_session_status(generation_id)
        logger.info(
            "Retry triggered for generation session %s (status=%s, retry_count=%s)",
            generation_id, session_doc["status"], session_doc["retry_count"],
        )

        # Re-fire via the shared runner (single source of truth — also used by the
        # boot-recovery job). It reads parameters from the persisted session, reuses
        # the same workspaces, resumes from checkpoint, and routes every failure
        # through _handle_workflow_exception.
        asyncio.create_task(
            rerun_generation_session(
                generation_id,
                generation_session_service=generation_session_service,
                user_email=getattr(request.state, "user_email", None),
                task_registry=task_registry,
            )
        )

        return RetryGenerationSessionResponse(
            generation_id=generation_id,
            status=session_doc["status"],
            retry_count=session_doc["retry_count"],
        )

    except InvalidRetryStateError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    except GenerationSessionNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )

    except Exception as e:
        logger.error("Failed to retry generation session %s: %s", generation_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retry generation session: {str(e)}",
        )


@router.delete("/{generation_id}", response_model=CancelGenerationSessionResponse)
@track_event(event_name="cancel_generation_session_triggered")
async def cancel_generation_session(
    generation_id: str,
    request: Request,
    _: None = Depends(require_generation_session_owner),
    generation_session_service: GenerationSessionService = Depends(get_generation_session_service),
    task_registry: GenerationTaskRegistry = Depends(get_generation_task_registry),
):
    """
    Cancel a running generation session (user-initiated).

    Transitions the session to the terminal CANCELLED state and stops the in-flight
    workflow: on the pod that owns the run, the workflow task is cancelled immediately;
    on other pods the workflow stops cooperatively at the next checkpoint. No failure
    notification is sent (the user requested the stop). Generated code is preserved —
    workspaces stay ALLOCATED (Commandment II) and are reclaimed by scheduled_wipe. A
    CANCELLED session is never resumed on restart and cannot be retried.

    Can only cancel sessions in 'pending', 'initializing', or 'running' state.

    Example:
        ```
        DELETE /api/v1/generation-sessions/est-abc123
        ```
    """
    try:
        # Get current session document
        session_doc = await generation_session_service.get_generation_session_status(generation_id)

        current_status = session_doc["status"]

        # Can only cancel if not already terminal.
        if current_status in (
            GenerationStatus.COMPLETED.value,
            GenerationStatus.FAILED.value,
            GenerationStatus.CANCELLED.value,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot cancel generation session in '{current_status}' state"
            )

        # Ordering matters: set CANCELLED FIRST (state machine + slot release, no notify),
        # THEN tear down the running task. Because the state is already terminal when the
        # injected CancelledError / cooperative GenerationCancelledError unwind, neither
        # re-fails the session nor emits an error notification.
        await generation_session_service.cancel_generation_session(generation_id)

        cancelled_locally = task_registry.cancel_task(generation_id)

        return CancelGenerationSessionResponse(
            generation_id=generation_id,
            status=GenerationStatus.CANCELLED.value,
            message=(
                "Generation session cancelled"
                if cancelled_locally
                else "Generation session cancelled; in-flight work will stop shortly"
            ),
        )

    except GenerationSessionNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )

    except InvalidGenerationSessionStateError as e:
        # Race: session became terminal between the status read and the transition.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e)
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Failed to cancel generation session {generation_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel generation session: {str(e)}"
        )



def _reconstruct_generation_session_response(
    stored_result: Dict[str, Any]
) -> MultiWorkspaceEstimationResponse:
    """
    Reconstruct MultiWorkspaceEstimationResponse from stored Firestore ``result`` data.

    Handles both new format (with ``workspace_estimations`` + ``comparative_analysis``) and
    legacy format (summary only). Legacy docs return a minimal response so that resend-email
    still works for sessions completed before the full-result store was introduced.
    """
    summary_data = stored_result.get("summary", {})

    risk_assessment = None
    if "risk_assessment" in summary_data and summary_data["risk_assessment"]:
        risk_data = summary_data["risk_assessment"]
        try:
            risk_assessment = RiskAssessment(**risk_data)
        except Exception:
            pass

    summary = EstimationSummary(
        average_hours=summary_data.get("average_hours", stored_result.get("final_estimate_hours", 0)),
        std_deviation=summary_data.get("std_deviation", 0),
        min_hours=summary_data.get("min_hours", stored_result.get("final_estimate_hours", 0)),
        max_hours=summary_data.get("max_hours", stored_result.get("final_estimate_hours", 0)),
        coefficient_of_variation=summary_data.get("coefficient_of_variation", 0),
        variance_assessment=summary_data.get("variance_assessment", "unknown"),
        risk_assessment=risk_assessment,
    )

    workspace_estimations = []
    for ws_data in stored_result.get("workspace_estimations") or []:
        if not isinstance(ws_data, dict):
            continue
        try:
            workspace_estimations.append(WorkspaceEstimation.model_validate(ws_data))
        except Exception:
            continue

    comp_analysis_data = stored_result.get("comparative_analysis") or {}
    component_comparison = {}
    if "component_comparison" in comp_analysis_data:
        for comp_name, comp_comp_data in comp_analysis_data["component_comparison"].items():
            try:
                component_comparison[comp_name] = ComponentComparison(**comp_comp_data)
            except Exception:
                continue

    comparative_analysis = ComparativeAnalysis(
        component_comparison=component_comparison,
        high_variance_components=comp_analysis_data.get("high_variance_components", []),
        insights=comp_analysis_data.get("insights", []),
    )

    skipped_raw = stored_result.get("skipped_workspaces") or []
    skipped_workspaces = [SkippedWorkspaceP10Y(**item) for item in skipped_raw if isinstance(item, dict)]

    return MultiWorkspaceEstimationResponse(
        summary=summary,
        workspace_estimations=workspace_estimations,
        comparative_analysis=comparative_analysis,
        timestamp=stored_result.get("timestamp", ""),
        skipped_workspaces=skipped_workspaces,
        aggregate_p10y_commit_coverage_pct=stored_result.get(
            "aggregate_p10y_commit_coverage_pct"
        ),
        total_usd_cost=stored_result.get("total_usd_cost"),
    )


async def _recalculate_p10y_and_notify(
    *,
    generation_id: str,
    workspace_ids: list,
    workflow_request: GenerationWorkflowRequest,
    agent_logger: Any,
    llm_overrides: Any,
    db_adapter: StateMachineDBAdapter,
    generation_session_service: GenerationSessionService,
    recipient: str,
    spec_path: str,
    db: IDatabase,
) -> None:
    """Background task: re-run P10Y, patch stored result, send email."""
    TelemetryContext.merge_generation_id(generation_id)
    try:
        fresh = await multi_workspace_estimation_p10y_workflow(
            request=workflow_request,
            settings=settings,
            logger=agent_logger,
            workspace_ids=workspace_ids,
            db_adapter=db_adapter,
            llm_overrides=llm_overrides,
        )
    except Exception as e:
        logger.error("P10Y recalculation failed for %s: %s", generation_id, e, exc_info=True)
        try:
            notifications.notify(
                f"Resending generation session results failed for {generation_id}. "
                "Please contact support.",
                recipient_email=recipient,
            )
        except Exception as notify_err:
            logger.error(
                "Failed to send failure notification for %s: %s", generation_id, notify_err
            )
        return
    simplified = _build_simplified_response(fresh)
    try:
        await generation_session_service.update_completed_generation_session_result(
            generation_id,
            _multi_workspace_result_to_store(fresh, simplified),
        )
    except Exception as e:
        logger.error(
            "Failed to persist recalculated P10Y result for %s: %s",
            generation_id,
            e,
            exc_info=True,
        )
    try:
        notifications.notify_generation_session_complete(
            generation_id=generation_id,
            workspace_ids=workspace_ids,
            result=fresh,
            spec_path=spec_path,
            recipient_email=recipient,
            db=db,
        )
    except Exception as e:
        logger.error("Failed to send email after P10Y recalc for %s: %s", generation_id, e, exc_info=True)


async def _run_generation_session_workflow(
    *,
    generation_id: str,
    generation_session_params: dict,
    workspace_ids: list,
    user_email: str,
    recipient_email: str,
    workflow_llm_overrides: dict,
    resolved_mcps: list,
    session_id,
    spec_path: str,
    session,
    generation_session_service,
    task_registry: GenerationTaskRegistry | None = None,
) -> None:
    """Background task: run app generation + P10Y estimation, release session slot when done."""
    agent_logger = create_agent_logger(
        workspace_name="generation-session-workflow",
        generation_id=generation_id,
    )
    TelemetryContext.set_user_context(user_email=user_email, generation_id=generation_id)
    TelemetryContext.merge_generation_id(generation_id)

    normalized_spec_path = generation_session_params["spec_path"]
    normalized_outputs_dir = generation_session_params["outputs_dir"]
    normalized_src_dir = generation_session_params.get("src_dir", "src")

    try:
        if task_registry is not None:
            task_registry.register_current_task(generation_id)
        async with session.task_slot(generation_id=generation_id):
            logger.info("Starting app generation + P10Y workflow for %s", generation_id)
            agent_logger.info("Starting app generation + P10Y workflow for %s", generation_id)

            # Transition session to RUNNING via state machine.
            # Workspaces were pre-allocated by the spec check (MCP flow).
            _current_status = parse_status((await generation_session_service.get_generation_session_status(generation_id))["status"])
            if _current_status == GenerationStatus.PENDING:
                await generation_session_service.begin_allocation(generation_id, triggered_by=TriggeredBy.RUN_GENERATION)
                await generation_session_service.allocation_succeeded(generation_id, triggered_by=TriggeredBy.RUN_GENERATION)
            elif _current_status == GenerationStatus.INITIALIZING:
                await generation_session_service.allocation_succeeded(generation_id, triggered_by=TriggeredBy.RUN_GENERATION)

            current_checkpoint = generation_session_service.get_checkpoint(generation_id)
            logger.info("Current checkpoint: %s", current_checkpoint.value)

            await run_generation_and_estimation(
                generation_id=generation_id,
                generation_session_service=generation_session_service,
                agent_logger=agent_logger,
                workspace_ids=workspace_ids,
                spec_path=normalized_spec_path,
                outputs_dir=normalized_outputs_dir,
                src_dir=normalized_src_dir,
                session_id=session_id,
                llm_overrides=workflow_llm_overrides,
                recipient_email=recipient_email,
                enabled_mcps=resolved_mcps,
                notify_spec_path=spec_path,
            )

    except Exception as e:
        # Single exit path — routes ContractRejection → reject (PENDING) and everything
        # else → fail (FAILED). GenerationCancelledError is handled as a silent no-op there.
        await _handle_workflow_exception(e, generation_id, generation_session_service)
    finally:
        if task_registry is not None:
            task_registry.deregister_task(generation_id)
        TelemetryContext.set_agent_query_totals_handler(None)


@router.post("/{generation_id}/resend-email", response_model=ResendEmailResponse)
@track_event(event_name="resend_generation_session_email_triggered")
async def resend_generation_session_email(
    generation_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    recipient_email: Optional[str] = Form(None),
    recalculate_p10y: bool = Form(False),
    generation_session_service: GenerationSessionService = Depends(get_generation_session_service),
    db: IDatabase = Depends(get_db),
    _owner: None = Depends(require_generation_session_owner),
):
    """
    Resend email notification for a completed generation session.

    Retrieves stored session result data and reconstructs the email
    with repository links and component breakdown.
    Works for all completed sessions; legacy docs without full workspace breakdowns
    produce a minimal email with summary-only content.

    Set ``recalculate_p10y=true`` to re-run the multi-workspace P10Y workflow against
    the current workspace filesystems (after P10Y has caught up), update the stored
    result, then send the email.
    
    Example:
        ```
        POST /api/v1/generation-sessions/est-abc123/resend-email
        Content-Type: multipart/form-data
        
        recipient_email: user@example.com  # Optional
        recalculate_p10y: false            # Optional — rerun P10Y before email
        ```
    """
    try:
        # Get session document
        session_doc = await generation_session_service.get_generation_session_status(generation_id)

        # Check if session is completed
        if session_doc["status"] != GenerationStatus.COMPLETED.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot resend email for generation session in '{session_doc['status']}' state. "
                    "Only completed sessions can have emails resent."
                ),
            )
        
        # Get stored result
        stored_result = session_doc.get("result")
        if not stored_result:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Generation session has no stored result data. Cannot resend email."
            )
        
        # Get workspace IDs
        workspace_ids = session_doc.get("workspace_ids", [])
        if not workspace_ids:
            logger.warning(f"Generation session {generation_id} has no workspace_ids")
        
        # Get spec_path from parameters
        parameters = session_doc.get("parameters", {})
        spec_path = parameters.get("spec_path", "unknown")
        
        # Determine recipient email
        user_email = request.state.user_email if hasattr(request.state, 'user_email') else None
        recipient = recipient_email or user_email or session_doc.get("user_email") or settings.NOTIFY_EMAIL_USERNAME
        
        if not recipient:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No recipient email specified. Provide recipient_email parameter or ensure user_email is set."
            )

        if recalculate_p10y:
            if not workspace_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Cannot recalculate P10Y: generation session has no workspace_ids. "
                        "Workspaces may have been released after completion."
                    ),
                )
            llm_overrides = build_llm_overrides(
                LLM_HIGH=parameters.get("LLM_HIGH"),
                LLM_MEDIUM=parameters.get("LLM_MEDIUM"),
                LLM_LOW=parameters.get("LLM_LOW"),
            )
            agent_logger = create_agent_logger(
                workspace_name="p10y-recalculate",
                generation_id=generation_id,
            )
            workflow_request = GenerationWorkflowRequest(
                spec_path=parameters.get("spec_path", "specifications"),
                outputs_dir=parameters.get("outputs_dir", "specflow"),
                session_id=parameters.get("session_id"),
                generation_id=generation_id,
            )
            background_tasks.add_task(
                _recalculate_p10y_and_notify,
                generation_id=generation_id,
                workspace_ids=workspace_ids,
                workflow_request=workflow_request,
                agent_logger=agent_logger,
                llm_overrides=llm_overrides,
                db_adapter=generation_session_service.db_adapter,
                generation_session_service=generation_session_service,
                recipient=recipient,
                spec_path=spec_path,
                db=db,
            )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=ResendEmailResponse(
                    generation_id=generation_id,
                    email_sent=False,
                    recipient=recipient,
                    message="P10Y recalculation triggered; email will be sent when complete",
                ).model_dump(),
            )

        # Reconstruct MultiWorkspaceEstimationResponse from stored data and send email
        try:
            result = _reconstruct_generation_session_response(stored_result)
        except Exception as e:
            logger.error(
                f"Failed to reconstruct P10Y result payload for {generation_id}: {e}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to reconstruct stored result data: {str(e)}",
            )

        try:
            notifications.notify_generation_session_complete(
                generation_id=generation_id,
                workspace_ids=workspace_ids,
                result=result,
                spec_path=spec_path,
                recipient_email=recipient,
                db=db
            )
            return ResendEmailResponse(
                generation_id=generation_id,
                email_sent=True,
                recipient=recipient,
                message="Email notification resent successfully",
            )
        except Exception as e:
            logger.error(f"Failed to send email for {generation_id}: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to send email: {str(e)}"
            )
    
    except GenerationSessionNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    
    except HTTPException:
        raise
    
    except Exception as e:
        logger.error(f"Failed to resend email for generation session {generation_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resend email: {str(e)}"
        )


@router.post("/run", response_model=RunGenerationSessionResponse, status_code=status.HTTP_201_CREATED)
@track_event(event_name="run_generation_session_triggered")
async def run_generation_session(
    request: Request,
    spec_path: str = Form(...),
    outputs_dir: str = Form(default="specflow"),
    src_dir: str = Form(default="src"),
    notification_email: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
    max_retries: Optional[int] = Form(3),
    generation_id: Optional[str] = Form(None),  # Optional: reuse existing session from spec check
    LLM_HIGH: Optional[str] = Form(None),
    LLM_MEDIUM: Optional[str] = Form(None),
    LLM_LOW: Optional[str] = Form(None),
    MCP_SERVERS_ENABLED: Optional[str] = Form(None),
    workspace_count: Optional[int] = Form(None, ge=1, le=3),
    workspace_pool: WorkspacePoolService = Depends(get_workspace_pool),
    generation_session_service: GenerationSessionService = Depends(get_generation_session_service),
    task_registry: GenerationTaskRegistry = Depends(get_generation_task_registry),
    _owner: None = Depends(require_generation_session_owner_form),
):
    """
    Run the full generation session workflow for the MCP flow.

    This endpoint orchestrates the full workflow:
    1. If generation_id provided (MCP flow):
       - Reuse existing session and allocated workspaces (from spec check)
       - Sync files from workspace 1 to workspaces 2 & 3
    2. Start async background task that:
       - Generates app for specification
       - Runs the multi-workspace P10Y estimation workflow
    3. Return generation_id immediately (non-blocking)
    
    Args:
        spec_path: Path to specification files
        outputs_dir: Output directory (default: "specflow")
        notification_email: Optional email for completion notification (defaults to user_email from X-User-Email header)
        session_id: Optional session ID
        max_retries: Maximum retry attempts (default: 3)
        generation_id: Optional generation_id to reuse (from spec completeness check)
        
    Note:
        User email is extracted from X-User-Email header (set by MCP server).
        This email is used for user identification and as notification recipient.
        
        MCP Flow: When generation_id is provided, the endpoint expects that files are already
        synced to workspace 1 (primary workspace). It will then copy these files to workspaces
        2 & 3 for parallel P10Y runs across workspaces.
        
        Model Configuration: Uses default provider (openrouter) and default model (claude-haiku-4-5)
        as defined in backend configuration.
    
    Returns:
        generation_id: ID to track generation session progress
        status: Current status (pending)
        message: Status message
        
    Example:
        ```
        POST /api/v1/generation-sessions/run
        Content-Type: multipart/form-data
        
        spec_path: "specifications/feature.md"
        outputs_dir: "specflow"
        generation_id: "est-abc123"  # From spec check
        ```
    """
    
    # Get user email from request state (set by auth middleware)
    user_email = request.state.user_email
    
    # Use notification_email from form if provided, otherwise use user_email
    recipient_email = notification_email or user_email
    
    existing_workspace_ids = None
    
    try:
        # Check if reusing existing generation_id (from spec completeness check)
        if generation_id:
            try:
                session_doc = await generation_session_service.get_generation_session_status(generation_id)

                # Reuse existing session — contract validation runs as the first workflow step.
                existing_workspace_ids = session_doc.get("workspace_ids", [])
                
                if existing_workspace_ids:
                    # Validate that workspaces are still allocated to this session
                    # This prevents issues where workspaces were released during maintenance
                    # but the session document still has stale workspace_ids
                    try:
                        existing_workspace_ids = await generation_session_service.validate_and_clean_workspace_ids(
                            generation_id=generation_id,
                            workspace_ids=existing_workspace_ids,
                            raise_if_empty=True
                        )
                        logger.info(
                            f"Reusing existing generation session {generation_id} with validated workspaces {existing_workspace_ids}"
                        )
                    except InvalidGenerationSessionStateError as e:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"{str(e)} Call /api/v1/workspace/sync first to allocate new workspaces."
                        )
                else:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Generation session {generation_id} exists but has no allocated workspaces. "
                               f"Call /api/v1/workspace/sync first to allocate workspaces."
                    )
            except GenerationSessionNotFoundError:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Generation session {generation_id} not found"
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="generation_id is required for MCP flow. Call /api/v1/workspace/sync first."
            )
        
        # Resolve workspace_count: frozen stored value wins; fall back to request arg → default.
        stored_workspace_count = session_doc.get("parameters", {}).get("workspace_count")
        effective_workspace_count = (
            stored_workspace_count
            if stored_workspace_count is not None
            else (workspace_count or settings.WORKSPACE_COUNT)
        )

        mcp_resolution = resolve_enabled_mcps_and_set_telemetry(
            form_value=MCP_SERVERS_ENABLED,
            generation_session_parameters=session_doc.get("parameters"),
            settings=settings,
            prefer_stored=True,
        )
        resolved_mcps = mcp_resolution.enabled

        # Update session parameters with normalized paths
        generation_session_params = {
            "spec_path": normalize_path_parameter(spec_path),
            "outputs_dir": normalize_path_parameter(outputs_dir),
            "src_dir": src_dir,
            "notification_email": recipient_email,
            "session_id": session_id,
            "workflow_type": "generation_run",
            "workspace_count": effective_workspace_count,
            "mcp_servers_enabled": enabled_mcps_to_parameter_string(resolved_mcps),
        }
        # Build overrides once: persisted in generation_session_params (for retry) and used directly below.
        workflow_llm_overrides = build_llm_overrides(LLM_HIGH=LLM_HIGH, LLM_MEDIUM=LLM_MEDIUM, LLM_LOW=LLM_LOW)
        generation_session_params.update(workflow_llm_overrides)

        # Authoritative model gate (defense-in-depth; the MCP server also pre-flights this).
        # A 2–8h run must not start against a model the active provider doesn't offer.
        # Block-on-any-invalid; an unverifiable catalog (no key / outage) never blocks.
        # Workspaces stay allocated — fix the model env and call run_generation again to reuse them.
        model_validation = await validate_models_config(workflow_llm_overrides, settings, logger)
        blocking_tier = next((tv for tv in model_validation.tiers if tv.blocking), None)
        if blocking_tier is not None:
            message = _model_unavailable_message(blocking_tier, settings.DEFAULT_PROVIDER.value)
            logger.warning(f"run_generation rejected (MODEL_UNAVAILABLE): {message}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": message, "code": RejectionCode.MODEL_UNAVAILABLE.value},
            )
        
        generation_session_service.merge_generation_session_parameters(
            generation_id,
            generation_session_params,
            top_level_updates={"max_retries": max_retries},
        )
        
        logger.info(f"Updated generation session {generation_id} parameters")
        workspace_ids = existing_workspace_ids

        # Acquire API key lock before spawning the background task.
        # Prevents two concurrent /run calls from both starting generation on the same workspace,
        # which would corrupt filesystem state. Same pattern as run_planning.
        api_key = request.state.api_key
        session = generation_session_service.api_key_sessions
        try:
            slot = await session.try_begin(
                api_key_doc_id=api_key,
                generation_id=generation_id,
                operation=OperationKind.GENERATION,
            )
        except ApiKeyDocMissingError:
            return {"status": "error", "message": "API key not registered.", "next_action": "contact_support"}
        if slot.outcome == SessionBeginOutcome.AT_CAPACITY:
            raise SessionAtCapacityError()
        if slot.outcome == SessionBeginOutcome.ALREADY_HELD:
            # Same lease refresh begin_or_raise performed for idempotent retries / MCP reconnect.
            await session.refresh_lease(
                generation_id=generation_id,
                operation=OperationKind.GENERATION,
            )
            return RunGenerationSessionResponse(
                generation_id=generation_id,
                status="running",
                message="Generation is already in progress. Use check_status to monitor.",
            )
        asyncio.create_task(_run_generation_session_workflow(
            generation_id=generation_id,
            generation_session_params=generation_session_params,
            workspace_ids=workspace_ids,
            user_email=user_email,
            recipient_email=recipient_email,
            workflow_llm_overrides=workflow_llm_overrides,
            resolved_mcps=resolved_mcps,
            session_id=session_id,
            spec_path=spec_path,
            session=session,
            generation_session_service=generation_session_service,
            task_registry=task_registry,
        ))

        return RunGenerationSessionResponse(
            generation_id=generation_id,
            status="pending",
            message="Generation workflow started. Use check_status to track progress."
        )
    
    except HTTPException:
        raise

    except SessionAtCapacityError:
        raise

    except Exception as e:
        logger.error(f"Failed to run generation session: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to run generation session: {str(e)}"
        )


# ============================================================
# 2.5b — Download generation session outputs
# ============================================================

@router.get("/{generation_id}/outputs")
@track_event(event_name="download_outputs_triggered")
async def download_generation_session_outputs(
    generation_id: str,
    request: Request,
    _: None = Depends(require_generation_session_owner),
    generation_session_service: GenerationSessionService = Depends(get_generation_session_service),
    db: IDatabase = Depends(get_db),
):
    """
    Download generation session outputs as a tar.gz binary file.

    Routing logic:
    - If outputs_archived=True  → serve from existing artifact path (normal path).
    - If outputs_archived=False and status in (FAILED, RUNNING) with ALLOCATED
      workspaces → trigger emergency archive, then serve. Response includes
      X-SpecFlow-Partial-Output: true header and PARTIAL_OUTPUT_WARNING.txt in the tarball.
    - Otherwise → 400: workspaces are gone, outputs cannot be retrieved.

    Tarball layout:
      {generation_id}/
        {workspace_id}/   workspace snapshot (source code + outputs, no build artifacts)
        analysis/         spec analysis outputs (present if analysis was run)
        report/           combined multi-workspace comparison report
        PARTIAL_OUTPUT_WARNING.txt  (present only on emergency archives)

    Returns:
        Binary tar.gz file (Content-Type: application/gzip).

    Raises:
        400 if outputs cannot be retrieved (workspaces already released)
        404 if generation session not found, artifact directory missing, or emergency
            archive failed because all workspaces were unreachable
    """
    try:
        session_doc = await generation_session_service.get_generation_session_status(generation_id)
    except GenerationSessionNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    outputs_archived = session_doc.get("outputs_archived")
    emergency_archived = session_doc.get("emergency_archived")
    is_partial = bool(emergency_archived) and not bool(outputs_archived)
    artifact_store = ArtifactStore(StateMachineDBAdapter(db))

    if not outputs_archived and not emergency_archived:
        est_status = session_doc.get("status")
        workspace_ids = session_doc.get("workspace_ids", [])
        # CANCELLED is intentionally excluded: a user-cancelled run is not emergency-archived
        # for download. Its workspaces stay ALLOCATED and are reclaimed by scheduled_wipe.
        recoverable_statuses = {GenerationStatus.FAILED.value, GenerationStatus.RUNNING.value}

        if est_status in recoverable_statuses and workspace_ids:
            # Emergency path: snapshot still-ALLOCATED workspaces.
            # emergency_archive() sets emergency_archived; build_tarball() re-fetches from DB.
            try:
                await artifact_store.emergency_archive(generation_id)
                is_partial = True
            except RuntimeError as e:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"Emergency archive failed for generation session {generation_id} — "
                        f"no workspaces could be read: {e}"
                    ),
                )
        else:
            # Check whether any archivable phase has completed.
            # With the new local-only analysis/planning flow, outputs first become
            # archivable after KB_INIT_DONE (the first backend-produced artifact).
            raw_checkpoint = session_doc.get("checkpoint")
            checkpoint_enum = parse_checkpoint(raw_checkpoint)

            has_archivable_phase = (
                checkpoint_enum is not None
                and GenerationCheckpoint.is_at_or_after(
                    checkpoint_enum, GenerationCheckpoint.KB_INIT_DONE
                )
            )

            if not has_archivable_phase:
                return JSONResponse(
                    status_code=200,
                    content={
                        "status": "no_outputs",
                        "generation_id": generation_id,
                        "checkpoint": raw_checkpoint,
                        "message": "No outputs available yet — generation has not produced any artifacts.",
                    },
                )

            # Artifacts may exist (e.g. planning/ subdir written by archive_planning).
            # Build tarball directly from the artifact dir without the outputs_archived guard.
            tarball_path = await artifact_store.build_tarball_from_dir(generation_id)
            if tarball_path is None:
                return JSONResponse(
                    status_code=200,
                    content={
                        "status": "no_outputs",
                        "generation_id": generation_id,
                        "checkpoint": raw_checkpoint,
                        "message": f"No artifact files found for checkpoint={raw_checkpoint}.",
                    },
                )

            try:
                with open(tarball_path, "rb") as fh:
                    tarball_bytes = fh.read()
            finally:
                tarball_path.unlink(missing_ok=True)

            return Response(
                content=tarball_bytes,
                media_type="application/gzip",
                headers={"Content-Disposition": f'attachment; filename="{generation_id}.tar.gz"'},
            )

    try:
        tarball_path = await artifact_store.build_tarball(generation_id)
        try:
            with open(tarball_path, "rb") as fh:
                tarball_bytes = fh.read()
        finally:
            # Tarballs are built on-demand and served in-memory; remove the file
            # immediately after reading so it does not accumulate on the NFS volume.
            tarball_path.unlink(missing_ok=True)

        response_headers: dict[str, str] = {
            "Content-Disposition": f'attachment; filename="{generation_id}.tar.gz"',
        }
        if is_partial:
            response_headers["X-SpecFlow-Partial-Output"] = "true"

        return Response(
            content=tarball_bytes,
            media_type="application/gzip",
            headers=response_headers,
        )
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact directory not found for generation session {generation_id}: {e}",
        )
    except Exception as e:
        logger.error(
            f"Failed to build tarball for generation session {generation_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to build outputs tarball: {e}",
        )


@router.get("/{generation_id}/report.html")
@track_event(event_name="download_report_html_triggered")
async def download_generation_session_report_html(
    generation_id: str,
    request: Request,
    _: None = Depends(require_generation_session_owner),
):
    """
    Serve the P10Y multi-workspace estimation report as raw HTML.

    Lightweight alternative to ``/outputs`` — a few KB rather than a full
    tarball of workspace code, so it's fast enough to fetch on a single local
    TUI keypress. Raises 404 when the report hasn't been written yet (P10Y
    estimation not complete, or this is an older run predating the report).
    """
    report_path = (
        ARTIFACTS_BASE / generation_id / REPORT_SUBDIR / MULTI_WORKSPACE_REPORT_HTML_FILE
    )
    if not report_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No HTML report found for generation session {generation_id}.",
        )
    return Response(content=report_path.read_bytes(), media_type="text/html")

