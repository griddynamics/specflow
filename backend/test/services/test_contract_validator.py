"""Unit tests for contract_validator — one test per rejection code in the catalog."""
import pytest
from pathlib import Path

from app.services.contract_validator import (
    ContractRejection,
    RejectionCode,
    is_integration_tests_ready,
    normalize_contract_files,
    validate_generation_contract_preflight,
    _find_candidates,
    _CanonicalFile,
    _normalize_for_match,
)
from app.core.artifact_files import (
    SPEC_COMPLETENESS_FILE,
    IMPLEMENTATION_PLAN_FILE,
    E2E_TEST_PLAN_FILE,
)
from app.core.artifact_subdirs import ANALYSIS_SUBDIR, PLANNING_SUBDIR

_VALID_ANALYSIS = "# Part F\n\nLOCAL_ONLY\n"
_INTEGRATION_READY = "# Part F\n\nINTEGRATION_TESTS_READY\n"
_MINIMAL_PLAN = "# Plan\n\n## Phase 1\nDo something.\n- task 1\n"


def _logger():
    import logging
    return logging.getLogger("test_contract_validator")


def _setup_outputs(tmp_path: Path, outputs_dir: str = "docs"):
    root = tmp_path
    out = root / outputs_dir
    (out / ANALYSIS_SUBDIR).mkdir(parents=True, exist_ok=True)
    (out / PLANNING_SUBDIR).mkdir(parents=True, exist_ok=True)
    return root


def _write(root: Path, outputs_dir: str, subdir: str, filename: str, content: str) -> Path:
    path = root / outputs_dir / subdir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TestNormalizeForMatch:
    def test_lowercases(self):
        assert _normalize_for_match("IMPLEMENTATION_PLAN.md") == _normalize_for_match("implementation_plan.md")

    def test_strips_separators(self):
        assert _normalize_for_match("implementation-plan.md") == _normalize_for_match("implementationplan.md")

    def test_space_treated_as_separator(self):
        assert _normalize_for_match("implementation plan.md") == _normalize_for_match("implementationplan.md")


class TestFindCandidatesSearchesAllSubdirs:
    """W1 regression: plan file placed in analysis/ should be found."""

    def test_finds_plan_in_canonical_planning_subdir(self, tmp_path):
        canonical = _CanonicalFile(
            subdir=PLANNING_SUBDIR,
            name=IMPLEMENTATION_PLAN_FILE,
            rejection_code=RejectionCode.PLAN_MISSING,
            mcp_tool="run_planning",
        )
        outputs_root = tmp_path
        (outputs_root / PLANNING_SUBDIR).mkdir()
        (outputs_root / PLANNING_SUBDIR / IMPLEMENTATION_PLAN_FILE).write_text("plan")

        result = _find_candidates(outputs_root, canonical)
        assert len(result) == 1

    def test_finds_plan_placed_in_analysis_subdir(self, tmp_path):
        """W1: plan file under analysis/ must be found (sibling-subdir gap fix)."""
        canonical = _CanonicalFile(
            subdir=PLANNING_SUBDIR,
            name=IMPLEMENTATION_PLAN_FILE,
            rejection_code=RejectionCode.PLAN_MISSING,
            mcp_tool="run_planning",
        )
        outputs_root = tmp_path
        (outputs_root / ANALYSIS_SUBDIR).mkdir()
        (outputs_root / ANALYSIS_SUBDIR / IMPLEMENTATION_PLAN_FILE).write_text("plan")

        result = _find_candidates(outputs_root, canonical)
        assert len(result) == 1, "Plan file in analysis/ subdir should be found"

    def test_finds_analysis_placed_in_planning_subdir(self, tmp_path):
        """Symmetric case: analysis file placed under planning/ should be found."""
        canonical = _CanonicalFile(
            subdir=ANALYSIS_SUBDIR,
            name=SPEC_COMPLETENESS_FILE,
            rejection_code=RejectionCode.ANALYSIS_MISSING,
            mcp_tool="check_specification_completeness",
        )
        outputs_root = tmp_path
        (outputs_root / PLANNING_SUBDIR).mkdir()
        (outputs_root / PLANNING_SUBDIR / SPEC_COMPLETENESS_FILE).write_text("analysis")

        result = _find_candidates(outputs_root, canonical)
        assert len(result) == 1, "Analysis file in planning/ subdir should be found"


