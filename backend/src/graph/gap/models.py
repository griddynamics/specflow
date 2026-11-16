"""Models of ``graph.gap`` (docs/graph/gap/api.md "Models", "Ports"; data-model.md "Relational store";
investigation.md "Report").

Every model is a frozen pydantic model that forbids unknown fields. Field limits of the investigator's report are
checked by the ``check`` step (it truncates or drops), so the report model holds types and shapes only. The rows
mirror the host's relational tables; JSON columns are mappings or tuples of models.

The first-slice findings models (``Finding`` and its companions) stay until the migration of first-slice findings.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from graph.gap.names import (
    AmendmentOperation,
    AmendmentStatus,
    ClarificationMode,
    ClarificationPlacement,
    ClarificationStatus,
    DecidedBy,
    EditPlacement,
    FindingKind,
    FindingStatus,
    FollowUpReason,
    GapKind,
    GapOrigin,
    GapResolution,
    GapRoute,
    GapSeverity,
    GapStatus,
    NoiseScope,
    ReconcileOutcome,
    RefType,
    Relevance,
    ReportSeverity,
    RunMode,
    RunStatus,
    SeedStrength,
    SiteOutcome,
    Verdict,
)
from graph.gap.settings import OPTIONS_CEILING


class GapModel(BaseModel):
    # validate_default: an empty mapping default (a read-only proxy) is validated into a plain mapping, so every model
    # serialises in JSON mode (the relational adapters write JSON columns from it).
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


def _empty() -> Mapping[str, Any]:
    return MappingProxyType({})


# Shared parts


class Quote(GapModel):
    """A verbatim line of a fragment."""

    fragment_id: str
    quote: str


class Witness(GapModel):
    """A seed's or gap's evidence: a quote (fragment id, line range, text) or a graph fact."""

    fragment_id: str | None = None
    quote: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    fact: str | None = None

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        quoted = self.fragment_id is not None and self.quote is not None
        if quoted == (self.fact is not None):
            raise ValueError("a witness is a fragment quote or a fact")
        return self


class EditTarget(GapModel):
    """Where an edit applies: a fragment and a verbatim anchor line (``amend``), or a heading (``document``)."""

    fragment_id: str | None = None
    anchor_quote: str | None = None
    heading: str | None = None


class Edit(GapModel):
    """One edit of an option (investigation.md "Options and edits")."""

    placement: EditPlacement
    target: EditTarget
    operation: AmendmentOperation
    text: str


class NodeRef(GapModel):
    """A spec node as a gap view shows it."""

    spec_id: str
    label: str
    name: str


# Change reports, settings and context


class GapChangeReport(GapModel):
    """The gap-side copy of one build change report; the host converts the build's report (graph.gap never imports
    graph.build). ``merged`` and ``rekeyed`` map old ids to new ids, ``fragments_changed`` lists fragment ids by
    lineage, ``documents`` maps a lineage to its revision id."""

    build_id: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    merged: Mapping[str, str] = Field(default_factory=_empty)
    rekeyed: Mapping[str, str] = Field(default_factory=_empty)
    changed_meaning: tuple[str, ...] = ()
    changed_links: tuple[str, ...] = ()
    changed_evidence: tuple[str, ...] = ()
    fragments_changed: Mapping[str, tuple[str, ...]] = Field(default_factory=_empty)
    documents: Mapping[str, str] = Field(default_factory=_empty)


class RefusedPair(GapModel):
    """A near-duplicate pair the build kept apart (its dedup refusal: a number, a negation or the modality differs),
    the ends by ``spec_id``; the input of CT-VALUE (detection.md "Detector catalogue")."""

    first_spec_id: str
    second_spec_id: str
    label: str
    guard: str


class CutEdge(GapModel):
    """An edge the build took out of a cycle (quarantined as ``cycle_broken``), the ends by ``spec_id``; IN-CYCLE
    reads it as part of the cycle."""

    relation: str
    source_spec_id: str
    target_spec_id: str
    fragment_ids: tuple[str, ...] = ()


class BuildFacts(GapModel):
    """What the build persisted with its result and detectors read (detection.md "Detector catalogue"): the dedup
    refusals and the cycle cuts of the build the run follows. The host converts them from the build result."""

    build_id: str | None = None
    refused_pairs: tuple[RefusedPair, ...] = ()
    cut_edges: tuple[CutEdge, ...] = ()


# Relational rows (data-model.md "Relational store"); every table also has project_id, which the ports take.


class OptionRow(GapModel):
    """``gap_option``: one offered answer; exactly one recommended per gap, 2 to 4 rows."""

    gap_id: str
    rank: int = Field(ge=1, le=OPTIONS_CEILING)
    label: str
    detail: str = ""
    recommended: bool
    edits: tuple[Edit, ...] = ()
    basis: tuple[Quote, ...] = ()
    created_run: str | None = None


class AmendmentAnchor(GapModel):
    """Where an amendment applies inside its document."""

    heading_path: tuple[str, ...]
    anchor_text: str
    anchor_hash: str


class AmendmentRow(GapModel):
    """``corpus_amendment``: one stored edit applied by the corpus reader."""

    amendment_id: str
    clr_id: str
    lineage: str
    heading_path: tuple[str, ...]
    anchor_text: str
    anchor_hash: str
    operation: AmendmentOperation
    text: str
    status: AmendmentStatus
    last_revision_id: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class RunRow(GapModel):
    """``gap_run``: one row per run."""

    run_id: str
    build_id: str | None = None
    mode: RunMode
    status: RunStatus
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    counts: Mapping[str, Any] = Field(default_factory=_empty)
    cost: Mapping[str, Any] = Field(default_factory=_empty)
    rules_version: str
    prompt_versions: Mapping[str, str] = Field(default_factory=_empty)


class SeedRow(GapModel):
    """``gap_seed``: every detector hit."""

    run_id: str
    detector: str
    spec_ids: tuple[str, ...]
    witness: tuple[Witness, ...]
    strength: SeedStrength
    relevance: Relevance
    corroborated: bool
    site_id: str | None = None


class SiteRow(GapModel):
    """``gap_site``: one investigation entry point; ``input_hash`` keys the verdict cache."""

    site_id: str
    run_id: str
    input_hash: str
    rank_score: float
    outcome: SiteOutcome
    gap_id: str | None = None


class VerdictRow(GapModel):
    """``gap_verdict``: a checked report (and its judge result) reused while a site's inputs stay the same."""

    input_hash: str
    report: Mapping[str, Any]
    model: str
    prompt_version: str
    created_at: AwareDatetime


