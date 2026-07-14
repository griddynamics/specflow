"""
Cooperative cancellation for in-flight generation workflows.

Single source of truth for "has this generation been cancelled?". The user-facing
cancel path (``DELETE`` endpoint → ``GenerationSessionService.cancel_generation_session``)
transitions the session to CANCELLED and, on the owning pod, calls ``task.cancel()`` for
immediate teardown. This module is the cross-pod-safe fallback: long-running workflow /
phase / poll loops call :func:`raise_if_cancelled` at coarse boundaries so a cancel that
lands on a different pod (or a task that is not locally registered) still stops the run.

Status is read through the async DB adapter and compared via ``parse_status`` — no second
status validator is introduced (Commandment: single source of truth). Reads are throttled
per generation so the helper stays cheap when called inside tight loops.
"""
import time

from app.schemas.generation_workflow_enums import GenerationStatus, parse_status
from app.state.exceptions import GenerationCancelledError

# generation_id -> last monotonic timestamp we hit the DB for this session.
_last_check: dict[str, float] = {}

# Minimum seconds between DB reads for the same generation inside a hot loop.
_DEFAULT_MIN_INTERVAL_S = 2.0


async def raise_if_cancelled(
    db_adapter,
    generation_id: str,
    *,
    min_interval_s: float = _DEFAULT_MIN_INTERVAL_S,
) -> None:
    """Raise :class:`GenerationCancelledError` if the session is CANCELLED.

    A ``None`` ``db_adapter`` is a no-op: cancellation can't be observed without a DB,
    and in practice that only happens in unit tests that run phases without a
    ``generation_session_service`` (every real run wires one). Centralizing the guard
    here lets call sites invoke this unconditionally. ``task.cancel()`` remains the
    local stop path regardless.

    The first check for a generation always reads; thereafter, if called again for the
    same ``generation_id`` within ``min_interval_s`` seconds, it returns immediately
    without touching the DB. (``time.monotonic()`` has no fixed epoch, so a "never
    checked" sentinel must be distinct from any real timestamp — not 0.0 — otherwise a
    small monotonic value on a freshly-booted host would throttle the very first read.)
    ``db_adapter`` is the async state-machine DB adapter (``service.db_adapter`` /
    ``orchestrator._db``).
    """
    if db_adapter is None:
        return

    now = time.monotonic()
    last = _last_check.get(generation_id)
    if last is not None and now - last < min_interval_s:
        return
    _last_check[generation_id] = now

    doc = await db_adapter.get_generation_session(generation_id)
    if doc and parse_status(doc.get("status")) == GenerationStatus.CANCELLED:
        raise GenerationCancelledError(generation_id)


def reset_cancellation_check(generation_id: str) -> None:
    """Drop the throttle timestamp for a generation (test/cleanup helper)."""
    _last_check.pop(generation_id, None)
