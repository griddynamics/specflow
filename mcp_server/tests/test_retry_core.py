"""Tests for the shared retry core (services/retry.py).

One case per outcome variant. Patches are applied at ``services.retry.*`` because
the core binds ``check_status_safe`` / ``call_backend_endpoint`` at import time.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from services.retry import (
    AlreadyRunning,
    BackendError,
    PendingNotFailed,
    PendingRejectedBeforeCodegen,
    Queued,
    retry_generation_core,
)

GID = "est-abc123"


def _patches(status_return, post=None):
    status = patch("services.retry.check_status_safe", new=AsyncMock(return_value=status_return))
    post_mock = AsyncMock(return_value=post) if not isinstance(post, Exception) else AsyncMock(side_effect=post)
    endpoint = patch("services.retry.call_backend_endpoint", new=post_mock)
    return status, endpoint, post_mock


@pytest.mark.asyncio
async def test_already_running():
    status, endpoint, post = _patches({"status": "running"})
    with status as st, endpoint:
        outcome = await retry_generation_core(GID)
    assert isinstance(outcome, AlreadyRunning)
    assert st.await_count == 1
    assert post.await_count == 0  # no POST when already running


@pytest.mark.asyncio
async def test_pending_rejected_before_codegen():
    status, endpoint, post = _patches({"status": "pending", "error": "Missing required files"})
    with status, endpoint:
        outcome = await retry_generation_core(GID)
    assert isinstance(outcome, PendingRejectedBeforeCodegen)
    assert outcome.error == "Missing required files"
    assert outcome.status_data["status"] == "pending"
    assert post.await_count == 0


@pytest.mark.asyncio
async def test_pending_not_failed():
    status, endpoint, post = _patches({"status": "pending"})
    with status, endpoint:
        outcome = await retry_generation_core(GID)
    assert isinstance(outcome, PendingNotFailed)
    assert outcome.status_data["status"] == "pending"
    assert post.await_count == 0


@pytest.mark.asyncio
async def test_queued():
    status, endpoint, post = _patches(
        {"status": "failed"}, post=json.dumps({"status": "pending", "retry_count": 1})
    )
    with status as st, endpoint:
        outcome = await retry_generation_core(GID)
    assert isinstance(outcome, Queued)
    assert outcome.backend_data == {"status": "pending", "retry_count": 1}
    assert st.await_count == 1
    assert post.await_count == 1  # exactly one POST


@pytest.mark.asyncio
async def test_backend_error():
    status, endpoint, post = _patches({"status": "failed"}, post=Exception("Backend returned HTTP 500"))
    with status, endpoint:
        outcome = await retry_generation_core(GID)
    assert isinstance(outcome, BackendError)
    assert "500" in outcome.error
    assert post.await_count == 1