class SectionKey(GapModel):
    """A section of a document lineage, with the hash of its text (the relevance cache key)."""

    lineage: str
    heading_path: tuple[str, ...]
    text_hash: str


class RelevanceRow(GapModel):
    """``gap_section_relevance``: the cached relevance of a section."""

    lineage: str
    heading_path: tuple[str, ...]
    text_hash: str
    relevance: Relevance
    signals: tuple[str, ...] = ()
    by: DecidedBy

    @property
    def key(self) -> SectionKey:
        return SectionKey(
            lineage=self.lineage,
            heading_path=self.heading_path,
            text_hash=self.text_hash,
        )


class NoiseRuleRow(GapModel):
    """``gap_noise_rule``: a document, section or pattern a person marked "not spec"."""

    rule_id: str
    scope: NoiseScope
    value: str
    reason: str
    created_by: str
    created_at: AwareDatetime


class SuppressionRow(GapModel):
    """``gap_suppression``: a dismissed signature, quiet until it changes."""

    signature: str
    gap_id: str | None = None
    question: str
    reason: str
    dismissed_by: str
    dismissed_at: AwareDatetime


class NoteRow(GapModel):
    """``gap_note``: an info finding, nothing to decide."""

    run_id: str | None = None
    detector: str
    spec_ids: tuple[str, ...]
    text: str


