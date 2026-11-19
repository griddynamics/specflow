"""The record of a site, the last phase of the step ``investigate``: write its checked and routed report as soon as it
is routed, before the run takes the next site (docs/graph/gap/data-model.md "Graph model", "Identity"; architecture.md
"Run steps", "Write ownership"; FR-GOWN-01, FR-GOWN-02, FR-GDET-09). ``merged`` sums the outcomes of the sites into
the step's totals.

For each site recorded, by its checked outcome:

- ``gap`` with a node severity: a ``Gap`` node (``gap_id`` from its signature, origin ``analysis``, status ``open``,
  the run id, created and updated instants) with ``ABOUT`` to every node it is about, ``IMPACTS`` to the nodes an
  answer would also change and ``CITES`` to the fragment of every witness quote (quote, lines, quote hash), written
  through the core owned writer as owner ``gap`` in one transaction per gap. Relations carry ``added_by_run`` and
  deterministic edge keys (``graph.gap.gaps``), so a resumed run writes the same relations again. The site row gets
  outcome ``gap`` and the gap id, and a ``created`` event (ids, codes and counts only) is logged.
- A report whose signature is that of a gap in the graph, or a site that joined an existing gap through a seed's
  signature, re-checks that gap: an open gap gets the relations it lacks (and a new ``updated_at`` when it got any);
  nothing is duplicated (``gaps_updated``, a ``rechecked`` event).
- ``gap`` of severity ``info``: a ``gap_note`` row only (site outcome ``not_a_gap``: nothing to decide).
- A new or re-checked open gap gets its checked options as ``gap_option`` rows (rank by report order, replacing the
  rows a re-check finds). A ``routed`` event keeps the route and its reasons.
- A confirmed pointer (route ``ai``, mode ``pointer``): the gap is written ``resolved`` with resolution ``pointer``,
  with a ``Clarification`` (mode ``pointer``, placement ``none``, status ``applied``, answered by ``ai``, the pointer
  line as basis, the second check's confidence), ``ANSWERS`` to the gap and ``GROUNDED_IN`` the answering fragment,
  in the gap's transaction; no option, no amendment (FR-GINV-07, answers.md "Pointer answers").
- An AI answer (route ``ai``, mode ``ai``; answers.md "AI answers", lifecycle.md "Gap transitions"): the gap is
  written ``answered`` together with a ``Clarification`` (mode ``ai``, ``answered_by`` ``ai``, the recommended option's
  label, basis quotes and edits' text, the second check's confidence, status ``pending``, placement by the edits),
  ``ANSWERS`` and ``PLACED_IN`` each source document it amends, in the gap's transaction; its amendment rows
  (``pending``) are written first through ``Amendments`` and deleted again when the graph write fails. The gap's options
  are stored as for any gap (a revert gives them to a person). The answer is staged like a person's answer and applied
  with the next "Resolve gaps"; it never applies at once.
- ``duplicate``: the site row with the gap it repeats, and a ``duplicate`` event on that gap; nothing else is written
  (FR-GINV-06). A rejected report (outcome ``not_a_gap`` with failed checks): the site row and a ``report_rejected``
  event naming the checks. ``not_a_gap``, ``not_spec``: the site row only (the run lists a ``not_spec`` site's nodes in
  the noise report). ``deferred``: the site row, no verdict.

Every investigated site with a checked outcome stores its report in the verdict cache under its input hash, so a
later run with the same inputs makes no call (FR-GRUN-03). The step writes only ``Gap`` nodes and the gap relations:
never a spec node, a spec relation, a document or a fragment (FR-GOWN-01); spec nodes are found by their current id
or alias through the core read queries, and only pairs of the gap ontology are written.

A record written again (``RecordRun.replay``: a resumed run, over the gaps as the first attempt read them and from the
same instant on) comes to the same outcome without writing twice: a new gap the graph holds now, or whose signature a
person dismissed since, is not written again (a dismissed one gets no options); a re-check or a close as ``not_spec``
writes nothing over a gap whose status or ``updated_at`` changed since, unless the change is the re-check's own
write. The rows are upserts, and the events and notes are appended under the key of the run and its sites
(``once``), so they are stored once.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator, Callable, Collection, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from graph.core.errors import ConformanceError
from graph.core.ontology import Ontology
from graph.core.ontology.compiled import BUILD_OWNER
from graph.core.writer import FRAGMENT, NodeRef, OwnedWriter
from graph.gap.answers import AnswerGraph, ClarificationWrite, amendment_rows, clarification_props, place
from graph.gap.answers import write_clarification as write_answer
from graph.gap.gaps import (
    StoredGap,
    answer_key,
    cite_key,
    clarification_uuid,
    gap_uuid,
    link_key,
    quote_hash,
    read_gaps,
)
from graph.gap.identity import clr_id, gap_id, gap_signature, normalise_quote, witness_entry
from graph.gap.models import AmendmentRow, EventRow, GapReport, NoteRow, OptionRow, SiteRow, VerdictRow, Witness
from graph.gap.names import (
    ABOUT,
    AI_ACTOR,
    ANSWERS,
    CITES,
    CLARIFICATION,
    GAP,
    GROUNDED_IN,
    IMPACTS,
    OWNER,
    RUN_ACTOR,
    GapRoute,
    ReportSeverity,
    SiteOutcome,
)
from graph.gap.ports import Amendments, GapRecords
from graph.gap.resolve import SpecRef, resolve_spec_ids
from graph.gap.steps.investigate import primary_seed
from graph.gap.steps.route import AI_MODE, recommended
from graph.gap.steps.sites import RAISED_ORIGINS, Site
from graph.gap.steps.snapshot import GraphSnapshot
from logger.logger import INFO, get_logger, log_event

logger = get_logger(__name__)

ORIGIN: Final = "analysis"
OPEN: Final = "open"
RESOLVED: Final = "resolved"
ANSWERED: Final = "answered"
POINTER: Final = "pointer"
NOT_BLOCKING: Final = "no"
YES: Final = "yes"
PRESENT: Final = "present"
DISMISSED: Final = "dismissed"
BLOCKER: Final = "blocker"
DEVELOPMENT: Final = "development"


@dataclass(frozen=True)
class SiteDecision:
    """What the steps before ``record`` decided for one investigated site."""

    site: Site
    outcome: SiteOutcome
    report: GapReport | None = None
    severity: ReportSeverity | None = None
    route: GapRoute | None = None
    mode: str | None = None
    reasons: tuple[str, ...] = ()
    gap_id: str | None = None
    rejected: tuple[str, ...] = ()
    judge: Mapping[str, Any] | None = None
    gates: Mapping[str, bool] = field(default_factory=dict)
    confidence: float | None = None
    """The second check's confidence of an AI answer (the site's route)."""


@dataclass(frozen=True)
class RecordRun:
    """What the record of a site needs of the run: the stores, the ontology, the ids and the clock."""

    source: Any
    executor: Any
    ontology: Ontology
    records: GapRecords
    project_id: str
    run_id: str
    now: Callable[[], datetime]
    model: str = ""
    prompt_version: str = ""
    database: str | None = None
    build_id: str | None = None
    amendments: Amendments | None = None
    """Where an AI answer's amendment rows go; without it a gap routed ``ai`` is recorded as a question (``ask``)."""
    replay: bool = False
    """The record is written again (a resumed run, over the gaps as they were and at the same instants): a graph write
    that already landed is not repeated, a gap a person changed or dismissed since is left as it is."""


