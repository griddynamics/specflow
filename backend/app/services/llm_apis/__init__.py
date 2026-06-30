"""Thin wrappers around vendor LLM HTTP APIs (one-shot repair calls, etc.)."""

from app.services.llm_apis.anthropic_api import anthropic_one_shot_text
from app.services.llm_apis.open_router_api import (
    OPENROUTER_DEFAULT_API_BASE,
    openrouter_first_model_from_llm_low_csv,
    openrouter_one_shot_text,
)

__all__ = [
    "OPENROUTER_DEFAULT_API_BASE",
    "anthropic_one_shot_text",
    "openrouter_first_model_from_llm_low_csv",
    "openrouter_one_shot_text",
]
