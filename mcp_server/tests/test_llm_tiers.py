"""Unit tests for LLM tier / MCP-servers env propagation (services/llm_tiers.py)."""

from services.llm_tiers import (
    LLM_TIER_KEYS,
    MCP_SERVERS_ENABLED_ENV,
    apply_llm_tier_overrides,
    apply_mcp_servers_enabled_form,
)


class TestApplyLlmTierOverrides:
    def test_injects_only_keys_present_in_env(self, monkeypatch):
        monkeypatch.setenv("LLM_HIGH", "claude-opus")
        monkeypatch.setenv("LLM_LOW", "claude-haiku")
        monkeypatch.delenv("LLM_MEDIUM", raising=False)

        form: dict[str, str] = {}
        apply_llm_tier_overrides(form)

        assert form == {"LLM_HIGH": "claude-opus", "LLM_LOW": "claude-haiku"}
        assert "LLM_MEDIUM" not in form

    def test_no_env_leaves_form_untouched(self, monkeypatch):
        for key in LLM_TIER_KEYS:
            monkeypatch.delenv(key, raising=False)

        form: dict[str, str] = {"spec_path": "specs"}
        apply_llm_tier_overrides(form)

        assert form == {"spec_path": "specs"}


class TestApplyMcpServersEnabledForm:
    def test_sets_trimmed_value_when_present(self, monkeypatch):
        monkeypatch.setenv(MCP_SERVERS_ENABLED_ENV, "  playwright,figma  ")
        form: dict[str, str] = {}
        apply_mcp_servers_enabled_form(form)
        assert form[MCP_SERVERS_ENABLED_ENV] == "playwright,figma"

    def test_blank_value_is_ignored(self, monkeypatch):
        monkeypatch.setenv(MCP_SERVERS_ENABLED_ENV, "   ")
        form: dict[str, str] = {}
        apply_mcp_servers_enabled_form(form)
        assert MCP_SERVERS_ENABLED_ENV not in form

    def test_unset_value_is_ignored(self, monkeypatch):
        monkeypatch.delenv(MCP_SERVERS_ENABLED_ENV, raising=False)
        form: dict[str, str] = {}
        apply_mcp_servers_enabled_form(form)
        assert MCP_SERVERS_ENABLED_ENV not in form
