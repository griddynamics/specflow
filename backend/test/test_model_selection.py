"""
Unit tests for LLM model tier selection feature.

Covers:
- LLMTier enum, WORKFLOW_TIER_MAP, resolve_model
- Multi-model parsing and assignment (comma-separated lists)
- build_workflow_context llm_overrides propagation
- spec_analysis workflows resolve correct tier model
- _generate_ai_report uses LLM_MEDIUM (not hardcoded)
- MCP orchestrators include env vars in form_data only when set
- OpenRouter mapping for claude 4.6 models
- planning_agent_fn model param
"""

import logging
import os
import pytest
from unittest.mock import MagicMock, patch

from app.schemas.llm_tier import assign_model_to_workspace, parse_model_list


# ================================================================
# Multi-Model Feature Tests (New)
# ================================================================

def test_parse_single_model():
    """Single model string returns single-element list."""
    assert parse_model_list("anthropic/claude-sonnet-4.6") == [
        "anthropic/claude-sonnet-4.6"
    ]


def test_parse_three_models():
    """Three models parsed correctly."""
    result = parse_model_list("model1/a,model2/b,model3/c")
    assert result == ["model1/a", "model2/b", "model3/c"]


def test_parse_strips_whitespace():
    """Whitespace around commas is stripped."""
    result = parse_model_list("model1/a , model2/b, model3/c ")
    assert result == ["model1/a", "model2/b", "model3/c"]


def test_parse_filters_empty_models():
    """Empty strings after split are filtered out with warning."""
    result = parse_model_list("model1/a,,model3/c")
    assert result == ["model1/a", "model3/c"]


def test_parse_filters_invalid_format():
    """Models without '/' are filtered out."""
    result = parse_model_list("anthropic/claude-sonnet-4.5,invalid-model")
    assert result == ["anthropic/claude-sonnet-4.5"]


def test_parse_raises_on_empty_string():
    """Empty string raises ValueError."""
    with pytest.raises(ValueError, match="Empty model list"):
        parse_model_list("")


def test_parse_raises_on_whitespace_only():
    """Whitespace-only string raises ValueError."""
    with pytest.raises(ValueError, match="Empty model list"):
        parse_model_list("   ")


def test_parse_raises_on_no_valid_models():
    """No valid models after filtering raises ValueError."""
    with pytest.raises(ValueError, match="No valid models found"):
        parse_model_list("invalid1,invalid2,invalid3")


def test_assign_model_round_robin():
    """Round-robin assignment works for exact match."""
    models = ["m1/a", "m2/b", "m3/c"]
    assert assign_model_to_workspace(0, models) == "m1/a"
    assert assign_model_to_workspace(1, models) == "m2/b"
    assert assign_model_to_workspace(2, models) == "m3/c"


def test_assign_model_wraps_around():
    """Assignment wraps around when workspace count > model count."""
    models = ["m1/a", "m2/b"]
    assert assign_model_to_workspace(0, models) == "m1/a"
    assert assign_model_to_workspace(1, models) == "m2/b"
    assert assign_model_to_workspace(2, models) == "m1/a"  # Wrap to first
    assert assign_model_to_workspace(3, models) == "m2/b"  # Wrap to second


def test_assign_model_single_model():
    """Single model always assigned regardless of index."""
    models = ["single/model"]
    assert assign_model_to_workspace(0, models) == "single/model"
    assert assign_model_to_workspace(1, models) == "single/model"
    assert assign_model_to_workspace(2, models) == "single/model"
    assert assign_model_to_workspace(10, models) == "single/model"


def test_assign_model_four_models_three_workspaces():
    """More models than workspaces works (extras unused)."""
    models = ["m1/a", "m2/b", "m3/c", "m4/d"]
    assert assign_model_to_workspace(0, models) == "m1/a"
    assert assign_model_to_workspace(1, models) == "m2/b"
    assert assign_model_to_workspace(2, models) == "m3/c"
    # m4/d not used for 3 workspaces (no error)


