"""Shared display formatting helpers (tokens, counts)."""
from typing import Optional, Tuple

from app.core.config import Settings
from app.schemas.llm_tier import (
    WORKFLOW_TIER_MAP,
    WorkflowName,
    build_llm_overrides,
    resolve_model,
)
from app.schemas.model_token_usage import ModelTokenUsage


def format_token_count(n: int) -> str:
    """
    Compact token count for UI/notifications.

    - < 1000: integer string (e.g. "842")
    - >= 1_000_000: millions with "m" (one decimal when < 10m, else integer m)
    - else: thousands with "k" (integer division)
    """
    if n < 0:
        n = 0
    if n < 1000:
        return str(int(n))
    if n >= 1_000_000:
        millions = n / 1_000_000
        if millions < 10:
            text = f"{millions:.1f}".rstrip("0").rstrip(".")
            return f"{text}m"
        return f"{int(round(millions))}m"
    k = n // 1000
    return f"{k}k"


def format_token_usage_lines(usage: ModelTokenUsage) -> list[str]:
    """Human-readable token breakdown (agent turns + per-bucket token counts)."""
    lines: list[str] = []
    if usage.num_turns:
        lines.append(f"Agent turns: {usage.num_turns}")
    lines.append("Tokens (cumulative):")
    lines.append(f"  input: {format_token_count(usage.input_tokens)}")
    lines.append(f"  output: {format_token_count(usage.output_tokens)}")
    lines.append(f"  cache write: {format_token_count(usage.cache_write_tokens)}")
    lines.append(f"  cache read: {format_token_count(usage.cache_read_tokens)}")
    lines.append(f"  total: {format_token_count(usage.total_tokens)}")
    return lines


def format_model_usage_notification_block(usage: ModelTokenUsage) -> str:
    """Multi-line token breakdown rendered as a single string for Slack/email."""
    return "\n".join(format_token_usage_lines(usage))


def overrides_from_generation_session_parameters(parameters: Optional[dict]) -> dict[str, str]:
    """Rebuild ``build_llm_overrides`` inputs from persisted generation parameters."""
    p = parameters or {}
    return build_llm_overrides(
        LLM_HIGH=p.get("LLM_HIGH"),
        LLM_MEDIUM=p.get("LLM_MEDIUM"),
        LLM_LOW=p.get("LLM_LOW"),
    )


def format_spec_analysis_agent_models(settings: Settings, overrides: dict[str, str]) -> str:
    """Resolved primary models for spec indexing vs completeness (for notifications)."""
    idx = resolve_model(
        WORKFLOW_TIER_MAP[WorkflowName.SPECIFICATION_INDEXING],
        settings,
        overrides,
    )
    comp = resolve_model(
        WORKFLOW_TIER_MAP[WorkflowName.SPECIFICATION_COMPLETENESS_CHECK],
        settings,
        overrides,
    )
    return f"indexer `{idx}`, completeness `{comp}`"


def format_planning_agent_model(settings: Settings, overrides: dict[str, str]) -> str:
    """Resolved primary model for the planning agent."""
    m = resolve_model(WORKFLOW_TIER_MAP[WorkflowName.PLANNING], settings, overrides)
    return f"`{m}`"


def est_notification_context(
    est_doc: Optional[dict],
) -> Tuple[Optional[str], str, ModelTokenUsage]:
    """Extract (recipient, models_line, usage) from an generation doc.

    Used for milestone notifications (spec-check, planning, etc.).
    Reads cumulative usage from nested ``workflow_usage_metrics`` when present,
    otherwise from the top-level ``model_usage`` field on the generation document.
    ``models_line`` lists persisted LLM_HIGH/MEDIUM/LOW when present, else ``default``.
    """
    doc = est_doc or {}
    params = doc.get("parameters") or {}
    recipient = params.get("notification_email") or doc.get("user_email")
    models_bits = [str(params[k]) for k in ("LLM_HIGH", "LLM_MEDIUM", "LLM_LOW") if params.get(k)]
    models_line = ", ".join(models_bits) if models_bits else "default"
    usage = ModelTokenUsage.from_generation_session_doc(doc)
    return recipient, models_line, usage
