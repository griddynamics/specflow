"""Schemas for LLM model validation results.

Data models shared by the validation service
(``app.services.model_validation.validate_models_config``) and the
``/api/v1/models/validate`` endpoint.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Dict, List, Optional

from pydantic import BaseModel, computed_field

from app.core.enums import LLMProvider
from app.schemas.llm_tier import LLMTier


class TierValidationStatus(StrEnum):
    """Outcome for a single configured model."""

    VALID = "valid"  # transformed model present in the provider catalog
    INVALID = "invalid"  # catalog fetched and model absent (real typo / removal)
    UNVERIFIED = "unverified"  # catalog could not be fetched — never blocks


class ModelValidationResult(BaseModel):
    """Validation outcome for one configured model within a tier."""

    configured: str  # raw value as configured (e.g. "anthropic/claude-opus-4.6")
    transformed: str  # what the SDK receives after transform_model_name
    status: TierValidationStatus
    suggestion: Optional[str] = None


class TierValidation(BaseModel):
    """Validation outcome for one LLM tier (HIGH / MEDIUM / LOW)."""

    tier: LLMTier
    models: List[ModelValidationResult]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_valid_model(self) -> bool:
        """True if at least one model is usable (VALID or UNVERIFIED)."""
        return any(m.status != TierValidationStatus.INVALID for m in self.models)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blocking(self) -> bool:
        """True when any configured model is confidently INVALID (block-on-any-invalid)."""
        return any(m.status == TierValidationStatus.INVALID for m in self.models)


class ValidateModelsResponse(BaseModel):
    """Response body for ``POST /api/v1/models/validate``."""

    provider: LLMProvider
    catalog_available: bool
    all_valid: bool
    has_blocking_tier: bool
    tiers: List[TierValidation]

    @classmethod
    def from_results(
        cls,
        provider: LLMProvider,
        results: Dict[LLMTier, TierValidation],
        catalog_available: bool,
    ) -> "ValidateModelsResponse":
        tiers = list(results.values())
        all_valid = all(
            m.status == TierValidationStatus.VALID
            for tv in tiers
            for m in tv.models
        )
        has_blocking_tier = any(tv.blocking for tv in tiers)
        return cls(
            provider=provider,
            catalog_available=catalog_available,
            all_valid=all_valid,
            has_blocking_tier=has_blocking_tier,
            tiers=tiers,
        )
