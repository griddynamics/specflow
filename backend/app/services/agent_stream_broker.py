"""Process-local pub/sub for live agent message events.

A tiny in-memory broker that fans out :class:`AgentStreamEvent` objects from the
Claude consumption loop (publisher side) to connected SSE clients (subscriber
side), keyed by ``(generation_id, workspace_id)``.

Design constraints (see the TUI message-stream plan):

- Publishing is **non-blocking**: ``publish`` uses ``put_nowait`` and drops the
  event when a subscriber queue is full. Backpressure from a slow TUI must never
  slow or break generation.
- Subscribers each get their own bounded ``asyncio.Queue`` and only receive
  events published *after* they subscribe (live tail, no history replay).
- **No cross-process guarantee.** In a multi-replica deployment a client only
  sees events from the replica running its agent. V1 is best-effort / self-host
  local; swap this for Redis / Pub/Sub if shared streaming is needed.

This module performs only small in-process work and holds no I/O resources.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, Set, Tuple

from app.core.telemetry_context import TelemetryContext
from app.services.agent_stream_events import AgentStreamEvent, message_to_ui_events

logger = logging.getLogger(__name__)

# Per-subscriber queue bound. Generous enough for bursty agent output yet small
# enough to bound memory per connected client.
DEFAULT_QUEUE_MAXSIZE = 500

_SubKey = Tuple[str, str]


class AgentStreamBroker:
    """In-memory fan-out of agent stream events keyed by (generation, workspace)."""

    def __init__(self, queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        self._queue_maxsize = queue_maxsize
        self._subscribers: Dict[_SubKey, Set["asyncio.Queue[AgentStreamEvent]"]] = {}
        self._dropped = 0

    def subscribe(self, generation_id: str, workspace_id: str) -> "asyncio.Queue[AgentStreamEvent]":
        """Register a new subscriber and return its bounded queue."""
        queue: "asyncio.Queue[AgentStreamEvent]" = asyncio.Queue(maxsize=self._queue_maxsize)
        key = (generation_id, workspace_id)
        self._subscribers.setdefault(key, set()).add(queue)
        logger.debug("agent_stream_broker: subscribed to %s (subs=%d)", key, len(self._subscribers[key]))
        return queue

    def unsubscribe(
        self,
        generation_id: str,
        workspace_id: str,
        queue: "asyncio.Queue[AgentStreamEvent]",
    ) -> None:
        """Remove a subscriber queue; clean up the key when it has no subscribers."""
        key = (generation_id, workspace_id)
        subs = self._subscribers.get(key)
        if not subs:
            return
        subs.discard(queue)
        if not subs:
            self._subscribers.pop(key, None)
        logger.debug("agent_stream_broker: unsubscribed from %s", key)

    def has_subscribers(self, generation_id: str, workspace_id: str) -> bool:
        """True when at least one client is listening for this key."""
        return bool(self._subscribers.get((generation_id, workspace_id)))

    def publish(self, generation_id: str, workspace_id: str, event: AgentStreamEvent) -> None:
        """Fan an event out to all subscribers; drop (never block) on full queues."""
        subs = self._subscribers.get((generation_id, workspace_id))
        if not subs:
            return
        for queue in subs:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the newest event under pressure — UI events are best-effort.
                self._dropped += 1
                logger.debug(
                    "agent_stream_broker: queue full for (%s, %s); dropped event (total=%d)",
                    generation_id,
                    workspace_id,
                    self._dropped,
                )


_broker: Optional[AgentStreamBroker] = None


def get_agent_stream_broker() -> AgentStreamBroker:
    """Return the process-wide broker singleton (lazily created)."""
    global _broker
    if _broker is None:
        _broker = AgentStreamBroker()
    return _broker


class StreamPublisher:
    """Best-effort tap that converts SDK messages and publishes them to the broker.

    Created once per ``agent_query`` and called inline from the Claude
    consumption loop. ``publish_nowait`` does only small in-process conversion +
    queue fan-out and **never awaits or raises**, so it cannot slow or break
    generation regardless of subscriber state.
    """

    def __init__(
        self,
        broker: AgentStreamBroker,
        generation_id: str,
        workspace_id: str,
        workflow: Optional[str] = None,
    ) -> None:
        self._broker = broker
        self._generation_id = generation_id
        self._workspace_id = workspace_id
        self._workflow = workflow

    def publish_nowait(self, sdk_message: object) -> None:
        try:
            # Hot path: this runs for every SDK message of every generation. Skip
            # all conversion work (and AgentStreamEvent allocation) when no client
            # is listening — the common case when no TUI drill-in is open.
            if not self._broker.has_subscribers(self._generation_id, self._workspace_id):
                return
            events = message_to_ui_events(
                sdk_message,
                generation_id=self._generation_id,
                workspace_id=self._workspace_id,
                workflow=self._workflow,
            )
            for event in events:
                self._broker.publish(self._generation_id, self._workspace_id, event)
        except Exception:
            # Contract: the publisher path must swallow its own errors so a bad
            # event can never escape into the agent stream loop.
            logger.debug("StreamPublisher: publish_nowait failed", exc_info=True)


def build_stream_publisher_from_context() -> Optional[StreamPublisher]:
    """Build a publisher from the current ``TelemetryContext``, or ``None``.

    Returns ``None`` when generation/workspace identity is unknown (e.g. one-off
    or test calls) so callers can skip the tap entirely. Reading identity from
    ``TelemetryContext`` avoids threading ``generation_id`` / ``workspace_id``
    through every agent call site; the context already carries these for
    telemetry. Never raises.
    """
    try:
        generation_id = TelemetryContext.get_generation_id()
        workspace_id = TelemetryContext.get_workspace_name()
        if not generation_id or not workspace_id:
            return None
        workflow_label = TelemetryContext.get_workflow()
        workflow = workflow_label.to_stored_string() if workflow_label else None
        return StreamPublisher(
            get_agent_stream_broker(),
            generation_id=generation_id,
            workspace_id=workspace_id,
            workflow=workflow,
        )
    except Exception:
        logger.debug("build_stream_publisher_from_context failed", exc_info=True)
        return None
