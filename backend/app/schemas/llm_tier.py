"""
LLM tier definitions for workflow model selection.

Single source of truth for:
- LLM tier enum (HIGH / MEDIUM / LOW)
- Which workflow stage maps to which tier
- How to resolve the model for a given tier

One name used everywhere — no mapping, no transformation:
  tier.value  ==  Settings field  ==  form param  ==  env var
  e.g. LLMTier.HIGH.value == "LLM_HIGH" == settings.LLM_HIGH == os.environ["LLM_HIGH"]
"""

from enum import Enum
import logging
from typing import Dict, List, Optional, TYPE_CHECKING

from app.core.config import settings
from app.services.openrouter_models import validate_and_filter_models, _cache_manager

if TYPE_CHECKING:
    from app.core.config import Settings


class WorkflowName(str, Enum):
    """Canonical workflow names used for tier mapping and telemetry tagging.

    The string value is the name stored in TelemetryContext and used as the
    WORKFLOW_TIER_MAP key.  Add an entry here when a new workflow is introduced.
    """

    KB_INIT = "kb_init"
    PLANNING = "planning"
    MARKDOWN_TO_JSON_CONVERTER = "markdown_to_json_converter"
    GENERATE_APP = "generate_app"
    SPECIFICATION_COMPLETENESS_CHECK = "specification_completeness_check"
    SPECIFICATION_INDEXING = "specification_indexing"
    MCP_SERVERS_PRUNE = "mcp_servers_prune"
    ESTIMATION_P10Y_MULTI_WORKSPACE = "estimation_p10y_multi_workspace"
    ESTIMATION_AWUS = "estimation_awus"


class LLMTier(str, Enum):
    """LLM capability tier assigned per workflow stage.

    The string value is the canonical name used for Settings fields,
    form params, env vars, and override dict keys — all identical.
    """

    HIGH = "LLM_HIGH"
    MEDIUM = "LLM_MEDIUM"
    LOW = "LLM_LOW"


# Maps workflow → LLM tier.
# Used by both model selection and telemetry tagging so they stay in sync.
# Add an entry here when a new workflow stage needs a tier assignment.
WORKFLOW_TIER_MAP: Dict[WorkflowName, LLMTier] = {
    WorkflowName.KB_INIT: LLMTier.HIGH,  # KB init is a planning-stage step; use the planning-tier model
    WorkflowName.PLANNING: LLMTier.HIGH,
    WorkflowName.MARKDOWN_TO_JSON_CONVERTER: LLMTier.LOW,  # mechanical extraction, cheap tier
    WorkflowName.GENERATE_APP: LLMTier.MEDIUM,           # code generation workspaces
    WorkflowName.SPECIFICATION_COMPLETENESS_CHECK: LLMTier.MEDIUM,
    WorkflowName.SPECIFICATION_INDEXING: LLMTier.LOW,
    WorkflowName.MCP_SERVERS_PRUNE: LLMTier.MEDIUM,
    WorkflowName.ESTIMATION_P10Y_MULTI_WORKSPACE: LLMTier.MEDIUM,
}


def build_llm_overrides(
    LLM_HIGH: Optional[str] = None,
    LLM_MEDIUM: Optional[str] = None,
    LLM_LOW: Optional[str] = None,
) -> Dict[str, str]:
    """
    Build an llm_overrides dict from optional per-tier model strings.

    Accepts keyword arguments whose names match the tier keys (LLM_HIGH, etc.)
    so callers can pass FastAPI form params directly:

        overrides = build_llm_overrides(LLM_HIGH=LLM_HIGH, LLM_MEDIUM=LLM_MEDIUM, LLM_LOW=LLM_LOW)

    Returns all tiers with their corresponding models (non-empty strings).
    """
    overrides: Dict[str, str] = {
        LLMTier.HIGH.value: LLM_HIGH or settings.LLM_HIGH,
        LLMTier.MEDIUM.value: LLM_MEDIUM or settings.LLM_MEDIUM,
        LLMTier.LOW.value: LLM_LOW or settings.LLM_LOW,
    }
    return overrides


def _check_cached_model(model: str) -> None:
    """
    Opportunistically validate model against cache if available.
    
    This provides free validation for single-workspace workflows when
    the cache has been populated by a previous multi-workspace run.
    
    Args:
        model: Model name to check
        
    Logs warning if model not in cache, but does not raise or block.
    """
    if _cache_manager.is_cache_valid():
        cached = _cache_manager.get_cached_models()
        if cached:
            available_models, _ = cached
            if model not in available_models:
                logging.warning(
                    f"Model '{model}' not found in cached OpenRouter models. "
                    "It may be invalid or newly added. Proceeding anyway (will fail at runtime if invalid)."
                )


