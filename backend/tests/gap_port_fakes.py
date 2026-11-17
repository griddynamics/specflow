"""In-memory fakes of the ``graph.gap`` ports for offline tests (test support, not shipped code).

``MemoryGapRecords`` and ``MemoryAmendments`` keep rows per project id, so a test can show that one project never
sees another's rows (FR-GOWN-02). They follow the port semantics of the PostgreSQL adapters (``api.gap.records``);
``tests/gap_records_contract.py`` runs the same contract on both. ``ScriptedInvestigator`` returns prepared results
per site, ``SeedInvestigator`` turns a site's seeds into a ``gap`` report with two options and no model call
(``NO_READS`` answers every read empty), ``MemoryStoryHolds``, ``MemoryBuildRequests`` and ``MemoryPlanView``
record what the run asked of the plan and the build runner.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, get_args

from graph.core.errors import ConformanceError, GraphError
from graph.gap.models import (
    AmendmentRow,
    Edit,
    EditTarget,
    EventRow,
    GapReport,
    HoldResult,
    InvestigationResult,
    NoiseRuleRow,
    NoteRow,
    OptionRow,
    ReadResult,
    RelevanceRow,
    ReportOption,
    RunRow,
    SectionKey,
    SeedRow,
    SiteBudget,
    SitePack,
    SiteRow,
    SuppressionRow,
    TaskRef,
    VerdictRow,
    WitnessRef,
)
from graph.gap.names import AmendmentStatus, FinishedRunStatus, ReportSeverity
from graph.gap.ports import SiteReads
from graph.gap.steps.investigate import primary_seed


def _same_run(run_id: str, rows: Sequence[SeedRow] | Sequence[SiteRow]) -> None:
    if any(row.run_id != run_id for row in rows):
        raise ConformanceError("gap_rows_of_another_run", run_id=run_id)


def _ordered(rows: Any) -> tuple[AmendmentRow, ...]:
    return tuple(sorted(rows, key=lambda row: (row.created_at, row.amendment_id)))


class MemoryGapRecords:
    """``GapRecords`` over dictionaries keyed by project id."""

    def __init__(self) -> None:
        self.option_rows: dict[str, dict[str, tuple[OptionRow, ...]]] = defaultdict(dict)
        self.runs: dict[str, dict[str, RunRow]] = defaultdict(dict)
        self.seeds: dict[str, list[SeedRow]] = defaultdict(list)
        self.sites: dict[str, list[SiteRow]] = defaultdict(list)
        self.verdicts: dict[str, dict[str, VerdictRow]] = defaultdict(dict)
        self.relevance_rows: dict[str, dict[SectionKey, RelevanceRow]] = defaultdict(dict)
        self.noise: dict[str, list[NoiseRuleRow]] = defaultdict(list)
        self.suppressions: dict[str, dict[str, SuppressionRow]] = defaultdict(dict)
        self.notes: dict[str, list[NoteRow]] = defaultdict(list)
        self.event_rows: dict[str, list[EventRow]] = defaultdict(list)

    async def options(self, project_id: str, gap_id: str) -> Sequence[OptionRow]:
        return self.option_rows[project_id].get(gap_id, ())

    async def put_options(self, project_id: str, gap_id: str, rows: Sequence[OptionRow]) -> None:
        ranks = [row.rank for row in rows]
        if len(set(ranks)) != len(ranks) or sum(row.recommended for row in rows) > 1:
            raise ConformanceError("gap_options_invalid", gap_id=gap_id)
        if any(row.gap_id != gap_id for row in rows):
            raise ConformanceError("gap_options_invalid", gap_id=gap_id)
        self.option_rows[project_id][gap_id] = tuple(sorted(rows, key=lambda row: row.rank))

    async def start_run(self, project_id: str, run: RunRow) -> None:
        self.runs[project_id][run.run_id] = run

    async def finish_run(
        self,
        project_id: str,
        run_id: str,
        counts: Mapping[str, Any],
        cost: Mapping[str, Any],
        *,
        status: FinishedRunStatus = "completed",
    ) -> None:
        if status not in get_args(FinishedRunStatus):
            raise ConformanceError("gap_run_status_invalid", status=status)
        run = self.runs[project_id].get(run_id)
        if run is None:
            raise ConformanceError("gap_run_unknown", run_id=run_id)
        self.runs[project_id][run_id] = run.model_copy(
            update={"status": status, "finished_at": datetime.now(UTC), "counts": counts, "cost": cost}
        )

    async def put_seeds(self, project_id: str, run_id: str, seeds: Sequence[SeedRow]) -> None:
        _same_run(run_id, seeds)
        self.seeds[project_id].extend(seeds)

    async def put_sites(self, project_id: str, run_id: str, sites: Sequence[SiteRow]) -> None:
        _same_run(run_id, sites)
        for site in sites:
            kept = [row for row in self.sites[project_id] if (row.run_id, row.site_id) != (run_id, site.site_id)]
            self.sites[project_id] = [*kept, site]

    async def verdict(self, project_id: str, input_hash: str) -> VerdictRow | None:
        return self.verdicts[project_id].get(input_hash)

    async def put_verdict(self, project_id: str, row: VerdictRow) -> None:
        self.verdicts[project_id][row.input_hash] = row

    async def relevance(self, project_id: str, keys: Sequence[SectionKey]) -> Mapping[SectionKey, RelevanceRow]:
        rows = self.relevance_rows[project_id]
        return {key: rows[key] for key in keys if key in rows}

    async def put_relevance(self, project_id: str, rows: Sequence[RelevanceRow]) -> None:
        for row in rows:
            self.relevance_rows[project_id][row.key] = row

    async def noise_rules(self, project_id: str) -> Sequence[NoiseRuleRow]:
        return tuple(sorted(self.noise[project_id], key=lambda row: (row.created_at, row.rule_id)))

    async def put_noise_rule(self, project_id: str, row: NoiseRuleRow) -> None:
        kept = [rule for rule in self.noise[project_id] if rule.rule_id != row.rule_id]
        self.noise[project_id] = [*kept, row]

    async def delete_noise_rule(self, project_id: str, rule_id: str) -> bool:
        kept = [rule for rule in self.noise[project_id] if rule.rule_id != rule_id]
        found = len(kept) != len(self.noise[project_id])
        self.noise[project_id] = kept
        return found

    async def suppressed(self, project_id: str, signatures: Sequence[str]) -> frozenset[str]:
        return frozenset(signature for signature in signatures if signature in self.suppressions[project_id])

    async def suppress(self, project_id: str, row: SuppressionRow) -> None:
        self.suppressions[project_id][row.signature] = row

    async def unsuppress(self, project_id: str, signature: str) -> None:
        self.suppressions[project_id].pop(signature, None)

    async def put_notes(self, project_id: str, run_id: str | None, notes: Sequence[NoteRow]) -> None:
        self.notes[project_id].extend(note.model_copy(update={"run_id": run_id}) for note in notes)

    async def log(self, project_id: str, events: Sequence[EventRow]) -> None:
        self.event_rows[project_id].extend(events)

    async def events(self, project_id: str, gap_id: str) -> Sequence[EventRow]:
        found = (row for row in self.event_rows[project_id] if row.gap_id == gap_id)
        return tuple(sorted(found, key=lambda row: row.at))


class MemoryAmendments:
    """``Amendments`` over dictionaries keyed by project id."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, AmendmentRow]] = defaultdict(dict)

    async def for_lineage(
        self, project_id: str, lineage: str, *, statuses: Sequence[str] = ("active",)
    ) -> Sequence[AmendmentRow]:
        return _ordered(
            row for row in self.rows[project_id].values() if row.lineage == lineage and row.status in statuses
        )

    async def for_clarification(self, project_id: str, clr_id: str) -> Sequence[AmendmentRow]:
        return _ordered(row for row in self.rows[project_id].values() if row.clr_id == clr_id)

    async def put(self, project_id: str, rows: Sequence[AmendmentRow]) -> None:
        for row in rows:
            self.rows[project_id][row.amendment_id] = row

    async def set_status(
        self,
        project_id: str,
        amendment_ids: Sequence[str],
        status: str,
        *,
        revision_id: str | None = None,
    ) -> None:
        if status not in get_args(AmendmentStatus):
            raise ConformanceError("gap_amendment_status_unknown", status=status)
        for amendment_id in amendment_ids:
            row = self.rows[project_id].get(amendment_id)
            if row is None:
                continue  # only the project's own amendments change
            update: dict[str, Any] = {"status": status, "updated_at": datetime.now(UTC)}
            if revision_id is not None:
                update["last_revision_id"] = revision_id
            self.rows[project_id][amendment_id] = row.model_copy(update=update)

    async def delete(self, project_id: str, amendment_ids: Sequence[str]) -> None:
        for amendment_id in amendment_ids:
            self.rows[project_id].pop(amendment_id, None)

    async def pending_lineages(self, project_id: str) -> Sequence[str]:
        return tuple(
            sorted({row.lineage for row in self.rows[project_id].values() if row.status in ("active", "undecided")})
        )


