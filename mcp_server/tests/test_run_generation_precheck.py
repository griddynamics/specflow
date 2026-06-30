"""Unit tests for run_generation_precheck — one test per rejection code."""
import pytest
from pathlib import Path

from services.run_generation_precheck import (
    RejectionCode,
    precheck,
    ANALYSIS_SUBDIR,
    PLANNING_SUBDIR,
    SPEC_COMPLETENESS_FILE,
    IMPLEMENTATION_PLAN_FILE,
    E2E_TEST_PLAN_FILE,
)

_VALID_ANALYSIS = "# Part F\n\nLOCAL_ONLY\n"
_INTEGRATION_READY_ANALYSIS = "# Part F\n\nINTEGRATION_TESTS_READY\n"


def _write_spec(root: Path, spec: str = "specs") -> Path:
    spec_dir = root / spec
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "app.md").write_text("spec")
    return spec_dir


def _write_analysis(root: Path, outputs: str = "docs", content: str = _VALID_ANALYSIS) -> Path:
    path = root / outputs / ANALYSIS_SUBDIR / SPEC_COMPLETENESS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _write_plan(root: Path, outputs: str = "docs") -> Path:
    path = root / outputs / PLANNING_SUBDIR / IMPLEMENTATION_PLAN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Plan\n")
    return path


def _write_e2e_plan(root: Path, outputs: str = "docs") -> Path:
    path = root / outputs / PLANNING_SUBDIR / E2E_TEST_PLAN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# E2E Plan\n")
    return path


class TestPrecheckHappyPath:
    def test_returns_none_when_all_local_only_files_present(self, tmp_path):
        _write_spec(tmp_path)
        _write_analysis(tmp_path)
        _write_plan(tmp_path)
        assert precheck(tmp_path, "specs", "docs") is None

    def test_returns_none_when_integration_ready_and_e2e_plan_present(self, tmp_path):
        _write_spec(tmp_path)
        _write_analysis(tmp_path, content=_INTEGRATION_READY_ANALYSIS)
        _write_plan(tmp_path)
        _write_e2e_plan(tmp_path)
        assert precheck(tmp_path, "specs", "docs") is None


class TestPrecheckRejectionCodes:
    def test_spec_dir_missing_when_spec_dir_absent(self, tmp_path):
        result = precheck(tmp_path, "specs", "docs")
        assert result is not None
        assert result.code == RejectionCode.SPEC_DIR_MISSING
        assert "specs/" in result.message

    def test_spec_dir_missing_when_spec_dir_empty(self, tmp_path):
        (tmp_path / "specs").mkdir()
        result = precheck(tmp_path, "specs", "docs")
        assert result is not None
        assert result.code == RejectionCode.SPEC_DIR_MISSING

    def test_outputs_dir_missing(self, tmp_path):
        _write_spec(tmp_path)
        result = precheck(tmp_path, "specs", "docs")
        assert result is not None
        assert result.code == RejectionCode.OUTPUTS_DIR_MISSING
        assert "docs/" in result.message

    def test_analysis_missing(self, tmp_path):
        _write_spec(tmp_path)
        (tmp_path / "docs").mkdir()
        result = precheck(tmp_path, "specs", "docs")
        assert result is not None
        assert result.code == RejectionCode.ANALYSIS_MISSING
        assert SPEC_COMPLETENESS_FILE in result.message
        assert result.missing_files

    def test_plan_missing(self, tmp_path):
        _write_spec(tmp_path)
        _write_analysis(tmp_path)
        result = precheck(tmp_path, "specs", "docs")
        assert result is not None
        assert result.code == RejectionCode.PLAN_MISSING
        assert IMPLEMENTATION_PLAN_FILE in result.message
        assert result.missing_files

    def test_analysis_unreadable_missing_part_f(self, tmp_path):
        _write_spec(tmp_path)
        analysis_path = tmp_path / "docs" / ANALYSIS_SUBDIR / SPEC_COMPLETENESS_FILE
        analysis_path.parent.mkdir(parents=True)
        analysis_path.write_text("# Summary\n\nNo Part F section here at all.")
        _write_plan(tmp_path)  # plan must exist so we reach the Part F check
        result = precheck(tmp_path, "specs", "docs")
        assert result is not None
        assert result.code == RejectionCode.ANALYSIS_UNREADABLE
        assert "Part F" in result.message

    def test_e2e_plan_missing_when_integration_tests_ready(self, tmp_path):
        _write_spec(tmp_path)
        _write_analysis(tmp_path, content=_INTEGRATION_READY_ANALYSIS)
        _write_plan(tmp_path)
        result = precheck(tmp_path, "specs", "docs")
        assert result is not None
        assert result.code == RejectionCode.E2E_PLAN_MISSING
        assert E2E_TEST_PLAN_FILE in result.message
        assert result.missing_files

    def test_no_e2e_required_when_local_only(self, tmp_path):
        _write_spec(tmp_path)
        _write_analysis(tmp_path, content=_VALID_ANALYSIS)
        _write_plan(tmp_path)
        # No e2e plan written — should still pass
        assert precheck(tmp_path, "specs", "docs") is None

    def test_custom_outputs_dir_respected(self, tmp_path):
        _write_spec(tmp_path)
        _write_analysis(tmp_path, outputs="output")
        _write_plan(tmp_path, outputs="output")
        assert precheck(tmp_path, "specs", "output") is None
        # Using default "docs" should fail since we wrote to "output"
        result = precheck(tmp_path, "specs", "docs")
        assert result is not None
        assert result.code == RejectionCode.OUTPUTS_DIR_MISSING

    def test_fuzzy_plan_filename_at_wrong_case(self, tmp_path):
        _write_spec(tmp_path)
        _write_analysis(tmp_path)
        plan_dir = tmp_path / "docs" / PLANNING_SUBDIR
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "implementation_plan.md").write_text("# Plan\n")
        assert precheck(tmp_path, "specs", "docs") is None

    def test_integration_token_outside_part_f_does_not_require_e2e(self, tmp_path):
        """Part F LOCAL_ONLY must win even if INTEGRATION_TESTS_READY appears in Part B."""
        _write_spec(tmp_path)
        analysis = (
            "# Part B\n\nINTEGRATION_TESTS_READY (future milestone)\n\n"
            "# Part F\n\nLOCAL_ONLY\n"
        )
        _write_analysis(tmp_path, content=analysis)
        _write_plan(tmp_path)
        assert precheck(tmp_path, "specs", "docs") is None
