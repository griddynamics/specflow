"""Deterministic contract validator for uploaded user artifacts.

Layer 2 of the rejection gate described in CLAUDE.md "run_generation rejection contract".
The MCP-side precheck (Layer 1) catches obvious missing-file cases in the IDE before
upload. This module runs on the backend after upload, immediately before KB init,
and:

  1. Fuzzy-matches required filenames (case-insensitive, separator-normalized) and
     moves them to canonical paths under ``<outputs_dir>/{analysis,planning}/``.
  2. Verifies analysis Part F is parseable.
  3. Keyword-only MCP prune: narrows session ``mcp_servers_enabled`` from spec/index
     evidence (see ``mcp_prune.prune_enabled_mcps_keyword_only``) before KB init.
  4. Triggers markdown→JSON conversion of the implementation plan (and e2e plan if
     analysis says ``INTEGRATION_TESTS_READY``) so the rest of the generation workflow
     has structured plan data in Firestore.
  5. On any failure, raises ``ContractRejection`` with a code from the rejection
     catalog. ``run_generation`` translates this into a short user-facing message.

This is intentionally NOT a state machine ``fail()`` — the orchestrator re-raises
``ContractRejection`` without advancing checkpoint, and the API handler calls
``reject_contract()`` (RUNNING → PENDING, workspaces released) so the user can fix
files and retry.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.core.artifact_files import (
    E2E_TEST_PLAN_FILE,
    IMPLEMENTATION_PLAN_FILE,
    SPEC_COMPLETENESS_FILE,
)
from app.core.artifact_subdirs import ANALYSIS_SUBDIR, PLANNING_SUBDIR
from app.schemas.specification import SpecReadiness


class RejectionCode(StrEnum):
    """Rejection codes — must match the catalog in CLAUDE.md exactly.

    Layer 1 (MCP precheck) uses: SPEC_DIR_MISSING, OUTPUTS_DIR_MISSING,
    ANALYSIS_MISSING, PLAN_MISSING, E2E_PLAN_MISSING, ANALYSIS_UNREADABLE,
    GENERATION_ALREADY_RUNNING.
    Layer 2 (backend contract validator) uses: ANALYSIS_MISSING, PLAN_MISSING,
    E2E_PLAN_MISSING, AMBIGUOUS_FILE, ANALYSIS_UNREADABLE, PLAN_NO_PHASES,
    PLAN_UNPARSEABLE, E2E_PLAN_UNPARSEABLE.
    Both layers share this enum to keep the full catalog in one place.
    The run_generation entrance additionally raises MODEL_UNAVAILABLE and
    SANDBOX_UNAVAILABLE (the latter only when BACKEND_RUNTIME=process).
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
    SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"


@dataclass
class ContractRejection(Exception):
    """Raised by ``validate_contract`` when the uploaded artifacts can't satisfy the contract."""

    code: RejectionCode
    message: str
    missing_files: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover — Exception protocol
        return self.message

    def to_dict(self) -> dict:
        result = {"error": self.message, "code": self.code.value}
        if self.missing_files:
            result["missing_files"] = self.missing_files
        if self.ambiguous:
            result["ambiguous"] = self.ambiguous
        return result


@dataclass(frozen=True)
class _CanonicalFile:
    """Description of a canonical file the validator looks for."""

    subdir: str
    name: str
    rejection_code: RejectionCode
    mcp_tool: str

    @property
    def canonical_path(self) -> str:
        return f"{self.subdir}/{self.name}"


_ANALYSIS_FILE = _CanonicalFile(
    subdir=ANALYSIS_SUBDIR,
    name=SPEC_COMPLETENESS_FILE,
    rejection_code=RejectionCode.ANALYSIS_MISSING,
    mcp_tool="check_specification_completeness",
)
_PLAN_FILE = _CanonicalFile(
    subdir=PLANNING_SUBDIR,
    name=IMPLEMENTATION_PLAN_FILE,
    rejection_code=RejectionCode.PLAN_MISSING,
    mcp_tool="run_planning",
)
_E2E_PLAN_FILE = _CanonicalFile(
    subdir=PLANNING_SUBDIR,
    name=E2E_TEST_PLAN_FILE,
    rejection_code=RejectionCode.E2E_PLAN_MISSING,
    mcp_tool="run_planning",
)