class EventRow(GapModel):
    """``gap_event``: every transition, quote and reconcile outcome."""

    gap_id: str | None = None
    clr_id: str | None = None
    at: AwareDatetime
    actor: str
    event: str
    detail: Mapping[str, Any] = Field(default_factory=_empty)


# Plan port models


class TaskRef(GapModel):
    """A plan task implementing a spec node, as ``PlanView`` returns it."""

    task_id: str
    story_id: str | None = None
    status: str


class HoldResult(GapModel):
    """What ``StoryHolds.hold_tasks`` did: the tasks it held, and those left alone because they were in progress
    (listed in the run result)."""

    held: tuple[str, ...] = ()
    in_progress: tuple[str, ...] = ()


# Investigation: the pack, the budget and the report of investigation.md


class SiteBudget(GapModel):
    """The budget of one investigation."""

    rounds: int = Field(gt=0)
    reads_per_round: int = Field(gt=0)
    context_tokens: int = Field(gt=0)


class PackSeed(GapModel):
    """A seed as the context pack shows it."""

    detector: str
    kind: GapKind
    strength: SeedStrength
    spec_ids: tuple[str, ...]
    context_ids: tuple[str, ...] = ()
    witness: tuple[Witness, ...] = ()


class DevelopmentContext(GapModel):
    """For a development gap: the agent's question, kind, quoted lines and the task it holds."""

    question: str
    kind: GapKind
    quotes: tuple[Quote, ...] = ()
    task_id: str | None = None


class SitePack(GapModel):
    """The context pack of one site (investigation.md "Context pack"). ``context`` holds the pack's parts by name
    (anchors, evidence, trace, nearby, existing gaps, plan), as data blocks the prompt renders untrusted."""

    project_id: str
    site_id: str
    input_hash: str
    mode: RunMode
    seeds: tuple[PackSeed, ...]
    context: Mapping[str, Any] = Field(default_factory=_empty)
    development: DevelopmentContext | None = None


class WitnessRef(GapModel):
    """A report's witness: a fragment quote, or a detector graph fact by its seed index."""

    fragment_id: str | None = None
    quote: str | None = None
    fact: int | None = None


class ReportOption(GapModel):
    """An offered answer of a report."""

    label: str
    detail: str = ""
    recommended: bool
    edits: tuple[Edit, ...]
    basis: tuple[Quote, ...] = ()


class GapReport(GapModel):
    """The investigator's whole output (investigation.md "Report"); the ``check`` step enforces its limits."""

    verdict: Verdict
    duplicate_of: str | None = None
    kind: GapKind | None = None
    severity: ReportSeverity | None = None
    title: str = ""
    question: str = ""
    why: str = ""
    about: tuple[str, ...] = ()
    impacts: tuple[str, ...] = ()
    witness: tuple[WitnessRef, ...] = ()
    options: tuple[ReportOption, ...] = ()
    pointer: Quote | None = None
    ai_answerable: bool = False
    ai_reason: str = ""
    not_spec_reason: str | None = None


class ReadRecord(GapModel):
    """One read an investigation made, for the event log (names and ids, no text)."""

    read: str
    arguments: Mapping[str, Any] = Field(default_factory=_empty)
    empty: bool = False
    reason: str | None = None


class ReadResult(GapModel):
    """The answer of a read of the menu: items as data, or an empty result with the reason (never an error)."""

    read: str
    items: tuple[Mapping[str, Any], ...] = ()
    reason: str | None = None
    truncated: bool = False


class InvestigationResult(GapModel):
    """What an ``Investigator`` returns: the report or none, the rounds and reads used, the cost, an error code."""

    report: GapReport | None = None
    rounds_used: int = 0
    reads: tuple[ReadRecord, ...] = ()
    model_calls: int = 0
    tokens: int = 0
    error: str | None = None


