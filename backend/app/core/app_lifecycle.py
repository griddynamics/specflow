"""FastAPI startup/shutdown orchestration.

Owns the `lifespan` handler: startup validation, background-job loops, boot recovery of
shutdown-interrupted sessions, and the graceful-shutdown handler. See
`docs/backend/graceful-shutdown-recovery.md` for the shutdown/recovery design.
"""
import asyncio
from contextlib import asynccontextmanager
import logging
import os

from cryptography.fernet import Fernet
from fastapi import FastAPI

from app.core.background_jobs import run_job_loop, run_scheduled_wipe
from app.core.config import (
    OPENROUTER_PRICING_REFRESH_INTERVAL_SECONDS,
    SCHEDULED_WIPE_JOB_INTERVAL_SECONDS,
    STUCK_CLEANING_JOB_INTERVAL_SECONDS,
    STUCK_INITIALIZING_JOB_INTERVAL_SECONDS,
    STUCK_RUNNING_JOB_INTERVAL_SECONDS,
    settings,
)
from app.core.enums import DatabaseType
from app.core.github_platform_secrets import (
    init_github_platform_secrets,
    init_github_platform_secrets_for_tests,
)
from app.core.logging import _cleanup_queue_logging
from app.database.factory import get_database
from app.database.sqlite import SqliteDatabase
from app.jobs.shutdown_interrupted_recovery import recover_interrupted_sessions
from app.jobs.stuck_cleaning_recovery import recover_stuck_cleaning
from app.jobs.stuck_initializing_detector import detect_stuck_initializing
from app.jobs.stuck_running_detector import detect_stuck_running
from app.services.generation_session import GenerationSessionService
from app.services.generation_task_registry import GenerationTaskRegistry
from app.services.langfuse import tracer
from app.services.openrouter_pricing import ensure_openrouter_pricing_cache
from app.services.shutdown_handler import mark_active_sessions_interrupted
from app.services.startup_validation import (
    StartupValidationError,
    StartupValidator,
    get_startup_state,
    mark_startup_complete,
    mark_startup_started,
)
from app.services.workspace_pool import WorkspacePoolService
from app.state.db_adapter import StateMachineDBAdapter

logger = logging.getLogger("api.main")


async def run_startup_validation() -> None:
    """Run startup validation in the background so the server can start listening immediately."""
    logger.info("=" * 60)
    logger.info("INSTANCE STARTUP - Beginning validation sequence")
    logger.info("=" * 60)

    mark_startup_started()

    try:
        db = get_database()
        workspace_pool = WorkspacePoolService(db)
        validator = StartupValidator(db, workspace_pool)
        results = await validator.run_all_checks()
        mark_startup_complete(healthy=True, checks=results)

        logger.info("Startup validation PASSED")
        for check_name, result in results.items():
            status = "✓" if result.get("passed") else "✗"
            logger.info(f"  {status} {check_name}: {result}")

    except StartupValidationError as e:
        mark_startup_complete(healthy=False, checks={}, error=str(e))
        logger.error(f"Startup validation FAILED: {e}")
        # Don't raise - let health check fail instead

    except Exception as e:
        mark_startup_complete(healthy=False, checks={}, error=f"Unexpected error: {str(e)}")
        logger.exception(f"Startup validation CRASHED: {e}")

    logger.info("=" * 60)
    state = get_startup_state()
    logger.info(f"INSTANCE READY: healthy={state['healthy']}")
    logger.info("=" * 60)


def _init_platform_secrets() -> None:
    """Load GitHub/Fernet platform secrets before any validation or git paths run."""
    try:
        init_github_platform_secrets(settings)
    except ValueError as e:
        if settings.DATABASE_TYPE == DatabaseType.MEMORY:
            logger.warning(
                "Ephemeral GitHub platform secrets for DATABASE_TYPE=memory (%s). "
                "Set TOKEN_ENCRYPTION_KEY for stable per-key encryption.",
                e,
            )
            init_github_platform_secrets_for_tests(
                fernet_key=Fernet.generate_key(),
                github_token_default=os.environ.get("GITHUB_TOKEN_DEFAULT")
                or os.environ.get("GITHUB_TOKEN"),
                git_user_name_default=os.environ.get("GIT_USER_NAME_DEFAULT")
                or os.environ.get("GIT_USER_NAME"),
            )
        else:
            logger.error("Failed to initialize GitHub platform secrets: %s", e, exc_info=True)
            raise
    except Exception as e:
        logger.error("Failed to initialize GitHub platform secrets: %s", e, exc_info=True)
        raise


