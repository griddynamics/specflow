"""
API Key management endpoints.

Provides endpoints for creating, listing, and revoking API keys.
"""

import logging
import secrets
import uuid
from typing import List, Optional
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.dependencies import require_admin
from app.core.github_platform_secrets import get_github_platform_secrets
from app.core.telemetry_decorator import track_event
from app.database.interface import IDatabase
from app.database.dependencies import get_db
from app.models.api_key import APIKeyCreate
from app.schemas.generation_workflow_enums import GenerationStatus
from app.state.api_key_session_concurrency import (
    ApiKeyDocMissingError,
    ApiKeySessionConcurrency,
    AuthSessionSnapshot,
)
from app.state.db_adapter import COL_GENERATION_SESSIONS, StateMachineDBAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["authentication"])


# ============================================================
# Response Models
# ============================================================

class APIKeyResponse(BaseModel):
    """Response when creating an API key."""
    
    api_key: str = Field(..., description="The generated API key")
    user_id: str = Field(..., description="User identifier")
    user_name: str = Field(..., description="User display name")
    key_uid: str = Field(..., description="Stable non-secret identifier for this key")
    workspace_pool: str = Field(..., description="Workspace pool for this key")
    created_at: str = Field(..., description="Creation timestamp")
    expires_at: Optional[str] = Field(None, description="Expiration timestamp")


class APIKeyListItem(BaseModel):
    """API key information for listing (key is masked)."""
    
    api_key_prefix: str = Field(..., description="First 15 chars of API key")
    key_uid: Optional[str] = None
    workspace_pool: str = "default"
    user_id: str
    user_name: str
    created_at: str
    last_used_at: Optional[str]
    expires_at: Optional[str]
    is_active: bool


class APIKeyListResponse(BaseModel):
    """Response for listing API keys."""
    
    keys: List[APIKeyListItem]
    total: int


class RevokeResponse(BaseModel):
    """Response after revoking an API key."""
    
    message: str
    api_key_prefix: str


class GithubTokenUploadBody(BaseModel):
    """Upload a GitHub PAT for the authenticated API key (TLS protects plaintext in transit)."""

    token: str = Field(..., min_length=1, description="GitHub personal access token")
    git_user_name: Optional[str] = Field(
        default=None,
        description="Optional git user for https:// URLs (default: platform default or x-access-token)",
    )
    target_key_uid: Optional[str] = Field(
        default=None,
        description="Admin-only: update a different key identified by this key_uid instead of the caller's own key",
    )


class GithubTokenUploadResponse(BaseModel):
    ok: bool = True
    updated_at: str


# ============================================================
# Helper Functions
# ============================================================

def _generate_api_key() -> str:
    """
    Generate a secure random API key.

    Format: gain_<32 random bytes in base64>
    """
    random_part = secrets.token_urlsafe(32)
    return f"gain_{random_part}"


def _find_recoverable_session(
    snap: AuthSessionSnapshot,
    db: IDatabase,
    owner_key_uid: str | None,
) -> str | None:
    """Return the generation_id of the most-recent non-completed session owned by this key, or None."""
    for s in sorted(snap.active, key=lambda x: x.lease_started_at, reverse=True):
        est = db.get(COL_GENERATION_SESSIONS, s.generation_id)
        if est is None or est.get("status") == GenerationStatus.COMPLETED.value:
            continue
        est_k = est.get("key_uid")
        if est_k and owner_key_uid and est_k != owner_key_uid:
            logger.warning(
                "get_auth_me: ignoring generation session %s (key_uid mismatch: est=%s key=%s)",
                s.generation_id, est_k, owner_key_uid,
            )
            continue
        return s.generation_id
    return None


def _serialize_active_sessions(snap: AuthSessionSnapshot) -> list[dict]:
    """Serialize active sessions to JSON-safe dicts for the /auth/me response."""
    return [
        {
            "generation_id": s.generation_id,
            "operation": s.operation.value,
            "lease_started_at": s.lease_started_at.isoformat(),
            "lease_ttl_minutes": s.lease_ttl_minutes,
        }
        for s in snap.active
    ]


# ============================================================
# Endpoints
# ============================================================