# Run results


class NoiseSection(GapModel):
    """A section judged tracking or mixed, and the nodes it removed from structural seeds."""

    lineage: str
    heading_path: tuple[str, ...]
    relevance: Relevance
    signals: tuple[str, ...] = ()
    by: DecidedBy
    removed: tuple[str, ...] = ()


class NoiseReport(GapModel):
    """Per section judged tracking or mixed (detection.md "Relevance filter")."""

    sections: tuple[NoiseSection, ...] = ()


class GapRunResult(GapModel):
    """The result of ``run_gap``. ``status`` is ``degraded`` when every investigation of the run failed
    (investigation.md "Failures"); ``counts`` holds the run metrics (detection.md "Run metrics"), ``cost`` the
    calls and tokens per gateway role."""

    run_id: str
    mode: RunMode
    build_id: str | None = None
    status: Literal["completed", "degraded", "failed"]
    counts: Mapping[str, Any] = Field(default_factory=_empty)
    cost: Mapping[str, Any] = Field(default_factory=_empty)
    noise: NoiseReport = Field(default_factory=NoiseReport)
    deferred_sites: tuple[str, ...] = ()
    held_tasks: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class GapEstimate(GapModel):
    """What ``estimate_gap`` expects: sites to investigate, cached ones, calls and tokens per gateway role."""

    sites: int = 0
    cached: int = 0
    calls: Mapping[str, int] = Field(default_factory=_empty)
    tokens: Mapping[str, int] = Field(default_factory=_empty)


# Reads of the service


class WitnessView(GapModel):
    """A ``CITES`` witness of a gap."""

    fragment_id: str
    quote: str
    line_start: int | None = None
    line_end: int | None = None


class ClarificationView(GapModel):
    """A clarification as reads return it."""

    clr_id: str
    mode: ClarificationMode
    placement: ClarificationPlacement
    status: ClarificationStatus
    option_label: str | None = None
    text: str | None = None
    answered_by: str
    answered_at: AwareDatetime
    applied_build_id: str | None = None
    basis: tuple[Quote, ...] = ()
    confidence: float | None = None


class FollowUp(GapModel):
    """One step of a follow-up chain: the gap this one continues, or that continues it, and why."""

    gap_id: str
    reason: FollowUpReason
    direction: Literal["continues", "continued_by"]


class GapHistory(GapModel):
    """The events of a gap and its follow-up chain."""

    events: tuple[EventRow, ...] = ()
    follow_ups: tuple[FollowUp, ...] = ()


class Reach(GapModel):
    """Stories and tasks an answer reaches, computed at read time from trace and the plan."""

    stories: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()


class GapView(GapModel):
    """What reads return: the gap's properties and what it is about; ``history`` and ``reach`` only from
    ``get_gap``."""

    gap_id: str
    kind: GapKind
    severity: GapSeverity
    status: GapStatus
    origin: GapOrigin
    route: GapRoute
    resolution: GapResolution | None = None
    title: str
    question: str
    why: str = ""
    detector: str
    signature: str
    blocking: bool = False
    held_back: bool = False
    run_id: str | None = None
    created_by: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    about: tuple[NodeRef, ...] = ()
    impacts: tuple[NodeRef, ...] = ()
    witness: tuple[WitnessView, ...] = ()
    options: tuple[OptionRow, ...] = ()
    clarification: ClarificationView | None = None
    history: GapHistory | None = None
    reach: Reach | None = None


class GapPage(GapModel):
    """One page of gaps; ``next`` is the cursor of the following page, or none."""

    gaps: tuple[GapView, ...]
    next: str | None = None