# Part F section header pattern — same regex as MCP precheck for behavior parity.
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
# Heading match is intentionally lenient (any heading level h1–h6) so the
# deterministic preflight never rejects a plan the downstream LLM conversion agent
# would accept. The skill emits `## Phase N:`, but a hand-edited `# Phase N` must
# still pass — the preflight only guards against the obviously-broken shapes
# (no phase headings at all, or headings with no body).
_IMPLEMENTATION_PHASE_HEADER = re.compile(
    r"^#{1,6}\s*phase\s+\d+\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_E2E_ROUND_HEADER = re.compile(
    r"^#{1,6}\s*(?:round|phase)\s+\d+\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


def _normalize_for_match(filename: str) -> str:
    """Normalize a filename for fuzzy matching.

    Rules (kept tight, not loose):
      - case-insensitive (lowercased)
      - ``_``, ``-``, and space treated as equivalent separators (all stripped)
      - extension must remain ``.md`` (caller pre-filters)
    """
    stem = filename.rsplit(".", 1)[0]
    return re.sub(r"[\s_\-]+", "", stem).lower()


def _find_candidates(
    outputs_root: Path,
    canonical: _CanonicalFile,
) -> list[Path]:
    """Find files whose normalized stem matches the canonical filename.

    Searches both the canonical subdirectory and the outputs_dir root, since users
    occasionally drop files at the wrong level.
    """
    target_stem = _normalize_for_match(canonical.name)
    candidates: list[Path] = []
    search_dirs = list(dict.fromkeys([
        outputs_root,
        outputs_root / ANALYSIS_SUBDIR,
        outputs_root / PLANNING_SUBDIR,
    ]))
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if not entry.is_file() or entry.suffix.lower() != ".md":
                continue
            if _normalize_for_match(entry.name) == target_stem:
                candidates.append(entry)
    return candidates


def _resolve_to_canonical_path(
    outputs_root: Path,
    canonical: _CanonicalFile,
    required: bool,
    outputs_dir_label: str,
    logger: logging.Logger,
) -> Path | None:
    """Locate a canonical file under outputs_root, moving it into place if found in
    a non-canonical location. Returns the canonical Path on success, None if absent
    and not required, or raises ContractRejection.
    """
    canonical_path = outputs_root / canonical.subdir / canonical.name
    candidates = _find_candidates(outputs_root, canonical)

    # If the canonical file already exists, that's the winner — extra candidates with
    # different cases/separators are tolerated as long as one exact-canonical match exists.
    exact_match = next((c for c in candidates if c == canonical_path), None)
    if exact_match is not None:
        return canonical_path

    if not candidates:
        if required:
            label = f"{outputs_dir_label}/{canonical.canonical_path}"
            raise ContractRejection(
                code=canonical.rejection_code,
                message=(
                    f"Missing required file `{label}`. "
                    f"Run `{canonical.mcp_tool}` to produce it."
                ),
                missing_files=[label],
            )
        return None

    if len(candidates) > 1:
        labels = sorted(str(c.relative_to(outputs_root)) for c in candidates)
        raise ContractRejection(
            code=RejectionCode.AMBIGUOUS_FILE,
            message=(
                f"Found multiple candidates for `{canonical.name}`: "
                + ", ".join(f"`{outputs_dir_label}/{lbl}`" for lbl in labels)
                + ". Keep only one and delete the others."
            ),
            ambiguous=[f"{outputs_dir_label}/{lbl}" for lbl in labels],
        )

    # Exactly one near-match — move it to the canonical location.
    source = candidates[0]
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "contract_validator: normalizing %s → %s",
        source.relative_to(outputs_root),
        canonical_path.relative_to(outputs_root),
    )
    source.replace(canonical_path)
    return canonical_path