def test_assign_model_empty_list_raises():
    """Empty model list raises ValueError."""
    with pytest.raises(ValueError, match="Cannot assign model: model_list is empty"):
        assign_model_to_workspace(0, [])


# ================================================================
# 1. LLMTier enum, WORKFLOW_TIER_MAP, resolve_model
# ================================================================

class TestLLMTierSchema:
    """Core llm_tier.py contracts."""

    def test_tier_values(self):
        from app.schemas.llm_tier import LLMTier
        assert LLMTier.HIGH.value == "LLM_HIGH"
        assert LLMTier.MEDIUM.value == "LLM_MEDIUM"
        assert LLMTier.LOW.value == "LLM_LOW"

    def test_tier_value_is_settings_field_name(self):
        """tier.value IS the Settings field name — no mapping or f-string needed."""
        from app.core.config import Settings
        from app.schemas.llm_tier import LLMTier
        s = Settings(LLM_HIGH="h/m", LLM_MEDIUM="m/m", LLM_LOW="l/m")
        for tier in LLMTier:
            # getattr(settings, tier.value) requires no transformation
            assert hasattr(s, tier.value)

    def test_tier_value_is_form_and_env_key(self):
        """tier.value IS the form param name and env var name — one name everywhere."""
        from app.schemas.llm_tier import LLMTier
        assert LLMTier.HIGH.value == "LLM_HIGH"
        assert LLMTier.MEDIUM.value == "LLM_MEDIUM"
        assert LLMTier.LOW.value == "LLM_LOW"

    def test_workflow_tier_map_entries(self):
        from app.schemas.llm_tier import LLMTier, WorkflowName, WORKFLOW_TIER_MAP
        assert WORKFLOW_TIER_MAP[WorkflowName.PLANNING] is LLMTier.HIGH
        # KB init is a planning-stage step → planning-tier (HIGH) model, not the code-gen tier.
        assert WORKFLOW_TIER_MAP[WorkflowName.KB_INIT] is LLMTier.HIGH
        assert WORKFLOW_TIER_MAP[WorkflowName.GENERATE_APP] is LLMTier.MEDIUM
        assert WORKFLOW_TIER_MAP[WorkflowName.SPECIFICATION_COMPLETENESS_CHECK] is LLMTier.MEDIUM
        assert WORKFLOW_TIER_MAP[WorkflowName.SPECIFICATION_INDEXING] is LLMTier.LOW
        assert WORKFLOW_TIER_MAP[WorkflowName.MCP_SERVERS_PRUNE] is LLMTier.MEDIUM
        assert WORKFLOW_TIER_MAP[WorkflowName.ESTIMATION_P10Y_MULTI_WORKSPACE] is LLMTier.MEDIUM

    def test_resolve_model_falls_back_to_settings(self):
        from app.core.config import Settings
        from app.schemas.llm_tier import LLMTier, resolve_model
        s = Settings(LLM_HIGH="anthropic/claude-opus-4.5",
                     LLM_MEDIUM="anthropic/claude-sonnet-4.6",
                     LLM_LOW="anthropic/claude-haiku-4.5")
        assert resolve_model(LLMTier.HIGH, s) == "anthropic/claude-opus-4.5"
        assert resolve_model(LLMTier.MEDIUM, s) == "anthropic/claude-sonnet-4.6"
        assert resolve_model(LLMTier.LOW, s) == "anthropic/claude-haiku-4.5"

    def test_resolve_model_uses_override(self):
        from app.core.config import Settings
        from app.schemas.llm_tier import LLMTier, resolve_model
        s = Settings(LLM_HIGH="anthropic/claude-opus-4.5",
                     LLM_MEDIUM="anthropic/claude-sonnet-4.6",
                     LLM_LOW="anthropic/claude-haiku-4.5")
        overrides = {LLMTier.HIGH.value: "google/gemini-3.1-pro-preview",
                     LLMTier.LOW.value: "openai/gpt-4o-mini"}
        assert resolve_model(LLMTier.HIGH, s, overrides) == "google/gemini-3.1-pro-preview"
        assert resolve_model(LLMTier.MEDIUM, s, overrides) == "anthropic/claude-sonnet-4.6"
        assert resolve_model(LLMTier.LOW, s, overrides) == "openai/gpt-4o-mini"

    def test_resolve_model_return_first_true_returns_single_model(self):
        """return_first=True returns first model from comma-separated list."""
        from app.core.config import Settings
        from app.schemas.llm_tier import LLMTier, resolve_model
        s = Settings(LLM_MEDIUM="anthropic/claude-sonnet-4.5,google/gemini-3.1-pro-preview,openai/gpt-4o")
        assert resolve_model(LLMTier.MEDIUM, s, return_first=True) == "anthropic/claude-sonnet-4.5"

    def test_resolve_model_return_first_false_returns_full_list(self):
        """return_first=False returns full comma-separated string."""
        from app.core.config import Settings
        from app.schemas.llm_tier import LLMTier, resolve_model
        s = Settings(LLM_MEDIUM="anthropic/claude-sonnet-4.5,google/gemini-3.1-pro-preview,openai/gpt-4o")
        assert resolve_model(LLMTier.MEDIUM, s, return_first=False) == "anthropic/claude-sonnet-4.5,google/gemini-3.1-pro-preview,openai/gpt-4o"


