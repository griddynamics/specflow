"""MCP-side LLM model validation.

Reads the LLM_HIGH / LLM_MEDIUM / LLM_LOW values from the MCP process environment
(set via mcp.json) and asks the backend to validate them against the active
provider's live catalog. The backend holds the API keys and knows DEFAULT_PROVIDER,
so validation runs there; this module only forwards the configured models and
interprets the result.

Used by:
  * the ``run_generation`` pre-flight gate (block before a 2–8h run), and
  * the connect-time best-effort notification.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastmcp import Context

from services.llm_tiers import LLM_TIER_KEYS, apply_llm_tier_overrides
from services.run_generation_precheck import RejectionCode
from services.specflow_backend import post_form_data_to_backend

logger = logging.getLogger(__name__)

VALIDATE_MODELS_ENDPOINT = "/api/v1/models/validate"


async def _post_validation(form_data: dict[str, str]) -> dict[str, Any]:
    """POST ``form_data`` to the validate-models endpoint and parse the response.

    The single call site for the endpoint so URL/timeout/parsing live in one place.
    """
    raw = await post_form_data_to_backend(
        VALIDATE_MODELS_ENDPOINT, form_data, timeout_seconds=30.0
    )
    return json.loads(raw)


async def request_model_validation() -> dict[str, Any]:
    """Call the backend validate-models endpoint with the configured LLM_* overrides.

    Reads LLM_HIGH/MEDIUM/LOW from the MCP *process env*. Returns the parsed
    ValidateModelsResponse dict. Raises on transport/parse errors so callers can
    stay permissive (connect/run gate) on infrastructure failures.
    """
    form_data: dict[str, str] = {}
    apply_llm_tier_overrides(form_data)  # injects LLM_HIGH/MEDIUM/LOW from env when set
    return await _post_validation(form_data)


async def request_model_validation_for(tier_values: dict[str, str]) -> dict[str, Any]:
    """Validate explicit LLM_* ``tier_values`` (e.g. from the local config block).

    Unlike ``request_model_validation`` (which reads the process env), this takes
    the values directly — the TUI edits ``mcp-config.json`` rather than its own
    environment, so it must forward what the file holds. Blank tiers are omitted
    so the backend falls back to its own default for that tier.
    """
    form_data = {
        key: value.strip()
        for key in LLM_TIER_KEYS
        if (value := (tier_values.get(key) or "").strip())
    }
    return await _post_validation(form_data)


def invalid_models(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the response into the list of confidently-invalid models (with tier)."""
    out: list[dict[str, Any]] = []
    for tier in response.get("tiers", []):
        for model in tier.get("models", []):
            if model.get("status") == "invalid":
                out.append({"tier": tier.get("tier"), **model})
    return out


def blocking_rejection(response: dict[str, Any]) -> dict[str, Any] | None:
    """Return a MODEL_UNAVAILABLE rejection dict if the response blocks the run, else None."""
    if not response.get("has_blocking_tier"):
        return None
    invalid = invalid_models(response)
    provider = response.get("provider", "the active provider")
    names = ", ".join(m.get("configured", "?") for m in invalid)
    suggestion = next((m.get("suggestion") for m in invalid if m.get("suggestion")), None)
    msg = f"The model(s) you configured aren't available on {provider}: {names}."
    if suggestion:
        msg += f" Did you mean '{suggestion}'?"
    msg += " Fix the LLM_HIGH/LLM_MEDIUM/LLM_LOW value in your MCP config and try again."
    return {
        "error": msg,
        "code": RejectionCode.MODEL_UNAVAILABLE.value,
        "invalid_models": invalid,
    }


async def validate_models_on_connect(ctx: Context) -> None:
    """Best-effort: warn the user at connect time if configured models are invalid.

    Fully swallowed — a missing backend, unset env, or unreachable catalog must never
    surface here. Only a confident INVALID result produces a warning; the invalid
    model is then re-reported (and the run blocked) when `run_generation` is called.
    """
    try:
        response = await request_model_validation()
        invalid = invalid_models(response)
        if not invalid:
            return
        provider = response.get("provider", "the active provider")
        names = ", ".join(m.get("configured", "?") for m in invalid)
        await ctx.warning(
            f"SpecFlow: configured model(s) not available on {provider}: {names}. "
            "Fix the LLM_HIGH/LLM_MEDIUM/LLM_LOW value in your MCP config before running generation."
        )
    except Exception as e:
        logger.debug("Model validation on connect skipped: %s", e)
