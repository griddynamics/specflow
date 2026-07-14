"""Tests for the shared retry helper (services/retry.py).

The helper POSTs the retry and returns the parsed backend dict, raising on
failure (the backend validates eligibility). Patch ``call_backend_endpoint`` at
``services.retry.*`` where the helper binds it.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from services.retry import retry_generation_core

GID = "est-abc123"


@pytest.mark.asyncio
async def test_returns_backend_dict_on_success():
    post = AsyncMock(return_value=json.dumps({"status": "pending", "retry_count": 1}))
    with patch("services.retry.call_backend_endpoint", new=post):
        data = await retry_generation_core(GID)
    assert data == {"status": "pending", "retry_count": 1}
    assert post.await_count == 1
    assert GID in post.await_args.kwargs["endpoint"]
    assert post.await_args.kwargs["method"] == "POST"


@pytest.mark.asyncio
async def test_raises_on_backend_failure():
    post = AsyncMock(side_effect=Exception("Backend returned HTTP 400: Cannot retry in 'running' state"))
    with patch("services.retry.call_backend_endpoint", new=post):
        with pytest.raises(Exception, match="running"):
            await retry_generation_core(GID)
