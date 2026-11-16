"""Step ``investigate``: one ``Investigator`` call per site to investigate (docs/graph/gap/investigation.md).

SLICE-1 STUB. Session 06 replaces ``SeedInvestigator`` with the structured loop over the gateway (role
``gap_investigate``) and ``NO_READS`` with the read menu bound to the project and the run. Until then the run records
what the detectors found: ``SeedInvestigator`` makes no model call and turns a site's seeds into a ``gap`` report
(``seed_report``), and the site's pack carries the seeds only (``site_pack``).

A failed investigation (an ``error`` in the result, no report, or a ``GraphError`` raised by the investigator) never
fails the run: the check step defers the site to the next run (FR-GRUN-04).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Protocol

from graph.core.errors import GraphError
from graph.gap.models import (
    GapReport,
    InvestigationResult,
    PackSeed,
    ReadResult,
    SiteBudget,
    SitePack,
    WitnessRef,
)
from graph.gap.names import GapKind, ReportSeverity, RunMode
from graph.gap.ports import Investigator, SiteReads
from graph.gap.settings import GapLimits
from graph.gap.steps.sites import Site
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger(__name__)

SEED_STUB_VERSION: Final = "gap.seed_stub.1"
"""The prompt version of the slice-1 stub (part of every site's input hash, so session 06's loop re-investigates)."""
READS_UNAVAILABLE: Final = "reads_unavailable"
INVESTIGATOR_FAILED: Final = "investigator_failed"
TITLE_CHARS: Final = 160
TITLE_IDS: Final = 3
KIND_ORDER: Final[tuple[GapKind, ...]] = (
    "contradiction",
    "open_question",
    "missing",
    "inconsistency",
    "assumption",
    "ambiguous",
)
"""Which seed of a site names the report's kind (and detector): the most decisive kind first."""
MAJOR_KINDS: Final = frozenset({"contradiction", "open_question"})
_TITLES: Final[Mapping[str, str]] = {
    "missing": "Missing in the spec",
    "ambiguous": "Ambiguous in the spec",
    "contradiction": "Contradiction in the spec",
    "inconsistency": "Inconsistent in the spec",
    "open_question": "Open question in the spec",
    "assumption": "Unconfirmed assumption in the spec",
}


class _NoReads:
    """The read menu of slice 1: every read returns an empty result with ``reads_unavailable``."""

    async def _empty(self, read: str) -> ReadResult:
        return ReadResult(read=read, reason=READS_UNAVAILABLE)

    async def node(self, spec_id: str) -> ReadResult:
        del spec_id
        return await self._empty("node")

    async def evidence(self, spec_id: str) -> ReadResult:
        del spec_id
        return await self._empty("evidence")

    async def neighbours(self, spec_id: str, relations: Sequence[str] = ()) -> ReadResult:
        del spec_id, relations
        return await self._empty("neighbours")

    async def trace(self, spec_id: str, direction: str, depth: int) -> ReadResult:
        del spec_id, direction, depth
        return await self._empty("trace")

    async def paths(self, source: str, target: str) -> ReadResult:
        del source, target
        return await self._empty("paths")

    async def similar(self, query: str, limit: int) -> ReadResult:
        del query, limit
        return await self._empty("similar")

    async def find_nodes(self, words: str, labels: Sequence[str] = ()) -> ReadResult:
        del words, labels
        return await self._empty("find_nodes")

    async def find_fragments(self, words: str) -> ReadResult:
        del words
        return await self._empty("find_fragments")


NO_READS: Final[SiteReads] = _NoReads()


class _Ranked(Protocol):
    @property
    def strength(self) -> str: ...

    @property
    def kind(self) -> GapKind: ...

    @property
    def detector(self) -> str: ...


def primary_seed[S: _Ranked](seeds: Sequence[S]) -> S:
    """The seed that names a report's kind and detector: strong before weak, then ``KIND_ORDER``, then detector."""

    return min(seeds, key=lambda seed: (seed.strength != "strong", KIND_ORDER.index(seed.kind), seed.detector))