class GapFinding(GapModel):
    """An open gap as the plan's findings port shows it (lifecycle.md "Plan link"): ``finding_id`` is the gap id,
    ``text`` the question, ``blocking`` true for a blocker or a development gap holding its story, ``about_spec_ids``
    the current ``ABOUT`` ids, ``orphaned`` true when no ``ABOUT`` relation is left. Satisfies the plan's
    ``FindingLike`` (structural: graph.gap does not import graph.plan)."""

    finding_id: str
    kind: str
    text: str
    blocking: bool
    about_spec_ids: tuple[str, ...]
    story_id: str | None = None
    created_by: str
    created_at: AwareDatetime
    orphaned: bool = False


class GapFindingPage(GapModel):
    """The open gaps a findings read returns (the plan's ``FindingPageLike``)."""

    findings: tuple[GapFinding, ...]
    next: str | None = None


# Inputs of the service


class AnswerInput(GapModel):
    """An answer to a gap: an offered option by rank or the person's own text (exactly one). ``edits`` are the edits
    confirmed in ``preview_edits`` (an option's own edits when None; an own text without edits goes to the
    clarifications document as written). ``updated_at`` is the gap's ``updated_at`` as the caller read it: a gap
    changed since then refuses the answer (``StaleGapError``); None skips the check."""

    gap_id: str
    option_rank: int | None = Field(default=None, ge=1)
    own_text: str | None = None
    placement: EditPlacement | None = None
    edits: tuple[Edit, ...] | None = None
    answered_by: str
    updated_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _one_answer(self) -> Self:
        if (self.option_rank is None) == (self.own_text is None):
            raise ValueError("give exactly one of option_rank and own_text")
        return self


class NewGap(GapModel):
    """A gap a person or a coding agent raises. A blocking gap is a development gap and names the story and the
    claim token of the agent that holds it."""

    kind: GapKind
    question: str
    about: tuple[str, ...]
    quotes: tuple[Quote, ...] = ()
    severity: GapSeverity | None = None
    origin: Literal["person", "development"]
    blocking: bool = False
    story_id: str | None = None
    claim_token: str | None = None
    created_by: str

    @model_validator(mode="after")
    def _blocking_needs_a_claim(self) -> Self:
        if self.blocking and (self.origin != "development" or not self.story_id or not self.claim_token):
            raise ValueError("a blocking gap is a development gap with a story id and a claim token")
        return self


class AnswerResult(GapModel):
    """What ``answer_gap`` returns: the new clarification, the clarification it replaced (if any), and when the
    answers of the project are applied (``immediate``: at once, for a development gap that blocks its story)."""

    gap_id: str
    clr_id: str
    replaced: str | None = None
    placement: ClarificationPlacement
    amendments: tuple[str, ...] = ()
    apply_at: AwareDatetime
    immediate: bool = False


class ApplyResult(GapModel):
    """What ``apply_now`` (and the quiet-period apply) returns: the gaps moved to ``applying``, the clarifications
    whose amendments became active and the build request's answer (``None`` without a ``BuildRequests`` port)."""

    gap_ids: tuple[str, ...] = ()
    clr_ids: tuple[str, ...] = ()
    amendments: tuple[str, ...] = ()
    build: str | None = None


class AiAnswer(GapModel):
    """One row of the review list "Answered by AI" (answers.md "Review list"): a clarification of mode ``ai`` or
    ``pointer`` with its gap's title, question and status, and the follow-up gap a revert opened."""

    gap_id: str
    title: str = ""
    question: str = ""
    gap_status: GapStatus
    clarification: ClarificationView
    follow_up: str | None = None


class AiAnswerList(GapModel):
    """What ``GapService.ai_answers`` returns: AI and pointer answers, newest first."""

    answers: tuple[AiAnswer, ...] = ()


class RevertResult(GapModel):
    """What ``revert_clarification`` returns: the reverted clarification, the gap a person answers next (the
    follow-up of a resolved gap, or the gap itself back ``open``), the amendments reverted and the build request's
    answer (None when no text left the corpus)."""

    clr_id: str
    gap_id: str
    follow_up: bool
    amendments: tuple[str, ...] = ()
    build: str | None = None


