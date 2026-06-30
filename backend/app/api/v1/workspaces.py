"""
Workspace Pool API endpoints.

Provides REST API for monitoring workspace pool status and file sync.
"""

import io
import json
import logging
import tarfile
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from typing import Optional

from app.api.dependencies import require_admin, verify_generation_session_owner
from app.core.config import settings
from app.core.mcp_config import mcp_resolution_from_workspace_sync_parameters
from app.core.telemetry_decorator import track_event
from app.database.dependencies import get_db
from app.database.interface import IDatabase
from app.services.generation_session import GenerationSessionNotFoundError, GenerationSessionService
from app.core.telemetry_context import TelemetryContext
from app.schemas.generation_workflow_enums import GenerationStatus, WorkspaceStatus, parse_status
from app.state.exceptions import InvalidGenerationSessionStateError
from app.services.contract_validator import ContractRejection, validate_generation_contract_preflight
from app.services.workspace_pool import NoAvailableWorkspacesError, WorkspacePoolService, WorkspaceNotFoundError, WorkspacePoolError
from app.state.api_key_session_concurrency import ApiKeySessionConcurrency
from app.utils.file_utils import extract_workspace_sync_archive

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspace", tags=["workspaces"])


# ============================================================
# Response Models
# ============================================================

class CleaningSetInfo(BaseModel):
    """Grace-period info for one workspace set currently in CLEANING state."""
    set_number: int = Field(..., description="Set number (1-based)")
    remaining_grace_seconds: int = Field(
        ..., description="Seconds until auto-clear grace expires (clamped to 0)"
    )


class WorkspacePoolStatusResponse(BaseModel):
    """Response with workspace pool status."""
    total: int = Field(..., description="Total workspaces in pool")
    available: int = Field(..., description="Available workspaces")
    allocated: int = Field(..., description="Allocated workspaces")
    cleaning: int = Field(..., description="Workspaces being cleaned")
    stuck: int = Field(..., description="Stuck workspaces (need manual intervention)")
    available_sets: int = Field(..., description="Number of complete available sets (3 workspaces each)")
    cleaning_sets: list[CleaningSetInfo] = Field(
        default_factory=list,
        description="Per-set grace-period info for sets currently in CLEANING state",
    )


class SyncWorkspaceResponse(BaseModel):
    """Response after syncing files to workspace."""
    generation_id: str = Field(..., description="Generation session ID created")
    workspace_ids: list[str] = Field(..., description="Allocated workspace IDs")
    status: str = Field(..., description="Session status (pending)")


class ForceDeallocateRequest(BaseModel):
    """Request to force deallocate workspace(s)."""
    workspace_ids: list[str] = Field(..., description="Workspace IDs to deallocate")
    reason: str = Field(default="manual_deallocation", description="Reason for forced deallocation")


class ForceDeallocateResponse(BaseModel):
    """Response after force deallocating workspaces."""
    total: int = Field(..., description="Total workspaces processed")
    success: int = Field(..., description="Successfully deallocated")
    failed: int = Field(..., description="Failed to deallocate")
    details: list[dict] = Field(..., description="Per-workspace results")


class WorkspaceCleanupCheckResponse(BaseModel):
    """Response with workspace cleanup check results."""
    total: int = Field(..., description="Total workspaces checked")
    clean: int = Field(..., description="Number of clean workspaces")
    needs_cleaning: int = Field(..., description="Number of workspaces that need cleaning")
    workspaces: list[dict] = Field(..., description="Detailed check results for each workspace")


class CleanWorkspaceRequest(BaseModel):
    """Request to clean a workspace."""
    workspace_id: str = Field(..., description="Workspace ID to clean")
    reason: str = Field(default="manual_cleanup", description="Reason for cleaning")


class CleanWorkspaceResponse(BaseModel):
    """Response after cleaning a workspace."""
    workspace_id: str = Field(..., description="Workspace ID")
    success: bool = Field(..., description="Whether cleanup succeeded")
    message: str = Field(..., description="Status message")
    error: Optional[str] = Field(None, description="Error message if failed")


def _should_preflight_generation_contract(parameters: dict) -> bool:
    """Only first-run generation uploads can be rejected before allocation."""
    return (
        parameters.get("workflow_type") == "generation_run"
        and not parameters.get("generation_id")
    )


