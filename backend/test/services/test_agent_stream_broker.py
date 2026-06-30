"""Unit tests for the in-memory agent stream broker (services/agent_stream_broker.py).

Scenario coverage:
- Subscribers receive events published for their (generation, workspace) key.
- Events for one workspace never leak to a different workspace's subscriber.
- A full subscriber queue drops events instead of blocking the publisher.
- Unsubscribe removes the queue and cleans up empty keys.
- StreamPublisher.publish_nowait converts + fans out and swallows its own errors.
- build_stream_publisher_from_context returns None without generation/workspace identity.
"""

import asyncio

import pytest

from app.core.telemetry_context import TelemetryContext
from app.services.agent_stream_broker import (
    AgentStreamBroker,
    StreamPublisher,
    build_stream_publisher_from_context,
)
from app.services.agent_stream_events import AgentStreamEvent

_GID = "est-1"
_WS = "ws-01-1"


def _event(message: str = "hi", workspace_id: str = _WS) -> AgentStreamEvent:
    return AgentStreamEvent(
        timestamp="2026-06-26T00:00:00+00:00",
        generation_id=_GID,
        workspace_id=workspace_id,
        kind="assistant_text",
        message=message,
    )


class TestBrokerDelivery:
    @pytest.mark.asyncio
    async def test_subscriber_receives_published_event(self):
        broker = AgentStreamBroker()
        queue = broker.subscribe(_GID, _WS)
        broker.publish(_GID, _WS, _event("hello"))
        received = await asyncio.wait_for(queue.get(), timeout=1)
        assert received.message == "hello"

    @pytest.mark.asyncio
    async def test_workspaces_are_isolated(self):
        broker = AgentStreamBroker()
        q1 = broker.subscribe(_GID, "ws-01-1")
        q2 = broker.subscribe(_GID, "ws-01-2")
        broker.publish(_GID, "ws-01-1", _event("for-ws1", workspace_id="ws-01-1"))
        assert (await asyncio.wait_for(q1.get(), timeout=1)).message == "for-ws1"
        assert q2.empty()

    def test_publish_with_no_subscribers_is_noop(self):
        broker = AgentStreamBroker()
        broker.publish(_GID, _WS, _event())  # must not raise

    def test_full_queue_drops_without_blocking(self):
        broker = AgentStreamBroker(queue_maxsize=2)
        broker.subscribe(_GID, _WS)
        # Publish more than the bound — must not raise or block.
        for i in range(10):
            broker.publish(_GID, _WS, _event(f"m{i}"))
        assert broker.has_subscribers(_GID, _WS)


class TestSubscribeUnsubscribe:
    def test_unsubscribe_cleans_up_key(self):
        broker = AgentStreamBroker()
        queue = broker.subscribe(_GID, _WS)
        assert broker.has_subscribers(_GID, _WS)
        broker.unsubscribe(_GID, _WS, queue)
        assert not broker.has_subscribers(_GID, _WS)

    def test_unsubscribe_unknown_queue_is_safe(self):
        broker = AgentStreamBroker()
        broker.unsubscribe(_GID, _WS, asyncio.Queue())  # must not raise


class TestStreamPublisher:
    @pytest.mark.asyncio
    async def test_publish_nowait_converts_and_fans_out(self):
        from claude_agent_sdk import AssistantMessage
        from claude_agent_sdk.types import TextBlock

        broker = AgentStreamBroker()
        queue = broker.subscribe(_GID, _WS)
        publisher = StreamPublisher(broker, _GID, _WS, workflow="phase:1:coding")
        publisher.publish_nowait(AssistantMessage(content=[TextBlock(text="hi there")], model="m"))
        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event.kind == "assistant_text"
        assert event.message == "hi there"

    def test_publish_nowait_skips_conversion_without_subscribers(self):
        # Hot-path optimization: with nobody listening, no SDK message is even
        # converted into an event (avoids per-message work for every generation).
        from unittest.mock import patch

        broker = AgentStreamBroker()
        publisher = StreamPublisher(broker, _GID, _WS)
        with patch(
            "app.services.agent_stream_broker.message_to_ui_events"
        ) as convert:
            publisher.publish_nowait(object())
            convert.assert_not_called()

    def test_publish_nowait_swallows_errors(self):
        class BadBroker(AgentStreamBroker):
            def publish(self, *a, **k):
                raise RuntimeError("boom")

        from claude_agent_sdk import AssistantMessage
        from claude_agent_sdk.types import TextBlock

        publisher = StreamPublisher(BadBroker(), _GID, _WS)
        # Subscribe so conversion produces an event that triggers publish().
        BadBroker.subscribe  # noqa: B018 - reference for clarity
        publisher._broker._subscribers[(_GID, _WS)] = {asyncio.Queue()}
        publisher.publish_nowait(AssistantMessage(content=[TextBlock(text="x")], model="m"))


class TestBuildFromContext:
    def test_returns_none_without_identity(self):
        TelemetryContext.clear_context()
        assert build_stream_publisher_from_context() is None

    def test_builds_publisher_from_context(self):
        try:
            TelemetryContext.set_user_context(generation_id=_GID, workspace_name=_WS)
            publisher = build_stream_publisher_from_context()
            assert isinstance(publisher, StreamPublisher)
            assert publisher._generation_id == _GID
            assert publisher._workspace_id == _WS
        finally:
            TelemetryContext.clear_context()