@router.get("/me")
@track_event(event_name="get_auth_me_triggered")
async def get_auth_me(
    request: Request,
    db: IDatabase = Depends(get_db),
):
    """
    Get current API key's active generation sessions for MCP session recovery.

    Returns a recoverable ``current_process`` (generation id) when an active session
    points at a non-completed generation owned by this key (read-only; no api_keys writes).
    """
    api_key = getattr(request.state, "api_key", None)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    doc = db.get("api_keys", api_key)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )
    adapter = StateMachineDBAdapter(db)
    session = ApiKeySessionConcurrency(adapter)
    try:
        snap = await session.snapshot(api_key_doc_id=api_key)
    except ApiKeyDocMissingError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )

    return {
        "current_process": _find_recoverable_session(snap, db, doc.get("key_uid")),
        "in_progress": len(snap.active) > 0,
        "active_generation_sessions": _serialize_active_sessions(snap),  # state-ok: read-only API response
        "max_concurrent_sessions": snap.max_concurrent_sessions,  # state-ok: read-only API response
        "at_capacity": snap.at_capacity,
        "user_id": doc.get("user_id"),
        "key_uid": doc.get("key_uid"),
        "workspace_pool": doc.get("workspace_pool", "default"),
    }


@router.put("/github-token", response_model=GithubTokenUploadResponse)
@track_event(event_name="put_github_token_triggered")
async def put_github_token(
    request: Request,
    body: GithubTokenUploadBody,
    db: IDatabase = Depends(get_db),
):
    """
    Store an encrypted GitHub PAT on an API key document.

    Default: updates the caller's own key.
    Admin only: if ``target_key_uid`` is provided in the body, updates the key
    with that uid instead (useful for provisioning scripts).
    """
    api_key = getattr(request.state, "api_key", None)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    if body.target_key_uid:
        # Admin-only: resolve target key by uid
        caller_doc = db.get("api_keys", api_key)
        if not caller_doc or "admin" not in (caller_doc.get("permissions") or []):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin permissions required to set github token for another key",
            )
        target_doc = db.get_api_key_by_uid(body.target_key_uid)
        if not target_doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"API key not found with key_uid: {body.target_key_uid}",
            )
        update_key = target_doc["api_key"]
    else:
        doc = db.get("api_keys", api_key)
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="API key not found",
            )
        update_key = api_key

    try:
        platform = get_github_platform_secrets()
        ciphertext = platform.encrypt_token(body.token.strip())
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server encryption is not initialized",
        )
    now = datetime.now(timezone.utc)
    updates: dict = {
        "github_token_ciphertext": ciphertext,
        "github_token_set_at": now,
    }
    if body.git_user_name is not None and body.git_user_name.strip():
        updates["git_user_name"] = body.git_user_name.strip()
    db.update("api_keys", update_key, updates)
    logger.info("Updated encrypted GitHub token for API key (redacted)")
    return GithubTokenUploadResponse(updated_at=now.isoformat())


@router.post("/keys", response_model=APIKeyResponse, status_code=status.HTTP_201_CREATED)
@track_event(event_name="create_api_key_triggered")
async def create_api_key(
    request: APIKeyCreate,
    db: IDatabase = Depends(get_db),
    _: None = Depends(require_admin),
):
    """
    Create a new API key for a user. Requires admin permissions.

    Example:
        ```bash
        curl -X POST "http://localhost:8000/api/v1/auth/keys" \\
          -H "X-API-Key: <admin-key>" \\
          -H "X-User-Email: admin@company.com" \\
          -H "Content-Type: application/json" \\
          -d '{
            "user_id": "john.doe@company.com",
            "user_name": "John Doe",
            "expires_days": 365,
            "permissions": ["*"]
          }'
        ```

    Returns:
        APIKeyResponse with the generated API key (save it securely!)
    """
    try:
        # Generate API key
        api_key = _generate_api_key()
        key_uid = str(uuid.uuid4())

        # Calculate expiration
        expires_at = None
        if request.expires_days:
            expires_at = datetime.now(timezone.utc) + timedelta(days=request.expires_days)
        
        # Create key document
        key_doc = {
            "api_key": api_key,
            "key_uid": key_uid,
            "workspace_pool": request.workspace_pool,
            "user_id": request.user_id,
            "user_name": request.user_name,
            "created_at": datetime.now(timezone.utc),
            "last_used_at": None,
            "expires_at": expires_at,
            "is_active": True,
            "permissions": request.permissions,
            "metadata": {},
            "github_token_ciphertext": None,
            "github_token_set_at": None,
            "git_user_name": None,
            "max_concurrent_sessions": request.max_concurrent_sessions,  # state-ok: initial key creation
            "active_generation_sessions": [],  # state-ok: initial key creation
        }
        
        # Store in Firestore (use api_key as document ID)
        db.set("api_keys", api_key, key_doc)
        
        logger.info(f"Created API key for {request.user_id}")
        
        return APIKeyResponse(
            api_key=api_key,
            user_id=request.user_id,
            user_name=request.user_name,
            key_uid=key_uid,
            workspace_pool=request.workspace_pool,
            created_at=key_doc["created_at"].isoformat(),
            expires_at=expires_at.isoformat() if expires_at else None
        )
    
    except Exception as e:
        logger.error(f"Failed to create API key: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create API key: {str(e)}"
        )