@dataclass
class RecordOutcome:
    """Each site's final outcome and gap id, and the metrics of the step."""

    outcomes: dict[str, tuple[SiteOutcome, str | None]] = field(default_factory=dict)
    gaps_new: Counter[tuple[str, str]] = field(default_factory=Counter)
    gaps_updated: Counter[tuple[str, str]] = field(default_factory=Counter)
    routes: Counter[str] = field(default_factory=Counter)
    notes: int = 0
    skipped_nodes: int = 0
    pointers: int = 0
    ai_answered: int = 0
    rejected: int = 0
    not_spec: dict[str, tuple[str, ...]] = field(default_factory=dict)
    holds: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)
    """Open blocker gaps this step wrote or re-checked, to their kind and ``ABOUT`` ids: the run holds their tasks
    (GD2)."""
    releases: list[str] = field(default_factory=list)
    """Gaps this step ended (a pointer resolved a raised gap, a development gap was not spec): their holds end."""
    closed: list[str] = field(default_factory=list)
    """Development gaps a ``not_spec`` investigation closed as a note (lifecycle.md "Development gaps", step 7)."""

    def payload(self) -> dict[str, Any]:
        return {
            "outcomes": {site_id: [outcome, gap] for site_id, (outcome, gap) in sorted(self.outcomes.items())},
            "gaps_new": [[kind, severity, count] for (kind, severity), count in sorted(self.gaps_new.items())],
            "gaps_updated": [[kind, severity, count] for (kind, severity), count in sorted(self.gaps_updated.items())],
            "routes": dict(sorted(self.routes.items())),
            "notes": self.notes,
            "skipped_nodes": self.skipped_nodes,
            "pointers": self.pointers,
            "ai_answered": self.ai_answered,
            "rejected": self.rejected,
            "not_spec": {site_id: list(ids) for site_id, ids in sorted(self.not_spec.items())},
            "holds": {gap: {"kind": kind, "about": list(ids)} for gap, (kind, ids) in sorted(self.holds.items())},
            "releases": sorted(set(self.releases)),
            "closed": sorted(self.closed),
        }


def report_signature(site: Site, report: GapReport) -> str:
    """data-model.md "Identity": the primary seed's detector, the report's ``ABOUT`` ids and its witness entries (a
    quote by fragment and normalised text, a graph fact by the text of the seed it names)."""

    entries: list[dict[str, str]] = []
    for ref in report.witness:
        if ref.fact is not None:
            entries += [witness_entry(fact=item.fact) for item in site.seeds[ref.fact].witness if item.fact is not None]
        elif ref.fragment_id is not None and ref.quote is not None:
            entries.append(witness_entry(fragment_id=ref.fragment_id, quote=ref.quote))
    unique = {tuple(sorted(entry.items())): entry for entry in entries}
    return gap_signature(primary_seed(site.seeds).detector, report.about, unique.values())


def _quotes(site: Site, report: GapReport, snapshot: GraphSnapshot) -> list[Witness]:
    """The report's witness quotes with their line ranges (from the site's seeds, else the fragment's lines)."""

    lines = {
        (item.fragment_id, normalise_quote(item.quote)): (item.line_start, item.line_end)
        for seed in site.seeds
        for item in seed.witness
        if item.fragment_id is not None and item.quote is not None
    }
    found: list[Witness] = []
    for ref in report.witness:
        if ref.fragment_id is None or ref.quote is None:
            continue
        start, end = lines.get((ref.fragment_id, normalise_quote(ref.quote)), (None, None))
        if start is None:
            start, end = _line_of(snapshot, ref.fragment_id, ref.quote)
        found.append(Witness(fragment_id=ref.fragment_id, quote=ref.quote, line_start=start, line_end=end))
    return found