def _preflight_generation_contract_archive(
    archive_content: bytes,
    parameters: dict,
) -> None:
    """Validate first-run generation contract before creating a session/workspaces."""
    if not _should_preflight_generation_contract(parameters):
        return

    outputs_dir = str(parameters.get("outputs_dir") or "docs")
    with tempfile.TemporaryDirectory(prefix="specflow-contract-") as tmp_dir:
        temp_root = Path(tmp_dir)
        archive_buffer = io.BytesIO(archive_content)
        with tarfile.open(fileobj=archive_buffer, mode="r:gz") as tar:
            try:
                # Only the outputs_dir markdown is inspected by the contract validator;
                # skip specs/ and src/ so a large first-run archive isn't fully
                # decompressed into a throwaway temp dir just to be discarded.
                extract_workspace_sync_archive(tar, temp_root, only_under=outputs_dir)
            except ValueError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(e),
                ) from e

        try:
            validate_generation_contract_preflight(temp_root, outputs_dir, logger)
        except ContractRejection as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=e.to_dict(),
            ) from e


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


# ============================================================
# Endpoints
# ============================================================

@router.get("/pool/status", response_model=WorkspacePoolStatusResponse)
@track_event(event_name="get_pool_status_triggered")
async def get_pool_status(
    workspace_pool: WorkspacePoolService = Depends(get_workspace_pool)
):
    """
    Get current workspace pool status.
    
    Returns statistics about workspace availability:
    - Total workspaces in pool
    - Count by status (available, allocated, cleaning, stuck)
    - Number of complete available sets (3 workspaces each)
    
    Example:
        ```
        GET /api/v1/workspace/pool/status
        
        Response:
        {
            "total": 30,
            "available": 24,
            "allocated": 3,
            "cleaning": 3,
            "stuck": 0,
            "available_sets": 8
        }
        ```
    """
    try:
        status_data = await workspace_pool.get_pool_status()
        
        return WorkspacePoolStatusResponse(**status_data)
    
    except Exception as e:
        logger.error(f"Failed to get pool status: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get pool status: {str(e)}"
        )


