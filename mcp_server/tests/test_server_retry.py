"""Characterization + parity tests for the MCP ``retry_generation`` tool.

Locks the ``chat_json`` shapes for every outcome so the core refactor preserves
them. `retry_generation` is a plain async function; `ctx` is only consumed by
`apply_project_root_from_context`, which we stub. Backend primitives are patched
at both `server.*` (resolution/guard) and `services.retry.*` (the core's POST +
status read) so these tests hold before and after the refactor.
"""
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

import server

GID = "est-abc123"


async def _run(status_return, *, resolved=GID, post=None):
    """Invoke retry_generation with all IO stubbed; return (payload dict, post mock)."""
    post_mock = (
        AsyncMock(side_effect=post) if isinstance(post, Exception)
        else AsyncMock(return_value=post)
    )
    status_mock = AsyncMock(return_value=status_return)
    with ExitStack() as stack:
        stack.enter_context(patch("server.apply_project_root_from_context", new=AsyncMock()))
        stack.enter_context(patch("server.resolve_generation_id", return_value=resolved))
        stack.enter_context(patch("server.check_status_safe", new=status_mock))
        stack.enter_context(patch("services.retry.check_status_safe", new=status_mock))
        stack.enter_context(patch("services.retry.call_backend_endpoint", new=post_mock))
        stack.enter_context(patch("server.call_backend_endpoint", new=post_mock))
        payload = await server.retry_generation(generation_id=None, ctx=object())
    return json.loads(payload), post_mock


@pytest.mark.asyncio
async def test_no_session():
    with (
        patch("server.apply_project_root_from_context", new=AsyncMock()),
        patch("server.resolve_generation_id", return_value=None),
    ):
        payload = await server.retry_generation(generation_id=None, ctx=object())
    data = json.loads(payload)
    assert "No previous generation found" in data["message"]


@pytest.mark.asyncio
async def test_already_running():
    data, post = await _run({"status": "running"})
    assert "already running" in data["message"].lower()
    assert data["code"] == "GENERATION_ALREADY_RUNNING"
    assert data["generation_id"] == GID
    assert post.await_count == 0


@pytest.mark.asyncio
async def test_pending_rejected_before_codegen():
    data, post = await _run({"status": "pending", "error": "Missing required files"})
    assert "rejected before codegen" in data["message"].lower()
    assert data["details"]["error"] == "Missing required files"
    assert data["generation_id"] == GID
    assert post.await_count == 0


@pytest.mark.asyncio
async def test_pending_not_failed():
    data, post = await _run({"status": "pending"})
    assert "pending but has not failed" in data["message"].lower()
    assert data["details"]["status"] == "pending"
    assert data["generation_id"] == GID
    assert post.await_count == 0


@pytest.mark.asyncio
async def test_queued():
    data, post = await _run(
        {"status": "failed"}, post=json.dumps({"status": "pending", "retry_count": 1})
    )
    assert "retry queued" in data["message"].lower()
    assert data["status"] == "pending"
    assert data["retry_count"] == 1
    assert data["details"] == {"status": "pending", "retry_count": 1}
    assert data["generation_id"] == GID
    assert post.await_count == 1


@pytest.mark.asyncio
async def test_backend_error():
    data, post = await _run({"status": "failed"}, post=Exception("Backend returned HTTP 500"))
    assert "couldn't retry" in data["message"].lower()
    assert "500" in data["details"]["error"]
    assert post.await_count == 1
