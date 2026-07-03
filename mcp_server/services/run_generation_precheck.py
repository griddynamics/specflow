"""MCP-side precheck for `run_generation`.

Runs locally before any backend call. Implements the first layer of the gate defined
in CLAUDE.md "run_generation rejection contract":

  1. MCP-side precheck (this module) — cheap, instant feedback in the IDE.
  2. Backend contract validator — runs after upload (see backend/app/services/contract_validator.py).

This precheck is intentionally conservative. It catches missing files and directories
with no fuzzy matching — the backend validator handles case/separator normalization.
The goal is to fail fast in the IDE for the obvious cases so the user doesn't wait
on an upload that's guaranteed to be rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class RejectionCode(StrEnum):
    """Rejection codes — must match the catalog in CLAUDE.md and backend RejectionCode exactly.

    String values are the single source of truth shared with the backend contract validator.
    Layer 1 (this module) uses: SPEC_DIR_MISSING, OUTPUTS_DIR_MISSING, ANALYSIS_MISSING,
    PLAN_MISSING, E2E_PLAN_MISSING, ANALYSIS_UNREADABLE, GENERATION_ALREADY_RUNNING.
    The remaining codes are defined here so both enums stay in sync with the full catalog.
    """

    SPEC_DIR_MISSING = "SPEC_DIR_MISSING"
    OUTPUTS_DIR_MISSING = "OUTPUTS_DIR_MISSING"
    ANALYSIS_MISSING = "ANALYSIS_MISSING"
    PLAN_MISSING = "PLAN_MISSING"
    E2E_PLAN_MISSING = "E2E_PLAN_MISSING"
    AMBIGUOUS_FILE = "AMBIGUOUS_FILE"
    ANALYSIS_UNREADABLE = "ANALYSIS_UNREADABLE"
    PLAN_NO_PHASES = "PLAN_NO_PHASES"
    PLAN_UNPARSEABLE = "PLAN_UNPARSEABLE"
    E2E_PLAN_UNPARSEABLE = "E2E_PLAN_UNPARSEABLE"
    GENERATION_ALREADY_RUNNING = "GENERATION_ALREADY_RUNNING"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"


# Canonical paths the local skills are required to produce.
# Source of truth lives in CLAUDE.md "File/Directory Contract".
ANALYSIS_SUBDIR = "analysis"
PLANNING_SUBDIR = "planning"
SPEC_COMPLETENESS_FILE = "specification_completeness.md"
IMPLEMENTATION_PLAN_FILE = "IMPLEMENTATION_PLAN.md"
E2E_TEST_PLAN_FILE = "e2e-test-plan.md"

# Part F section header pattern (case-insensitive, allows for variations in formatting).
_PART_F_HEADER = re.compile(r"^#+\s*part\s*f\b", re.IGNORECASE | re.MULTILINE)
# Any "Part <letter>" heading — used to bound Part F to its own section instead of
# scanning to end-of-document (a later Part, or a summary line restating Part F,
# must not be read as the readiness declaration).
_PART_HEADER = re.compile(r"^#+\s*part\s*[a-z]\b", re.IGNORECASE | re.MULTILINE)
_INTEGRATION_READY_TOKEN = re.compile(
    r"integration[_\s-]*tests?[_\s-]*ready", re.IGNORECASE
)

# The authoritative "**Integration Readiness:** <token>" declaration. Anchored to a
# single line (^...$, MULTILINE) so a prose sentence that merely mentions the label
# — before or after the real field — can never be captured instead of it.
_READINESS_FIELD = re.compile(
    r"^\s*\**\s*integration\s+readiness[\s*:]{1,12}"
    r"(?:(?P<ready>integration[_\s-]*tests?[_\s-]*ready)"
    r"|(?P<not_ready>local[_\s-]*only|not[_\s-]*ready))"
    r"[\s*.]{0,6}$",
    re.IGNORECASE | re.MULTILINE,
)
# Detects an attempted (but unparseable) declaration — present so callers can refuse
# rather than guess, instead of silently falling back to a whole-section token scan.
_READINESS_LABEL = re.compile(r"integration\s+readiness", re.IGNORECASE)


def _normalize_stem(filename: str) -> str:
    """Match backend contract_validator stem normalization (case + separators)."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return re.sub(r"[\s_\-]+", "", stem).lower()


def _find_md_by_stem(directory: Path, canonical_name: str) -> Path | None:
    """Return a single ``.md`` under ``directory`` whose stem matches ``canonical_name``."""
    if not directory.is_dir():
        return None
    target = _normalize_stem(canonical_name)
    matches: list[Path] = []
    for entry in directory.iterdir():
        if entry.is_file() and entry.suffix.lower() == ".md":
            if _normalize_stem(entry.name) == target:
                matches.append(entry)
    if len(matches) == 1:
        return matches[0]
    return None


def _find_required_md(
    outputs_root: Path,
    subdir: str,
    canonical_name: str,
) -> Path | None:
    """Find a required markdown file in canonical subdir, outputs root, or sibling subdir."""
    canonical = outputs_root / subdir / canonical_name
    if canonical.is_file():
        return canonical
    direct = _find_md_by_stem(outputs_root, canonical_name)
    if direct is not None:
        return direct
    return _find_md_by_stem(outputs_root / subdir, canonical_name)


def _part_f_section(analysis_text: str) -> str | None:
    """Return the text of the Part F section only, or None if absent.

    Bounded at the next "Part <letter>" heading (or end of document if there is
    none) so a later section — or a summary line elsewhere that merely restates
    "Part F (Integration Readiness): ..." — is never scanned as part of Part F.
    """
    match = _PART_F_HEADER.search(analysis_text)
    if match is None:
        return None
    next_part = _PART_HEADER.search(analysis_text, match.end())
    end = next_part.start() if next_part is not None else len(analysis_text)
    return analysis_text[match.start():end]


