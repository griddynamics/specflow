"""Tests for spec-driven MCP prune (keywords, parse, apply, workflow entry)."""

import logging
from pathlib import Path

import pytest

from app.core.artifact_subdirs import ANALYSIS_SUBDIR
from app.core.config import MCP_FIGMA, MCP_PLAYWRIGHT, Settings
from app.prompts.mcp_workflow_registry import (
    MCP_PRUNE_LLM_RULES_BY_MCP_ID,
    format_mcp_prune_llm_rules_section,
)
from app.services.mcp_prune import (
    McpPruneOutcome,
    apply_keyword_scan_to_candidates,
    apply_mcp_prune,
    parse_keyword_csv,
    parse_mcp_prune_agent_json,
    prune_enabled_mcps_keyword_only,
    scan_mcp_keyword_evidence,
)


class TestMcpPruneLlmRegistry:
    def test_rules_dict_is_immutable_and_covers_supported_ids(self) -> None:
        assert set(MCP_PRUNE_LLM_RULES_BY_MCP_ID.keys()) == {MCP_FIGMA, MCP_PLAYWRIGHT}
        with pytest.raises(TypeError):
            MCP_PRUNE_LLM_RULES_BY_MCP_ID[MCP_FIGMA] = "x"  # type: ignore[index]

    def test_format_section_includes_rules_for_candidate(self) -> None:
        text = format_mcp_prune_llm_rules_section(frozenset({MCP_FIGMA, MCP_PLAYWRIGHT}))
        assert "### figma" in text
        assert "### playwright" in text
        assert "figma.com" in text
        assert "npm test library" in text
        assert "Schema:" in text


class TestParseKeywordCsv:
    def test_splits_and_lowercases(self) -> None:
        assert parse_keyword_csv("A, b ,,C") == ("a", "b", "c")


class TestParseMcpPruneJson:
    @pytest.mark.asyncio
    async def test_parse_code_fence_json(self) -> None:
        text = """```json
{"enabled": ["figma"], "reasons": {"figma": "Specs reference figma.com links."}}
```
"""
        pr = await parse_mcp_prune_agent_json(text)
        assert pr.outcome is not None
        assert pr.outcome.enabled == frozenset({"figma"})
        assert not pr.preserve_candidate_mcps

    @pytest.mark.asyncio
    async def test_parse_plain_object(self) -> None:
        text = '{"enabled": ["figma"], "reasons": {"figma": "ok"}}'
        pr = await parse_mcp_prune_agent_json(text)
        assert pr.outcome is not None
        assert pr.outcome.enabled == frozenset({"figma"})
        assert pr.outcome.reasons.get("figma") == "ok"
        assert not pr.preserve_candidate_mcps

    @pytest.mark.asyncio
    async def test_parse_invalid_returns_none(self) -> None:
        empty = await parse_mcp_prune_agent_json("")
        assert empty.outcome is None
        assert not empty.preserve_candidate_mcps
        nb = await parse_mcp_prune_agent_json("no braces here")
        assert nb.outcome is None
        assert not nb.preserve_candidate_mcps


class TestApplyMcpPrune:
    def test_intersects_candidate(self) -> None:
        candidate = frozenset({"playwright", "figma"})
        parsed = McpPruneOutcome(
            enabled=frozenset({"playwright", "figma", "unknown"}),
            reasons={"playwright": "x", "figma": "y"},
        )
        eff, reasons = apply_mcp_prune(candidate, parsed)
        assert eff == frozenset({"playwright", "figma"})