def normalize_contract_files(
    workspace_root: Path,
    outputs_dir: str,
    logger: logging.Logger,
) -> dict[str, Path]:
    """Find and normalize all required contract files in the uploaded workspace.

    Returns a dict with keys ``analysis``, ``plan``, and optionally ``e2e_plan``
    mapping to the canonical Path of each file (the e2e plan is included only when
    analysis says ``INTEGRATION_TESTS_READY``).

    Raises ContractRejection with one of the catalog codes on any failure.
    """
    outputs_root = workspace_root / outputs_dir
    label = outputs_dir

    analysis_path = _resolve_to_canonical_path(
        outputs_root, _ANALYSIS_FILE, required=True, outputs_dir_label=label, logger=logger
    )
    if analysis_path is None:
        raise RuntimeError("analysis_path is None after required=True — this is a bug in _resolve_to_canonical_path")

    plan_path = _resolve_to_canonical_path(
        outputs_root, _PLAN_FILE, required=True, outputs_dir_label=label, logger=logger
    )
    if plan_path is None:
        raise RuntimeError("plan_path is None after required=True — this is a bug in _resolve_to_canonical_path")

    # Read analysis to determine integration readiness — Part F must be present and
    # parseable, otherwise we refuse rather than guess.
    try:
        analysis_text = analysis_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ContractRejection(
            code=RejectionCode.ANALYSIS_UNREADABLE,
            message=(
                f"Couldn't read `{label}/{_ANALYSIS_FILE.canonical_path}` ({exc}). "
                f"Re-run `check_specification_completeness`."
            ),
        ) from exc

    if not _PART_F_HEADER.search(analysis_text):
        raise ContractRejection(
            code=RejectionCode.ANALYSIS_UNREADABLE,
            message=(
                f"Couldn't read integration readiness from `{SPEC_COMPLETENESS_FILE}`. "
                f"Re-run `check_specification_completeness` — the file is missing Part F."
            ),
        )

    part_f = _part_f_section(analysis_text)
    if part_f is not None and _readiness_field_is_ambiguous(part_f):
        raise ContractRejection(
            code=RejectionCode.ANALYSIS_UNREADABLE,
            message=(
                f"Couldn't read integration readiness from `{SPEC_COMPLETENESS_FILE}`. "
                f"Part F declares an Integration Readiness field, but its value isn't "
                f"`INTEGRATION_TESTS_READY` or `LOCAL_ONLY`. Re-run "
                f"`check_specification_completeness`."
            ),
        )

    result: dict[str, Path] = {"analysis": analysis_path, "plan": plan_path}

    if is_integration_tests_ready(analysis_text):
        e2e_path = _resolve_to_canonical_path(
            outputs_root, _E2E_PLAN_FILE, required=True, outputs_dir_label=label, logger=logger
        )
        if e2e_path is None:
            raise RuntimeError("e2e_path is None after required=True — this is a bug in _resolve_to_canonical_path")
        result["e2e_plan"] = e2e_path

    return result


def _section_has_body(
    markdown: str,
    match: re.Match[str],
    heading_pattern: re.Pattern[str],
) -> bool:
    """Return True when a markdown heading has non-heading content after it."""
    next_heading = heading_pattern.search(markdown, pos=match.end())
    section_end = next_heading.start() if next_heading else len(markdown)
    body = markdown[match.end():section_end].strip()
    return bool(body)


def _validate_plan_markdown(
    plan_path: Path,
    *,
    heading_pattern: re.Pattern[str],
    no_phases_code: RejectionCode | None,
    unparsable_code: RejectionCode,
    unparsable_message: str,
    no_phases_message: str | None = None,
) -> None:
    try:
        markdown = plan_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ContractRejection(
            code=unparsable_code,
            message=unparsable_message,
        ) from exc

    phase_headings = list(heading_pattern.finditer(markdown))
    if not phase_headings:
        raise ContractRejection(
            code=no_phases_code or unparsable_code,
            message=no_phases_message or unparsable_message,
        )

    # Every phase must have body content. The error message promises "each phase has
    # a heading, description, and task list", so a single fleshed-out phase followed by
    # empty stubs must not slip through to allocation.
    if not all(_section_has_body(markdown, heading, heading_pattern) for heading in phase_headings):
        raise ContractRejection(
            code=unparsable_code,
            message=unparsable_message,
        )


