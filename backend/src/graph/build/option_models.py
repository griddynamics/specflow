"""Build options and settings models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationInfo, field_validator

from graph.core.config import FrozenModel

Locality = Literal["grounded", "strict"]


class FragmentTokens(FrozenModel):
    """Fragment size range in estimated tokens; ``min <= target <= max``."""

    min: int = Field(default=300, ge=1)
    target: int = Field(default=2000, ge=1, validate_default=True)
    max: int = Field(default=3000, ge=1, validate_default=True)

    @field_validator("target", "max")
    @classmethod
    def _ordered(cls, value: int, info: ValidationInfo) -> int:
        lower = info.data.get("target" if info.field_name == "max" else "min")
        if lower is not None and value < lower:
            raise ValueError("fragment_tokens must satisfy min <= target <= max")
        return value


class BuildOptions(FrozenModel):
    """Per-project options that shape extraction; a change needs a rebuild."""

    fragment_tokens: FragmentTokens = Field(default_factory=FragmentTokens)
    hub_catalog: bool = True
    known_terms_limit: int = Field(default=30, ge=0)
    catalog_limit: int = Field(default=150, ge=0)
    locality: Locality = "grounded"
    unit_tokens: int = Field(default=24000, ge=1, validate_default=True)
    max_node_windows: int = Field(default=8, ge=1)

    @field_validator("unit_tokens")
    @classmethod
    def _unit_holds_a_fragment(cls, value: int, info: ValidationInfo) -> int:
        fragment_tokens = info.data.get("fragment_tokens")
        if fragment_tokens is not None and value < fragment_tokens.max:
            raise ValueError("unit_tokens must be at least fragment_tokens.max")
        return value


class BuildSettings(FrozenModel):
    """Per-build settings that do not shape extraction."""

    inference: bool = True