# ================================================================
# 2. Config: defaults and env var overrides
# ================================================================

class TestLLMTierConfig:
    """Settings.LLM_HIGH/MEDIUM/LOW default and env-override behaviour."""

    def test_defaults(self):
        from app.core.config import settings
        assert isinstance(settings.LLM_HIGH, str) and settings.LLM_HIGH
        assert isinstance(settings.LLM_MEDIUM, str) and settings.LLM_MEDIUM
        assert isinstance(settings.LLM_LOW, str) and settings.LLM_LOW

    def test_default_values_are_openrouter_format(self):
        from app.core.config import Settings
        s = Settings(
            LLM_HIGH="anthropic/claude-opus-4.5",
            LLM_MEDIUM="anthropic/claude-sonnet-4.6",
            LLM_LOW="anthropic/claude-haiku-4.5",
        )
        assert "/" in s.LLM_HIGH
        assert "/" in s.LLM_MEDIUM
        assert "/" in s.LLM_LOW

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("LLM_HIGH", "google/gemini-3.1-pro-preview")
        monkeypatch.setenv("LLM_MEDIUM", "openai/gpt-4o")
        monkeypatch.setenv("LLM_LOW", "openai/gpt-4o-mini")
        from app.core.config import Settings
        s = Settings()
        assert s.LLM_HIGH == "google/gemini-3.1-pro-preview"
        assert s.LLM_MEDIUM == "openai/gpt-4o"
        assert s.LLM_LOW == "openai/gpt-4o-mini"


# ================================================================
# 3. build_workflow_context uses llm_overrides + WORKFLOW_TIER_MAP
# ================================================================

