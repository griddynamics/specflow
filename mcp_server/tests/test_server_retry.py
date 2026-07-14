"""Tests for the MCP ``retry_generation`` tool after deferring to the backend.

The tool resolves the id, POSTs via the shared helper, and renders success vs
the backend's error message. `ctx` is only consumed by
`apply_project_root_from_context`, which we stub. The POST is patched at
`services.retry.*` where the helper binds it.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

import server

GID = "est-abc123"


async def _run(*, resolved=GID, post=None):
    """Invoke retry_generation with IO stubbed; return (payload dict, post mock)."""
    post_mock = (
        AsyncMock(side_effect=post) if isinstance(post, Exception)
        else AsyncMock(return_value=post)
    )
    with (
        patch("server.apply_project_root_from_context", new=AsyncMock()),
        patch("server.resolve_generation_id", return_value=resolved),
        patch("services.retry.call_backend_endpoint", new=post_mock),
    ):
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
async def test_queued():
    data, post = await _run(post=json.dumps({"status": "pending", "retry_count": 1}))
    assert "retry queued" in data["message"].lower()
    assert data["status"] == "pending"
    assert data["retry_count"] == 1
    assert data["details"] == {"status": "pending", "retry_count": 1}
    assert data["generation_id"] == GID
    assert post.await_count == 1


@pytest.mark.asyncio
async def test_backend_rejection_or_error_is_surfaced():
    # Covers wrong-state rejections (HTTP 400) and unreachable backend alike —
    # both raise from the helper and render the backend's reason.
    data, post = await _run(
        post=Exception("Backend returned HTTP 400: Cannot retry in 'running' state")
    )
    assert "couldn't retry" in data["message"].lower()
    assert "running" in data["details"]["error"]
    assert data["generation_id"] == GID