class TestKeywordScan:
    def test_figma_hit_in_index(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        (root / "out").mkdir(parents=True)
        (root / "specs").mkdir(parents=True)
        (root / "out" / "specification_index.md").write_text(
            "Design at https://figma.com/file/x", encoding="utf-8"
        )
        (root / "specs" / "r.md").write_text("API only", encoding="utf-8")
        s = Settings()
        hits, counts, ev = scan_mcp_keyword_evidence(
            isolated_root=root,
            spec_rel="specs",
            outputs_rel="out",
            index_filename="specification_index.md",
            candidate=frozenset({"figma", "playwright"}),
            settings=s,
        )
        assert hits["figma"] is True
        assert "figma.com" in ev.lower()

    def test_figma_hit_in_analysis_subdir_index(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        analysis = root / "docs" / ANALYSIS_SUBDIR
        analysis.mkdir(parents=True)
        (root / "specs").mkdir(parents=True)
        (analysis / "specification_index.md").write_text(
            "Design at https://figma.com/file/x", encoding="utf-8"
        )
        hits, _, ev = scan_mcp_keyword_evidence(
            isolated_root=root,
            spec_rel="specs",
            outputs_rel="docs",
            index_filename="specification_index.md",
            candidate=frozenset({"figma", "playwright"}),
            settings=Settings(),
        )
        assert hits["figma"] is True
        assert "figma.com" in ev.lower()

    def test_playwright_hit_frontend_keyword(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        (root / "out").mkdir(parents=True)
        (root / "specs").mkdir(parents=True)
        (root / "out" / "specification_index.md").write_text("Overview", encoding="utf-8")
        (root / "specs" / "r.md").write_text("React dashboard SPA", encoding="utf-8")
        s = Settings()
        hits, _, _ = scan_mcp_keyword_evidence(
            isolated_root=root,
            spec_rel="specs",
            outputs_rel="out",
            index_filename="specification_index.md",
            candidate=frozenset({"playwright"}),
            settings=s,
        )
        assert hits["playwright"] is True


class TestApplyKeywordScan:
    def test_keeps_only_hits(self) -> None:
        c = frozenset({"figma", "playwright"})
        h = {"figma": True, "playwright": False}
        eff, _ = apply_keyword_scan_to_candidates(c, h)
        assert eff == frozenset({"figma"})

    def test_fallback_when_no_hits(self) -> None:
        c = frozenset({"playwright"})
        h = {"playwright": False}
        eff, reasons = apply_keyword_scan_to_candidates(c, h)
        assert eff == c
        assert "fallback" in reasons["playwright"].lower()


@pytest.mark.parametrize(
    "spec_path,outputs_dir",
    [("specs", "out"), ("./specs", "./out")],
)
def test_scan_finds_file_under_normalized_paths(tmp_path: Path, spec_path: str, outputs_dir: str) -> None:
    root = tmp_path / "ws"
    (root / outputs_dir.strip("./")).mkdir(parents=True)
    (root / spec_path.strip("./")).mkdir(parents=True)
    (root / outputs_dir.strip("./") / "specification_index.md").write_text("x", encoding="utf-8")
    (root / spec_path.strip("./") / "req.md").write_text("uses figma.com", encoding="utf-8")
    hits, _, ev = scan_mcp_keyword_evidence(
        isolated_root=root,
        spec_rel=spec_path,
        outputs_rel=outputs_dir,
        index_filename="specification_index.md",
        candidate=frozenset({"figma"}),
        settings=Settings(),
    )
    assert hits["figma"] is True
    assert "figma" in ev.lower()


class TestPruneEnabledMcpsKeywordOnly:
    def test_prunes_to_figma_only(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        (root / "docs" / ANALYSIS_SUBDIR).mkdir(parents=True)
        (root / "specs").mkdir(parents=True)
        (root / "docs" / ANALYSIS_SUBDIR / "specification_index.md").write_text(
            "figma.com/design/abc", encoding="utf-8"
        )
        (root / "specs" / "api.md").write_text("REST only", encoding="utf-8")
        log = logging.getLogger("test_mcp_prune")
        effective, reasons, persist = prune_enabled_mcps_keyword_only(
            settings=Settings(),
            logger=log,
            workspace_root=root,
            spec_path="specs",
            outputs_dir="docs",
            candidate_enabled_mcps=frozenset({"figma", "playwright"}),
        )
        assert persist is True
        assert effective == frozenset({"figma"})
        assert "figma" in reasons

    def test_disabled_returns_candidate_unchanged(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        candidate = frozenset({"playwright"})
        effective, _, persist = prune_enabled_mcps_keyword_only(
            settings=Settings(MCP_AUTO_PRUNE_ENABLED=False),
            logger=logging.getLogger("test_mcp_prune"),
            workspace_root=root,
            spec_path="specs",
            outputs_dir="docs",
            candidate_enabled_mcps=candidate,
        )
        assert persist is False
        assert effective == candidate


# --- legacy LLM prune agent JSON parsing ---


def _prune_settings(**kwargs: object) -> Settings:
    return Settings().model_copy(update=dict(kwargs))



@pytest.mark.asyncio
async def test_parse_mcp_prune_agent_json_multiple_fences() -> None:
    """Second ```json fence holds valid MCP prune object."""
    text = (
        "Noise\n```json\n{ not valid json\n```\n"
        'Real:\n```json\n{"enabled": ["playwright"], "reasons": {"playwright": "UI"}}\n```\n'
    )
    pr = await parse_mcp_prune_agent_json(text)
    assert pr.outcome is not None
    assert pr.outcome.enabled == frozenset({"playwright"})
