"""Unit tests for provider-aware LLM tier validation.

Validation must compare ``provider.transform_model_name(configured)`` against the
live catalog, treat an empty catalog as UNVERIFIED (never blocking), and mark a
tier blocking only when at least one configured model is confidently INVALID
(the chosen block-on-any-invalid policy).
"""
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.schemas.llm_tier import LLMTier
from app.schemas.model_validation import TierValidationStatus
from app.services.model_validation import validate_models_config


@pytest.fixture
def logger():
    return MagicMock(spec=logging.Logger)


def _settings(env: dict) -> Settings:
    """Build Settings with only ``env`` present, isolated from ambient env/.env.

    DEFAULT_PROVIDER is derived from which key is set, so the intended provider
    is selected by the key alone — passing a ``DEFAULT_PROVIDER`` kwarg is
    silently dropped (``extra="ignore"``). ``clear=True`` also strips an ambient
    OPENROUTER_API_KEY that would otherwise flip an Anthropic-intended test.
    """
    with patch.dict(os.environ, env, clear=True):
        return Settings(_env_file=None)


def _patch_catalog(models: set):
    return patch(
        "app.services.model_validation.fetch_catalog_for_provider",
        new=AsyncMock(return_value=models),
    )


async def _validate(overrides, settings, logger):
    """Call the service and return a {tier: TierValidation} map for assertions."""
    response = await validate_models_config(overrides, settings, logger)
    return {tv.tier: tv for tv in response.tiers}


@pytest.mark.asyncio
async def test_openrouter_all_valid(logger):
    settings = _settings({"OPENROUTER_API_KEY": "k"})
    overrides = {
        "LLM_HIGH": "anthropic/claude-opus-4.6",
        "LLM_MEDIUM": "anthropic/claude-sonnet-4.6",
        "LLM_LOW": "anthropic/claude-haiku-4.5",
    }
    catalog = {
        "anthropic/claude-opus-4.6",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-haiku-4.5",
    }
    with _patch_catalog(catalog):
        result = await _validate(overrides, settings, logger)

    for tier in LLMTier:
        tv = result[tier]
        assert tv.has_valid_model is True
        assert tv.blocking is False
        assert all(m.status == TierValidationStatus.VALID for m in tv.models)


@pytest.mark.asyncio
async def test_openrouter_transform_applied_for_short_name(logger):
    """A short internal name maps to OpenRouter format before catalog compare."""
    settings = _settings({"OPENROUTER_API_KEY": "k"})
    overrides = {
        "LLM_HIGH": "claude-opus-4-6",  # no slash; mapped via MODEL_MAPPING
        "LLM_MEDIUM": "anthropic/claude-sonnet-4.6",
        "LLM_LOW": "anthropic/claude-haiku-4.5",
    }
    catalog = {
        "anthropic/claude-opus-4.6",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-haiku-4.5",
    }
    with _patch_catalog(catalog):
        result = await _validate(overrides, settings, logger)

    high = result[LLMTier.HIGH]
    assert high.models[0].configured == "claude-opus-4-6"
    assert high.models[0].transformed == "anthropic/claude-opus-4.6"
    assert high.models[0].status == TierValidationStatus.VALID


@pytest.mark.asyncio
async def test_invalid_model_with_suggestion_blocks(logger):
    settings = _settings({"OPENROUTER_API_KEY": "k"})
    overrides = {
        "LLM_HIGH": "anthropic/claude-opus-4.7",  # typo / not in catalog
        "LLM_MEDIUM": "anthropic/claude-sonnet-4.6",
        "LLM_LOW": "anthropic/claude-haiku-4.5",
    }
    catalog = {
        "anthropic/claude-opus-4.6",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-haiku-4.5",
    }
    with _patch_catalog(catalog):
        result = await _validate(overrides, settings, logger)

    high = result[LLMTier.HIGH]
    assert high.blocking is True
    assert high.has_valid_model is False
    bad = high.models[0]
    assert bad.status == TierValidationStatus.INVALID
    assert bad.suggestion == "anthropic/claude-opus-4.6"


@pytest.mark.asyncio
async def test_mixed_tier_blocks_on_any_invalid(logger):
    """Per the block-on-any-invalid policy, one bad model blocks the tier."""
    settings = _settings({"OPENROUTER_API_KEY": "k"})
    overrides = {
        "LLM_HIGH": "anthropic/claude-opus-4.6",
        "LLM_MEDIUM": "anthropic/claude-sonnet-4.6,openai/does-not-exist",
        "LLM_LOW": "anthropic/claude-haiku-4.5",
    }
    catalog = {
        "anthropic/claude-opus-4.6",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-haiku-4.5",
    }
    with _patch_catalog(catalog):
        result = await _validate(overrides, settings, logger)

    medium = result[LLMTier.MEDIUM]
    assert medium.blocking is True  # one invalid => blocking
    statuses = {m.configured: m.status for m in medium.models}
    assert statuses["anthropic/claude-sonnet-4.6"] == TierValidationStatus.VALID
    assert statuses["openai/does-not-exist"] == TierValidationStatus.INVALID


@pytest.mark.asyncio
async def test_empty_catalog_is_unverified_not_blocking(logger):
    settings = _settings({"OPENROUTER_API_KEY": "k"})
    overrides = {
        "LLM_HIGH": "anthropic/whatever",
        "LLM_MEDIUM": "x/y",
        "LLM_LOW": "z/w",
    }
    with _patch_catalog(set()):
        result = await _validate(overrides, settings, logger)

    for tier in LLMTier:
        tv = result[tier]
        assert tv.blocking is False
        assert tv.has_valid_model is True  # unverified counts as "usable"
        assert all(m.status == TierValidationStatus.UNVERIFIED for m in tv.models)


@pytest.mark.asyncio
async def test_anthropic_bare_ids_validate_without_slash(logger):
    """Anthropic catalog ids are bare; the '/' requirement must not drop them."""
    settings = _settings({"ANTHROPIC_API_KEY": "k"})
    overrides = {
        "LLM_HIGH": "claude-opus-4-6-20260101",
        "LLM_MEDIUM": "claude-sonnet-4-6-20260101",
        "LLM_LOW": "claude-haiku-4-5-20260101",
    }
    catalog = {
        "claude-opus-4-6-20260101",
        "claude-sonnet-4-6-20260101",
        "claude-haiku-4-5-20260101",
    }
    with _patch_catalog(catalog):
        result = await _validate(overrides, settings, logger)

    high = result[LLMTier.HIGH]
    assert high.models[0].transformed == "claude-opus-4-6-20260101"
    assert high.models[0].status == TierValidationStatus.VALID
    assert high.blocking is False