class TestNormalizeContractFilesRejections:
    def test_analysis_missing(self, tmp_path):
        _setup_outputs(tmp_path)
        # No analysis file written
        _write(tmp_path, "docs", PLANNING_SUBDIR, IMPLEMENTATION_PLAN_FILE, _MINIMAL_PLAN)
        with pytest.raises(ContractRejection) as exc_info:
            normalize_contract_files(tmp_path, "docs", _logger())
        assert exc_info.value.code == RejectionCode.ANALYSIS_MISSING
        assert SPEC_COMPLETENESS_FILE in exc_info.value.message
        assert exc_info.value.missing_files

    def test_plan_missing(self, tmp_path):
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        # No plan file
        with pytest.raises(ContractRejection) as exc_info:
            normalize_contract_files(tmp_path, "docs", _logger())
        assert exc_info.value.code == RejectionCode.PLAN_MISSING
        assert IMPLEMENTATION_PLAN_FILE in exc_info.value.message

    def test_analysis_unreadable_missing_part_f(self, tmp_path):
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, "# No Part F here")
        _write(tmp_path, "docs", PLANNING_SUBDIR, IMPLEMENTATION_PLAN_FILE, _MINIMAL_PLAN)
        with pytest.raises(ContractRejection) as exc_info:
            normalize_contract_files(tmp_path, "docs", _logger())
        assert exc_info.value.code == RejectionCode.ANALYSIS_UNREADABLE
        assert "Part F" in exc_info.value.message

    def test_e2e_plan_missing_when_integration_tests_ready(self, tmp_path):
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _INTEGRATION_READY)
        _write(tmp_path, "docs", PLANNING_SUBDIR, IMPLEMENTATION_PLAN_FILE, _MINIMAL_PLAN)
        # No e2e plan
        with pytest.raises(ContractRejection) as exc_info:
            normalize_contract_files(tmp_path, "docs", _logger())
        assert exc_info.value.code == RejectionCode.E2E_PLAN_MISSING
        assert E2E_TEST_PLAN_FILE in exc_info.value.message

    def test_ambiguous_file_when_two_candidates_match(self, tmp_path):
        _setup_outputs(tmp_path)
        out = tmp_path / "docs"
        # Two non-canonical files that normalize to the same stem — neither is exact canonical
        (out / PLANNING_SUBDIR / "implementation-plan.md").write_text("plan 1")
        (out / PLANNING_SUBDIR / "implementation_plan.md").write_text("plan 2")
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        with pytest.raises(ContractRejection) as exc_info:
            normalize_contract_files(tmp_path, "docs", _logger())
        assert exc_info.value.code == RejectionCode.AMBIGUOUS_FILE
        assert len(exc_info.value.ambiguous) == 2

    def test_normalizes_case_insensitive_filename(self, tmp_path):
        """Wrong-case filename should be moved to canonical location and succeed."""
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        _write(tmp_path, "docs", PLANNING_SUBDIR, "implementation_plan.md", _MINIMAL_PLAN)
        result = normalize_contract_files(tmp_path, "docs", _logger())
        assert "plan" in result
        assert result["plan"].name == IMPLEMENTATION_PLAN_FILE

    def test_normalizes_file_from_outputs_root(self, tmp_path):
        """File at outputs_dir root (not in subdir) should be moved to canonical subdir."""
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        # Plan at outputs_root, not in planning/
        (tmp_path / "docs" / IMPLEMENTATION_PLAN_FILE).write_text(_MINIMAL_PLAN)
        result = normalize_contract_files(tmp_path, "docs", _logger())
        assert result["plan"] == tmp_path / "docs" / PLANNING_SUBDIR / IMPLEMENTATION_PLAN_FILE

    def test_normalizes_plan_from_sibling_analysis_subdir(self, tmp_path):
        """W1: plan file placed under analysis/ must be found and moved to planning/."""
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        # Plan accidentally placed under analysis/
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, IMPLEMENTATION_PLAN_FILE, _MINIMAL_PLAN)
        result = normalize_contract_files(tmp_path, "docs", _logger())
        assert result["plan"] == tmp_path / "docs" / PLANNING_SUBDIR / IMPLEMENTATION_PLAN_FILE

    def test_happy_path_local_only(self, tmp_path):
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        _write(tmp_path, "docs", PLANNING_SUBDIR, IMPLEMENTATION_PLAN_FILE, _MINIMAL_PLAN)
        result = normalize_contract_files(tmp_path, "docs", _logger())
        assert "analysis" in result
        assert "plan" in result
        assert "e2e_plan" not in result

    def test_happy_path_integration_tests_ready(self, tmp_path):
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _INTEGRATION_READY)
        _write(tmp_path, "docs", PLANNING_SUBDIR, IMPLEMENTATION_PLAN_FILE, _MINIMAL_PLAN)
        _write(tmp_path, "docs", PLANNING_SUBDIR, E2E_TEST_PLAN_FILE, "# E2E\n")
        result = normalize_contract_files(tmp_path, "docs", _logger())
        assert "e2e_plan" in result

    def test_rejection_message_contains_mcp_tool_name(self, tmp_path):
        """Every rejection must tell the user which MCP tool to re-run."""
        _setup_outputs(tmp_path)
        with pytest.raises(ContractRejection) as exc_info:
            normalize_contract_files(tmp_path, "docs", _logger())
        assert "check_specification_completeness" in exc_info.value.message