class SectionPreview(GapModel):
    """One edit of a preview: where it goes (fragment and document lineage, or the clarifications document), the
    section's heading path, its current text and its text after the edit."""

    fragment_id: str | None = None
    lineage: str | None = None
    heading_path: tuple[str, ...] = ()
    before: str = ""
    after: str


class EditPreview(GapModel):
    """What ``preview_edits`` returns: the checked edits an answer would make and each target section before and
    after. ``drafted`` when ``gap_edits`` wrote the edits of an own text; ``fallback`` names why the text goes to the
    clarifications document as written instead (the call failed or its edits did not check)."""

    gap_id: str
    placement: EditPlacement
    edits: tuple[Edit, ...]
    sections: tuple[SectionPreview, ...]
    drafted: bool = False
    fallback: str | None = None


class RaisedGap(GapModel):
    """What ``raise_gap`` returns: the gap id, and whether it is new (a raised question already open is that gap)."""

    gap_id: str
    created: bool = True


# Reconcile


class AmendmentResult(GapModel):
    """The outcome of one amendment against a new source revision (docs/graph/gap/reconcile.md "The four questions"):
    ``anchor`` is the new anchor when re-anchored (or the owner's line a conflict suspends it on), ``line`` the line
    or section heading that decided it, ``similarity`` the line's or section's similarity, ``verdict`` and
    ``confidence`` the same-meaning judge's answer when it was asked (``failed`` when the call failed)."""

    amendment_id: str
    clr_id: str = ""
    outcome: ReconcileOutcome
    anchor: AmendmentAnchor | None = None
    line: str | None = None
    similarity: float | None = None
    verdict: str | None = None
    confidence: float | None = None


class ReconcileResult(GapModel):
    """Every amendment's outcome and the effective text the build must read (reconcile.md "Where it runs").

    ``changed`` are the amendment rows to store (status, anchor, ``last_revision_id``); ``applied`` the rows applied
    to ``text`` (an ``undecided`` amendment as an insertion after its section, never stored so); ``follow_ups`` the
    clarifications whose amendments became ``suspended`` or ``orphaned`` (the next gap run opens their follow-ups)."""

    amendments: tuple[AmendmentResult, ...] = ()
    text: str
    changed: tuple[AmendmentRow, ...] = ()
    applied: tuple[AmendmentRow, ...] = ()
    follow_ups: tuple[str, ...] = ()


# First slice: findings (docs/graph/gap.md "Library: Findings")


class FindingRef(GapModel):
    """A ``refers_to`` entry: where a finding comes from. Stored, never fetched."""

    type: RefType
    value: str


class Finding(GapModel):
    """One finding with its references; ``uuid`` and ``dedupe_key`` stay inside the store."""

    finding_id: str
    kind: FindingKind
    text: str
    origin: str
    status: FindingStatus
    blocking: bool
    about_spec_ids: tuple[str, ...]
    story_id: str | None = None
    created_by: str
    created_at: AwareDatetime
    resolved_by: str | None = None
    resolved_at: AwareDatetime | None = None
    resolution_note: str | None = None
    orphaned: bool = False
    updated_at: AwareDatetime | None = None
    refers_to: tuple[FindingRef, ...] = ()


class AddedFinding(GapModel):
    """The answer to ``add``: the finding and whether this call created it (false: an open one already matched)."""

    finding: Finding
    created: bool


class FindingPage(GapModel):
    """One page of findings; ``next`` is the opaque ``after`` of the following page, or none."""

    findings: tuple[Finding, ...]
    next: str | None = None


class FindingRefresh(GapModel):
    """What ``refresh`` did: open findings whose ids or edges it updated, and those it left orphaned."""

    refreshed: int
    orphaned: int