def _line_of(snapshot: GraphSnapshot, fragment_id: str, quote: str) -> tuple[int | None, int | None]:
    wanted = normalise_quote(quote)
    for line in snapshot.fragment_lines(fragment_id):
        if wanted and wanted in normalise_quote(line.text):
            return line.line_start, line.line_end
    return None, None


class Recorder:
    """Writes the gaps of one run; ``existing`` are the gaps in the graph when the run read them."""

    def __init__(self, run: RecordRun, snapshot: GraphSnapshot, existing: Sequence[StoredGap]) -> None:
        self.run = run
        self.snapshot = snapshot
        self.by_id: dict[str, StoredGap] = {gap.gap_id: gap for gap in existing}
        self.by_signature: dict[str, StoredGap] = {gap.signature: gap for gap in existing if gap.signature}
        for gap in existing:  # a resolved gap's topic continues in its open follow-up (lifecycle.md "Follow-ups")
            follow = next(
                (
                    self.by_id[item.gap_id]
                    for item in gap.follow_ups
                    if item.direction == "continued_by"
                    and item.gap_id in self.by_id
                    and self.by_id[item.gap_id].status == OPEN
                ),
                None,
            )
            if follow is not None and gap.signature:
                self.by_signature[gap.signature] = follow
        self.taken: dict[str, str] = {gap.gap_id: gap.signature for gap in existing}
        self.labels = tuple(label for label in run.ontology.labels if run.ontology.owner_of(label) == BUILD_OWNER)
        self.outcome = RecordOutcome()
        self.site_rows: list[SiteRow] = []
        self.verdicts: list[VerdictRow] = []
        self.notes: list[NoteRow] = []
        self.events: list[EventRow] = []
        self.options: dict[str, list[OptionRow]] = {}
        self.gone: set[str] = set()
        """New gaps a replay found dismissed since the first attempt wrote them (their options stay deleted)."""

    async def record(self, decisions: Sequence[SiteDecision]) -> RecordOutcome:
        run = self.run
        for decision in decisions:
            outcome, gap = await self._one(decision)
            self.outcome.outcomes[decision.site.site_id] = (outcome, gap)
            self.site_rows.append(
                SiteRow(
                    site_id=decision.site.site_id,
                    run_id=run.run_id,
                    input_hash=decision.site.input_hash,
                    rank_score=decision.site.rank_score,
                    outcome=outcome,
                    gap_id=gap,
                )
            )
            if outcome != "deferred" and decision.report is not None:
                cached: dict[str, Any] = {"outcome": outcome, "gap_id": gap}
                if outcome == "gap" and not decision.rejected:
                    cached["report"] = decision.report.model_dump(mode="json")  # checked: what was written
                elif decision.report.not_spec_reason:
                    cached["not_spec_reason"] = decision.report.not_spec_reason
                if decision.judge is not None:
                    cached["judge"] = dict(decision.judge)
                if decision.rejected:
                    cached["rejected"] = list(decision.rejected)
                self.verdicts.append(
                    VerdictRow(
                        input_hash=decision.site.input_hash,
                        report=cached,
                        model=run.model,
                        prompt_version=run.prompt_version,
                        created_at=run.now(),
                    )
                )
        await self._flush([decision.site.site_id for decision in decisions])
        log_event(
            logger,
            INFO,
            "graph.gap.record.done",
            project_id=run.project_id,
            run_id=run.run_id,
            sites=len(decisions),
            gaps_new=sum(self.outcome.gaps_new.values()),
            gaps_updated=sum(self.outcome.gaps_updated.values()),
            notes=self.outcome.notes,
        )
        return self.outcome

    async def _flush(self, sites: Sequence[str]) -> None:
        """The relational rows: keyed upserts and replacements, then the events and the notes, appended once under the
        key of the run and its sites (a replay appends nothing twice)."""

        run, records = self.run, self.run.records
        once = "|".join((run.run_id, *sites))
        if self.site_rows:
            await records.put_sites(run.project_id, run.run_id, self.site_rows)
        for gap, rows in sorted(self.options.items()):
            await records.put_options(run.project_id, gap, rows)
        for row in self.verdicts:
            await records.put_verdict(run.project_id, row)
        if self.events:
            await records.log(run.project_id, self.events, once=once)
        if self.notes:
            await records.put_notes(run.project_id, run.run_id, self.notes, once=once)

    def _event(self, gap: str | None, event: str, **detail: Any) -> None:
        self.events.append(
            EventRow(
                gap_id=gap,
                at=self.run.now(),
                actor=RUN_ACTOR,
                event=event,
                detail={"run_id": self.run.run_id, **detail},
            )
        )

    def _no_gap(self, decision: SiteDecision) -> tuple[SiteOutcome, str | None]:
        """The rows of a site that writes no gap: a duplicate, a rejected report, ``not_spec`` and the rest."""

        site_id = decision.site.site_id
        if decision.outcome == "duplicate":
            if decision.gap_id is not None:
                self._event(decision.gap_id, "duplicate", site_id=site_id)
            return "duplicate", decision.gap_id
        if decision.rejected:
            self.outcome.rejected += 1
            self._event(None, "report_rejected", site_id=site_id, checks=list(decision.rejected))
        elif decision.outcome == "not_spec":
            self.outcome.not_spec[site_id] = tuple(sorted(decision.site.about))
        return decision.outcome, None

    def _note(self, decision: SiteDecision, report: GapReport) -> tuple[SiteOutcome, str | None]:
        self.notes.append(
            NoteRow(
                run_id=self.run.run_id,
                detector=primary_seed(decision.site.seeds).detector,
                spec_ids=report.about,
                text=report.title or f"info: {report.kind}",
            )
        )
        self.outcome.notes += 1
        return "not_a_gap", None

    async def _one(self, decision: SiteDecision) -> tuple[SiteOutcome, str | None]:
        report = decision.report
        raised = self.by_id.get(decision.site.gap_id) if decision.site.gap_id else None
        if (
            decision.outcome == "not_spec"
            and raised is not None
            and raised.origin == DEVELOPMENT
            and raised.status == OPEN
        ):
            return await self._close_not_spec(raised, decision)
        if decision.outcome != "gap" or report is None or decision.rejected:
            return self._no_gap(decision)
        if decision.severity == "info" or decision.route is None:
            return self._note(decision, report)
        signature = report_signature(decision.site, report)
        known = self.by_signature.get(signature) or (
            self.by_id.get(decision.site.gap_id) if decision.site.gap_id else None
        )
        if known is not None:
            await self._recheck(known, decision, report)
            return "gap", known.gap_id
        return await self._create(decision, report, signature)

    def _option_rows(self, gap: str, report: GapReport) -> list[OptionRow]:
        return [
            OptionRow(
                gap_id=gap,
                rank=rank,
                label=option.label,
                detail=option.detail,
                recommended=option.recommended,
                edits=option.edits,
                basis=option.basis,
                created_run=self.run.run_id,
            )
            for rank, option in enumerate(report.options, start=1)
        ]

    async def _nodes(self, ids: Collection[str], relation: str) -> list[SpecRef]:
        resolved = await resolve_spec_ids(self.run.executor, self.run.project_id, list(ids), self.labels)
        nodes = {ref.spec_id: ref for spec_id in ids if (ref := resolved.get(spec_id)) is not None}
        legal = [ref for ref in nodes.values() if self.run.ontology.is_legal(relation, GAP, ref.label)]
        self.outcome.skipped_nodes += len(set(ids)) - len(legal)
        return sorted(legal, key=lambda ref: ref.spec_id)

    async def _create(
        self, decision: SiteDecision, report: GapReport, signature: str
    ) -> tuple[SiteOutcome, str | None]:
        run = self.run
        about = await self._nodes(report.about, ABOUT)
        impacts = [
            ref for ref in await self._nodes(report.impacts, IMPACTS) if ref.spec_id not in {a.spec_id for a in about}
        ]
        quotes = [
            item
            for item in _quotes(decision.site, report, self.snapshot)
            if item.fragment_id in self.snapshot.fragments
        ]
        if not about and not quotes:
            return "deferred", None
        new_id = gap_id(signature, self.taken)
        instant = run.now()
        pointer = decision.mode == POINTER and report.pointer is not None
        option = recommended(report)
        ai = (  # only a run's new gap (never a raised gap or a follow-up a site joined) is answered by AI
            decision.mode == AI_MODE
            and decision.site.gap_id is None
            and option is not None
            and bool(option.edits)
            and run.amendments is not None
        )
        props = self._props(decision, report, signature, new_id, instant, pointer=pointer, ai=ai)
        answer = self._ai_answer(StoredGap(gap_id=new_id, props=props), report, decision, instant) if ai else None
        owner = NodeRef(GAP, gap_uuid(run.project_id, new_id))
        clarification: str | None = None
        written = await self._written(new_id, signature) if run.replay else None
        write = written is None
        if write and answer is not None and answer.rows and run.amendments is not None:
            await run.amendments.put(run.project_id, answer.rows)
        try:
            async with self._transaction(write=write) as tx:
                if tx is not None:
                    await tx.upsert_node(GAP, owner.key, props)
                    await self._relations(tx, owner, new_id, about=about, impacts=impacts, quotes=quotes, keys=set())
                if pointer:
                    clarification = await self._pointer(
                        tx, owner, new_id, decision=decision, report=report, instant=instant
                    )
                if answer is not None and tx is not None:
                    await write_answer(tx, run.project_id, answer.write)
        except BaseException:
            if write and answer is not None and answer.rows and run.amendments is not None:
                await run.amendments.delete(run.project_id, [row.amendment_id for row in answer.rows])
            raise
        if written is DISMISSED:
            self.gone.add(new_id)
        self.taken[new_id] = signature
        if props["status"] == OPEN and props["severity"] == BLOCKER:
            self.outcome.holds[new_id] = (str(props["kind"]), tuple(ref.spec_id for ref in about))
        stored = StoredGap(gap_id=new_id, props=props)
        self.by_id[new_id] = stored
        self.by_signature[signature] = stored
        counts = {"about": len(about), "impacts": len(impacts), "cites": len(quotes)}
        self._created(decision, report, stored, counts, pointer=clarification, answer=answer)
        return "gap", new_id

    def _props(
        self,
        decision: SiteDecision,
        report: GapReport,
        signature: str,
        new_id: str,
        instant: datetime,
        *,
        pointer: bool,
        ai: bool,
    ) -> dict[str, Any]:
        """The properties of a new gap: ``open``, ``resolved`` by a pointer, or ``answered`` by AI (route ``ai``); a
        gap routed ``ai`` that cannot be answered (no amendments port) is a question (route ``ask``)."""

        now = instant.isoformat(timespec="milliseconds")
        detector = primary_seed(decision.site.seeds).detector
        props: dict[str, Any] = {
            "gap_id": new_id,
            "kind": report.kind,
            "severity": decision.severity or "major",
            "status": RESOLVED if pointer else ANSWERED if ai else OPEN,
            "origin": ORIGIN,
            "route": "ask" if decision.mode == AI_MODE and not ai else decision.route,
            "title": report.title,
            "question": report.question,
            "why": report.why,
            "detector": detector,
            "signature": signature,
            "seed_signatures": seed_signatures(decision.site, detector),
            "blocking": NOT_BLOCKING,
            "run_id": self.run.run_id,
            "created_by": RUN_ACTOR,
            "created_at": now,
            "updated_at": now,
        }
        if pointer:
            props |= {"resolution": POINTER, "resolved_at": now}
        return props

    def _created(
        self,
        decision: SiteDecision,
        report: GapReport,
        gap: StoredGap,
        counts: Mapping[str, int],
        *,
        pointer: str | None,
        answer: _AiAnswer | None,
    ) -> None:
        """The metrics, events and option rows of a new gap."""

        run, new_id = self.run, gap.gap_id
        route, severity = str(gap.props["route"]), str(gap.props["severity"])
        self.outcome.gaps_new[(str(report.kind), severity)] += 1
        self.outcome.routes[route] += 1
        self._event(
            new_id,
            "created",
            site_id=decision.site.site_id,
            kind=report.kind,
            severity=severity,
            route=route,
            **counts,
            options=0 if pointer is not None else len(report.options),
        )
        kept = route == decision.route
        routed: dict[str, Any] = {
            "route": route,
            "mode": decision.mode if kept else None,
            "reasons": list(decision.reasons) if kept else ["amendments_unavailable"],
        }
        if decision.gates:  # every gate result, so a person sees why AI answered or why not (answers.md "Gates")
            routed["gates"] = dict(decision.gates)
            routed["confidence"] = decision.confidence
        self._event(new_id, "routed", **routed)
        answered: tuple[str, dict[str, Any]] | None = None
        if answer is not None:
            self.outcome.ai_answered += 1
            detail = {"mode": AI_MODE, "placement": answer.write.props["placement"], "amendments": len(answer.rows)}
            answered = (answer.write.clr_id, detail)
        elif pointer is not None:
            self.outcome.pointers += 1
            answered = (pointer, {"mode": POINTER})
        if answered is not None:
            self.events.append(
                EventRow(
                    gap_id=new_id,
                    clr_id=answered[0],
                    at=run.now(),
                    actor=AI_ACTOR,
                    event="answered",
                    detail={"run_id": run.run_id, **answered[1]},
                )
            )
        if pointer is None and report.options and new_id not in self.gone:
            self.options[new_id] = self._option_rows(new_id, report)

    def _ai_answer(
        self, gap: StoredGap, report: GapReport, decision: SiteDecision, instant: datetime
    ) -> _AiAnswer | None:
        """The clarification and amendment rows of an AI answer: the recommended option's edits, placed as for a
        person's answer (answers.md "Placement rules"), ``pending`` until the host applies them."""

        option = recommended(report)
        if option is None or not option.edits:
            return None
        edits, placement = place(gap, option.edits, None)
        body = "\n\n".join(edit.text for edit in edits)
        clr = clr_id(gap.gap_id, body, instant)
        rows = amendment_rows(AnswerGraph(gap, self.snapshot), clr, edits, instant)
        props = clarification_props(
            clr,
            mode=AI_MODE,
            placement=placement,
            option_label=option.label,
            text=body,
            answered_by=AI_ACTOR,
            answered_at=instant,
        )
        props["basis"] = json.dumps([item.model_dump(mode="json") for item in option.basis], ensure_ascii=False)
        props["confidence"] = decision.confidence
        lineages = tuple(sorted({row.lineage for row in rows}))
        return _AiAnswer(ClarificationWrite(clr, gap, props, lineages), rows)

    async def _pointer(
        self,
        tx: OwnedWriter | None,
        owner: NodeRef,
        gap: str,
        *,
        decision: SiteDecision,
        report: GapReport,
        instant: datetime,
    ) -> str:
        """The pointer clarification of a gap, ``ANSWERS`` it and ``GROUNDED_IN`` the answering fragment; its id alone
        without ``tx`` (a write that already landed)."""

        pointer = report.pointer
        if pointer is None:
            raise ConformanceError("gap_pointer_missing", gap_id=gap)
        clr = clr_id(gap, pointer.quote, instant)
        key = clarification_uuid(self.run.project_id, clr)
        judge = decision.judge or {}
        props: dict[str, Any] = {
            "clr_id": clr,
            "mode": POINTER,
            "placement": "none",
            "status": "applied",
            "option_label": "",
            "text": "",
            "answered_by": AI_ACTOR,
            "answered_at": instant.isoformat(timespec="milliseconds"),
            "basis": json.dumps([pointer.model_dump(mode="json")], ensure_ascii=False),
            "confidence": judge.get("confidence"),
        }
        if self.run.build_id is not None:
            props["applied_build_id"] = self.run.build_id
        if tx is None:
            return clr
        await tx.upsert_node(CLARIFICATION, key, props)
        node = NodeRef(CLARIFICATION, key)
        await tx.upsert_relationship(ANSWERS, node, owner, answer_key(clr, ANSWERS, gap), {})
        grounded: dict[str, Any] = {} if self.run.build_id is None else {"build_id": self.run.build_id}
        await tx.upsert_relationship(
            GROUNDED_IN,
            node,
            NodeRef(FRAGMENT, pointer.fragment_id),
            answer_key(clr, GROUNDED_IN, pointer.fragment_id),
            grounded,
        )
        return clr

    async def _recheck(self, known: StoredGap, decision: SiteDecision, report: GapReport) -> None:
        """Re-check ``known`` with a report (an open gap gets what it lacks, ``_recheck_open``), its options, counts and
        ``rechecked`` event."""

        run = self.run
        added, ours = await self._recheck_open(known, decision, report) if known.status == OPEN else (0, True)
        if ours and known.status == OPEN and report.options and decision.mode != POINTER:
            self.options[known.gap_id] = self._option_rows(known.gap_id, report)
        kind = str(known.props.get("kind") or report.kind)
        self.outcome.gaps_updated[(kind, str(known.props.get("severity") or decision.severity))] += 1
        self.events.append(
            EventRow(
                gap_id=known.gap_id,
                at=run.now(),
                actor=RUN_ACTOR,
                event="rechecked",
                detail={"run_id": run.run_id, "site_id": decision.site.site_id, "added": added},
            )
        )

    async def _recheck_open(self, known: StoredGap, decision: SiteDecision, report: GapReport) -> tuple[int, bool]:
        """The writes of a re-check of an open gap: the relations it lacks, a raised gap's refinements, a pointer. The
        relations added, and whether the outcome is the run's: on a replay nothing is written again when the write
        already landed, and a gap a person changed or dismissed since stays as they left it (False)."""

        run = self.run
        about = await self._nodes(report.about, ABOUT)
        impacts = [
            ref for ref in await self._nodes(report.impacts, IMPACTS) if ref.spec_id not in {a.spec_id for a in about}
        ]
        quotes = [
            item
            for item in _quotes(decision.site, report, self.snapshot)
            if item.fragment_id in self.snapshot.fragments
        ]
        keys = {link.edge_key for link in (*known.about, *known.impacts)} | {cite.edge_key for cite in known.cites}
        owner = NodeRef(GAP, gap_uuid(run.project_id, known.gap_id))
        pointer = decision.mode == POINTER and report.pointer is not None
        changed = _raised_changes(known, decision, report)
        added = await self._relations(
            None, owner, known.gap_id, about=about, impacts=impacts, quotes=quotes, keys=set(keys)
        )
        instant = run.now() if added or changed or pointer else None
        now = None if instant is None else instant.isoformat(timespec="milliseconds")
        write, ours = _rewrite(await self._current(known.gap_id), known, now) if run.replay else (True, True)
        if not ours:
            return 0, False
        clr: str | None = None
        async with self._transaction(write=write) as tx:
            if tx is not None:
                await self._relations(
                    tx, owner, known.gap_id, about=about, impacts=impacts, quotes=quotes, keys=set(keys)
                )
            if instant is not None:
                resolved = {"status": RESOLVED, "resolution": POINTER, "resolved_at": now} if pointer else {}
                if tx is not None:
                    await tx.upsert_node(GAP, owner.key, {**changed, **resolved, "updated_at": now})
                if pointer:  # a raised gap the spec already answers resolves now (FR-GDEV-04)
                    clr = await self._pointer(
                        tx, owner, known.gap_id, decision=decision, report=report, instant=instant
                    )
            seeds = more_seed_signatures(known, decision.site)
            if seeds and tx is not None:  # what a dismissal suppresses; no change a reader sees (no new ``updated_at``)
                await tx.upsert_node(GAP, owner.key, {"seed_signatures": seeds})
        self._settle(known, changed, about, clr)
        return added, True

    def _settle(self, known: StoredGap, changed: Mapping[str, Any], about: Sequence[SpecRef], clr: str | None) -> None:
        """The holds a re-check changes: a pointer resolved the gap (its holds end), a blocker refined below blocker
        releases its tasks, a blocker holds them."""

        if clr is not None:
            self._pointered(known.gap_id, clr)
        elif (
            known.props.get("severity") == BLOCKER
            and changed.get("severity", BLOCKER) != BLOCKER
            and known.props.get("blocking") != YES
        ):  # refined below blocker: major and minor gaps hold no task
            self.outcome.releases.append(known.gap_id)
        elif str(changed.get("severity") or known.props.get("severity")) == BLOCKER:
            kind = str(changed.get("kind") or known.props.get("kind") or "")
            self.outcome.holds[known.gap_id] = (
                kind,
                tuple(dict.fromkeys([*known.about_ids, *(ref.spec_id for ref in about)])),
            )

    def _pointered(self, gap: str, clr: str) -> None:
        """The metrics and events of a gap a confirmed pointer resolved in a re-check; its holds end."""

        self.outcome.pointers += 1
        self.outcome.releases.append(gap)
        self.events.append(
            EventRow(
                gap_id=gap,
                clr_id=clr,
                at=self.run.now(),
                actor=AI_ACTOR,
                event="answered",
                detail={"run_id": self.run.run_id, "mode": POINTER},
            )
        )
        self._event(gap, RESOLVED, clr=clr, resolution=POINTER)

    async def _close_not_spec(self, known: StoredGap, decision: SiteDecision) -> tuple[SiteOutcome, str | None]:
        """A development gap the investigation judged ``not_spec`` (a library bug, a failing test, a tooling issue):
        closed as obsolete with a ``gap_note`` holding the agent's question, its options go and its holds end
        (lifecycle.md "Development gaps", step 7; FR-GDEV-06). Its nodes are not marked as noise: the agent's
        question says nothing about the sections. On a replay a gap a person changed since stays as it is."""

        if self.run.replay:
            current = await self._current(known.gap_id)
            if current is not None and _rewrite(current, known, None) == (False, False):
                return "not_spec", known.gap_id

        async with self._writer().transaction() as tx:
            for props in known.clarifications:
                await tx.delete_node(CLARIFICATION, clarification_uuid(self.run.project_id, str(props["clr_id"])))
            await tx.delete_node(GAP, gap_uuid(self.run.project_id, known.gap_id))
        self.by_id.pop(known.gap_id, None)
        self.notes.append(
            NoteRow(
                run_id=self.run.run_id,
                detector=str(known.props.get("detector") or known.origin),
                spec_ids=known.about_ids,
                text=str(known.props.get("question") or known.props.get("title") or ""),
            )
        )
        self.outcome.notes += 1
        self.options[known.gap_id] = []
        self.outcome.releases.append(known.gap_id)
        self.outcome.closed.append(known.gap_id)
        reason = decision.report.not_spec_reason if decision.report is not None else None
        self._event(known.gap_id, "obsolete", reason="not_spec", site_id=decision.site.site_id, noted=bool(reason))
        return "not_spec", known.gap_id

    async def _relations(
        self,
        tx: OwnedWriter | None,
        owner: NodeRef,
        gap: str,
        *,
        about: Sequence[SpecRef],
        impacts: Sequence[SpecRef],
        quotes: Sequence[Witness],
        keys: set[str],
    ) -> int:
        """Write the ``ABOUT``, ``IMPACTS`` and ``CITES`` relations whose keys are not in ``keys``; how many (only
        counted without ``tx``)."""

        added = await self._links(tx, owner, gap, ABOUT, nodes=about, keys=keys)
        added += await self._links(tx, owner, gap, IMPACTS, nodes=impacts, keys=keys)
        return added + await self._cites(tx, owner, gap, quotes, keys)

    async def _links(
        self,
        tx: OwnedWriter | None,
        owner: NodeRef,
        gap: str,
        relation: str,
        *,
        nodes: Sequence[SpecRef],
        keys: set[str],
    ) -> int:
        added = 0
        for ref in nodes:
            key = link_key(gap, relation, ref.spec_id)
            if key in keys:
                continue
            if tx is not None:
                await tx.upsert_relationship(
                    relation, owner, NodeRef(ref.label, ref.uuid), key, {"added_by_run": self.run.run_id}
                )
            keys.add(key)
            added += 1
        return added

    async def _cites(
        self, tx: OwnedWriter | None, owner: NodeRef, gap: str, quotes: Sequence[Witness], keys: set[str]
    ) -> int:
        added = 0
        for item in quotes:
            fragment, quote = str(item.fragment_id), str(item.quote)
            digest = quote_hash(quote)
            key = cite_key(gap, fragment, digest)
            if key in keys:
                continue
            props: dict[str, Any] = {"quote": quote, "quote_hash": digest, "added_by_run": self.run.run_id}
            if item.line_start is not None:
                props["line_start"] = item.line_start
                props["line_end"] = item.line_end if item.line_end is not None else item.line_start
            if tx is not None:
                await tx.upsert_relationship(CITES, owner, NodeRef(FRAGMENT, fragment), key, props)
            keys.add(key)
            added += 1
        return added

    def _writer(self) -> OwnedWriter:
        return OwnedWriter(self.run.source, self.run.ontology, OWNER, self.run.project_id, database=self.run.database)

    @asynccontextmanager
    async def _transaction(self, *, write: bool) -> AsyncIterator[OwnedWriter | None]:
        """A writer transaction, or None when nothing is to be written (a replay over a write that landed)."""

        if not write:
            yield None
            return
        async with self._writer().transaction() as tx:
            yield tx

    async def _current(self, gap: str) -> StoredGap | None:
        """The gap as the graph holds it now (a replay compares it with the gap as the first attempt read it)."""

        found = await read_gaps(self.run.executor, self.run.project_id, self.run.ontology, gap_ids=[gap])
        return found[0] if found else None

    async def _written(self, gap: str, signature: str) -> str | None:
        """On a replay, whether a new gap's write already landed: ``PRESENT`` (whatever a person did to it since),
        ``DISMISSED`` (a person dismissed it since: its signature is suppressed), or None (it did not land)."""

        if await self._current(gap) is not None:
            return PRESENT
        if signature in await self.run.records.suppressed(self.run.project_id, [signature]):
            return DISMISSED
        return None


