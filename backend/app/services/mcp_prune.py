"""
Spec-driven MCP pruning: keyword scan over specs + analysis artifacts.

``prune_enabled_mcps_keyword_only`` runs during contract validation (before KB init)
to narrow session-wide Playwright/Figma enablement. Keyword matching replicates the
old ``MCP_PRUNE_USE_LLM=false`` path; the LLM prune agent is not used in the local
analysis/planning workflow.

Also provides ``resolve_enabled_mcps_and_set_telemetry`` for generation run handlers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Optional, Tuple

from app.core.artifact_files import SPEC_INDEX_FILE
from app.core.artifact_subdirs import ANALYSIS_SUBDIR
from app.core.config import SUPPORTED_MCPS, Settings
from app.core.mcp_config import EnabledMcpsResolution, resolve_enabled_mcps_detailed
from app.core.telemetry_context import TelemetryContext
from app.prompts.mcp_workflow_registry import prune_keywords_raw_from_settings
from app.services.skip_mode_mock import is_skip_mode_enabled
from app.schemas.mcp_prune_agent import McpPruneAgentJson
from app.services.json_llm_repair import parse_json_with_pydantic_and_repair

logger = logging.getLogger(__name__)

SPEC_TEXT_SUFFIXES = {".md", ".txt", ".markdown"}


def resolve_enabled_mcps_and_set_telemetry(
    *,
    form_value: Optional[str],
    generation_session_parameters: Optional[dict],
    settings: Settings,
    prefer_stored: bool = False,
) -> EnabledMcpsResolution:
    """
    Shared by specification analyze, planning, and generation run handlers:
    resolve Playwright/Figma enablement, then register on TelemetryContext.
    """
    resolution = resolve_enabled_mcps_detailed(
        form_value=form_value,
        generation_session_parameters=generation_session_parameters,
        settings=settings,
        prefer_stored=prefer_stored,
    )
    TelemetryContext.set_mcp_resolution(resolution)
    return resolution


@dataclass(frozen=True)
class McpPruneOutcome:
    """Result of parsing + applying the prune agent JSON output."""

    enabled: FrozenSet[str]
    reasons: Dict[str, str]


@dataclass(frozen=True)
class McpPruneAgentParseResult:
    """Parse + optional JSON repair outcome for the MCP prune LLM."""

    outcome: Optional[McpPruneOutcome]
    preserve_candidate_mcps: bool


def parse_keyword_csv(raw: str) -> Tuple[str, ...]:
    """Split comma-separated keywords; normalize to lowercase stripped tokens."""
    if not raw or not str(raw).strip():
        return ()
    parts = []
    for p in str(raw).split(","):
        t = p.strip().lower()
        if t:
            parts.append(t)
    return tuple(dict.fromkeys(parts))  # dedupe, preserve order


def keywords_for_mcp(settings: Settings, mcp_id: str) -> Tuple[str, ...]:
    """Return configured keyword tuple for a supported MCP id."""
    return parse_keyword_csv(prune_keywords_raw_from_settings(settings, mcp_id))


def _iter_spec_text_files(
    root: Path,
    spec_rel: str,
    outputs_rel: str,
    index_filename: str,
) -> Iterator[Path]:
    """Yield spec-tree and analysis markdown files used for keyword MCP pruning."""
    root = root.resolve()
    out_part = outputs_rel.strip("./").replace("\\", "/").strip("/")
    seen: set[Path] = set()

    def _yield_file(path: Path) -> Iterator[Path]:
        resolved = path.resolve()
        if resolved.is_file() and resolved not in seen:
            seen.add(resolved)
            yield resolved

    for index_path in (
        root / out_part / index_filename,
        root / out_part / ANALYSIS_SUBDIR / index_filename,
    ):
        yield from _yield_file(index_path)

    analysis_dir = root / out_part / ANALYSIS_SUBDIR
    if analysis_dir.is_dir():
        for path in sorted(analysis_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in SPEC_TEXT_SUFFIXES:
                yield from _yield_file(path)

    spec_dir = root / spec_rel.strip("./").replace("\\", "/")
    if spec_dir.is_dir():
        for path in sorted(spec_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in SPEC_TEXT_SUFFIXES:
                yield from _yield_file(path)


def scan_mcp_keyword_evidence(
    isolated_root: Path,
    spec_rel: str,
    outputs_rel: str,
    index_filename: str,
    candidate: FrozenSet[str],
    settings: Settings,
) -> Tuple[Dict[str, bool], Dict[str, int], str]:
    """
    Grep-style scan: for each line, test keywords per candidate MCP.
    Returns (hits, line_counts_per_mcp, evidence_block).
    """
    hits: Dict[str, bool] = {m: False for m in candidate}
    counts: Dict[str, int] = {m: 0 for m in candidate}
    evidence_lines: List[str] = []
    total_evidence_lines = 0
    max_total = settings.MCP_PRUNE_GREP_MAX_LINES_TOTAL
    max_per = settings.MCP_PRUNE_GREP_MAX_LINES_PER_MCP

    kw_cache = {m: keywords_for_mcp(settings, m) for m in candidate}

    for path in _iter_spec_text_files(isolated_root, spec_rel, outputs_rel, index_filename):
        rel = path.relative_to(isolated_root.resolve())
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("mcp_prune: skip file %s: %s", path, e)
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            for m in candidate:
                kws = kw_cache[m]
                if not kws:
                    continue
                if any(k in low for k in kws):
                    hits[m] = True
                    if counts[m] < max_per and total_evidence_lines < max_total:
                        counts[m] += 1
                        total_evidence_lines += 1
                        evidence_lines.append(
                            f"[{m}] {rel.as_posix()}:{lineno}: {line.strip()[:400]}"
                        )

    block = "\n".join(evidence_lines)
    max_chars = settings.MCP_PRUNE_GREP_MAX_CHARS
    if len(block) > max_chars:
        block = block[: max_chars - 80] + "\n[TRUNCATED grep evidence]\n"
    return hits, counts, block


def apply_keyword_scan_to_candidates(
    candidate: FrozenSet[str],
    hits: Dict[str, bool],
) -> Tuple[FrozenSet[str], Dict[str, str]]:
    """
    Keep MCP m in candidate only if hits.get(m).
    If nothing would remain, fall back to full candidate (conservative).
    """
    if not candidate:
        return frozenset(), {}
    kept = frozenset(m for m in candidate if hits.get(m, False))
    if not kept:
        logger.info("mcp_prune: no keyword hits for any candidate MCP; keeping full candidate")
        return candidate, {m: "No keyword hits; fallback to full candidate." for m in candidate}
    reasons = {
        m: "Spec/index lines matched configured keywords for this agent MCP."
        for m in sorted(kept)
    }
    return kept, reasons


async def parse_mcp_prune_agent_json(
    agent_text: str,
    *,
    settings: Optional[Settings] = None,
    log: Optional[logging.Logger] = None,
) -> McpPruneAgentParseResult:
    """
    Parse MCP prune agent output: Pydantic-validated JSON, optional OpenRouter repair (LLM_LOW; see json_llm_repair).

    ``preserve_candidate_mcps`` is True when repair ran but returned empty text — caller should keep the
    full candidate MCP set instead of keyword-only fallback.
    """
    lg = log or logger
    res = await parse_json_with_pydantic_and_repair(
        agent_text,
        McpPruneAgentJson,
        settings,
        log=lg,
    )
    if res.value is not None:
        return McpPruneAgentParseResult(
            outcome=McpPruneOutcome(enabled=frozenset(res.value.enabled), reasons=dict(res.value.reasons)),
            preserve_candidate_mcps=False,
        )
    lg.warning("mcp_prune: could not parse agent JSON (with optional repair)")
    return McpPruneAgentParseResult(outcome=None, preserve_candidate_mcps=res.repair_returned_empty)


def apply_mcp_prune(
    candidate: FrozenSet[str],
    parsed: Optional[McpPruneOutcome],
) -> Tuple[FrozenSet[str], Dict[str, str]]:
    """
    Intersect parsed.enabled with candidate ∩ SUPPORTED_MCPS.
    If parsed is None or enabled empty after intersect → return candidate (fallback).
    """
    if not candidate:
        return frozenset(), {}
    if parsed is None:
        return candidate, {}
    effective = frozenset(
        n for n in parsed.enabled if n in candidate and n in SUPPORTED_MCPS
    )
    if not effective:
        logger.info("mcp_prune: empty effective set after validation, keeping full candidate")
        return candidate, {}
    reasons = {k: v for k, v in parsed.reasons.items() if k in effective}
    return effective, reasons


def prune_enabled_mcps_keyword_only(
    *,
    settings: Settings,
    logger: logging.Logger,
    workspace_root: Path,
    spec_path: str,
    outputs_dir: str,
    candidate_enabled_mcps: FrozenSet[str],
) -> Tuple[FrozenSet[str], Dict[str, str], bool]:
    """
    Narrow session-wide optional MCPs using keyword evidence only (no LLM).

    Scans ``specification_index.md`` (canonical ``analysis/`` path or legacy root),
    other files under ``outputs_dir/analysis/``, and all text specs under ``spec_path``.

    Returns ``(effective_mcps, reasons_by_id, should_persist_to_parameters)``.
    When no keyword hits match any candidate, returns the full candidate set
    (conservative fallback — same as legacy keyword-only prune).
    """
    if not settings.MCP_AUTO_PRUNE_ENABLED:
        logger.info("mcp_prune: MCP_AUTO_PRUNE_ENABLED is False; skipping")
        return candidate_enabled_mcps, {}, False
    if is_skip_mode_enabled():
        logger.info("[SKIP_MODE] MCP keyword prune skipped")
        return candidate_enabled_mcps, {}, False
    if not candidate_enabled_mcps:
        logger.info("mcp_prune: no candidate MCPs; skipping")
        return frozenset(), {}, False

    hits, _counts, _evidence = scan_mcp_keyword_evidence(
        isolated_root=workspace_root,
        spec_rel=spec_path,
        outputs_rel=outputs_dir,
        index_filename=SPEC_INDEX_FILE,
        candidate=candidate_enabled_mcps,
        settings=settings,
    )
    effective, reasons = apply_keyword_scan_to_candidates(candidate_enabled_mcps, hits)
    logger.info(
        "mcp_prune (keyword-only, contract): candidate=%s effective=%s hits=%s",
        sorted(candidate_enabled_mcps),
        sorted(effective),
        {m: hits.get(m) for m in candidate_enabled_mcps},
    )
    return effective, reasons, True
