"""Shared retry call — one definition of the retry endpoint for CLI, MCP, and TUI.

Retry eligibility is validated by the backend state machine (``reset_for_retry``
accepts FAILED or stuck-PENDING); there is no client-side status pre-check to
drift from it. Callers POST here and render success (the returned dict) vs the
backend's error message (``str(exc)``) in their own style — mirroring the cancel
flow, which likewise defers to the backend.
"""
from __future__ import annotations

import json

from services.specflow_backend import call_backend_endpoint


async def retry_generation_core(generation_id: str) -> dict:
    """POST a retry for an already-resolved id; return the parsed backend response.

    Raises on any failure — the backend validates state and returns the reason,
    so callers surface ``str(exc)`` to the user.
    """
    text = await call_backend_endpoint(
        endpoint=f"/api/v1/generation-sessions/{generation_id}/retry",
        method="POST",
        timeout_seconds=30,
    )
    return json.loads(text)
