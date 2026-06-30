"""
Authorization dependencies for API endpoints.

Provides FastAPI dependencies for verifying resource ownership and admin access.
Follows the StateMachineDBAdapter pattern for database access.
"""

import logging
from typing import Optional

from fastapi import Depends, Form, HTTPException, Request, status

from app.database.dependencies import get_db
from app.database.interface import IDatabase
from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL
from app.state.db_adapter import StateMachineDBAdapter

logger = logging.getLogger(__name__)


def _has_admin_permission(request: Request) -> bool:
    permissions = getattr(request.state, "permissions", [])
    return "admin" in permissions or "*" in permissions


async def verify_generation_session_owner(
    generation_id: str,
    request: Request,
    db: StateMachineDBAdapter,
) -> None:
    """
    Verify that the authenticated user owns the generation session.

    This function checks that the session exists and that the user_email
    in the session document matches the authenticated user's email from
    request.state.user_email (set by AuthMiddleware).

    Uses case-insensitive email comparison to handle variations like
    Alice@example.com vs alice@example.com.

    Args:
        generation_id: Generation session ID to check
        request: FastAPI request with authenticated user
        db: StateMachineDBAdapter instance

    Raises:
        HTTPException:
            - 404 if generation session not found
            - 403 if user doesn't own the generation session
            - 500 on unexpected errors
    """
    try:
        # Get generation session document using adapter
        session_doc = await db.get_generation_session(generation_id)

        # Return 404 if session doesn't exist
        if not session_doc or not session_doc.get("generation_id"):
            logger.warning("Generation session not found: %s", generation_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Generation session {generation_id} not found",
            )

        # Get owner and current user (case-insensitive comparison)
        session_owner = session_doc.get("user_email", "").lower()
        current_user = request.state.user_email.lower()  # Already normalized in middleware

        # Check ownership
        if session_owner != current_user:
            logger.warning(
                "Authorization failed: user %s attempted to access "
                "generation session %s owned by %s",
                current_user,
                generation_id,
                session_owner,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Access denied: Generation session {generation_id} is owned by another user. "
                    "You can only access generation sessions you created."
                ),
            )

        session_pool = session_doc.get("workspace_pool") or DEFAULT_WORKSPACE_POOL
        req_pool = getattr(request.state, "workspace_pool", None) or DEFAULT_WORKSPACE_POOL
        if session_pool != req_pool:
            logger.warning(
                "Authorization failed: workspace_pool mismatch for generation session %s",
                generation_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Access denied: API key workspace pool does not match this generation session."
                ),
            )

        # key_uid check — always enforced after migration.
        # A session without key_uid means the migration has not run; treat as bug.
        session_kuid = session_doc.get("key_uid")
        req_kuid = getattr(request.state, "key_uid", None)
        if not session_kuid or not req_kuid or session_kuid != req_kuid:
            logger.warning(
                "Authorization failed: key_uid mismatch for generation session %s "
                "(session_kuid=%s, req_kuid=%s)",
                generation_id,
                session_kuid,
                req_kuid,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: This generation session was started with a different API key.",
            )

        logger.debug("Authorization success: user %s owns %s", current_user, generation_id)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error verifying generation session ownership: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error verifying generation session ownership: {str(e)}",
        )


async def require_generation_session_owner(
    generation_id: str,
    request: Request,
    raw_db: IDatabase = Depends(get_db),
):
    """
    FastAPI dependency: Verify user owns the generation session before endpoint execution.

    This dependency performs authorization checks before the endpoint handler runs.
    If the user doesn't own the session, an HTTPException is raised automatically.
    
    Usage:
        @router.get("/{generation_id}/status")
        async def get_status(
            generation_id: str,
            request: Request,
            _: None = Depends(require_generation_session_owner)
        ):
            # User is verified owner at this point
            ...
    
    Args:
        generation_id: From path parameter
        request: FastAPI request
        db: Database instance (injected)
        
    Raises:
        HTTPException: 403 if not owner, 404 if not found
    """
    await verify_generation_session_owner(generation_id, request, StateMachineDBAdapter(raw_db))


async def require_generation_session_owner_form(
    request: Request,
    generation_id: Optional[str] = Form(None),
    raw_db: IDatabase = Depends(get_db),
) -> None:
    """
    FastAPI dependency: Verify user owns the generation session (Form-based generation_id).

    Same as require_generation_session_owner but reads generation_id from form data
    instead of path parameters. Skips check if generation_id is not provided
    (the endpoint itself handles that case).

    Usage:
        @router.post("/plan")
        async def run_planning(
            generation_id: Optional[str] = Form(None),
            _: None = Depends(require_generation_session_owner_form),
        ):
            ...
    """
    if not generation_id:
        return
    await verify_generation_session_owner(generation_id, request, StateMachineDBAdapter(raw_db))


async def verify_generation_session_owner_or_admin(
    generation_id: str,
    request: Request,
    db: StateMachineDBAdapter,
) -> None:
    """
    Verify the user owns the generation session, or has admin permissions.

    Admins may access any generation session without matching user_email,
    key_uid, or workspace_pool. The session must still exist (404 otherwise).
    """
    if _has_admin_permission(request):
        session_doc = await db.get_generation_session(generation_id)
        if not session_doc or not session_doc.get("generation_id"):
            logger.warning("Generation session not found: %s", generation_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Generation session {generation_id} not found",
            )
        logger.debug(
            "Admin authorization success for generation session %s",
            generation_id,
        )
        return

    await verify_generation_session_owner(generation_id, request, db)


async def require_generation_session_owner_or_admin(
    generation_id: str,
    request: Request,
    raw_db: IDatabase = Depends(get_db),
) -> None:
    """FastAPI dependency: owner or admin may access the generation session."""
    await verify_generation_session_owner_or_admin(
        generation_id,
        request,
        StateMachineDBAdapter(raw_db),
    )


async def require_authenticated_user(request: Request) -> None:
    """
    FastAPI dependency: require any authenticated user.

    The auth middleware (AuthMiddleware / LocalAuthMiddleware) populates
    ``request.state.user_email`` for every non-public path. This dependency makes
    that requirement explicit at the route layer for endpoints that need a valid
    user but no ownership or admin check (e.g. model validation).

    Usage:
        router = APIRouter(dependencies=[Depends(require_authenticated_user)])

    Raises:
        HTTPException: 401 if no authenticated user is present on the request.
    """
    if not getattr(request.state, "user_email", None):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )


async def require_admin(request: Request) -> None:
    """
    FastAPI dependency: Verify the authenticated user has admin permissions.

    Admin access is granted if the user's permissions list contains either
    "*" (wildcard / all permissions) or "admin" (explicit admin role).

    Usage:
        @router.post("/keys")
        async def create_api_key(
            _: None = Depends(require_admin),
        ):
            ...

    Raises:
        HTTPException: 403 if user lacks admin permissions
    """
    permissions = getattr(request.state, "permissions", [])
    if "admin" in permissions:
        return
    logger.warning(
        f"Admin access denied for user {getattr(request.state, 'user_email', 'unknown')} "
        f"with permissions {permissions}"
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required",
    )