@dataclass(frozen=True)
class _AiAnswer:
    """An AI answer as the record writes it: the clarification and its ``pending`` amendment rows."""

    write: ClarificationWrite
    rows: tuple[AmendmentRow, ...]


def seed_signatures(site: Site, detector: str) -> list[str]:
    """The signatures of the site's seeds of ``detector``: what a dismissal of the gap suppresses beside the gap's own
    signature, so the same detector hit on the same nodes and witness stays quiet (data-model.md "Identity",
    detection.md "Suppression"; FR-GDET-07, FR-GANS-11). Seeds of other detectors the site merged in are left out:
    their problem may still be one."""

    return sorted({seed.signature for seed in site.seeds if seed.detector == detector})


def more_seed_signatures(known: StoredGap, site: Site) -> list[str]:
    """The gap's seed signatures with those of a re-checking site added, or empty when the site adds none."""

    stored = [str(item) for item in known.props.get("seed_signatures") or ()]
    found = seed_signatures(site, str(known.props.get("detector") or ""))
    merged = sorted({*stored, *found})
    return merged if len(merged) > len(set(stored)) else []


def _raised_changes(known: StoredGap, decision: SiteDecision, report: GapReport) -> dict[str, Any]:
    """What the investigation of a gap a person or a coding agent raised refines (investigation.md "Development
    gaps"): severity, kind, title and why; the asker's question stays, the route stays ``ask``."""

    if known.origin not in RAISED_ORIGINS or decision.severity in {None, "info"}:
        return {}
    changed: dict[str, Any] = {"severity": decision.severity}
    if report.kind is not None:
        changed["kind"] = report.kind
    if report.title:
        changed["title"] = report.title
    if report.why:
        changed["why"] = report.why
    return changed