def resolve_model(
    tier: LLMTier,
    settings: "Settings",
    overrides: Optional[Dict[str, str]] = None,
    return_first: bool = True,
) -> str:
    """
    Return the model string for a given tier.

    Checks per-request overrides first (keyed by tier.value), then falls back
    to the Settings field of the same name.

    For single-workspace workflows, returns the first model from a comma-separated list.
    For multi-workspace workflows, set return_first=False to get the full list.

    Args:
        tier: Which capability tier to resolve.
        settings: Application settings (holds LLM_HIGH / LLM_MEDIUM / LLM_LOW).
        overrides: Optional dict mapping tier.value → model string (from API/MCP caller).
        return_first: If True, returns first model from comma-separated list (default).
                     If False, returns full comma-separated string.

    Returns:
        Model name(s) in OpenRouter format.
        - If return_first=True: "anthropic/claude-opus-4.5" (single model)
        - If return_first=False: "anthropic/claude-opus-4.5,google/gemini-3.1-pro-preview" (full list)
    """
    tier_value = overrides.get(tier.value) if overrides else None
    if not tier_value:
        tier_value = getattr(settings, tier.value)
    
    if return_first:
        try:
            model_list = parse_model_list(tier_value)
            first_model = model_list[0]
            
            # Opportunistic validation: if cache exists, validate instantly
            _check_cached_model(first_model)
            
            return first_model
        except (ValueError, IndexError) as e:
            # Fallback to raw value if parsing fails
            logging.warning(f"Failed to parse model list '{tier_value}': {e}. Using as-is.")
            return tier_value
    
    return tier_value


def parse_model_list(tier_value: str, require_slash: bool = True) -> List[str]:
    """
    Parse tier value into list of models.

    Examples:
        "model1,model2,model3" → ["model1", "model2", "model3"]
        "model1" → ["model1"]
        "model1, model2" → ["model1", "model2"]  # strip whitespace

    Args:
        tier_value: Model string or comma-separated list
        require_slash: When True (default, OpenRouter format), models without a
            '/' are skipped. Set False for providers whose catalog ids are bare
            (e.g. Anthropic's "claude-opus-4-6-...").

    Returns:
        List of model names (non-empty)

    Raises:
        ValueError: If no valid models found
    """
    if not tier_value or not tier_value.strip():
        raise ValueError("Empty model list")

    # Split by comma and strip whitespace
    models = [m.strip() for m in tier_value.split(",")]

    # Filter out empty strings and validate format
    valid_models = []
    for model in models:
        if not model:
            logging.warning("Empty model string found in list. Skipping.")
            continue
        if require_slash and "/" not in model:
            logging.warning(
                f"Model '{model}' does not follow OpenRouter format (must contain '/'). Skipping."
            )
            continue
        valid_models.append(model)

    if not valid_models:
        raise ValueError("No valid models found after filtering")

    return valid_models


def assign_model_to_workspace(
    workspace_index: int,
    model_list: List[str],
) -> str:
    """
    Assign model to workspace using round-robin strategy.

    Args:
        workspace_index: 0-based index (0, 1, 2 for 3 workspaces)
        model_list: Parsed models from LLM tier

    Returns:
        Model string for this workspace

    Raises:
        ValueError: If model_list is empty

    Examples:
        - 3 models, index 0 → model_list[0]
        - 3 models, index 2 → model_list[2]
        - 2 models, index 2 → model_list[0]  # wrap around
        - 1 model, index 2 → model_list[0]   # always first
    """
    if not model_list:
        raise ValueError("Cannot assign model: model_list is empty")
    return model_list[workspace_index % len(model_list)]


async def parse_and_validate_models(
    tier_value: str,
    settings: "Settings",
    logger: logging.Logger,
) -> List[str]:
    """
    Parse and validate model list with OpenRouter API.

    Args:
        tier_value: Model string or comma-separated list
        settings: Application settings (for API key)
        logger: Logger for warnings

    Returns:
        List of valid models (at least one)
    """

    # Parse comma-separated list
    try:
        models = parse_model_list(tier_value)
    except ValueError as e:
        logger.error(f"Failed to parse model list: {e}")
        raise

    # Validate against OpenRouter API (with graceful fallback)
    validated_models = await validate_and_filter_models(models, settings, logger)

    return validated_models
