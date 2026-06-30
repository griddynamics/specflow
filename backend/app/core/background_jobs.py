import asyncio
import logging

from app.jobs.scheduled_wipe import execute_confirmed_wipes, schedule_expired_workspace_wipes

logger = logging.getLogger(__name__)


async def run_job_loop(name: str, coro_fn, interval_seconds: int, **kwargs):
    """Runs a background job on a fixed interval. Logs exceptions but never crashes."""
    logger.info("Background job '%s' started (interval=%ds)", name, interval_seconds)
    while True:
        try:
            await coro_fn(**kwargs)
        except Exception:
            logger.exception("Background job '%s' raised an exception", name)
        await asyncio.sleep(interval_seconds)


async def run_scheduled_wipe(db):
    """Runs both passes of the scheduled wipe job sequentially."""
    await schedule_expired_workspace_wipes(db)
    await execute_confirmed_wipes(db)