def _rewrite(current: StoredGap | None, known: StoredGap, stamp: str | None) -> tuple[bool, bool]:
    """On a replay of a re-check: (write, ours). The gap as read before (``known``) unchanged: write again. Its
    ``updated_at`` the re-check's own ``stamp``: the write landed, keep its outcome. Changed otherwise or gone: a person
    acted since, write nothing."""

    if current is not None and (current.status, current.props.get("updated_at")) == (
        known.status,
        known.props.get("updated_at"),
    ):
        return True, True
    if current is not None and stamp is not None and current.props.get("updated_at") == stamp:
        return False, True
    return False, False


def reads_of(existing: Sequence[StoredGap], decision: SiteDecision) -> tuple[StoredGap, ...]:
    """The gaps of ``existing`` the record of ``decision`` reads: its site's gap, the gaps of its report's signature
    and the open follow-ups continuing them, and the gaps holding an id its new gap could take. Recorded over these
    alone, a site takes the same path and writes the same, whatever an earlier attempt already wrote."""

    wanted = {decision.site.gap_id} if decision.site.gap_id else set()
    report = decision.report
    if decision.outcome == "gap" and report is not None and not decision.rejected:
        signature = report_signature(decision.site, report)
        same = {gap.gap_id for gap in existing if gap.signature == signature}
        wanted |= same | {
            item.gap_id
            for gap in existing
            if gap.gap_id in same
            for item in gap.follow_ups
            if item.direction == "continued_by"
        }
        base = gap_id(signature)
        wanted |= {gap.gap_id for gap in existing if gap.gap_id.startswith(base)}
    return tuple(gap for gap in existing if gap.gap_id in wanted)