def seed_report(pack: SitePack) -> GapReport:
    """The slice-1 report of a site: a ``gap`` of the primary seed's kind, ``ABOUT`` every seed's nodes, ``IMPACTS``
    their context nodes, every quote and graph fact of the seeds as witness. Severity (a proposal the route step may
    change): ``major`` for a strong seed or a contradiction or stated question, ``minor`` otherwise, and ``info`` for
    a site whose seeds name no node and quote no line (a coverage fact such as MS-COVERAGE's: a ``gap_note`` until
    session 06's investigator chooses the nodes such a gap is about). The question of a
    stated question or an assumption is its quoted line; other kinds get a generic question naming the spec ids."""

    primary = primary_seed(pack.seeds)
    about = tuple(sorted({spec_id for seed in pack.seeds for spec_id in seed.spec_ids}))
    impacts = tuple(sorted({spec_id for seed in pack.seeds for spec_id in seed.context_ids} - set(about)))
    witness: list[WitnessRef] = []
    for index, seed in enumerate(pack.seeds):
        for item in seed.witness:
            ref = (
                WitnessRef(fact=index)
                if item.fact is not None
                else WitnessRef(fragment_id=item.fragment_id, quote=item.quote)
            )
            if ref not in witness:
                witness.append(ref)
    quote = next((item.quote for item in primary.witness if item.quote), None)
    named = ", ".join(about[:TITLE_IDS]) + (" and more" if len(about) > TITLE_IDS else "")
    if primary.kind == "open_question" and quote:
        question = quote
    elif primary.kind == "assumption" and quote:
        question = f"Is this assumption confirmed: {quote}"
    else:
        question = f"What must the spec state to settle this {primary.kind.replace('_', ' ')} about {named}?"
    severity: ReportSeverity = "major" if primary.strength == "strong" or primary.kind in MAJOR_KINDS else "minor"
    if not about and not any(ref.quote for ref in witness):
        severity = "info"  # nothing to anchor a Gap node to (GI1): a note until the investigator picks the nodes
    detectors = ", ".join(sorted({seed.detector for seed in pack.seeds}))
    return GapReport(
        verdict="gap",
        kind=primary.kind,
        severity=severity,
        title=f"{_TITLES[primary.kind]}: {named}"[:TITLE_CHARS],
        question=question,
        why=f"Found by the detectors {detectors} ({len(pack.seeds)} seeds).",
        about=about,
        impacts=impacts,
        witness=tuple(witness),
    )


class SeedInvestigator:
    """SLICE-1 STUB of the ``Investigator`` port (session 06 replaces it): no model call, no read; the report is
    ``seed_report`` of the pack."""

    prompt_version: Final = SEED_STUB_VERSION

    async def investigate(self, pack: SitePack, reads: SiteReads, budget: SiteBudget) -> InvestigationResult:
        del reads, budget
        if not pack.seeds:
            return InvestigationResult(error="site_without_seeds")
        return InvestigationResult(report=seed_report(pack))


def site_pack(project_id: str, site: Site, mode: RunMode) -> SitePack:
    """The pack of one site: its seeds (slice 1: no context parts yet)."""

    return SitePack(
        project_id=project_id,
        site_id=site.site_id,
        input_hash=site.input_hash,
        mode=mode,
        seeds=tuple(
            PackSeed(
                detector=seed.detector,
                kind=seed.kind,
                strength=seed.strength,
                spec_ids=seed.about,
                context_ids=seed.context,
                witness=seed.witness,
            )
            for seed in site.seeds
        ),
    )


def site_budget(limits: GapLimits) -> SiteBudget:
    return SiteBudget(
        rounds=limits.rounds_per_site, reads_per_round=limits.reads_per_round, context_tokens=limits.context_tokens
    )


async def investigate_sites(
    sites: Sequence[Site],
    investigator: Investigator,
    *,
    project_id: str,
    run_id: str,
    mode: RunMode,
    limits: GapLimits,
    reads: SiteReads = NO_READS,
) -> dict[str, InvestigationResult]:
    """Investigate each site in order; a raised ``GraphError`` becomes a result with its code (never raised)."""

    budget = site_budget(limits)
    results: dict[str, InvestigationResult] = {}
    for site in sites:
        try:
            result = await investigator.investigate(site_pack(project_id, site, mode), reads, budget)
        except GraphError as error:
            result = InvestigationResult(error=error.code or INVESTIGATOR_FAILED)
        if result.error is not None or result.report is None:
            log_event(
                logger,
                WARNING,
                "graph.gap.investigate.failed",
                project_id=project_id,
                run_id=run_id,
                site_id=site.site_id,
                code=result.error or "no_report",
            )
        results[site.site_id] = result
    log_event(
        logger,
        INFO,
        "graph.gap.investigate.finished",
        project_id=project_id,
        run_id=run_id,
        sites=len(sites),
        failed=sum(1 for result in results.values() if result.report is None),
    )
    return results