@router.post("/sync", response_model=SyncWorkspaceResponse, status_code=status.HTTP_201_CREATED)
@track_event(event_name="sync_triggered")
async def sync_workspace(
    request: Request,
    archive: UploadFile,
    params: str = Form(...),
    user_id: str = Form(None),  # Optional for backward compatibility, prefer request.state.user_email
    sync_to_all: str = Form("true"),  # "true" or "false" string
    workspace_pool: WorkspacePoolService = Depends(get_workspace_pool),
    generation_session_service: GenerationSessionService = Depends(get_generation_session_service),
):
    """
    Sync user files to workspace and create or reuse a generation session.

    Receives a tar.gz archive of user's project files, extracts them to
    allocated workspaces, initializes git, and creates a pending session record when needed.

    Flow:
    1. Receive and validate archive (tar.gz, max 100MB)
    2. Parse session parameters from form data
    3. Allocate workspace set (3 workspaces)
    4. Extract archive to primary workspace OR all workspaces (based on sync_to_all)
    5. Initialize git and create initial commit
    6. Create session record (pending state) when not reusing ``generation_id``

    Args:
        archive: tar.gz archive of project files
        params: JSON string with session parameters (spec_file, model, etc.)
        user_id: User ID requesting the session (optional, defaults to X-User-Email header)
        sync_to_all: "true" to sync to all workspaces, "false" to sync only to primary (default: "true")

    Returns:
        generation_id: Created or reused generation session ID
        workspace_ids: Allocated workspace IDs
        status: Session status (pending)
        
    Note:
        User email is extracted from X-User-Email header (set by auth middleware).
        If user_id form parameter is provided, it takes precedence (for backward compatibility).
        
    Example:
        ```
        POST /api/v1/workspace/sync
        Content-Type: multipart/form-data
        
        archive: <tar.gz file>
        params: {"spec_file": "spec.md", "model": "claude-sonnet-4-20250514"}
        sync_to_all: "false"  # For spec checking, only sync to primary workspace
        ```
    """
    MAX_ARCHIVE_SIZE = 100 * 1024 * 1024  # 100 MB
    
    try:
        # 1. Validate archive format
        if not archive.filename or not archive.filename.endswith((".tar.gz", ".tgz")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Archive must be .tar.gz or .tgz format"
            )
        
        # 2. Read archive content (with size limit)
        archive_content = await archive.read()
        if len(archive_content) > MAX_ARCHIVE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"Archive too large. Max size: {MAX_ARCHIVE_SIZE / 1024 / 1024:.0f} MB"
            )
        
        # 3. Get user_email (prefer request.state.user_email from auth middleware, fallback to form parameter)
        user_email = getattr(request.state, "user_email", None) or user_id
        if not user_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing user identification. Provide X-User-Email header or user_id form parameter."
            )
        
        # 4. Parse parameters
        try:
            parameters = json.loads(params)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid params JSON: {str(e)}"
            )

        _preflight_generation_contract_archive(archive_content, parameters)
        
        # 5. Check if reusing existing generation_id (from spec completeness check)
        generation_id = parameters.get("generation_id")
        if generation_id:
            # Verify the requesting user owns this session
            await verify_generation_session_owner(generation_id, request, generation_session_service.db_adapter)

            # Reuse existing session — keep workspaces when still allocated, or
            # re-allocate when PENDING after contract rejection (released workspaces).
            try:
                session_doc = await generation_session_service.get_generation_session_status(generation_id)
                session_status = parse_status(session_doc.get("status"))
                if session_status not in (GenerationStatus.PENDING, None):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Generation session {generation_id} is '{session_doc.get('status')}'. "
                            f"Wait for it to finish or use retry_generation only after a failed run."
                        ),
                    )
                try:
                    workspace_count = parameters.get("workspace_count")
                    workspace_ids = await generation_session_service.ensure_workspaces_for_sync(
                        generation_id,
                        workspace_count=workspace_count,
                    )
                except InvalidGenerationSessionStateError as e:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=str(e),
                    ) from e

                logger.info(
                    "Sync for generation session %s using workspaces %s",
                    generation_id,
                    workspace_ids,
                )
            except GenerationSessionNotFoundError:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Generation session {generation_id} not found. Cannot reuse non-existent session."
                )
        else:
            # Create new session record (pending state)
            api_key_id = getattr(request.state, "api_key", None)
            if api_key_id:
                snap = await ApiKeySessionConcurrency(
                    generation_session_service.db_adapter
                ).snapshot(api_key_doc_id=api_key_id)
                if snap.at_capacity:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            "This API key has reached its maximum concurrent generation sessions. "
                            "Finish or clean up an existing run before creating a new session."
                        ),
                    )
            generation_id = await generation_session_service.create_generation_session(
                user_email=user_email,
                parameters=parameters,
                max_retries=parameters.get("max_retries", 3),
                key_uid=getattr(request.state, "key_uid", None),
                workspace_pool=getattr(request.state, "workspace_pool", None),
            )
            
            # Allocate workspace set for new session
            try:
                workspace_count = parameters.get("workspace_count")
                workspace_ids = await workspace_pool.allocate_workspace_set(
                    generation_id,
                    count=workspace_count,
                )
                logger.info(f"Created new generation session {generation_id} and allocated workspaces {workspace_ids}")
                sync_mcp_res = mcp_resolution_from_workspace_sync_parameters(parameters, settings)
                TelemetryContext.set_mcp_resolution(sync_mcp_res)
            except NoAvailableWorkspacesError as e:
                # Cleanup: mark session as failed before re-raising
                await generation_session_service.fail_generation_session(
                    generation_id,
                    f"No available workspaces: {str(e)}"
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(e)
                )
        
        try:
            # 5. Determine which workspaces to sync
            # If sync_to_all is "false", only sync to primary workspace (first one)
            # Otherwise sync to all 3 workspaces
            sync_all = sync_to_all.lower() == "true"
            workspaces_to_sync = workspace_ids if sync_all else [workspace_ids[0]]
            
            logger.info(
                f"Syncing archive to {'all workspaces' if sync_all else 'primary workspace only'}: "
                f"{workspaces_to_sync}"
            )
            
            # 6. Extract archive to selected workspace(s)
            for ws_id in workspaces_to_sync:
                # Get workspace path (same logic as WorkspacePoolService)
                workspace_path = workspace_pool.workspace_base_path / ws_id
                
                # Ensure workspace directory exists
                workspace_path.mkdir(parents=True, exist_ok=True)
                
                # Extract archive (overwrites files, preserves others)
                # IMPORTANT: We do NOT wipe the workspace because:
                # 1. Analysis workflow generates outputs (e.g., specification_completeness.md)
                # 2. User workflow: spec_analyze → fix specs → spec_analyze → run_generation
                # 3. run_generation re-uploads specs/src but should preserve analysis outputs
                # 4. The archive contains updated specs/src from user's local directory
                # 5. Extracting on top will update specs/src and preserve generated outputs
                archive_buffer = io.BytesIO(archive_content)
                with tarfile.open(fileobj=archive_buffer, mode="r:gz") as tar:
                    # Security: validate paths for traversal attacks (CVE-2007-4559)
                    try:
                        extract_workspace_sync_archive(tar, workspace_path)
                    except ValueError as e:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(e),
                        )

                # Initialize git repo and create initial commit
                await workspace_pool.initialize_git_repo(workspace_path, generation_id)
                
                logger.info(
                    f"Extracted archive to workspace {ws_id} "
                    f"and created initial commit"
                )
            
            # 8. Update session with workspace IDs
            generation_session_service.set_workspace_ids(generation_id, workspace_ids)
            
            logger.info(
                f"Synced files to workspaces {workspace_ids} "
                f"for generation session {generation_id}"
            )
            
            return SyncWorkspaceResponse(
                generation_id=generation_id,
                workspace_ids=workspace_ids,
                status="pending"
            )
        
        except HTTPException:
            # Re-raise HTTPException so FastAPI handles it correctly
            raise
        
        except NoAvailableWorkspacesError as e:
            # Cleanup: mark session as failed
            await generation_session_service.fail_generation_session(
                generation_id,
                f"No available workspaces: {str(e)}"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(e)
            )
        
        except Exception as e:
            # Cleanup: mark session as failed
            logger.error(
                f"Failed to sync files for generation session {generation_id}: {e}",
                exc_info=True
            )
            await generation_session_service.fail_generation_session(
                generation_id,
                f"File sync failed: {str(e)}"
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to sync files: {str(e)}"
            )
    
    except HTTPException:
        # Re-raise HTTPException so FastAPI handles it correctly
        raise
    
    except Exception as e:
        logger.error(f"Unexpected error in sync_workspace: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}"
        )