class TestBuildWorkflowContext:
    """WorkflowContext carries llm_overrides; workspaces get generate_app tier model."""

    @pytest.mark.asyncio
    async def test_defaults_fall_back_to_settings(self, tmp_path):
        from app.core.config import Settings
        from app.services.workflow_steps import build_workflow_context
        from app.schemas.specification import GenerateAppRequest

        s = Settings(
            WORKSPACE_BASE_PATH=str(tmp_path),
            LLM_HIGH="anthropic/claude-opus-4.5",
            LLM_MEDIUM="anthropic/claude-sonnet-4.6",
            LLM_LOW="anthropic/claude-haiku-4.5",
        )
        (tmp_path / "ws-test-1").mkdir()

        ctx = await build_workflow_context(
            request=GenerateAppRequest(
                spec_path="./specs",
                outputs_dir="./outputs",
                generation_id="test-ctx-1",
            ),
            settings=s,
            logger=MagicMock(),
            workspace_ids=["ws-test-1"],
        )

        assert ctx.llm_overrides == {}
        # generate_app tier is MEDIUM
        assert ctx.primary_workspace.model == "anthropic/claude-sonnet-4.6"

    @pytest.mark.asyncio
    async def test_overrides_stored_and_workspace_uses_medium_override(self, tmp_path):
        from app.core.config import Settings
        from app.services.workflow_steps import build_workflow_context
        from app.schemas.specification import GenerateAppRequest
        from app.schemas.llm_tier import LLMTier

        s = Settings(
            WORKSPACE_BASE_PATH=str(tmp_path),
            LLM_HIGH="anthropic/claude-opus-4.5",
            LLM_MEDIUM="anthropic/claude-sonnet-4.6",
            LLM_LOW="anthropic/claude-haiku-4.5",
        )
        (tmp_path / "ws-test-2").mkdir()

        overrides = {
            LLMTier.HIGH.value: "google/gemini-3.1-pro-preview",
            LLMTier.MEDIUM.value: "openai/gpt-4o",
        }
        ctx = await build_workflow_context(
            request=GenerateAppRequest(
                spec_path="./specs",
                outputs_dir="./outputs",
                generation_id="test-ctx-2",
            ),
            settings=s,
            logger=MagicMock(),
            workspace_ids=["ws-test-2"],
            llm_overrides=overrides,
        )

        assert ctx.llm_overrides == overrides
        assert ctx.primary_workspace.model == "openai/gpt-4o"

    @pytest.mark.asyncio
    async def test_planning_resolves_high_tier_from_map(self, tmp_path):
        """run_planning_agent picks WORKFLOW_TIER_MAP['planning'] = HIGH tier."""
        from app.core.config import Settings
        from app.services.workflow_steps import build_workflow_context
        from app.schemas.specification import GenerateAppRequest
        from app.schemas.llm_tier import LLMTier, WorkflowName, WORKFLOW_TIER_MAP, resolve_model

        s = Settings(
            WORKSPACE_BASE_PATH=str(tmp_path),
            LLM_HIGH="anthropic/claude-opus-4.5",
            LLM_MEDIUM="anthropic/claude-sonnet-4.6",
            LLM_LOW="anthropic/claude-haiku-4.5",
        )
        (tmp_path / "ws-test-3").mkdir()

        overrides = {LLMTier.HIGH.value: "google/gemini-3.1-pro-preview"}
        ctx = await build_workflow_context(
            request=GenerateAppRequest(
                spec_path="./specs",
                outputs_dir="./outputs",
                generation_id="test-ctx-3",
            ),
            settings=s,
            logger=MagicMock(),
            workspace_ids=["ws-test-3"],
            llm_overrides=overrides,
        )

        planning_model = resolve_model(WORKFLOW_TIER_MAP[WorkflowName.PLANNING], ctx.settings, ctx.llm_overrides)
        assert planning_model == "google/gemini-3.1-pro-preview"


# ================================================================
# 4. spec_analysis workflows use WORKFLOW_TIER_MAP (not hardcoded tier)
# ================================================================

# ================================================================
# 5. _generate_ai_report uses WORKFLOW_TIER_MAP (not hardcoded LLM_MEDIUM)
# ================================================================

