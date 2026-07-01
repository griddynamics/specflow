"""
Local authentication middleware for single-user self-hosted mode.

Reads the sentinel api_keys document (doc-id "local") seeded by init_db.py
and populates all 7 request.state identity fields without requiring any inbound
API key header. Intended exclusively for AUTH_MODE=local.
"""

import logging
from typing import Optional

from fastapi import Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.local_identity import LOCAL_API_KEY_DOC_ID
from app.core.telemetry_context import TelemetryContext
from app.core.workspace_pool_names import DEFAULT_WORKSPACE_POOL
from app.database.factory import get_database
from app.database.interface import IDatabase
from app.middleware.auth import PUBLIC_PATHS

logger = logging.getLogger(__name__)


class LocalAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware for local single-user auth mode.

    Bypasses header-based API key validation entirely.  Instead, reads the
    sentinel "local" document from the api_keys collection and populates the
    same 7 request.state fields that AuthMiddleware sets, so all downstream
    endpoints work unchanged.

    Inbound X-API-Key and X-User-Email headers are ignored.
    """

    def __init__(self, app, db: Optional[IDatabase] = None) -> None:
        """
        Initialize local auth middleware.

        Args:
            app: FastAPI/Starlette application.
            db: Database instance (optional, defaults to the configured database).
        """
        super().__init__(app)
        self.db = db or get_database()

    async def dispatch(self, request: Request, call_next):
        """Process request through local auth middleware."""
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        doc = self.db.get("api_keys", LOCAL_API_KEY_DOC_ID)
        if not doc:
            logger.error(
                "Local identity sentinel doc '%s' missing in api_keys — "
                "run init_db.py to seed it.",
                LOCAL_API_KEY_DOC_ID,
            )
            return JSONResponse(
                content={
                    "detail": (
                        "Local identity not seeded — run init_db.py "
                        "to initialise the local user identity before starting the server."
                    )
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        user_id_raw = doc.get("user_id")
        user_name_raw = doc.get("user_name")
        if not user_id_raw or not user_name_raw:
            logger.error(
                "Local sentinel doc '%s' is missing required fields (user_id/user_name) — "
                "re-run init_db.py to re-seed it.",
                LOCAL_API_KEY_DOC_ID,
            )
            return JSONResponse(
                content={
                    "detail": (
                        "Local identity is incomplete — re-run init_db.py "
                        "to re-seed the local user identity."
                    )
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        user_email = user_id_raw.lower()

        request.state.user_email = user_email
        request.state.user_id = user_email
        request.state.api_key = LOCAL_API_KEY_DOC_ID
        request.state.user_name = user_name_raw
        request.state.permissions = doc.get("permissions", ["user"])
        request.state.key_uid = doc.get("key_uid")
        request.state.workspace_pool = doc.get("workspace_pool") or DEFAULT_WORKSPACE_POOL

        TelemetryContext.set_user_context(user_email=user_email)

        logger.info(
            "Local auth: identity resolved from sentinel doc '%s' for path %s",
            LOCAL_API_KEY_DOC_ID,
            request.url.path,
        )

        return await call_next(request)