@router.post("/force-deallocate", response_model=ForceDeallocateResponse)
@track_event(event_name="force_deallocate_workspaces_triggered")
async def force_deallocate_workspaces(
    request: ForceDeallocateRequest,
    workspace_pool: WorkspacePoolService = Depends(get_workspace_pool),
    _: None = Depends(require_admin),
):
    """
    Force deallocate workspaces that are stuck in allocated state.
    
    This is a manual recovery operation for workspaces that got stuck allocated
    due to failures (e.g., git clone failures during allocation).
    
    **CAUTION**: Only use this when you're sure no generation session is actively using
    the workspaces. Check session status first.
    
    Common use cases:
    - Git clone failed during allocation, leaving workspaces in "allocated" state
    - System crash left workspaces locked without an active session
    - Manual intervention needed to recover from allocation failures
    
    Args:
        request: Contains workspace IDs to deallocate and reason
        
    Returns:
        Summary of deallocations with success/failure counts
        
    Example:
        ```
        POST /api/v1/workspace/force-deallocate
        Content-Type: application/json
        
        {
            "workspace_ids": ["ws-01-1", "ws-01-2", "ws-01-3"],
            "reason": "git_clone_failure_during_allocation"
        }
        
        Response:
        {
            "total": 3,
            "success": 3,
            "failed": 0,
            "details": [
                {"workspace_id": "ws-01-1", "status": "success"},
                {"workspace_id": "ws-01-2", "status": "success"},
                {"workspace_id": "ws-01-3", "status": "success"}
            ]
        }
        ```
    """
    try:
        if not request.workspace_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No workspace IDs provided"
            )
        
        logger.warning(
            f"Force deallocating {len(request.workspace_ids)} workspaces: "
            f"{request.workspace_ids} (reason: {request.reason})"
        )
        
        result = await workspace_pool.force_deallocate_workspaces_batch(
            request.workspace_ids,
            request.reason
        )
        
        return ForceDeallocateResponse(**result)
    
    except HTTPException:
        raise
    
    except Exception as e:
        logger.error(f"Failed to force deallocate workspaces: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to force deallocate workspaces: {str(e)}"
        )