class TestGenerationContractPreflight:
    def test_rejects_plan_with_no_phase_headings(self, tmp_path):
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        _write(tmp_path, "docs", PLANNING_SUBDIR, IMPLEMENTATION_PLAN_FILE, "# Plan\n\nNo phases yet.\n")

        with pytest.raises(ContractRejection) as exc_info:
            validate_generation_contract_preflight(tmp_path, "docs", _logger())

        assert exc_info.value.code == RejectionCode.PLAN_NO_PHASES

    def test_rejects_plan_with_empty_phase_section(self, tmp_path):
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        _write(tmp_path, "docs", PLANNING_SUBDIR, IMPLEMENTATION_PLAN_FILE, "# Plan\n\n## Phase 1\n")

        with pytest.raises(ContractRejection) as exc_info:
            validate_generation_contract_preflight(tmp_path, "docs", _logger())

        assert exc_info.value.code == RejectionCode.PLAN_UNPARSEABLE

    def test_accepts_minimal_valid_plan(self, tmp_path):
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        _write(tmp_path, "docs", PLANNING_SUBDIR, IMPLEMENTATION_PLAN_FILE, _MINIMAL_PLAN)

        result = validate_generation_contract_preflight(tmp_path, "docs", _logger())

        assert "plan" in result

    def test_accepts_phase_with_nested_subheadings(self, tmp_path):
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        _write(
            tmp_path,
            "docs",
            PLANNING_SUBDIR,
            IMPLEMENTATION_PLAN_FILE,
            "# Plan\n\n## Phase 1\n\n### Tasks\n\n- Build the smallest useful slice.\n",
        )

        result = validate_generation_contract_preflight(tmp_path, "docs", _logger())

        assert "plan" in result

    def test_rejects_plan_when_any_phase_section_is_empty(self, tmp_path):
        """A single fleshed-out phase must not let empty phase stubs slip through —
        the contract promises *each* phase has a body."""
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        _write(
            tmp_path,
            "docs",
            PLANNING_SUBDIR,
            IMPLEMENTATION_PLAN_FILE,
            "# Plan\n\n## Phase 1\n\nReal work here.\n\n## Phase 2\n\n## Phase 3\n",
        )

        with pytest.raises(ContractRejection) as exc_info:
            validate_generation_contract_preflight(tmp_path, "docs", _logger())

        assert exc_info.value.code == RejectionCode.PLAN_UNPARSEABLE

    def test_accepts_single_hash_phase_headings(self, tmp_path):
        """The preflight is lenient about heading level: a hand-edited `# Phase N` (h1)
        must pass — the regex must not be stricter than the downstream conversion agent."""
        _setup_outputs(tmp_path)
        _write(tmp_path, "docs", ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE, _VALID_ANALYSIS)
        _write(
            tmp_path,
            "docs",
            PLANNING_SUBDIR,
            IMPLEMENTATION_PLAN_FILE,
            "# Phase 1: Setup\n\nInitialize the project.\n- task 1\n",
        )

        result = validate_generation_contract_preflight(tmp_path, "docs", _logger())

        assert "plan" in result


class TestIsIntegrationTestsReady:
    """PR255 Bug 4: readiness is read from Part F ONLY, not the whole document."""

    def test_part_f_integration_ready(self):
        assert is_integration_tests_ready(_INTEGRATION_READY) is True

    def test_part_f_local_only(self):
        assert is_integration_tests_ready(_VALID_ANALYSIS) is False

    def test_token_outside_part_f_is_ignored(self):
        # The token appears in a Part B summary, but Part F declares LOCAL_ONLY.
        # Must NOT flip to ready — otherwise the user is wrongly forced to supply
        # an e2e-test-plan.md (E2E_PLAN_MISSING).
        analysis = (
            "# Part B\n\nStatus: INTEGRATION_TESTS_READY (planned for a later milestone)\n\n"
            "# Part F\n\nLOCAL_ONLY\n"
        )
        assert is_integration_tests_ready(analysis) is False

    def test_token_in_part_f_after_other_sections(self):
        analysis = (
            "# Part B\n\nSome notes.\n\n"
            "# Part F\n\nINTEGRATION_TESTS_READY\n"
        )
        assert is_integration_tests_ready(analysis) is True

    def test_no_part_f_returns_false(self):
        # No Part F header at all → not ready here; the missing-Part-F case is
        # rejected separately by normalize_contract_files with ANALYSIS_UNREADABLE.
        assert is_integration_tests_ready("# Part A\n\nINTEGRATION_TESTS_READY\n") is False