def build_background_jobs(db, workspace_pool) -> list:
    """Create the periodic background-job loop tasks (one per detector/recovery job)."""
    return [
        asyncio.create_task(run_job_loop(
            "openrouter_pricing", ensure_openrouter_pricing_cache,
            OPENROUTER_PRICING_REFRESH_INTERVAL_SECONDS, settings=settings, force=True)),
        asyncio.create_task(run_job_loop(
            "stuck_running", detect_stuck_running, STUCK_RUNNING_JOB_INTERVAL_SECONDS, db=db)),
        asyncio.create_task(run_job_loop(
            "stuck_initializing", detect_stuck_initializing, STUCK_INITIALIZING_JOB_INTERVAL_SECONDS, db=db)),
        asyncio.create_task(run_job_loop(
            "stuck_cleaning", recover_stuck_cleaning, STUCK_CLEANING_JOB_INTERVAL_SECONDS, db=db, workspace_pool_service=workspace_pool)),
        asyncio.create_task(run_job_loop(
            "scheduled_wipe", run_scheduled_wipe, SCHEDULED_WIPE_JOB_INTERVAL_SECONDS, db=db)),
    ]


def start_boot_recovery(db, raw_db, workspace_pool, task_registry: GenerationTaskRegistry) -> asyncio.Task:
    """One-shot task: auto-retry sessions interrupted by a previous shutdown (pod eviction).

    Non-blocking so it never delays the port-open. See docs/backend/graceful-shutdown-recovery.md.
    Recovered runs register with ``task_registry`` so they remain cancellable.
    """
    generation_session_service = GenerationSessionService(raw_db, workspace_pool)
    return asyncio.create_task(
        recover_interrupted_sessions(db, generation_session_service, task_registry=task_registry)
    )


async def run_shutdown_session_handling(db) -> None:
    """Mark in-flight sessions FAILED + Slack-notify so an eviction doesn't strand them.

    Workspaces are preserved (the code resumes from checkpoint on the next boot). Non-fatal.
    """
    try:
        failed_ids = await mark_active_sessions_interrupted(db)
        if failed_ids:
            logger.warning(
                "Marked %d in-flight session(s) FAILED on shutdown: %s",
                len(failed_ids), failed_ids,
            )
    except Exception:
        logger.exception("Shutdown session-handling step failed (non-fatal)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: validation + background jobs + boot recovery on startup; in-flight
    session handling + task teardown on shutdown. Yields immediately so the port opens fast
    (Cloud Run requirement) while validation runs in the background.
    """
    _init_platform_secrets()

    logger.info("Starting FastAPI server - validation will run in background")
    validation_task = asyncio.create_task(run_startup_validation())

    raw_db = get_database()
    db = StateMachineDBAdapter(raw_db)
    workspace_pool = WorkspacePoolService(raw_db)
    job_tasks = build_background_jobs(db, workspace_pool)

    # Process-local registry of running workflow tasks, shared by the run/retry/cancel
    # endpoints (via dependency injection) and boot recovery.
    app.state.generation_task_registry = GenerationTaskRegistry()

    tracer.init()

    recovery_task = start_boot_recovery(db, raw_db, workspace_pool, app.state.generation_task_registry)

    # CRITICAL: Yield immediately so FastAPI starts listening (this is what Cloud Run checks).
    yield

    # SHUTDOWN
    logger.info("Instance shutting down...")

    # Stop boot recovery first, so it cannot re-fire a session while we mark in-flight ones below.
    if not recovery_task.done():
        recovery_task.cancel()
        try:
            await recovery_task
        except asyncio.CancelledError:
            pass

    await run_shutdown_session_handling(db)

    if isinstance(raw_db, SqliteDatabase):
        logger.info("Closing SQLite connection before shutdown...")
        raw_db.close()

    if not validation_task.done():
        logger.info("Cancelling startup validation task...")
        validation_task.cancel()
        try:
            await validation_task
        except asyncio.CancelledError:
            pass

    logger.info("Cancelling background job tasks...")
    for task in job_tasks:
        task.cancel()
    await asyncio.gather(*job_tasks, return_exceptions=True)

    await tracer.flush_async()

    logger.info("Cleaning up logging infrastructure...")
    _cleanup_queue_logging()
