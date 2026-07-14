"""
Process-local registry mapping generation_id → the asyncio.Task running its workflow.

Used by the cancel endpoint to immediately tear down an in-flight run on the pod that
owns it (``task.cancel()`` injects ``CancelledError`` at the current ``await``). This is
the fast path; the cooperative :func:`app.state.cancellation.raise_if_cancelled` checks
are the cross-pod-safe fallback for cancels that land on a different pod.

The workflow task registers itself via ``asyncio.current_task()`` from inside its own
body, so there is no plumbing at the ``asyncio.create_task(...)`` call sites — the initial
run and the shared rerun path (manual retry + boot recovery) are all covered.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

_ACTIVE_TASKS: dict[str, asyncio.Task] = {}


def register_current_task(generation_id: str) -> None:
    """Register the currently running task as the workflow task for ``generation_id``."""
    task = asyncio.current_task()
    if task is not None:
        _ACTIVE_TASKS[generation_id] = task


def deregister_task(generation_id: str) -> None:
    """Remove the registry entry for ``generation_id`` — only if it is *this* task.

    Guarding on identity avoids a re-fired run (retry/recovery) that has already
    registered its own task being clobbered by the previous task's ``finally`` cleanup.
    """
    current = asyncio.current_task()
    if _ACTIVE_TASKS.get(generation_id) is current:
        _ACTIVE_TASKS.pop(generation_id, None)


def get_task(generation_id: str) -> asyncio.Task | None:
    """Return the registered workflow task for ``generation_id``, or None."""
    return _ACTIVE_TASKS.get(generation_id)


def cancel_task(generation_id: str) -> bool:
    """Cancel the registered workflow task if present and not already done.

    Returns True if a live task was found and ``cancel()`` was requested (immediate
    local teardown), False otherwise (run owned by another pod, already finished, or
    never registered — the cooperative status check handles those).
    """
    task = _ACTIVE_TASKS.get(generation_id)
    if task is not None and not task.done():
        task.cancel()
        logger.info("Requested cancellation of local workflow task for %s", generation_id)
        return True
    return False
