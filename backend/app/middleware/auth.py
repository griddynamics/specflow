"""
Authentication middleware for SpecFlow backend API.

Validates API keys from request headers against Firestore.
"""

from datetime import datetime, timezone
import logging
from typing import Optional

from fastapi import Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.telemetry_context import TelemetryContext
from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL
from app.database.factory import get_database
from app.database.interface import IDatabase

logger = logging.getLogger(__name__)

# Public endpoints that don't require authentication.
# Shared with LocalAuthMiddleware to avoid drift (DRY).
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    "/health/live",
    "/health/ready",
    "/docs",
    "/openapi.json",
    "/redoc",
})


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware to authenticate API requests using API keys.

    Looks for X-API-Key header or Authorization: Bearer header,
    validates against Firestore, and attaches user info to request.state.
    """

    # Public endpoints that don't require authentication
    PUBLIC_PATHS = PUBLIC_PATHS
    
    def __init__(self, app, db: Optional[IDatabase] = None):
        """
        Initialize authentication middleware.
        
        Args:
            app: FastAPI application
            db: Database instance (optional, will use default if not provided)
        """
        super().__init__(app)
        self.db = db or get_database()
    
    async def dispatch(self, request: Request, call_next):
        """Process request through authentication middleware."""
        # Skip authentication for public endpoints
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)
        
        # Extract API key from header
        api_key = self._extract_api_key(request)
        
        if not api_key:
            logger.warning(f"Missing API key for {request.url.path} from {request.client.host}")
            return JSONResponse(
                content={"detail": "Missing API key. Include X-API-Key header or Authorization: Bearer token."},
                status_code=status.HTTP_401_UNAUTHORIZED
            )
        
        # Validate API key
        try:
            user_info = await self._validate_api_key(api_key)
            
            if not user_info:
                logger.warning(f"Invalid API key attempted: {api_key[:15]}... from {request.client.host}")
                return JSONResponse(
                    content={"detail": "Invalid or expired API key"},
                    status_code=status.HTTP_401_UNAUTHORIZED
                )
            
            # Extract user email from header (primary user identification)
            user_email = request.headers.get("X-User-Email")
            if not user_email:
                logger.warning(f"Missing X-User-Email header for {request.url.path} from {request.client.host}")
                return JSONResponse(
                    content={"detail": "Missing X-User-Email header. This header is required for user identification."},
                    status_code=status.HTTP_400_BAD_REQUEST
                )
            
            # Validate email matches API key owner (case-insensitive comparison)
            key_owner_email = user_info["user_id"]
            if user_email.lower() != key_owner_email.lower():
                logger.warning(
                    f"Email mismatch: header={user_email}, key_owner={key_owner_email}, "
                    f"api_key={api_key[:15]}... from {request.client.host}"
                )
                return JSONResponse(
                    content={
                        "detail": (
                            f"Email mismatch: X-User-Email header ({user_email}) does not match "
                            f"API key owner ({key_owner_email}). "
                            f"Ensure your MCP configuration email matches your API key."
                        )
                    },
                    status_code=status.HTTP_401_UNAUTHORIZED
                )
            
            # Attach user info to request for use in endpoints (normalized to lowercase)
            request.state.user_email = user_email.lower()
            request.state.user_id = key_owner_email.lower()
            request.state.api_key = api_key
            request.state.user_name = user_info["user_name"]
            request.state.permissions = user_info.get("permissions", ["user"])
            request.state.key_uid = user_info.get("key_uid")
            request.state.workspace_pool = (
                user_info.get("workspace_pool") or DEFAULT_WORKSPACE_POOL
            )
            
            # Set telemetry context for LLM analytics (will be enriched by decorators with generation_id, etc.)
            TelemetryContext.set_user_context(user_email=user_email.lower())
            
            # Update last_used_at (fire and forget)
            try:
                await self._update_last_used(api_key)
            except Exception as e:
                logger.error(f"Error updating last_used_at: {e}")
                # Non-critical, don't fail request
            
            logger.info(f"Authenticated request from {user_email.lower()} (verified) to {request.url.path}")
            
        except Exception as e:
            logger.error(f"Authentication error: {e}", exc_info=True)
            return JSONResponse(
                content={"detail": "Authentication error"},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        # Continue processing request
        response = await call_next(request)
        return response
    
    def _extract_api_key(self, request: Request) -> Optional[str]:
        """
        Extract API key from request headers.
        
        Supports:
        - X-API-Key header
        - Authorization: Bearer <token> header
        """
        # Try X-API-Key header first
        api_key = request.headers.get("X-API-Key")
        if api_key:
            return api_key
        
        # Try Authorization header with Bearer token
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header.replace("Bearer ", "")
        
        return None
    
    async def _validate_api_key(self, api_key: str) -> Optional[dict]:
        """
        Validate API key against Firestore.
        
        Args:
            api_key: The API key to validate
            
        Returns:
            User info dict if valid, None if invalid/expired
        """
        try:
            # Lookup in Firestore
            doc = self.db.get("api_keys", api_key)
            
            if not doc:
                return None
            
            # Check if active
            if not doc.get("is_active", False):
                logger.warning(f"Inactive API key used: {api_key[:15]}...")
                return None
            
            # Check expiration
            expires_at = doc.get("expires_at")
            if expires_at and expires_at < datetime.now(timezone.utc):
                logger.warning(f"Expired API key used: {api_key[:15]}...")
                return None
            
            return {
                "user_id": doc["user_id"],
                "user_name": doc["user_name"],
                "permissions": doc.get("permissions", ["user"]),
                "key_uid": doc.get("key_uid"),
                "workspace_pool": doc.get("workspace_pool"),
            }
        
        except Exception as e:
            logger.error(f"Error validating API key: {e}", exc_info=True)
            return None
    
    async def _update_last_used(self, api_key: str):
        """
        Update last_used_at timestamp for API key.
        
        Args:
            api_key: The API key to update
        """
        try:
            self.db.update("api_keys", api_key, {
                "last_used_at": datetime.now(timezone.utc)
            })
        except Exception as e:
            logger.error(f"Error updating last_used_at: {e}")
            # Non-critical, don't fail request