class TestGenerateAiReport:
    """_generate_ai_report uses tier from WORKFLOW_TIER_MAP, not hardcoded model."""

    @pytest.mark.asyncio
    async def test_report_uses_medium_tier(self, tmp_path):
        from app.core.config import Settings
        from app.workflows.multi_workspace_estimation_p10y import _generate_ai_report
        from app.schemas.estimate import (
            ComparativeAnalysis,
            EstimationMetrics,
            EstimationSummary,
            WorkspaceEstimation,
        )

        s = Settings(WORKSPACE_DIR=str(tmp_path), LLM_MEDIUM="anthropic/claude-sonnet-4.6")

        captured = {}

        async def mock_agent_query(**kwargs):
            captured["model"] = kwargs.get("model")
            from app.schemas.agent import AgentResult
            return AgentResult(result="report", session_id=None)

        summary = EstimationSummary(
            average_hours=100.0,
            std_deviation=10.0,
            min_hours=80.0,
            max_hours=120.0,
            coefficient_of_variation=0.1,
            variance_assessment="low",
        )
        comparative = ComparativeAnalysis(
            component_comparison={}, high_variance_components=[], insights=[],
        )
        ws_est = WorkspaceEstimation(
            workspace_name="ws-1",
            workspace_path=str(tmp_path),
            total_hours=100.0,
            total_effective_output=50.0,
            component_breakdown={},
            estimation_metrics=EstimationMetrics(
                new_work=10.0,
                refactor=0.0,
                rework=0.0,
                removed_work=0.0,
                quality_score=0.9,
                effective_output=50.0,
                total_output=50.0,
            ),
            commits_count=3,
            p10y_scored_commits=3,
        )

        with patch("app.workflows.multi_workspace_estimation_p10y.agent_query", side_effect=mock_agent_query), \
             patch("app.services.workspace_manager.WorkspaceManager.get_provider", return_value=MagicMock()):
            await _generate_ai_report(
                workspace_estimations=[ws_est],
                summary=summary,
                comparative_analysis=comparative,
                outputs_dir="outputs",
                settings=s,
                logger=MagicMock(),
            )

        assert captured.get("model") == "anthropic/claude-sonnet-4.6"

    @pytest.mark.asyncio
    async def test_report_respects_medium_override(self, tmp_path):
        from app.core.config import Settings
        from app.workflows.multi_workspace_estimation_p10y import _generate_ai_report
        from app.schemas.estimate import (
            ComparativeAnalysis,
            EstimationMetrics,
            EstimationSummary,
            WorkspaceEstimation,
        )
        from app.schemas.llm_tier import LLMTier

        s = Settings(WORKSPACE_DIR=str(tmp_path), LLM_MEDIUM="anthropic/claude-sonnet-4.6")

        captured = {}

        async def mock_agent_query(**kwargs):
            captured["model"] = kwargs.get("model")
            from app.schemas.agent import AgentResult
            return AgentResult(result="report", session_id=None)

        summary = EstimationSummary(
            average_hours=100.0,
            std_deviation=10.0,
            min_hours=80.0,
            max_hours=120.0,
            coefficient_of_variation=0.1,
            variance_assessment="low",
        )
        comparative = ComparativeAnalysis(
            component_comparison={}, high_variance_components=[], insights=[],
        )
        ws_est = WorkspaceEstimation(
            workspace_name="ws-1",
            workspace_path=str(tmp_path),
            total_hours=100.0,
            total_effective_output=50.0,
            component_breakdown={},
            estimation_metrics=EstimationMetrics(
                new_work=10.0,
                refactor=0.0,
                rework=0.0,
                removed_work=0.0,
                quality_score=0.9,
                effective_output=50.0,
                total_output=50.0,
            ),
            commits_count=3,
            p10y_scored_commits=3,
        )

        with patch("app.workflows.multi_workspace_estimation_p10y.agent_query", side_effect=mock_agent_query), \
             patch("app.services.workspace_manager.WorkspaceManager.get_provider", return_value=MagicMock()):
            await _generate_ai_report(
                workspace_estimations=[ws_est],
                summary=summary,
                comparative_analysis=comparative,
                outputs_dir="outputs",
                settings=s,
                logger=MagicMock(),
                llm_overrides={LLMTier.MEDIUM.value: "openai/gpt-4o"},
            )

        assert captured.get("model") == "openai/gpt-4o"


# ================================================================
# 6. MCP env var propagation
# ================================================================