@router.get("/cleanup/check", response_model=WorkspaceCleanupCheckResponse)
async def check_workspaces_to_clean(
    workspace_pool: WorkspacePoolService = Depends(get_workspace_pool)
):
    """
    Check all available workspaces for stale data on disk/git.
    
    This endpoint checks the actual filesystem and git state of workspaces
    that are marked "available" in Firestore. It identifies workspaces that
    may have stale data (uncommitted changes, commits on main, P10Y artifact markers).
    
    Returns:
        List of all available workspaces with their disk/git state:
        - workspace_id: Workspace ID
        - status: Firestore status (should be "available")
        - is_clean: Whether workspace is actually clean
        - issues: List of issues found (if any)
        - directory_exists: Whether workspace directory exists
        - has_uncommitted_changes: Whether there are uncommitted changes
        - has_commits_on_main: Whether main branch has commits
        - has_estimation_artifacts: Whether P10Y marker files exist (field name unchanged)
        
    Example:
        ```
        GET /api/v1/workspace/cleanup/check
        
        Response:
        {
            "total": 24,
            "clean": 20,
            "needs_cleaning": 4,
            "workspaces": [
                {
                    "workspace_id": "ws-01-1",
                    "status": "available",  # state-ok: docstring response example
                    "is_clean": false,
                    "issues": ["Has uncommitted changes", "Main branch has commits"],
                    "directory_exists": true,
                    "has_uncommitted_changes": true,
                    "has_commits_on_main": true,
                    "has_estimation_artifacts": false,
                    ...
                },
                ...
            ]
        }
        ```
    """
    try:
        results = await workspace_pool.check_all_available_workspaces()
        
        clean_count = sum(1 for r in results if r.get("is_clean"))
        needs_cleaning_count = len(results) - clean_count
        
        return WorkspaceCleanupCheckResponse(
            total=len(results),
            clean=clean_count,
            needs_cleaning=needs_cleaning_count,
            workspaces=results
        )
    
    except Exception as e:
        logger.error(f"Failed to check workspaces: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check workspaces: {str(e)}"
        )


@router.post("/cleanup/clean", response_model=CleanWorkspaceResponse)
async def clean_workspace(
    request: CleanWorkspaceRequest,
    workspace_pool: WorkspacePoolService = Depends(get_workspace_pool),
    _: None = Depends(require_admin),
):
    """
    Force clean an available workspace that has stale data.
    
    This is an admin operation to clean workspaces that are marked "available"
    but have stale files/commits on disk. The workspace must be in "available"
    state (safety check).
    
    Process:
    1. Verify workspace is "available" (safety check)
    2. Set status to "cleaning"
    3. Run cleanup (archive + reset + verify)
    4. Set back to "available" with clean_verified=True
    
    **CAUTION**: This will archive any work in the workspace to a branch
    (if generation_id is available) and then reset the workspace to clean state.
    
    Args:
        request: Contains workspace_id and reason
        
    Returns:
        Cleanup result with success status and message
        
    Example:
        ```
        POST /api/v1/workspace/cleanup/clean
        Content-Type: application/json
        
        {
            "workspace_id": "ws-01-1",
            "reason": "manual_cleanup_stale_data"
        }
        
        Response:
        {
            "workspace_id": "ws-01-1",
            "success": true,
            "message": "Workspace cleaned successfully",
            "error": null
        }
        ```
    """
    try:
        logger.info(
            f"Force cleaning workspace {request.workspace_id} "
            f"(reason: {request.reason})"
        )
        
        result = await workspace_pool.force_clean_available_workspace(
            request.workspace_id,
            request.reason
        )
        
        return CleanWorkspaceResponse(**result)

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Failed to clean workspace: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clean workspace: {str(e)}"
        )


# ============================================================
# 2.5d — Force-release (operator escape hatch)
# ============================================================

class ForceReleaseRequest(BaseModel):
    """Request to force-release an ALLOCATED workspace."""
    reason: str = Field(..., description="Reason for bypassing normal archive check")
    confirmed_by: str = Field(..., description="Name/email of operator authorizing the release")


class ForceReleaseResponse(BaseModel):
    """Response after force-releasing a workspace."""
    workspace_id: str
    status: str
    message: str


