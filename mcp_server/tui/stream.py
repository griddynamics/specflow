"""Async SSE client for the TUI workspace message drill-in.

Wraps the service-layer ``stream_backend_sse`` primitive and parses each
``data:`` payload into an :class:`AgentStreamEvent` dataclass. The contract is
strictly best-effort, mirroring the backend's: a connection error, a malformed
event, or a dropped stream must never crash the TUI — the async generator simply
ends or skips the bad event. The Textual screen consumes this in a background
worker and appends events to its live feed.

The parsed shape mirrors the backend ``AgentStreamEvent`` SSE payload built by
``backend/app/services/agent_stream_events.py``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

from services.specflow_backend import stream_backend_sse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentStreamEvent:
    """A compact, display-ready agent message event (client mirror)."""

    timestamp: str
    kind: str
    message: str
    workflow: str | None = None
    tool_name: str | None = None
    subagent_name: str | None = None
    workspace_id: str | None = None

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "AgentStreamEvent":
        """Build from a parsed SSE JSON payload, tolerating missing fields."""
        return cls(
            timestamp=str(data.get("timestamp") or ""),
            kind=str(data.get("kind") or "unknown"),
            message=str(data.get("message") or ""),
            workflow=data.get("workflow"),
            tool_name=data.get("tool_name"),
            subagent_name=data.get("subagent_name"),
            workspace_id=data.get("workspace_id"),
        )


def workspace_stream_endpoint(generation_id: str, workspace_id: str) -> str:
    """Build the SSE endpoint path for one workspace's live message stream."""
    return (
        f"/api/v1/generation-sessions/{generation_id}"
        f"/workspaces/{workspace_id}/messages/stream"
    )


async def workspace_message_events(
    generation_id: str,
    workspace_id: str,
) -> AsyncIterator[AgentStreamEvent]:
    """Yield live :class:`AgentStreamEvent`s for a workspace; never raises.

    Connects to the backend SSE endpoint and parses each ``data:`` payload.
    Malformed payloads are skipped; any connection/stream error ends the
    generator cleanly so the caller can show a "stream ended" state without the
    TUI crashing.
    """
    endpoint = workspace_stream_endpoint(generation_id, workspace_id)
    try:
        async for raw in stream_backend_sse(endpoint):
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("tui.stream: skipping non-JSON SSE payload")
                continue
            if isinstance(payload, dict):
                yield AgentStreamEvent.from_payload(payload)
    except Exception:  # noqa: BLE001 - live stream is strictly best-effort
        logger.debug("tui.stream: message stream ended with error", exc_info=True)
        return
