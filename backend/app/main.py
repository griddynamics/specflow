import logging

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.api.v1.auth import router as auth_router
from app.api.v1.generation_sessions import router as generation_sessions_router
from app.api.v1.model_validation import router as model_validation_router
from app.api.v1.workspaces import router as workspaces_router
from app.core.app_lifecycle import lifespan
from app.core.config import settings
from app.core.enums import AuthMode
from app.core.logging import configure_logging
from app.state.api_key_session_concurrency import SessionAtCapacityError
from app.middleware.auth import AuthMiddleware
from app.middleware.local_auth import LocalAuthMiddleware
from app.middleware.logging import RequestLoggingMiddleware
from app.services.startup_validation import get_startup_state, StartupValidationError

# Configure logging at startup
LOG_LEVEL = logging.DEBUG
configure_logging(LOG_LEVEL)
logger = logging.getLogger("api.main")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)


@app.exception_handler(SessionAtCapacityError)
async def _session_at_capacity_handler(_request: Request, _exc: SessionAtCapacityError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": "This API key has reached its maximum concurrent generation sessions.",
            "status": "at_capacity",
        },
    )


# Add Middleware — auth mode selected at startup; unknown mode raises immediately.
if settings.AUTH_MODE == AuthMode.LOCAL:
    app.add_middleware(LocalAuthMiddleware)
elif settings.AUTH_MODE == AuthMode.API_KEY:
    app.add_middleware(AuthMiddleware)
else:
    raise StartupValidationError(f"Unknown AUTH_MODE: {settings.AUTH_MODE}")
app.add_middleware(RequestLoggingMiddleware)

# Include API Routers
app.include_router(auth_router, prefix=settings.API_V1_STR)
app.include_router(generation_sessions_router, prefix=settings.API_V1_STR)
app.include_router(workspaces_router, prefix=settings.API_V1_STR)
app.include_router(model_validation_router, prefix=settings.API_V1_STR)


# Global Exception Handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global exception occurred: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "path": str(request.url)}
    )

@app.get("/health")
async def health_check():
    """
    Health check endpoint for Cloud Run.
    Returns 200 only if startup validation passed.

    This endpoint is PUBLIC (no API key required) for use by:
    - Cloud Run health checks
    - Load balancers
    - Monitoring systems

    Returns minimal information for security. Use /status for detailed diagnostics.
    """
    state = get_startup_state()

    if not state["complete"]:
        if state.get("error"):
            # Startup failed
            raise HTTPException(
                status_code=503,
                detail=f"Startup failed: {state['error']}"
            )
        # Startup still in progress
        raise HTTPException(
            status_code=503,
            detail="Startup in progress"
        )

    # Return minimal info - no sensitive details
    return {
        "status": "healthy",
        "project": settings.PROJECT_NAME
    }


@app.get("/status")
async def status_check():
    """
    Detailed system status endpoint (requires API key).

    Returns comprehensive diagnostic information including:
    - Workspace pool statistics
    - Crash recovery statistics
    - Filestore mount information
    - All startup validation check details

    This endpoint requires authentication via X-API-Key header.
    """
    state = get_startup_state()

    if not state["complete"]:
        # Startup still in progress
        raise HTTPException(
            status_code=503,
            detail="Startup in progress"
        )

    if not state["healthy"]:
        # Startup failed
        raise HTTPException(
            status_code=503,
            detail=f"Startup failed: {state['error']}"
        )

    # Return detailed diagnostic information
    return {
        "status": "healthy",
        "project": settings.PROJECT_NAME,
        "checks": state["checks"]
    }


@app.get("/health/live")
async def liveness_check():
    """
    Liveness probe - is the process alive?
    Always returns 200 if the process is running.

    Used by:
    - Kubernetes/Cloud Run liveness probe
    """
    return {"status": "alive"}


@app.get("/health/ready")
async def readiness_check():
    """
    Readiness probe - can this instance handle requests?
    Returns 200 only after startup validation passes.

    Used by:
    - Kubernetes/Cloud Run readiness probe
    - Load balancer traffic routing
    """
    state = get_startup_state()

    if not state["complete"] or not state["healthy"]:
        raise HTTPException(
            status_code=503,
            detail="Not ready"
        )

    return {"status": "ready"}

@app.get("/")
async def root():
    return {"message": "Welcome to SpecFlow Backend API"}