def _readiness_field_is_ambiguous(part_f: str) -> bool:
    """True if Part F attempts an "Integration Readiness" declaration that doesn't
    parse to a recognized token — refuse rather than guess (see `precheck`)."""
    return _READINESS_LABEL.search(part_f) is not None and _READINESS_FIELD.search(part_f) is None


def _is_integration_tests_ready(analysis_text: str) -> bool:
    part_f = _part_f_section(analysis_text)
    if part_f is None:
        return False
    field_match = _READINESS_FIELD.search(part_f)
    if field_match is not None:
        return field_match.group("ready") is not None
    return bool(_INTEGRATION_READY_TOKEN.search(part_f))


@dataclass
class Rejection:
    """Structured rejection returned to the IDE. The `message` field is user-facing."""

    code: RejectionCode
    message: str
    missing_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = {"error": self.message, "code": self.code.value}
        if self.missing_files:
            result["missing_files"] = self.missing_files
        return result


def precheck(
    project_root: Path,
    spec_dir: str,
    outputs_dir: str,
) -> Rejection | None:
    """Run the MCP-side precheck. Returns None if all required files are present,
    or a Rejection describing the first failure encountered.

    Args:
        project_root: Absolute path to the user's project root (parent of spec_dir).
        spec_dir: Spec directory name as the user passed it to run_generation.
        outputs_dir: Outputs directory name as the user passed it to run_generation.
    """
    spec_dir_path = project_root / spec_dir
    if not spec_dir_path.exists() or not spec_dir_path.is_dir() or not any(spec_dir_path.iterdir()):
        return Rejection(
            code=RejectionCode.SPEC_DIR_MISSING,
            message=f"No specs found at `{spec_dir}/`. Add your specification files there and try again.",
        )

    outputs_path = project_root / outputs_dir
    if not outputs_path.exists() or not outputs_path.is_dir():
        return Rejection(
            code=RejectionCode.OUTPUTS_DIR_MISSING,
            message=(
                f"No `{outputs_dir}/` directory found. "
                f"Run `check_specification_completeness` and `run_planning` first to produce the required files."
            ),
        )

    analysis_file = _find_required_md(outputs_path, ANALYSIS_SUBDIR, SPEC_COMPLETENESS_FILE)
    if analysis_file is None:
        return Rejection(
            code=RejectionCode.ANALYSIS_MISSING,
            message=(
                f"Missing analysis file `{outputs_dir}/{ANALYSIS_SUBDIR}/{SPEC_COMPLETENESS_FILE}`. "
                f"Run `check_specification_completeness` to produce it."
            ),
            missing_files=[f"{outputs_dir}/{ANALYSIS_SUBDIR}/{SPEC_COMPLETENESS_FILE}"],
        )

    plan_file = _find_required_md(outputs_path, PLANNING_SUBDIR, IMPLEMENTATION_PLAN_FILE)
    if plan_file is None:
        return Rejection(
            code=RejectionCode.PLAN_MISSING,
            message=(
                f"Missing implementation plan `{outputs_dir}/{PLANNING_SUBDIR}/{IMPLEMENTATION_PLAN_FILE}`. "
                f"Run `run_planning` to produce it."
            ),
            missing_files=[f"{outputs_dir}/{PLANNING_SUBDIR}/{IMPLEMENTATION_PLAN_FILE}"],
        )

    # If analysis is marked INTEGRATION_TESTS_READY, the e2e plan is mandatory.
    try:
        analysis_text = analysis_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Rejection(
            code=RejectionCode.ANALYSIS_UNREADABLE,
            message=(
                f"Couldn't read `{outputs_dir}/{ANALYSIS_SUBDIR}/{SPEC_COMPLETENESS_FILE}` ({exc}). "
                f"Re-run `check_specification_completeness`."
            ),
        )

    if not _PART_F_HEADER.search(analysis_text):
        return Rejection(
            code=RejectionCode.ANALYSIS_UNREADABLE,
            message=(
                f"Couldn't read integration readiness from `{SPEC_COMPLETENESS_FILE}`. "
                f"Re-run `check_specification_completeness` — the file is missing Part F."
            ),
        )

    part_f = _part_f_section(analysis_text)
    if part_f is not None and _readiness_field_is_ambiguous(part_f):
        return Rejection(
            code=RejectionCode.ANALYSIS_UNREADABLE,
            message=(
                f"Couldn't read integration readiness from `{SPEC_COMPLETENESS_FILE}`. "
                f"Part F declares an Integration Readiness field, but its value isn't "
                f"`INTEGRATION_TESTS_READY` or `LOCAL_ONLY`. Re-run "
                f"`check_specification_completeness`."
            ),
        )

    if _is_integration_tests_ready(analysis_text):
        e2e_plan_file = _find_required_md(outputs_path, PLANNING_SUBDIR, E2E_TEST_PLAN_FILE)
        if e2e_plan_file is None:
            return Rejection(
                code=RejectionCode.E2E_PLAN_MISSING,
                message=(
                    f"Your analysis is marked `INTEGRATION_TESTS_READY` but "
                    f"`{outputs_dir}/{PLANNING_SUBDIR}/{E2E_TEST_PLAN_FILE}` is missing. "
                    f"Re-run `run_planning` to produce it, or update the analysis to `LOCAL_ONLY`."
                ),
                missing_files=[f"{outputs_dir}/{PLANNING_SUBDIR}/{E2E_TEST_PLAN_FILE}"],
            )

    return None