class ScriptedInvestigator:
    """``Investigator`` that returns the prepared result of each site (by site id), or a failure code."""

    def __init__(self, results: Mapping[str, InvestigationResult] | None = None) -> None:
        self.results = dict(results or {})
        self.seen: list[tuple[str, SiteBudget]] = []

    async def investigate(self, pack: SitePack, reads: SiteReads, budget: SiteBudget) -> InvestigationResult:
        del reads
        self.seen.append((pack.site_id, budget))
        return self.results.get(pack.site_id, InvestigationResult(error="no_script"))


class MemoryStoryHolds:
    """``StoryHolds`` that holds the tasks of ``tasks`` (spec id to task ids); ``in_progress`` tasks are left alone. A
    block holds the story's id as its one task; ``released`` lists every release call (project id, gap id)."""

    def __init__(
        self,
        tasks: Mapping[str, Sequence[str]] | None = None,
        in_progress: Sequence[str] = (),
    ) -> None:
        self.tasks = {spec_id: tuple(ids) for spec_id, ids in (tasks or {}).items()}
        self.in_progress = frozenset(in_progress)
        self.holds: dict[tuple[str, str], tuple[str, ...]] = {}
        self.blocks: list[tuple[str, str, str, str]] = []
        self.reasons: dict[tuple[str, str], str] = {}
        self.released: list[tuple[str, str]] = []
        self.refuse: GraphError | None = None
        self.refuse_block: GraphError | None = None

    async def hold_tasks(
        self, project_id: str, gap_id: str, spec_ids: Sequence[str], *, reason: str = "spec_ambiguous"
    ) -> HoldResult:
        found = sorted({task for spec_id in spec_ids for task in self.tasks.get(spec_id, ())})
        held = tuple(task for task in found if task not in self.in_progress)
        self.holds[(project_id, gap_id)] = (*self.holds.get((project_id, gap_id), ()), *held)
        self.reasons[(project_id, gap_id)] = reason
        return HoldResult(
            held=held,
            in_progress=tuple(task for task in found if task in self.in_progress),
        )

    async def release_gap(self, project_id: str, gap_id: str) -> Sequence[str]:
        self.released.append((project_id, gap_id))
        return self.holds.pop((project_id, gap_id), ())

    async def check_block(
        self,
        project_id: str,
        story_id: str,
        *,
        claimed_by: str,
        claim_token: str,
        spec_ids: Sequence[str],
    ) -> None:
        del project_id, story_id, claimed_by, claim_token, spec_ids
        if self.refuse is not None:
            raise self.refuse

    async def block_story(
        self,
        project_id: str,
        story_id: str,
        *,
        claim_token: str,
        reason: str,
        finding_id: str,
    ) -> Any:
        del claim_token
        if self.refuse_block is not None:
            raise self.refuse_block
        self.blocks.append((project_id, story_id, reason, finding_id))
        self.holds[(project_id, finding_id)] = (story_id,)
        return story_id


