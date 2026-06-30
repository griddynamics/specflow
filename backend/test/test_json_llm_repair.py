"""Tests for JSON extraction + Pydantic validation + optional LLM repair."""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import LLM_LOW_DEFAULT_FIRST_MODEL, Settings
from app.schemas.mcp_prune_agent import McpPruneAgentJson
from app.services.json_llm_repair import (
    extract_json_string_candidates,
    parse_json_with_pydantic_and_repair,
    try_validate_json_model,
)
from app.services.llm_apis.open_router_api import openrouter_first_model_from_llm_low_csv


class TestOpenRouterModelCsv:
    def test_first_model_from_llm_low(self) -> None:
        assert openrouter_first_model_from_llm_low_csv("anthropic/claude-haiku-4.5") == (
            "anthropic/claude-haiku-4.5"
        )
        assert openrouter_first_model_from_llm_low_csv("a/b,c/d") == "a/b"

    def test_fallback_on_none_and_bad_input(self) -> None:
        assert openrouter_first_model_from_llm_low_csv("") == LLM_LOW_DEFAULT_FIRST_MODEL
        assert openrouter_first_model_from_llm_low_csv(None) == LLM_LOW_DEFAULT_FIRST_MODEL  # type: ignore[arg-type]


class TestExtractJsonStringCandidates:
    def test_plain_object(self) -> None:
        s = '{"enabled": []}'
        c = extract_json_string_candidates(s)
        assert '{"enabled": []}' in c

    def test_fenced_json(self) -> None:
        s = 'prefix\n```json\n{"enabled": ["a"]}\n```\n'
        c = extract_json_string_candidates(s)
        assert any('"a"' in x for x in c)

    def test_balanced_nested(self) -> None:
        s = 'x {"enabled": ["x"], "reasons": {"x": "{}"}} y'
        c = extract_json_string_candidates(s)
        assert any("reasons" in x for x in c)

    def test_prose_before_and_after_json(self) -> None:
        """Concrete case: assistant wraps object in narrative text."""
        s = (
            "Analysis complete.\n\n"
            '```json\n{"enabled": ["figma"], "reasons": {"figma": "link"}}\n```\n\n'
            "End."
        )
        c = extract_json_string_candidates(s)
        assert try_validate_json_model(c, McpPruneAgentJson) is not None

    def test_multiple_fences_second_is_valid(self) -> None:
        """First fence has no balanced JSON; second fence yields a valid object."""
        s = (
            "Try 1:\n```json\n{ broken\n```\n"
            'Valid:\n```json\n{"enabled": [], "reasons": {}}\n```\n'
        )
        c = extract_json_string_candidates(s)
        m = try_validate_json_model(c, McpPruneAgentJson)
        assert m is not None
        assert m.enabled == []

    def test_truncated_fence_no_valid_candidate(self) -> None:
        """Unclosed object inside fence — no json.loads success for MCP prune schema."""
        s = '```json\n{"enabled": ["x"], "reasons":\n```'
        c = extract_json_string_candidates(s)
        assert try_validate_json_model(c, McpPruneAgentJson) is None


class TestTryValidateJsonModel:
    def test_valid(self) -> None:
        m = try_validate_json_model(['{"enabled": ["Figma"], "reasons": {"figma": "ok"}}'], McpPruneAgentJson)
        assert m is not None
        assert m.enabled == ["figma"]

    def test_invalid_type_enabled(self) -> None:
        m = try_validate_json_model(['{"enabled": "figma"}'], McpPruneAgentJson)
        assert m is None


class TestParseJsonWithRepair:
    @pytest.mark.asyncio
    async def test_no_api_key_skips_repair(self) -> None:
        s = Settings()
        s.OPENROUTER_API_KEY = None
        out = await parse_json_with_pydantic_and_repair(
            "not json {",
            McpPruneAgentJson,
            s,
            max_llm_repairs=2,
        )
        assert out.value is None
        assert not out.repair_returned_empty

    @pytest.mark.asyncio
    async def test_repair_uses_openrouter_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_to_thread(func, /, *args, **kwargs):  # noqa: ARG001
            return '{"enabled":["playwright"],"reasons":{"playwright":"UI mentioned"}}'

        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
        s = Settings()
        s.OPENROUTER_API_KEY = "sk-or-test-key"
        out = await parse_json_with_pydantic_and_repair(
            "broken {{{",
            McpPruneAgentJson,
            s,
            max_llm_repairs=2,
        )
        assert out.value is not None
        assert out.value.enabled == ["playwright"]

    @pytest.mark.asyncio
    async def test_empty_repair_sets_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_to_thread(func, /, *args, **kwargs):  # noqa: ARG001
            return ""

        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
        s = Settings()
        s.OPENROUTER_API_KEY = "sk-or-test-key"
        out = await parse_json_with_pydantic_and_repair(
            "not json",
            McpPruneAgentJson,
            s,
            max_llm_repairs=2,
        )
        assert out.value is None
        assert out.repair_returned_empty is True

    @pytest.mark.asyncio
    async def test_max_llm_repairs_zero_skips_repair_even_with_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Repair off: no OpenRouter calls even when API key is set."""

        async def boom_to_thread(func, /, *args, **kwargs):  # noqa: ARG001
            raise AssertionError("to_thread should not run when max_llm_repairs=0")

        monkeypatch.setattr(asyncio, "to_thread", boom_to_thread)
        s = Settings()
        s.OPENROUTER_API_KEY = "sk-or-test-key"
        out = await parse_json_with_pydantic_and_repair(
            "not valid json {",
            McpPruneAgentJson,
            s,
            max_llm_repairs=0,
        )
        assert out.value is None
        assert not out.repair_returned_empty