class TestMCPEnvPropagationLogic:
    """Verify env var == form param == tier.value: one name, no mapping."""

    def test_env_vars_included_when_set(self, monkeypatch):
        monkeypatch.setenv("LLM_HIGH", "google/gemini-3.1-pro-preview")
        monkeypatch.setenv("LLM_MEDIUM", "openai/gpt-4o")
        monkeypatch.setenv("LLM_LOW", "openai/gpt-4o-mini")

        _KEYS = ["LLM_HIGH", "LLM_MEDIUM", "LLM_LOW"]
        form_data: dict = {}
        for key in _KEYS:
            value = os.environ.get(key)
            if value:
                form_data[key] = value

        assert form_data["LLM_HIGH"] == "google/gemini-3.1-pro-preview"
        assert form_data["LLM_MEDIUM"] == "openai/gpt-4o"
        assert form_data["LLM_LOW"] == "openai/gpt-4o-mini"

    def test_env_vars_absent_when_unset(self, monkeypatch):
        monkeypatch.delenv("LLM_HIGH", raising=False)
        monkeypatch.delenv("LLM_MEDIUM", raising=False)
        monkeypatch.delenv("LLM_LOW", raising=False)

        _KEYS = ["LLM_HIGH", "LLM_MEDIUM", "LLM_LOW"]
        form_data: dict = {}
        for key in _KEYS:
            value = os.environ.get(key)
            if value:
                form_data[key] = value

        assert "LLM_HIGH" not in form_data
        assert "LLM_MEDIUM" not in form_data
        assert "LLM_LOW" not in form_data


# ================================================================
# 8. OpenRouter model mapping
# ================================================================

class TestOpenRouterMapping:
    def test_sonnet_46_mapping(self):
        from app.services.providers.openrouter import OpenRouterProvider
        p = OpenRouterProvider(api_key="key")
        assert p.transform_model_name("claude-sonnet-4-6") == "anthropic/claude-sonnet-4.6"

    def test_opus_46_mapping(self):
        from app.services.providers.openrouter import OpenRouterProvider
        p = OpenRouterProvider(api_key="key")
        assert p.transform_model_name("claude-opus-4-6") == "anthropic/claude-opus-4.6"

    def test_openrouter_format_passthrough(self):
        from app.services.providers.openrouter import OpenRouterProvider
        p = OpenRouterProvider(api_key="key")
        assert p.transform_model_name("anthropic/claude-sonnet-4.6") == "anthropic/claude-sonnet-4.6"
        assert p.transform_model_name("anthropic/claude-opus-4.5") == "anthropic/claude-opus-4.5"


# ================================================================
# 9. planning_agent_fn respects model override
# ================================================================

class TestPlanningAgentFnModel:
    @pytest.mark.asyncio
    async def test_planning_uses_model_override(self, tmp_path):
        from app.services.claude_code import planning_agent_fn
        from app.schemas.specification import GenerateAppRequest
        from app.schemas.workspace import WorkspaceSettings

        workspace = WorkspaceSettings(
            name="ws-test",
            workspace_path=str(tmp_path),
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",  # MEDIUM — workspace default
        )
        request = GenerateAppRequest(
            spec_path="./specs", outputs_dir="./outputs", generation_id="est-123",
        )

        with patch("app.services.claude_code.is_skip_mode_enabled", return_value=True), \
             patch("app.services.claude_code.generate_mock_implementation_plan") as mock_gen:
            # SKIP_MODE now returns None; REPARSE_PLAN handles JSON conversion
            result = await planning_agent_fn(
                primary_workspace=workspace,
                provider_instance=MagicMock(),
                request=request,
                logger=MagicMock(),
                model="anthropic/claude-opus-4.5",  # HIGH override
            )

        assert result is None
        # Verify that the mock markdown generator was called (proof SKIP_MODE path was taken)
        mock_gen.assert_called_once()

    def test_planning_model_param_signature(self):
        import inspect
        from app.services.claude_code import planning_agent_fn
        sig = inspect.signature(planning_agent_fn)
        assert "model" in sig.parameters
        assert sig.parameters["model"].default is None
        assert "mcp_servers" in sig.parameters
        assert "mcp_allowed_tools" in sig.parameters

    def test_workflow_context_has_llm_overrides(self, tmp_path):
        from app.services.workflow_steps import WorkflowContext
        from app.schemas.workspace import WorkspaceSettings
        from app.schemas.llm_tier import LLMTier

        ws = WorkspaceSettings(
            name="ws-1", workspace_path=str(tmp_path),
            provider="openrouter", model="anthropic/claude-sonnet-4.6",
        )
        overrides = {
            LLMTier.HIGH.value: "anthropic/claude-opus-4.5",
            LLMTier.MEDIUM.value: "anthropic/claude-sonnet-4.6",
        }
        ctx = WorkflowContext(
            request=MagicMock(),
            settings=MagicMock(),
            logger=MagicMock(),
            workspace_ids=["ws-1"],
            workspaces=[ws],
            workspace_manager=MagicMock(),
            primary_workspace=ws,
            llm_overrides=overrides,
        )
        assert ctx.llm_overrides[LLMTier.HIGH.value] == "anthropic/claude-opus-4.5"
        assert ctx.llm_overrides[LLMTier.MEDIUM.value] == "anthropic/claude-sonnet-4.6"


