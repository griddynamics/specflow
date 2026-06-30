"""Unit tests for local SpecFlow MCP tools (check_specification_completeness, run_planning)."""

import json
import re

import pytest

from server import (
    _make_prompt_text,
    check_specification_completeness,
    run_planning,
)
from services.bundled_skills import SKILLS

_PLACEHOLDER_RE = re.compile(r"<<[A-Z_]+>>")


class TestMakePromptText:
    def test_substitutes_spec_dir_outputs_dir_and_src_dir(self) -> None:
        text = _make_prompt_text(
            "specflow-analysis",
            spec_dir="my-specs",
            outputs_dir="artifacts",
            src_dir="lib",
        )

        assert "<<SPEC_DIR>>" not in text
        assert "<<OUTPUTS_DIR>>" not in text
        assert "<<SRC_DIR>>" not in text
        assert "my-specs" in text
        assert "artifacts" in text
        assert "lib/" in text or "`lib`" in text

    def test_missing_substitution_leaves_placeholder_visible(self) -> None:
        text = _make_prompt_text("specflow-analysis", spec_dir="specs")

        assert "specs" in text
        assert "<<OUTPUTS_DIR>>" in text
        assert "<<SRC_DIR>>" in text

    def test_unknown_skill_raises_key_error(self) -> None:
        with pytest.raises(KeyError, match="not bundled"):
            _make_prompt_text("nonexistent-skill", spec_dir="specs")


def _parse_tool_response(raw: str) -> dict:
    return json.loads(raw)


class TestCheckSpecificationCompletenessTool:
    def test_returns_local_json_with_template(self) -> None:
        payload = _parse_tool_response(check_specification_completeness())

        assert payload["mode"] == "local"
        assert payload["skill"] == "specflow-analysis"
        assert "template" in payload
        assert payload["writes_to"] == "docs/analysis/specification_completeness.md"
        assert _PLACEHOLDER_RE.search(payload["template"]) is None

    def test_custom_paths_in_payload_and_template(self) -> None:
        payload = _parse_tool_response(
            check_specification_completeness(
                spec_dir="requirements",
                outputs_dir="out",
                src_dir="existing-code",
            )
        )

        assert payload["spec_dir"] == "requirements"
        assert payload["outputs_dir"] == "out"
        assert payload["src_dir"] == "existing-code"
        assert "requirements" in payload["template"]
        assert "out" in payload["template"]
        assert "existing-code" in payload["template"]

    def test_template_documents_src_dir_argument(self) -> None:
        payload = _parse_tool_response(check_specification_completeness())

        assert "`src_dir`" in payload["template"]
        assert "brownfield" in payload["template"].lower()


class TestRunPlanningTool:
    def test_returns_local_json_with_template(self) -> None:
        payload = _parse_tool_response(run_planning())

        assert payload["mode"] == "local"
        assert payload["skill"] == "specflow-planning"
        assert _PLACEHOLDER_RE.search(payload["template"]) is None
        assert "IMPLEMENTATION_PLAN.md" in payload["writes_to"]

    def test_custom_paths_for_brownfield_planning(self) -> None:
        payload = _parse_tool_response(
            run_planning(
                spec_dir="specifications",
                outputs_dir="plan-docs",
                src_dir="backend",
            )
        )

        assert "specifications" in payload["template"]
        assert "plan-docs" in payload["template"]
        assert "backend" in payload["template"]
        assert "extend" in payload["template"].lower() or "brownfield" in payload["template"].lower()


class TestBundledSkillContracts:
    @pytest.mark.parametrize("skill_name", ["specflow-analysis", "specflow-planning"])
    def test_skill_contains_src_dir_placeholder(self, skill_name: str) -> None:
        skill = next(s for s in SKILLS if s["name"] == skill_name)
        content = skill["content"]

        assert "<<SRC_DIR>>" in content
        assert "`src_dir`" in content
        assert "run_generation" in content

    def test_analysis_skill_recommends_medium_tier_models(self) -> None:
        content = next(s for s in SKILLS if s["name"] == "specflow-analysis")["content"]

        assert "recommended-models:" in content
        assert "claude-sonnet-4.6" in content
        assert "Execution constraints" in content

    def test_analysis_skill_requires_full_dimension_inventory(self) -> None:
        content = next(s for s in SKILLS if s["name"] == "specflow-analysis")["content"]

        assert "Dimension Status — Full inventory" in content
        assert "list all 6" in content.lower()
        assert "A1" in content and "A6" in content
        assert "summary-only" in content.lower()

    def test_planning_skill_recommends_high_tier_models(self) -> None:
        content = next(s for s in SKILLS if s["name"] == "specflow-planning")["content"]

        assert "recommended-models:" in content
        assert "claude-opus-4.6" in content
        assert "Execution constraints" in content

    @pytest.mark.parametrize("skill_name", ["specflow-analysis", "specflow-planning"])
    def test_argument_hint_includes_src_dir(self, skill_name: str) -> None:
        content = next(s for s in SKILLS if s["name"] == skill_name)["content"]

        assert "src_dir" in content
        assert "spec_dir outputs_dir src_dir" in content
