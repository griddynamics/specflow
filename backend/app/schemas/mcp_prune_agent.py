"""Pydantic model for MCP prune LLM JSON output."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class McpPruneAgentJson(BaseModel):
    """Schema: {\"enabled\": [\"figma\"], \"reasons\": {\"figma\": \"...\"}}."""

    model_config = ConfigDict(extra="ignore")

    enabled: list[str] = Field(default_factory=list)
    reasons: dict[str, str] = Field(default_factory=dict)

    @field_validator("enabled", mode="before")
    @classmethod
    def _normalize_enabled(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("enabled must be a list")
        out: list[str] = []
        for x in v:
            if isinstance(x, str) and x.strip():
                out.append(x.strip().lower())
        return out

    @field_validator("reasons", mode="before")
    @classmethod
    def _normalize_reasons(cls, v: Any) -> dict[str, str]:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("reasons must be an object")
        out: dict[str, str] = {}
        for k, val in v.items():
            if isinstance(k, str) and k.strip() and isinstance(val, str) and val.strip():
                out[k.strip().lower()] = val.strip()
        return out