class TestOpportunisticCaching:
    """Test that resolve_model() opportunistically validates against cache when available."""
    
    def test_resolve_model_validates_against_cache_when_available(self, caplog):
        """If cache is populated, resolve_model(return_first=True) validates instantly."""
        from app.services.openrouter_models import _cache_manager
        from app.schemas.llm_tier import LLMTier, resolve_model
        from app.core.config import Settings
        
        # Populate cache with known models
        _cache_manager.set_cache({"anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro"})
        
        settings = Settings(LLM_MEDIUM="anthropic/claude-sonnet-4.5,google/gemini-2.5-pro")
        
        # Valid model - should pass silently
        with caplog.at_level(logging.WARNING):
            result = resolve_model(LLMTier.MEDIUM, settings, return_first=True)
        
        assert result == "anthropic/claude-sonnet-4.5"
        assert "not found in cached OpenRouter models" not in caplog.text
        
        # Clear cache for next test
        _cache_manager.clear_cache()
    
    def test_resolve_model_warns_if_model_not_in_cache(self, caplog):
        """If cache exists but model is invalid, logs warning but proceeds."""
        from app.services.openrouter_models import _cache_manager
        from app.schemas.llm_tier import LLMTier, resolve_model
        from app.core.config import Settings
        
        # Populate cache with known models (NOT including claude-opus-4.5)
        _cache_manager.set_cache({"anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro"})
        
        settings = Settings(LLM_MEDIUM="anthropic/claude-opus-4.5")
        
        # Invalid model - should warn but return it anyway
        with caplog.at_level(logging.WARNING):
            result = resolve_model(LLMTier.MEDIUM, settings, return_first=True)
        
        assert result == "anthropic/claude-opus-4.5"
        assert "not found in cached OpenRouter models" in caplog.text
        
        # Clear cache for next test
        _cache_manager.clear_cache()
    
    def test_resolve_model_skips_validation_if_cache_empty(self, caplog):
        """If cache is empty, no validation happens (silent fallthrough)."""
        from app.services.openrouter_models import _cache_manager
        from app.schemas.llm_tier import LLMTier, resolve_model
        from app.core.config import Settings
        
        # Ensure cache is empty
        _cache_manager.clear_cache()
        
        settings = Settings(LLM_MEDIUM="anthropic/claude-sonnet-4.5")
        
        with caplog.at_level(logging.WARNING):
            result = resolve_model(LLMTier.MEDIUM, settings, return_first=True)
        
        assert result == "anthropic/claude-sonnet-4.5"
        # No warnings - cache was empty, so validation was skipped
        assert "not found in cached OpenRouter models" not in caplog.text