@router.get("/keys", response_model=APIKeyListResponse)
@track_event(event_name="list_api_keys_triggered")
async def list_api_keys(
    db: IDatabase = Depends(get_db),
    _: None = Depends(require_admin),
):
    """
    List all API keys (admin only).
    
    Returns list of keys with masked key values for security.
    Only shows first 15 characters of each key.
    
    Example:
        ```bash
        curl "http://localhost:8000/api/v1/auth/keys"
        ```
    """
    try:
        # Get all API keys
        docs = db.query("api_keys", [])
        
        # Mask API keys in response
        keys = []
        for doc in docs:
            keys.append(APIKeyListItem(
                api_key_prefix=doc["api_key"][:15] + "...",
                key_uid=doc.get("key_uid"),
                workspace_pool=doc.get("workspace_pool", "default"),
                user_id=doc["user_id"],
                user_name=doc["user_name"],
                created_at=doc["created_at"].isoformat(),
                last_used_at=doc.get("last_used_at").isoformat() if doc.get("last_used_at") else None,
                expires_at=doc.get("expires_at").isoformat() if doc.get("expires_at") else None,
                is_active=doc["is_active"]
            ))
        
        return APIKeyListResponse(keys=keys, total=len(keys))
    
    except Exception as e:
        logger.error(f"Failed to list API keys: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list API keys: {str(e)}"
        )


@router.delete("/keys/{api_key_prefix}", response_model=RevokeResponse)
@track_event(event_name="revoke_api_key_triggered")
async def revoke_api_key(
    api_key_prefix: str,
    db: IDatabase = Depends(get_db),
    _: None = Depends(require_admin),
):
    """
    Revoke an API key (mark as inactive).
    
    Pass the first 15 characters of the API key to revoke.
    The key will be marked as inactive but not deleted (for audit trail).
    
    Args:
        api_key_prefix: First 15 characters of the API key (e.g., "gain_xxxxxxxx")
    
    Example:
        ```bash
        curl -X DELETE "http://localhost:8000/api/v1/auth/keys/gain_xxxxxxxx"
        ```
    """
    try:
        # Find key by prefix
        docs = db.query("api_keys", [])
        
        matching_key = None
        for doc in docs:
            if doc["api_key"].startswith(api_key_prefix):
                matching_key = doc["api_key"]
                break
        
        if not matching_key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"API key not found with prefix: {api_key_prefix}"
            )
        
        # Mark as inactive
        db.update("api_keys", matching_key, {"is_active": False})
        
        logger.info(f"Revoked API key: {api_key_prefix}...")
        
        return RevokeResponse(
            message="API key revoked successfully",
            api_key_prefix=api_key_prefix
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to revoke API key: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to revoke API key: {str(e)}"
        )


@router.post("/keys/{api_key_prefix}/reactivate")
@track_event(event_name="reactivate_api_key_triggered")
async def reactivate_api_key(
    api_key_prefix: str,
    db: IDatabase = Depends(get_db),
    _: None = Depends(require_admin),
):
    """
    Reactivate a previously revoked API key.
    
    Args:
        api_key_prefix: First 15 characters of the API key
    
    Example:
        ```bash
        curl -X POST "http://localhost:8000/api/v1/auth/keys/gain_xxxxxxxx/reactivate"
        ```
    """
    try:
        # Find key by prefix
        docs = db.query("api_keys", [])
        
        matching_key = None
        for doc in docs:
            if doc["api_key"].startswith(api_key_prefix):
                matching_key = doc["api_key"]
                break
        
        if not matching_key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"API key not found with prefix: {api_key_prefix}"
            )
        
        # Mark as active
        db.update("api_keys", matching_key, {"is_active": True})
        
        logger.info(f"Reactivated API key: {api_key_prefix}...")
        
        return {
            "message": "API key reactivated successfully",
            "api_key_prefix": api_key_prefix
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to reactivate API key: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reactivate API key: {str(e)}"
        )