def validate_preconverted_plan_contract(canonical_paths: dict[str, Path]) -> None:
    """Reject obviously invalid markdown plans before allocating workspaces.

    The later markdown-to-JSON agent can still fail for infrastructure reasons; those
    should be treated as workflow failures, not user contract rejections. User-fixable
    plan-shape errors must be caught here so first-run uploads can stop before session
    creation and workspace allocation.
    """
    _validate_plan_markdown(
        canonical_paths["plan"],
        heading_pattern=_IMPLEMENTATION_PHASE_HEADER,
        no_phases_code=RejectionCode.PLAN_NO_PHASES,
        unparsable_code=RejectionCode.PLAN_UNPARSEABLE,
        no_phases_message=(
            "Your implementation plan has no phases. Re-run `run_planning` — "
            "the plan must contain at least one phase."
        ),
        unparsable_message=(
            f"Couldn't parse `{_PLAN_FILE.name}` into phases. "
            "Re-run `run_planning` — check that each phase has a heading, "
            "description, and task list."
        ),
    )

    if "e2e_plan" in canonical_paths:
        _validate_plan_markdown(
            canonical_paths["e2e_plan"],
            heading_pattern=_E2E_ROUND_HEADER,
            no_phases_code=None,
            unparsable_code=RejectionCode.E2E_PLAN_UNPARSEABLE,
            unparsable_message=(
                f"Couldn't parse `{_E2E_PLAN_FILE.name}` into rounds. "
                "Re-run `run_planning` — check that each round has a heading "
                "and verification steps."
            ),
        )


def validate_generation_contract_preflight(
    workspace_root: Path,
    outputs_dir: str,
    logger: logging.Logger,
) -> dict[str, Path]:
    """Run all user-contract checks that may reject a generation upload."""
    canonical_paths = normalize_contract_files(workspace_root, outputs_dir, logger)
    validate_preconverted_plan_contract(canonical_paths)
    return canonical_paths


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
    parse to a recognized token — e.g. a paraphrase or a value split across
    decoration the field regex doesn't recognize.

    Distinguishes "field missing entirely" (legitimate non-standard formatting,
    left to the lenient fallback in ``is_integration_tests_ready``) from "field
    present but unreadable" (refuse rather than guess, per callers below).
    """
    return _READINESS_LABEL.search(part_f) is not None and _READINESS_FIELD.search(part_f) is None


def is_integration_tests_ready(analysis_text: str) -> bool:
    """Return True if the analysis file declares INTEGRATION_TESTS_READY in Part F.

    Only the Part F section is scanned. Within it, the "**Integration Readiness:**"
    field — anchored to its own line — is checked first, so neither a preamble
    sentence nor a Rationale sentence that merely mentions the other token can be
    captured in its place. If no such field is found at all (non-standard
    formatting with no labeled declaration), falls back to scanning the whole
    Part F section for the token. If a field IS attempted but doesn't parse, the
    caller is expected to have already rejected via ``_readiness_field_is_ambiguous``
    rather than reach this fallback.

    A document with no Part F header is treated as not-ready here; genuine "Part F
    missing" cases are rejected earlier in ``normalize_contract_files`` with a
    clearer ANALYSIS_UNREADABLE error.
    """
    part_f = _part_f_section(analysis_text)
    if part_f is None:
        return False
    field_match = _READINESS_FIELD.search(part_f)
    if field_match is not None:
        return field_match.group("ready") is not None
    return bool(_INTEGRATION_READY_TOKEN.search(part_f))


def parse_integration_readiness_from_file(completeness_file: Path):
    """Return the SpecReadiness label declared in Part F of the given completeness file.

    Returns ``SpecReadiness.INTEGRATION_TESTS_READY`` if the file exists and Part F
    contains the INTEGRATION_TESTS_READY token; otherwise ``SpecReadiness.LOCAL_ONLY``.
    Missing or unreadable files default to LOCAL_ONLY — the validator's own checks
    will catch genuine "analysis missing" cases earlier with a clearer error.
    """
    try:
        text = completeness_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return SpecReadiness.LOCAL_ONLY
    if is_integration_tests_ready(text):
        return SpecReadiness.INTEGRATION_TESTS_READY
    return SpecReadiness.LOCAL_ONLY