class MemoryBuildRequests:
    """``BuildRequests`` that records each request and returns a build id."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bool]] = []

    async def request_update_build(self, project_id: str, reason: str, *, immediate: bool) -> str:
        self.requests.append((project_id, reason, immediate))
        return f"build-{len(self.requests)}"


class MemoryPlanView:
    """``PlanView`` over prepared tasks per spec id (one plan for every project)."""

    def __init__(self, tasks: Mapping[str, Sequence[TaskRef]] | None = None) -> None:
        self.tasks = {spec_id: tuple(refs) for spec_id, refs in (tasks or {}).items()}

    async def tasks_for(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, Sequence[TaskRef]]:
        del project_id
        return {spec_id: self.tasks[spec_id] for spec_id in spec_ids if spec_id in self.tasks}

    async def stories_for(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, Sequence[str]]:
        del project_id
        return {
            spec_id: tuple(sorted({ref.story_id for ref in self.tasks[spec_id] if ref.story_id}))
            for spec_id in spec_ids
            if spec_id in self.tasks
        }


# A seed-based investigator: no model call


SEED_STUB_VERSION = "gap.seed_stub.2"
READS_UNAVAILABLE = "reads_unavailable"
MAJOR_KINDS = frozenset({"contradiction", "open_question"})
TITLE_IDS = 3
_TITLES = {
    "missing": "Missing in the spec",
    "ambiguous": "Ambiguous in the spec",
    "contradiction": "Contradiction in the spec",
    "inconsistency": "Inconsistent in the spec",
    "open_question": "Open question in the spec",
    "assumption": "Unconfirmed assumption in the spec",
}


class _NoReads:
    """A read menu whose every read returns an empty result with ``reads_unavailable``."""

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


NO_READS: SiteReads = _NoReads()


def _as_question(text: str) -> str:
    clean = text.strip().rstrip(".!:;")
    return clean if clean.endswith("?") else f"{clean}?"


def seed_report(pack: SitePack) -> GapReport:
    """A site's seeds as a ``gap`` report: the primary seed's kind; ``ABOUT`` every seed node, ``IMPACTS`` their
    context nodes, every quote and fact as witness; ``major`` for a strong seed, a contradiction or a stated question,
    else ``minor``, and ``info`` when the seeds name no node and quote no line. Options: insert after the first
    witness line (recommended) and a clarifications-document section; both document sections without a quote."""

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
    first = next((item for item in primary.witness if item.quote and item.fragment_id), None)
    named = ", ".join(about[:TITLE_IDS]) + (" and more" if len(about) > TITLE_IDS else "")
    if primary.kind == "open_question" and first is not None:
        question = _as_question(str(first.quote))
    elif primary.kind == "assumption" and first is not None:
        question = _as_question(f"Is this assumption confirmed: {first.quote}")
    else:
        question = f"What must the spec state to settle this {primary.kind.replace('_', ' ')} about {named}?"
    severity: ReportSeverity = "major" if primary.strength == "strong" or primary.kind in MAJOR_KINDS else "minor"
    if not about and not any(ref.quote for ref in witness):
        severity = "info"
    document = Edit(
        placement="document",
        target=EditTarget(heading=named or "Project"),
        operation="append_section",
        text=f"The spec settles this for {named or 'the project'}.",
    )
    if first is not None:
        amend = Edit(
            placement="amend",
            target=EditTarget(fragment_id=first.fragment_id, anchor_quote=first.quote),
            operation="insert_after",
            text="The spec states the answer here.",
        )
        options = (
            ReportOption(label="State it in the section", recommended=True, edits=(amend,)),
            ReportOption(label="Clarifications document", recommended=False, edits=(document,)),
        )
    else:
        options = (
            ReportOption(label="Clarifications document", recommended=True, edits=(document,)),
            ReportOption(
                label="Clarifications document, owner decides",
                recommended=False,
                edits=(document.model_copy(update={"text": "The owner decides this."}),),
            ),
        )
    detectors = ", ".join(sorted({seed.detector for seed in pack.seeds}))
    return GapReport(
        verdict="gap",
        kind=primary.kind,
        severity=severity,
        title=f"{_TITLES[primary.kind]}: {named}"[:160],
        question=question,
        why=f"Found by the detectors {detectors} ({len(pack.seeds)} seeds).",
        about=about,
        impacts=impacts,
        witness=tuple(witness),
        options=() if severity == "info" else options,
    )


class SeedInvestigator:
    """An ``Investigator`` with no model call and no read: the report is ``seed_report`` of the pack."""

    prompt_version = SEED_STUB_VERSION

    async def investigate(self, pack: SitePack, reads: SiteReads, budget: SiteBudget) -> InvestigationResult:
        del reads, budget
        if not pack.seeds:
            return InvestigationResult(error="site_without_seeds")
        return InvestigationResult(report=seed_report(pack))
