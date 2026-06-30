"""Tests for the MCP-side model validation service."""
import json
from unittest.mock import AsyncMock, patch

import pytest

from services.validate_models import (
    blocking_rejection,
    invalid_models,
    request_model_validation,
)


def _response(*, all_valid=True, has_blocking=False, catalog_available=True, tiers=None):
    return {
        "provider": "openrouter",
        "catalog_available": catalog_available,
        "all_valid": all_valid,
        "has_blocking_tier": has_blocking,
        "tiers": tiers or [],
    }


def _tier(name, models):
    return {"tier": name, "models": models, "blocking": any(m["status"] == "invalid" for m in models)}


@pytest.mark.asyncio
async def test_request_model_validation_injects_env_and_posts(monkeypatch):
    monkeypatch.setenv("LLM_HIGH", "anthropic/claude-opus-4.6")
    monkeypatch.delenv("LLM_MEDIUM", raising=False)
    monkeypatch.delenv("LLM_LOW", raising=False)

    captured = {}

    async def fake_post(endpoint, form_data, timeout_seconds=30.0):
        captured["endpoint"] = endpoint
        captured["form_data"] = dict(form_data)
        return json.dumps(_response())

    with patch("services.validate_models.post_form_data_to_backend", new=fake_post):
        result = await request_model_validation()

    assert captured["endpoint"] == "/api/v1/models/validate"
    assert captured["form_data"]["LLM_HIGH"] == "anthropic/claude-opus-4.6"
    assert "LLM_MEDIUM" not in captured["form_data"]  # unset env not forwarded
    assert result["all_valid"] is True


def test_invalid_models_flattens_with_tier():
    resp = _response(
        all_valid=False,
        has_blocking=True,
        tiers=[
            _tier("LLM_HIGH", [
                {"configured": "anthropic/claude-opus-4.7", "transformed": "anthropic/claude-opus-4.7",
                 "status": "invalid", "suggestion": "anthropic/claude-opus-4.6"},
            ]),
            _tier("LLM_LOW", [
                {"configured": "anthropic/claude-haiku-4.5", "transformed": "anthropic/claude-haiku-4.5",
                 "status": "valid", "suggestion": None},
            ]),
        ],
    )
    invalid = invalid_models(resp)
    assert len(invalid) == 1
    assert invalid[0]["tier"] == "LLM_HIGH"
    assert invalid[0]["suggestion"] == "anthropic/claude-opus-4.6"


def test_blocking_rejection_none_when_not_blocking():
    assert blocking_rejection(_response(all_valid=True, has_blocking=False)) is None


def test_blocking_rejection_builds_message_with_suggestion():
    resp = _response(
        all_valid=False,
        has_blocking=True,
        tiers=[
            _tier("LLM_HIGH", [
                {"configured": "anthropic/claude-opus-4.7", "transformed": "anthropic/claude-opus-4.7",
                 "status": "invalid", "suggestion": "anthropic/claude-opus-4.6"},
            ]),
        ],
    )
    rejection = blocking_rejection(resp)
    assert rejection["code"] == "MODEL_UNAVAILABLE"
    assert "anthropic/claude-opus-4.7" in rejection["error"]
    assert "anthropic/claude-opus-4.6" in rejection["error"]  # suggestion


def test_blocking_rejection_does_not_reference_removed_tool():
    """The rejection message must not point at the removed `validate_models` tool."""
    resp = _response(
        all_valid=False,
        has_blocking=True,
        tiers=[
            _tier("LLM_MEDIUM", [
                {"configured": "openai/nope", "transformed": "openai/nope",
                 "status": "invalid", "suggestion": "openai/gpt-5.4"},
            ]),
        ],
    )
    rejection = blocking_rejection(resp)
    assert "validate_models" not in rejection["error"]
    assert rejection["invalid_models"][0]["configured"] == "openai/nope"
