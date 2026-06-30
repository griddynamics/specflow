"""API key models for authentication."""

from datetime import datetime
from typing import Annotated, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.core.workspace_pool_names import ALLOWED_WORKSPACE_POOLS, DEFAULT_WORKSPACE_POOL
from app.schemas.permissions import Permission, VALID_PERMISSIONS


class APIKey(BaseModel):
    """API key for authenticating MCP clients."""

    api_key: str = Field(..., description="The API key itself")
    user_id: str = Field(..., description="User identifier (email)")
    user_name: str = Field(..., description="User display name")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    is_active: bool = True
    permissions: List[str] = Field(default_factory=lambda: [Permission.USER.value])
    metadata: dict = Field(default_factory=dict)
    max_concurrent_sessions: Annotated[
        int, Field(ge=1, le=20, description="Maximum concurrent generation sessions")
    ] = 5
    active_generation_sessions: List[dict] = Field(default_factory=list)


class APIKeyCreate(BaseModel):
    """Request to create a new API key."""

    user_id: str = Field(..., description="User identifier (email)")
    user_name: str = Field(..., description="User display name")
    workspace_pool: str = Field(
        default=DEFAULT_WORKSPACE_POOL,
        description="Workspace pool slug for this key (dedicated pools require per-key GitHub PAT upload)",
    )
    expires_days: Optional[int] = Field(
        None,
        description="Number of days until expiration (None = never expires)"
    )
    permissions: List[str] = Field(
        default_factory=lambda: [Permission.USER.value],
        description=f"List of permissions. Allowed values: {sorted(VALID_PERMISSIONS)}",
    )
    max_concurrent_sessions: Annotated[
        int, Field(ge=1, le=20, description="Maximum concurrent generation sessions for this API key")
    ] = 5

    @field_validator("workspace_pool")
    @classmethod
    def _pool_allowed(cls, v: str) -> str:
        if v not in ALLOWED_WORKSPACE_POOLS:
            raise ValueError(
                f"workspace_pool must be one of {sorted(ALLOWED_WORKSPACE_POOLS)}, got {v!r}"
            )
        return v

    @field_validator("permissions")
    @classmethod
    def _permissions_allowed(cls, v: List[str]) -> List[str]:
        invalid = set(v) - VALID_PERMISSIONS
        if invalid:
            raise ValueError(
                f"Invalid permissions: {sorted(invalid)}. Allowed: {sorted(VALID_PERMISSIONS)}"
            )
        return v
