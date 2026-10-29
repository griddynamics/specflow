"""``create_app(settings)``: lifespan, routers, error handlers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from api.contained import Contained
from api.errors import install_error_handlers, required
from api.request_id import SpecflowApi
from api.routers import (
    documents,
    graph_build,
    health,
    me,
    projects,
    source_batches,
    source_removal,
    sources,
)
from api.runtime import DriveTimeoutError, Runtime, RuntimeFactory, build_runtime
from auth.dependencies import VERIFIER_STATE_ATTR, log_dev_mode_warning
from auth.verifiers import build_verifier
from drive.gateway import DriveError
from drive.reasons import Reason
from drive.select import build_drive_gateway
from logger.logger import ERROR, INFO, WARNING, get_logger, log_event
from settings import RoleSettings

logger = get_logger("api.app")

PRIME_TIMEOUT_SECONDS = 5.0
UNTRACED_PATHS = frozenset({"/healthz", "/readyz"})


def untraced(scope: MutableMapping[str, Any]) -> bool:
    """FastAPI telemetry ``exclude`` hook: probes make no HTTP spans (no Langfuse noise).

    Every other request keeps its HTTP span, including ``POST .../summarize``,
    the parent of the summarize agent run in the trace.
    """

    return scope.get("path") in UNTRACED_PATHS


async def prime_storage(runtime: Runtime) -> None:
    """Warm the storage client once so the first ``/readyz`` is not a cold start.

    Best effort and bounded: it never fails or delays the startup beyond the timeout.
    """

    with Contained() as prime:  # any failure, including TimeoutError and a missing store
        await asyncio.wait_for(runtime.store.check(), PRIME_TIMEOUT_SECONDS)
    if prime.failed:
        log_event(
            logger,
            WARNING,
            "api.startup.storage_prime_failed",
            error_type=prime.error_type,
        )
    else:
        log_event(logger, INFO, "api.startup.storage_primed")


DRIVE_IDENTITY_TIMEOUT_SECONDS = 5.0


async def select_drive(runtime: Runtime, app_env: str) -> None:
    """Pick the Drive gateway by ``APP_ENV`` and log mode and email (NFR-PORT-0002, NFR-MAIN-0001).

    A fake inside Kubernetes refuses the start (``ConfigurationError``). The
    email is best effort: an unreadable identity is logged, never fatal.
    """

    if runtime.drive is None:
        runtime.drive = build_drive_gateway(app_env)
    email: str | None = None
    reason: str | None = None
    with Contained() as read:
        try:
            email = await runtime.drive_call(
                runtime.drive.identity,
                by=time.monotonic() + DRIVE_IDENTITY_TIMEOUT_SECONDS,
                identity=True,
            )
        except DriveTimeoutError:
            reason = Reason.TOO_SLOW.value
        except DriveError as error:
            reason = error.reason.value
    if read.failed:
        reason = Reason.UNKNOWN_ERROR.value
    log_event(
        logger,
        INFO if email else WARNING,
        "drive.gateway.selected",
        mode=runtime.drive.mode,
        email=email,
        role="api",
        reason=reason,
    )


def create_app(settings: RoleSettings, *, runtime_factory: RuntimeFactory = build_runtime) -> FastAPI:
    """Build the app; no connection is opened until the lifespan starts.

    CORS is not enabled: the browser only talks to the BFF (C18).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        auth = required(settings.auth, "the api role requires auth settings")
        try:
            setattr(app.state, VERIFIER_STATE_ATTR, build_verifier(auth))
            if auth.mode == "dev":
                log_dev_mode_warning()
            app.state.runtime = await runtime_factory(settings)
            try:
                await select_drive(app.state.runtime, settings.app.app_env)
            except BaseException:
                await app.state.runtime.aclose()  # do not leak the open resources
                raise
        except Exception as error:
            log_event(
                logger,
                ERROR,
                "api.startup.failed",
                error_type=type(error).__name__,
                exc_info=True,
            )
            raise
        await prime_storage(app.state.runtime)
        log_event(logger, INFO, "api.startup.completed", port=settings.app.api_port)
        try:
            yield
        finally:
            await app.state.runtime.aclose()
            log_event(logger, INFO, "api.shutdown.completed")

    local = settings.app.app_env == "local"
    app = SpecflowApi(
        title="specflow",
        lifespan=lifespan,
        docs_url="/docs" if local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if local else None,
        telemetry={"exclude": untraced},
    )
    install_error_handlers(app)
    app.include_router(health.router)
    for router in (
        me.router,
        projects.router,
        documents.router,
        graph_build.router,
        sources.router,
        source_batches.router,
        source_removal.router,
    ):
        app.include_router(router, prefix="/api/v1")
    return app