@router.post("/{workspace_id}/force-release", response_model=ForceReleaseResponse)
async def force_release_workspace(
    workspace_id: str,
    body: ForceReleaseRequest,
    db: IDatabase = Depends(get_db),
    _: None = Depends(require_admin),
):
    """
    Operator escape hatch: force-release an ALLOCATED workspace to CLEANING.

    Bypasses the archive precondition check. Use ONLY when the workspace must
    be reclaimed and the archive branch cannot be confirmed (e.g., repo corruption).

    Both reason and confirmed_by are written to the workspace audit trail.
    A 7-day auto-wipe will be scheduled (Phase 3 background job).

    Requires: workspace status == ALLOCATED.
    """
    from app.state import WorkspaceStateMachine
    from app.state.db_adapter import StateMachineDBAdapter
    from app.state.exceptions import InvalidWorkspaceStateError

    try:
        from app.state import GenerationSessionStateMachine
        from app.state.transitions import TriggeredBy

        db_adapter = StateMachineDBAdapter(db)
        wsm = WorkspaceStateMachine(db_adapter)

        # Capture locked_by before force_release clears it — needed to fail the session below.
        ws_doc = await db_adapter.get_workspace(workspace_id)
        locked_by_generation_id = ws_doc.get("locked_by") if ws_doc else None

        await wsm.force_release(
            workspace_id=workspace_id,
            reason=body.reason,
            confirmed_by=body.confirmed_by,
        )

        # Fail the owning session so it does not remain permanently RUNNING.
        # Without this, the session has no workspace and can never complete or retry.
        # A soft try/except is intentional: workspace is already CLEANING, so we log
        # and surface the issue rather than roll back an irreversible transition.
        est_failed = False
        if locked_by_generation_id:
            esm = GenerationSessionStateMachine(db_adapter)
            try:
                await esm.fail(
                    generation_id=locked_by_generation_id,
                    reason=f"force_release by {body.confirmed_by}: {body.reason}",
                    triggered_by=TriggeredBy.FORCE_RELEASE,
                )
                est_failed = True
                logger.warning(
                    "Generation session %s failed after force_release of workspace %s by %s",
                    locked_by_generation_id, workspace_id, body.confirmed_by,
                )
            except Exception:
                logger.error(
                    "force_release: workspace %s released but could not fail generation session %s — "
                    "session may be permanently stuck in RUNNING",
                    workspace_id, locked_by_generation_id, exc_info=True,
                )

        suffix = (
            f" Generation session {locked_by_generation_id} marked FAILED."
            if est_failed
            else (
                f" WARNING: could not fail generation session {locked_by_generation_id} — check logs."
                if locked_by_generation_id
                else ""
            )
        )
        return ForceReleaseResponse(
            workspace_id=workspace_id,
            status="cleaning",
            message=(
                f"Workspace {workspace_id} force-released to CLEANING by {body.confirmed_by}. "
                f"Reason: {body.reason}.{suffix}"
            ),
        )
    except InvalidWorkspaceStateError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        logger.error(f"Force-release failed for workspace {workspace_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Force-release failed: {e}",
        )


# ============================================================
# On-demand CLEANING → AVAILABLE (LV2.3)
# ============================================================

class ClearWorkspaceResponse(BaseModel):
    """Response after clearing a CLEANING workspace back to AVAILABLE."""
    workspace_id: str = Field(..., description="Workspace ID that was cleared")
    status: WorkspaceStatus = Field(..., description="New workspace status (available)")
    message: str = Field(..., description="Human-readable result message")


@router.post("/{workspace_id}/clear", response_model=ClearWorkspaceResponse)
async def clear_workspace(
    workspace_id: str,
    workspace_pool: WorkspacePoolService = Depends(get_workspace_pool),
    _: None = Depends(require_admin),
):
    """
    On-demand clear: transition a CLEANING workspace back to AVAILABLE.

    This is the manual, on-demand equivalent of the stuck_cleaning_recovery job.
    It invokes the existing ``WorkspacePoolService.cleanup_workspace`` which
    archives session work, resets git state, and calls ``WorkspaceStateMachine.mark_clean``
    (CLEANING → AVAILABLE) — no new state-transition logic is added here.

    Owner/admin gated (same as the force-clean route).

    Use when you have already downloaded your outputs and want to free a CLEANING
    set before the 2-hour auto-reclaim grace period expires.

    Args:
        workspace_id: ID of the workspace to clear (must be in CLEANING state)

    Returns:
        workspace_id, new status ("available"), and a result message.

    Example:
        ```
        POST /api/v1/workspace/ws-01-1/clear

        Response:
        {
            "workspace_id": "ws-01-1",
            "status": "available",
            "message": "Workspace ws-01-1 cleared successfully"
        }
        ```
    """
    try:
        await workspace_pool.cleanup_workspace(workspace_id)
        return ClearWorkspaceResponse(
            workspace_id=workspace_id,
            status=WorkspaceStatus.AVAILABLE,
            message=f"Workspace {workspace_id} cleared successfully",
        )

    except WorkspaceNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )

    except WorkspacePoolError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    except Exception as e:
        logger.error(f"Failed to clear workspace {workspace_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clear workspace {workspace_id}: {e}",
        )
