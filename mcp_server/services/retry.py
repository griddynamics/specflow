"""Shared retry decision logic — one source of truth for CLI, MCP, and TUI.

The core operates on an already-resolved generation id and returns a sum type:
exactly one outcome dataclass, each carrying only the fields it always has.
Callers own resolution ("which session?") and rendering (print / chat_json /
toast) — those genuinely differ per surface. The core owns the status→retry
decision and is non-raising for every expected outcome.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from schemas.generation_workflow_enums import GenerationStatus
from services.specflow_backend import call_backend_endpoint
from services.tool_helpers import check_status_safe, is_generation_in_progress


@dataclass(frozen=True)
class AlreadyRunning:
    """Session is running/initializing — retry is not allowed."""


@dataclass(frozen=True)
class PendingRejectedBeforeCodegen:
    """Session is pending with a rejection error — user must fix files, not retry."""
    status_data: dict
    error: str


@dataclass(frozen=True)
class PendingNotFailed:
    """Session is pending but not failed — nothing to retry yet."""
    status_data: dict


@dataclass(frozen=True)
class Queued:
    """Retry accepted — backend reset the session and re-fired the workflow."""
    backend_data: dict


@dataclass(frozen=True)
class BackendError:
    """The retry POST failed (server unreachable / HTTP error)."""
    error: str


RetryOutcome = (
    AlreadyRunning
    | PendingRejectedBeforeCodegen
    | PendingNotFailed
    | Queued
    | BackendError
)


async def retry_generation_core(generation_id: str) -> RetryOutcome:
    """Decide and perform a retry for an already-resolved generation id."""
    status_data = await check_status_safe(generation_id)

    if is_generation_in_progress(status_data):
        return AlreadyRunning()

    if status_data:
        st = (status_data.get("status") or "").lower()
        if st == GenerationStatus.PENDING:
            error = (status_data.get("error") or "").strip()
            if error:
                return PendingRejectedBeforeCodegen(status_data=status_data, error=error)
            return PendingNotFailed(status_data=status_data)

    try:
        text = await call_backend_endpoint(
            endpoint=f"/api/v1/generation-sessions/{generation_id}/retry",
            method="POST",
            timeout_seconds=30,
        )
        return Queued(backend_data=json.loads(text))
    except Exception as e:  # noqa: BLE001 - surfaced as a structured outcome, not raised
        return BackendError(error=str(e))
