"""
Process-local registry mapping generation_id → the asyncio.Task running its workflow.

Used by the cancel endpoint to immediately tear down an in-flight run on the pod that
owns it (``task.cancel()`` injects ``CancelledError`` at the current ``await``). This is
the fast path; the cooperative :func:`app.state.cancellation.raise_if_cancelled` checks
are the cross-pod-safe fallback for cancels that land on a different pod.

One instance per process is created at app startup and stored on ``app.state`` (see
``app_lifecycle``); endpoints receive it via dependency injection and it is threaded into
the workflow task bodies. The workflow task registers itself via ``asyncio.current_task()``
from inside its own body, so callers only pass the registry — they don't manage handles.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


class GenerationTaskRegistry:
    """In-memory, single-loop registry of active generation workflow tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def register_current_task(self, generation_id: str) -> None:
        """Register the currently running task as the workflow task for ``generation_id``."""
        task = asyncio.current_task()
        if task is not None:
            self._tasks[generation_id] = task

    def deregister_task(self, generation_id: str) -> None:
        """Remove the registry entry for ``generation_id`` — only if it is *this* task.

        Guarding on identity avoids a re-fired run (retry/recovery) that has already
        registered its own task being clobbered by the previous task's ``finally`` cleanup.
        """
        current = asyncio.current_task()
        if self._tasks.get(generation_id) is current:
            self._tasks.pop(generation_id, None)

    def get_task(self, generation_id: str) -> asyncio.Task | None:
        """Return the registered workflow task for ``generation_id``, or None."""
        return self._tasks.get(generation_id)

    def cancel_task(self, generation_id: str) -> bool:
        """Cancel the registered workflow task if present and not already done.

        Returns True if a live task was found and ``cancel()`` was requested (immediate
        local teardown), False otherwise (run owned by another pod, already finished, or
        never registered — the cooperative status check handles those).
        """
        task = self._tasks.get(generation_id)
        if task is not None and not task.done():
            task.cancel()
            logger.info("Requested cancellation of local workflow task for %s", generation_id)
            return True
        return False
