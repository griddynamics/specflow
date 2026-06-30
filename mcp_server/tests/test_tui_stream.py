"""Tests for the TUI SSE stream client (tui/stream.py).

The client wraps the service-layer ``stream_backend_sse`` primitive; tests mock
that primitive to yield raw ``data:`` payload strings and assert parsing,
tolerance of malformed payloads, and the best-effort (never-raises) contract.
"""

import json

import pytest

from tui import stream


def _async_iter(items):
    async def _gen():
        for item in items:
            yield item

    return _gen()


def _event_json(**kw) -> str:
    base = {
        "timestamp": "2026-06-26T14:02:31+00:00",
        "generation_id": "est-1",
        "workspace_id": "ws-01-1",
        "kind": "assistant_text",
        "message": "hello",
    }
    base.update(kw)
    return json.dumps(base)


class TestEndpoint:
    def test_builds_expected_path(self):
        assert stream.workspace_stream_endpoint("est-1", "ws-01-1") == (
            "/api/v1/generation-sessions/est-1/workspaces/ws-01-1/messages/stream"
        )


class TestAgentStreamEvent:
    def test_from_payload_tolerates_missing_fields(self):
        ev = stream.AgentStreamEvent.from_payload({"message": "x"})
        assert ev.message == "x"
        assert ev.kind == "unknown"
        assert ev.tool_name is None


class TestWorkspaceMessageEvents:
    @pytest.mark.asyncio
    async def test_parses_data_payloads_into_events(self, monkeypatch):
        payloads = [_event_json(message="first"), _event_json(kind="tool_use", tool_name="Bash", message="ls")]

        def fake_stream(endpoint, connect_timeout_seconds=10.0):
            return _async_iter(payloads)

        monkeypatch.setattr(stream, "stream_backend_sse", fake_stream)

        events = [e async for e in stream.workspace_message_events("est-1", "ws-01-1")]
        assert [e.message for e in events] == ["first", "ls"]
        assert events[1].tool_name == "Bash"

    @pytest.mark.asyncio
    async def test_skips_malformed_and_empty_payloads(self, monkeypatch):
        payloads = ["", "not-json", _event_json(message="ok")]

        monkeypatch.setattr(
            stream, "stream_backend_sse", lambda *a, **k: _async_iter(payloads)
        )

        events = [e async for e in stream.workspace_message_events("est-1", "ws-01-1")]
        assert [e.message for e in events] == ["ok"]

    @pytest.mark.asyncio
    async def test_stream_error_ends_generator_without_raising(self, monkeypatch):
        async def boom(endpoint, connect_timeout_seconds=10.0):
            raise ConnectionError("backend down")
            yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(stream, "stream_backend_sse", boom)

        # Must not raise — best-effort contract; just yields nothing.
        events = [e async for e in stream.workspace_message_events("est-1", "ws-01-1")]
        assert events == []

    @pytest.mark.asyncio
    async def test_error_midstream_ends_cleanly(self, monkeypatch):
        async def partial(endpoint, connect_timeout_seconds=10.0):
            yield _event_json(message="one")
            raise RuntimeError("dropped")

        monkeypatch.setattr(stream, "stream_backend_sse", partial)

        events = [e async for e in stream.workspace_message_events("est-1", "ws-01-1")]
        assert [e.message for e in events] == ["one"]
