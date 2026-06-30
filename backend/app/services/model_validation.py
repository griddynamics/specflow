"""Provider-aware validation of configured LLM tiers against the live catalog.

Single source of truth for "are the models configured for LLM_HIGH / LLM_MEDIUM /
LLM_LOW actually available on the active provider". Used by both the
``/api/v1/models/validate`` endpoint and the ``run_generation`` gate.

Key rule: a configured model is compared **after** ``provider.transform_model_name``,
because that transformed string is what the Claude Agent SDK actually receives — so
it is what must exist in the provider catalog. OpenRouter catalog ids are
``provider/model``; Anthropic ids are bare. The transform reconciles both.

Policy:
  * VALID      — transformed model is present in the catalog.
  * INVALID    — catalog was fetched and the model is absent (a real typo / removal).
  * UNVERIFIED — catalog could not be fetched (no key / outage); never blocks.
A tier is ``blocking`` when **any** of its models is INVALID (block-on-any-invalid).
"""
from __future__ import annotations

import logging
from typing import Dict, List

from app.core.config import Settings
from app.core.enums import LLMProvider
from app.schemas.llm_tier import LLMTier, parse_model_list
from app.schemas.model_validation import (
    ModelValidationResult,
    TierValidation,
    TierValidationStatus,
    ValidateModelsResponse,
)
from app.services.model_catalog import fetch_catalog_for_provider
from app.services.openrouter_models import suggest_similar_model
from app.services.providers import BaseProvider, ProviderFactory
from app.services.providers.credentials import (
    resolve_provider_api_key,
    resolve_provider_base_url,
)

__all__ = ["validate_models_config"]


def _build_provider(provider_enum: LLMProvider, settings: Settings) -> BaseProvider:
    """Build a provider instance for its ``transform_model_name`` (tolerates a missing key)."""
    name = provider_enum.value
    api_key = resolve_provider_api_key(name, settings) or ""
    base_url = resolve_provider_base_url(name, settings)
    return ProviderFactory.create_provider(
        provider_name=name, api_key=api_key, base_url=base_url
    )


async def validate_models_config(
    overrides: Dict[str, str],
    settings: Settings,
    logger: logging.Logger,
) -> ValidateModelsResponse:
    """Validate every LLM tier's configured models against the active provider catalog.

    Args:
        overrides: Mapping of tier key (``LLM_HIGH`` etc.) → configured model string,
            already merged with settings defaults (see ``build_llm_overrides``). This is
            the established llm-overrides shape produced by ``build_llm_overrides``.
        settings: Application settings (provides DEFAULT_PROVIDER + credentials).
        logger: Logger for warnings.

    Returns:
        A :class:`ValidateModelsResponse` with per-tier results, ``catalog_available``,
        ``all_valid`` and ``has_blocking_tier``. ``catalog_available`` is False when the
        provider catalog could not be fetched (every model is then UNVERIFIED).
    """
    provider_enum = settings.DEFAULT_PROVIDER
    provider = _build_provider(provider_enum, settings)
    catalog = await fetch_catalog_for_provider(provider_enum, settings)
    catalog_available = bool(catalog)

    results: Dict[LLMTier, TierValidation] = {}
    for tier in LLMTier:
        tier_value = overrides.get(tier.value) or getattr(settings, tier.value)
        try:
            # Parse leniently: transform_model_name maps short/internal names to
            # the provider format, so the slash check is applied AFTER transform
            # (a name that stays bare simply won't match the OpenRouter catalog).
            models = parse_model_list(tier_value, require_slash=False)
        except ValueError as e:
            logger.warning(f"Could not parse {tier.value} value '{tier_value}': {e}")
            models = []

        model_results: List[ModelValidationResult] = []
        for model in models:
            transformed = provider.transform_model_name(model)
            if not catalog_available:
                status = TierValidationStatus.UNVERIFIED
                suggestion = None
            elif transformed in catalog:
                status = TierValidationStatus.VALID
                suggestion = None
            else:
                status = TierValidationStatus.INVALID
                suggestion = suggest_similar_model(transformed, catalog)
                logger.warning(
                    f"{tier.value}: model '{model}' (-> '{transformed}') not available "
                    f"on {provider_enum.value}."
                    + (f" Did you mean '{suggestion}'?" if suggestion else "")
                )
            model_results.append(
                ModelValidationResult(
                    configured=model,
                    transformed=transformed,
                    status=status,
                    suggestion=suggestion,
                )
            )

        results[tier] = TierValidation(tier=tier, models=model_results)

    return ValidateModelsResponse.from_results(
        provider=provider_enum,
        results=results,
        catalog_available=catalog_available,
    )