async def record_sites(
    run: RecordRun, snapshot: GraphSnapshot, existing: Sequence[StoredGap], decisions: Sequence[SiteDecision]
) -> RecordOutcome:
    """Write the gaps, site rows, verdicts, notes and events of ``decisions`` (module docstring)."""

    return await Recorder(run, snapshot, existing).record(decisions)


def merged(payloads: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The step's totals over sites recorded one by one: their ``RecordOutcome`` payloads summed."""

    total = RecordOutcome()
    for payload in payloads:
        total.outcomes |= {site_id: (value[0], value[1]) for site_id, value in payload["outcomes"].items()}
        for kind, severity, count in payload["gaps_new"]:
            total.gaps_new[(kind, severity)] += int(count)
        for kind, severity, count in payload["gaps_updated"]:
            total.gaps_updated[(kind, severity)] += int(count)
        total.routes.update(payload["routes"])
        for name in ("notes", "skipped_nodes", "pointers", "ai_answered", "rejected"):
            setattr(total, name, getattr(total, name) + int(payload[name]))
        total.not_spec |= {site_id: tuple(ids) for site_id, ids in payload["not_spec"].items()}
        total.holds |= {gap: (item["kind"], tuple(item["about"])) for gap, item in payload["holds"].items()}
        total.releases += payload["releases"]
        total.closed += payload["closed"]
    return total.payload()


def counted(rows: Sequence[Sequence[Any]]) -> dict[str, Any]:
    """``gaps_new`` / ``gaps_updated`` of the run metrics from the step's ``[kind, severity, count]`` rows."""

    by_kind: Counter[str] = Counter()
    by_severity: Counter[str] = Counter()
    for kind, severity, count in rows:
        by_kind[str(kind)] += int(count)
        by_severity[str(severity)] += int(count)
    return {
        "total": sum(by_kind.values()),
        "by_kind": dict(sorted(by_kind.items())),
        "by_severity": dict(sorted(by_severity.items())),
    }


def outcome_counts(outcomes: Mapping[str, Sequence[Any]]) -> dict[str, int]:
    """Sites per outcome of investigation (detection.md "Run metrics")."""

    found = Counter(str(value[0]) for value in outcomes.values())
    return {name: found[name] for name in ("gap", "not_a_gap", "not_spec", "duplicate", "deferred")}
