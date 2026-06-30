"""LLM model validation endpoint.

``POST /api/v1/models/validate`` checks that the configured (or overridden)
LLM_HIGH / LLM_MEDIUM / LLM_LOW models are available on the active provider
(OpenRouter or Anthropic, per ``DEFAULT_PROVIDER``).

Always returns 200 — the validation outcome is data, not an HTTP error. Auth is
enforced globally by the auth middleware (this path is not in PUBLIC_PATHS).
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form

from app.api.dependencies import require_authenticated_user
from app.core.config import settings
from app.schemas.llm_tier import build_llm_overrides
from app.schemas.model_validation import ValidateModelsResponse
from app.services.model_validation import validate_models_config

logger = logging.getLogger(__name__)

# Requires an authenticated user (auth middleware populates request.state); no
# ownership/admin check needed — validation is not user-scoped data.
router = APIRouter(
    prefix="/models",
    tags=["models"],
    dependencies=[Depends(require_authenticated_user)],
)


@router.post("/validate", response_model=ValidateModelsResponse)
async def validate_models(
    LLM_HIGH: Optional[str] = Form(None),
    LLM_MEDIUM: Optional[str] = Form(None),
    LLM_LOW: Optional[str] = Form(None),
) -> ValidateModelsResponse:
    """Validate configured LLM tier models against the active provider catalog.

    Optional form params override the configured defaults for this check (the MCP
    server forwards its ``LLM_*`` env vars here). Omitted tiers fall back to the
    backend's settings.
    """
    overrides = build_llm_overrides(
        LLM_HIGH=LLM_HIGH, LLM_MEDIUM=LLM_MEDIUM, LLM_LOW=LLM_LOW
    )
    return await validate_models_config(overrides, settings, logger)
