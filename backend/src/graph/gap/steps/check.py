"""Step ``check``: validate each investigation's report in code before anything is written (docs/graph/gap/
investigation.md "Checks", "Second check", "Pointer answers"; FR-GINV-05, FR-GINV-06, FR-GINV-07, FR-GRUN-04,
NFR-GSEC-01).

Per investigated site:

- A failed investigation (an error, or no report) defers the site to the next run (FR-GRUN-04).
- ``not_a_gap`` and ``not_spec`` become those outcomes; ``duplicate_of`` names an existing gap (else the report is
  rejected).
- A ``gap`` report passes ``validate_report``: ids resolve to active spec nodes and were shown by the pack or a read
  (no new ids), witness, basis and pointer quotes snap to their shown, active fragment, the text fields fit their
  limits (cut at a sentence boundary when over by at most 10 %), the options number ``options_min`` to
  ``options_max`` with exactly one recommended and distinct labels, and every edit's anchor resolves
  (``check_edit``). A failed id, quote or edit is dropped with a warning; a report left without an ``ABOUT`` node or
  a witness, with too few options or with a text it cannot fit is rejected. An options or edit-anchor failure is
  repairable once: one repair call through ``gap_investigate`` (prompt ``gap.investigate.<rev>.repair``) with the
  failures and the fragments the report names, then the checks again.
- Duplicates: a signature some open or resolved gap has is that gap (the record step re-checks it); a dismissed
  signature (``GapRecords.suppressed``) is dropped; a question whose embedding has cosine at least
  ``DUPLICATE_COSINE`` with the question of an open gap whose ``ABOUT`` set overlaps becomes ``duplicate`` of that gap
  (FR-GINV-06).
- A pointer (a stated line that already answers the gap) goes to the second check (gateway role ``gap_judge``),
  which sees only the question, the witness quotes and the pointer line; the pointer is confirmed when the judge
  finds it entailed and consistent with confidence at least ``routing.gates.min_confidence`` (FR-GINV-07). A
  contradiction never gets a pointer.
- A new gap that could be answered by AI (``route.ai_candidate``: ``minor``, a kind of ``routing.ai_kinds``, AI answers
  on, a recommended option with a basis quote, the local gate holding) goes to the second check of an AI answer
  (prompt ``AI_ANSWER_PROMPT``), which sees only the witness quotes, the recommended option's basis quotes and its
  edits (answers.md "Gates"); the route step decides with its answer. A failed call is no answer: the gap is routed
  ``ask`` (``ai_judge_failed``), never deferred.

A rejected report gives the outcome ``not_a_gap`` with the failed checks (``rejected``); the record step logs them
and caches the verdict, so the same inputs are not investigated again. A model failure in a repair or a judge call
defers the site. Only what passed is passed on: the record step writes nothing the checks did not keep (NFR-GSEC-01).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph.core.errors import GraphError, LLMError
from graph.core.llm.gateway import estimate_tokens, untrusted
from graph.gap.gaps import StoredGap
from graph.gap.identity import normalise_quote
from graph.gap.models import Edit, GapReport, Quote, ReadResult, ReportOption, WitnessRef
from graph.gap.names import SiteOutcome
from graph.gap.ports import GapRecords
from graph.gap.prompts import AI_ANSWER_PROMPT, JUDGE_PROMPT, REPAIR_PROMPT, prompt_text
from graph.gap.rules import Gates
from graph.gap.services.vector_similarity import cosine
from graph.gap.settings import GapLimits
from graph.gap.steps.investigate import FinalTurn, Investigated, report_of
from graph.gap.steps.reads import GraphSiteReads, cut_items
from graph.gap.steps.route import ai_candidate, recommended
from graph.gap.steps.sites import Site
from graph.gap.steps.snapshot import GraphSnapshot
from logger.logger import WARNING, get_logger, log_event

logger = get_logger(__name__)

TITLE_CHARS: Final = (8, 160)
QUESTION_CHARS: Final = (10, 1000)
WHY_CHARS: Final = (0, 2000)
AI_REASON_CHARS: Final = (0, 500)
NOT_SPEC_REASON_CHARS: Final = (0, 300)
LABEL_CHARS: Final = (3, 120)
DETAIL_CHARS: Final = (0, 600)
EDIT_TEXT_CHARS: Final = (1, 2000)
QUOTE_CHARS: Final = 400
ABOUT_MAX: Final = 20
IMPACTS_MAX: Final = 20
WITNESS_MAX: Final = 6
EDITS_MAX: Final = 6
BASIS_MAX: Final = 6
OVER_SHARE: Final = 0.10
"""A text over its limit by at most this share is cut at a sentence boundary; a longer one fails."""
DUPLICATE_COSINE: Final = 0.9
JUDGE_CALL_CLASS: Final = "judge"
REPAIR_CALL_CLASS: Final = "extraction"
INVESTIGATE_ROLE: Final = "gap_investigate"
JUDGE_ROLE: Final = "gap_judge"
OPEN_STATUSES: Final = frozenset({"open", "answered", "applying"})
REPAIRABLE: Final = frozenset({"options_count", "options_recommended", "options_labels", "option_dropped"})
_SENTENCE_END: Final = re.compile(r"[.!?](?=\s|$)")
_HEADING: Final = re.compile(r"^\s{0,3}#", re.MULTILINE)
_VERDICT_OUTCOMES: Final[dict[str, SiteOutcome]] = {"not_a_gap": "not_a_gap", "not_spec": "not_spec"}


def fit(text: str, limits: tuple[int, int]) -> str | None:
    """``text`` stripped and within ``limits`` (min, max characters): as it is, cut at the last sentence end (else word
    break) inside the maximum when it is over by at most ``OVER_SHARE``, or None."""

    low, high = limits
    clean = text.strip()
    if len(clean) < low:
        return None
    if len(clean) <= high:
        return clean
    if len(clean) > high * (1 + OVER_SHARE):
        return None
    head = clean[:high]
    ends = [match.end() for match in _SENTENCE_END.finditer(head)]
    cut = ends[-1] if ends else head.rfind(" ")
    found = head[:cut].strip() if cut > 0 else ""
    return found if len(found) >= low else None


def snaps(snapshot: GraphSnapshot, fragment_id: str | None, quote: str | None) -> bool:
    """The quote is found in the active fragment's text (whitespace and quote marks normalised)."""

    if fragment_id is None or not quote or not quote.strip():
        return False
    fragment = snapshot.fragments.get(fragment_id)
    return fragment is not None and normalise_quote(quote) in normalise_quote(fragment.text)


def anchor_line(snapshot: GraphSnapshot, fragment_id: str, anchor: str) -> str | None:
    """The one line of the fragment the anchor quote names (equal to it, or the only line containing it), or None."""

    wanted = normalise_quote(anchor)
    if not wanted:
        return None
    lines = [normalise_quote(line.text) for line in snapshot.fragment_lines(fragment_id)]
    if wanted in lines:
        return wanted
    holding = [line for line in lines if wanted in line]
    return holding[0] if len(holding) == 1 else None


@dataclass
class SiteView:
    """What the checks know of one site: the snapshot, the ids shown to the investigator and the site's seeds."""

    snapshot: GraphSnapshot
    site: Site
    shown_spec_ids: frozenset[str]
    shown_fragment_ids: frozenset[str]
    limits: GapLimits

    @classmethod
    def of(cls, snapshot: GraphSnapshot, site: Site, investigated: Investigated, limits: GapLimits) -> SiteView:
        seeds_ids = {spec_id for seed in site.seeds for spec_id in (*seed.about, *seed.context)}
        seeds_fragments = {item.fragment_id for seed in site.seeds for item in seed.witness if item.fragment_id}
        return cls(
            snapshot=snapshot,
            site=site,
            shown_spec_ids=frozenset({*seeds_ids, *site.about, *site.context, *investigated.shown_spec_ids}),
            shown_fragment_ids=frozenset({*seeds_fragments, *investigated.shown_fragment_ids}),
            limits=limits,
        )

    def fragment_ok(self, fragment_id: str | None) -> bool:
        return (
            fragment_id is not None
            and fragment_id in self.shown_fragment_ids
            and fragment_id in self.snapshot.fragments
        )

    def quote_ok(self, fragment_id: str | None, quote: str | None) -> bool:
        return (
            self.fragment_ok(fragment_id)
            and quote is not None
            and len(quote) <= QUOTE_CHARS
            and snaps(self.snapshot, fragment_id, quote)
        )


@dataclass
class Validation:
    """The outcome of ``validate_report``: the kept report, warnings, and the checks that failed."""

    report: GapReport | None
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def repairable(self) -> bool:
        return bool(self.failures) and set(self.failures) <= REPAIRABLE


def _ids(view: SiteView, ids: Sequence[str], limit: int, name: str, warnings: list[str]) -> tuple[str, ...] | None:
    kept: list[str] = []
    for spec_id in dict.fromkeys(ids):
        if spec_id not in view.snapshot.nodes:
            warnings.append(f"{name}_unresolved")
        elif spec_id not in view.shown_spec_ids:
            warnings.append(f"{name}_not_shown")
        else:
            kept.append(spec_id)
    if len(kept) > limit:
        if len(kept) > limit * (1 + OVER_SHARE):
            return None
        warnings.append(f"{name}_cut")
        kept = kept[:limit]
    return tuple(kept)


def _witness_ref(view: SiteView, ref: WitnessRef) -> WitnessRef | None:
    """The entry as one kind of evidence: its quote when it snaps, else its fact when that names a seed's graph fact
    (a model may fill both fields of one entry); None when neither holds."""

    if ref.quote is not None and view.quote_ok(ref.fragment_id, ref.quote):
        return WitnessRef(fragment_id=ref.fragment_id, quote=ref.quote)
    fact = ref.fact
    if fact is not None and 0 <= fact < len(view.site.seeds) and any(i.fact for i in view.site.seeds[fact].witness):
        return WitnessRef(fact=fact)
    return None


def _witness(view: SiteView, report: GapReport, warnings: list[str]) -> tuple[WitnessRef, ...]:
    kept: list[WitnessRef] = []
    for ref in report.witness:
        found = _witness_ref(view, ref)
        if found is None:
            warnings.append("witness_dropped")
        elif found not in kept:
            kept.append(found)
    return tuple(kept[:WITNESS_MAX])


def _quotes(view: SiteView, quotes: Sequence[Quote], warnings: list[str], name: str) -> tuple[Quote, ...]:
    kept = []
    for item in quotes:
        if view.quote_ok(item.fragment_id, item.quote):
            kept.append(item)
        else:
            warnings.append(f"{name}_dropped")
    return tuple(kept[:BASIS_MAX])


def check_edit(view: SiteView, edit: Edit, report: GapReport) -> tuple[Edit | None, str | None]:
    """One edit of an option against investigation.md "Options and edits" and answers.md "Edits": the kept edit (text
    fitted), or None and the failed rule."""

    text = fit(edit.text, EDIT_TEXT_CHARS)
    failed = "edit_text_length" if text is None else "edit_text_heading" if _HEADING.search(text) else None
    if failed is None:
        failed = _document_target(edit) if edit.placement == "document" else _amend_target(view, edit, report)
    return (None, failed) if failed is not None else (edit.model_copy(update={"text": text}), None)


def _document_target(edit: Edit) -> str | None:
    """A clarifications-document edit adds a section under a heading."""

    ok = edit.operation == "append_section" and bool((edit.target.heading or "").strip())
    return None if ok else "edit_document_target"


def _amend_target(view: SiteView, edit: Edit, report: GapReport) -> str | None:
    """An amendment names a shown, active fragment and one of its lines; a ``replace`` only a line of the gap."""

    target = edit.target
    if edit.operation == "append_section":
        return "edit_operation"
    if not view.fragment_ok(target.fragment_id) or not target.anchor_quote:
        return "edit_target"
    fragment_id = str(target.fragment_id)
    line = anchor_line(view.snapshot, fragment_id, target.anchor_quote)
    if line is None:
        return "edit_anchor"
    if edit.operation == "replace" and not _replaceable(view, report, fragment_id, line):
        return "edit_replace_outside_gap"
    return None


def _replaceable(view: SiteView, report: GapReport, fragment_id: str, line: str) -> bool:
    """A ``replace`` targets a line the gap cites or a line of a node it is ``ABOUT``."""

    cited = [normalise_quote(ref.quote) for ref in report.witness if ref.fragment_id == fragment_id and ref.quote]
    if any(quote and (quote in line or line in quote) for quote in cited):
        return True
    for spec_id in report.about:
        node = view.snapshot.nodes.get(spec_id)
        if node is None:
            continue
        for item in view.snapshot.lines(node):
            text = normalise_quote(item.text)
            if item.fragment_id == fragment_id and text and (text in line or line in text):
                return True
    return False


def _option(view: SiteView, option: ReportOption, report: GapReport, warnings: list[str]) -> ReportOption | None:
    label = fit(option.label, LABEL_CHARS)
    detail = fit(option.detail, DETAIL_CHARS)
    if label is None or detail is None:
        warnings.append("option_text_length")
        return None
    if not 1 <= len(option.edits) <= EDITS_MAX:
        warnings.append("option_edits_count")
        return None
    edits: list[Edit] = []
    anchors: set[tuple[str, str]] = set()
    for edit in option.edits:
        kept, failed = check_edit(view, edit, report)
        if kept is None:
            warnings.append(str(failed))
            return None
        if kept.placement == "amend":
            key = (str(kept.target.fragment_id), normalise_quote(str(kept.target.anchor_quote)))
            if key in anchors:
                warnings.append("edit_anchor_twice")
                return None
            anchors.add(key)
        edits.append(kept)
    basis = _quotes(view, option.basis, warnings, "basis")
    return option.model_copy(update={"label": label, "detail": detail, "edits": tuple(edits), "basis": basis})


def _options(view: SiteView, report: GapReport, warnings: list[str], failures: list[str]) -> tuple[ReportOption, ...]:
    kept = [found for option in report.options if (found := _option(view, option, report, warnings)) is not None]
    if len(kept) < len(report.options):
        failures.append("option_dropped")
    limits = view.limits
    if not limits.options_min <= len(kept) <= limits.options_max:
        failures.append("options_count")
    if sum(1 for option in kept if option.recommended) != 1:
        failures.append("options_recommended")
    if len({option.label.casefold() for option in kept}) != len(kept):
        failures.append("options_labels")
    return tuple(kept)


def validate_report(view: SiteView, report: GapReport) -> Validation:
    """The checks of a ``gap`` report (module docstring); ``failures`` empty means it passed."""

    warnings: list[str] = []
    failures: list[str] = []
    if report.kind is None:
        return Validation(None, warnings, ["kind_missing"])
    about = _ids(view, report.about, ABOUT_MAX, "about", warnings)
    impacts = _ids(view, report.impacts, IMPACTS_MAX, "impacts", warnings)
    if about is None or impacts is None:
        return Validation(None, warnings, ["ids_length"])
    note = report.severity == "info"  # a run note needs no node and no witness (answers.md "Severity")
    if not about and not note:
        return Validation(None, warnings, ["about_empty"])
    impacts = tuple(spec_id for spec_id in impacts if spec_id not in about)
    witness = _witness(view, report, warnings)
    if not witness and not note:
        return Validation(None, warnings, ["witness_empty"])
    texts = {
        "title": fit(report.title, TITLE_CHARS),
        "question": fit(report.question, QUESTION_CHARS),
        "why": fit(report.why, WHY_CHARS),
        "ai_reason": fit(report.ai_reason, AI_REASON_CHARS),
    }
    failed = [f"{name}_length" for name, value in texts.items() if value is None]
    question = texts["question"]
    if question is not None and not question.endswith("?"):
        failed.append("question_form")
    if failed:
        return Validation(None, warnings, failed)
    pointer = report.pointer
    if pointer is not None and not view.quote_ok(pointer.fragment_id, pointer.quote):
        warnings.append("pointer_dropped")
        pointer = None
    if report.kind not in {seed.kind for seed in view.site.seeds}:
        warnings.append("kind_changed")
    checked = report.model_copy(
        update={**texts, "about": about, "impacts": impacts, "witness": witness, "pointer": pointer, "options": ()}
    )
    if report.severity != "info" and (report.options or pointer is None):  # a pointer may come without options
        options = _options(view, checked.model_copy(update={"options": report.options}), warnings, failures)
        checked = checked.model_copy(update={"options": options})
    return Validation(checked if not failures else None, warnings, failures)


# Model calls of the check step: the repair call and the second check


class JudgeAnswer(BaseModel):
    """The second check's answer (investigation.md "Second check")."""

    model_config = ConfigDict(extra="forbid")

    entailed: bool = False
    consistent: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    find_words: str | None = None


@dataclass
class Usage:
    """Model calls and estimated input tokens of the check step, per gateway role."""

    calls: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)

    def add(self, role: str, messages: Sequence[Mapping[str, Any]]) -> None:
        self.calls[role] = self.calls.get(role, 0) + 1
        self.tokens[role] = self.tokens.get(role, 0) + sum(estimate_tokens(str(m["content"])) for m in messages)


def _fragment_blocks(view: SiteView, report: GapReport) -> list[str]:
    named = [ref.fragment_id for ref in report.witness if ref.fragment_id]
    named += [edit.target.fragment_id for option in report.options for edit in option.edits if edit.target.fragment_id]
    named += [item.fragment_id for option in report.options for item in option.basis]
    return [
        untrusted(f"fragment {fragment_id}", view.snapshot.fragments[fragment_id].text)
        for fragment_id in dict.fromkeys(named)
        if view.fragment_ok(fragment_id)
    ]


def repair_messages(
    view: SiteView, report: GapReport, failures: Sequence[str], warnings: Sequence[str]
) -> list[dict[str, Any]]:
    """The repair call: the failed checks, the fragments the report names (data blocks) and the report itself."""

    head = json.dumps(
        {
            "failed_checks": sorted(set(failures)),
            "dropped": sorted(set(warnings)),
            "options_min": view.limits.options_min,
            "options_max": view.limits.options_max,
        },
        sort_keys=True,
    )
    body = untrusted("your report", report.model_dump_json(exclude_none=True))
    user = "\n\n".join(
        [
            (
                "Your report failed these checks. Correct it: options need edits whose anchor_quote is one whole "
                "line of the target fragment, exactly one recommended option and distinct labels. Answer with the "
                "corrected report."
            ),
            head,
            body,
            *_fragment_blocks(view, report),
        ]
    )
    return [{"role": "system", "content": prompt_text("investigate")}, {"role": "user", "content": user}]


def _witness_blocks(report: GapReport) -> list[str]:
    return [
        untrusted(f"witness {number} {ref.fragment_id}", str(ref.quote))
        for number, ref in enumerate((ref for ref in report.witness if ref.quote), start=1)
    ]


def _judge_user(mode: str, blocks: list[str], found: ReadResult | None) -> str:
    if found is not None:
        items, _ = cut_items(found.items)
        blocks.append(untrusted("retrieved lines", json.dumps(list(items), ensure_ascii=False, default=str)))
    head = json.dumps({"mode": mode, "retrieved": found is not None}, sort_keys=True)
    return "\n\n".join(["Check this answer (JSON, then data blocks).", head, *blocks])


def judge_messages(report: GapReport, found: ReadResult | None = None) -> list[dict[str, Any]]:
    """The second check of a pointer: the question, the witness quotes and the pointer line, nothing else."""

    blocks = [untrusted("question", report.question), *_witness_blocks(report)]
    pointer = report.pointer
    if pointer is not None:
        blocks.append(untrusted(f"basis 1 {pointer.fragment_id}", pointer.quote))
        blocks.append(untrusted("answer", pointer.quote))
    user = _judge_user("pointer", blocks, found)
    return [{"role": "system", "content": prompt_text("judge")}, {"role": "user", "content": user}]


def ai_answer_messages(report: GapReport, found: ReadResult | None = None) -> list[dict[str, Any]]:
    """The second check of an AI answer (investigation.md "Second check"): the question, the witness quotes, the
    recommended option's basis quotes and its edits (placement, operation, text), nothing of the investigator's
    reasoning, labels or details."""

    option = recommended(report)
    blocks = [untrusted("question", report.question), *_witness_blocks(report)]
    if option is not None:
        blocks += [
            untrusted(f"basis {number} {item.fragment_id}", item.quote)
            for number, item in enumerate(option.basis, start=1)
        ]
        blocks += [
            untrusted(f"edit {number} {edit.placement} {edit.operation}", edit.text)
            for number, edit in enumerate(option.edits, start=1)
        ]
    user = _judge_user("ai", blocks, found)
    return [{"role": "system", "content": prompt_text("ai_answer")}, {"role": "user", "content": user}]


JudgeMessages = Callable[[GapReport, ReadResult | None], list[dict[str, Any]]]


@dataclass(frozen=True)
class CheckRun:
    """What the check step needs of the run."""

    project_id: str
    run_id: str
    snapshot: GraphSnapshot
    limits: GapLimits
    records: GapRecords
    gateway: Any
    reader: Any = None
    gaps: Sequence[StoredGap] = ()
    min_confidence: float = 0.85
    signature: Any = None
    """``(site, report) -> str``: the report's gap signature (the record step's)."""
    gates: Gates | None = None
    """The routing gates; None asks no second check of an AI answer."""
    ai_kinds: Collection[str] = ()
    ai_answers: bool = False


@dataclass(frozen=True)
class Checked:
    """A site's outcome after the check, the checked report, warnings and the failed checks of a rejected report;
    ``gap_id`` names the gap a duplicate repeats; ``judge`` holds the second check of a pointer, ``pointer`` whether it
    was confirmed; ``ai_judge`` holds the second check of an AI answer (``ai_judge_failed`` when its call failed);
    ``new`` is false for a report that re-checks a gap in the graph."""

    outcome: SiteOutcome
    report: GapReport | None = None
    warnings: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    gap_id: str | None = None
    judge: Mapping[str, Any] | None = None
    pointer: bool = False
    ai_judge: Mapping[str, Any] | None = None
    ai_judge_failed: bool = False
    new: bool = True

    def payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "report": None if self.report is None else self.report.model_dump(mode="json"),
            "warnings": list(self.warnings),
            "rejected": list(self.rejected),
            "gap_id": self.gap_id,
            "judge": None if self.judge is None else dict(self.judge),
            "pointer": self.pointer,
            "ai_judge": None if self.ai_judge is None else dict(self.ai_judge),
            "ai_judge_failed": self.ai_judge_failed,
            "new": self.new,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Checked:
        report = payload.get("report")
        return cls(
            outcome=payload["outcome"],
            report=None if report is None else GapReport.model_validate(report),
            warnings=tuple(payload.get("warnings", ())),
            rejected=tuple(payload.get("rejected", ())),
            gap_id=payload.get("gap_id"),
            judge=payload.get("judge"),
            pointer=bool(payload.get("pointer", False)),
            ai_judge=payload.get("ai_judge"),
            ai_judge_failed=bool(payload.get("ai_judge_failed", False)),
            new=bool(payload.get("new", True)),
        )


class Checker:
    """Checks the reports of one run (module docstring)."""

    def __init__(self, run: CheckRun) -> None:
        self.run = run
        self.usage = Usage()
        self.by_signature = {gap.signature: gap for gap in run.gaps if gap.signature}
        self.by_id = {gap.gap_id: gap for gap in run.gaps}

    async def check(self, site: Site, investigated: Investigated) -> Checked:
        result = investigated.result
        report = result.report
        if result.error is not None or report is None:
            return Checked("deferred", warnings=(result.error or "no_report",))
        if report.verdict != "gap":
            return self._verdict(report, site)
        view = SiteView.of(self.run.snapshot, site, investigated, self.run.limits)
        validation = validate_report(view, report)
        if validation.report is None and validation.repairable:
            try:
                repaired = await self._repair(view, report, validation)
            except (LLMError, ValidationError) as error:
                return Checked("deferred", warnings=(*validation.warnings, _code(error)))
            again = validate_report(view, repaired)
            validation = Validation(again.report, [*validation.warnings, "repaired", *again.warnings], again.failures)
        warnings = tuple(dict.fromkeys(validation.warnings))
        if validation.report is None:
            self._log_rejected(site, validation.failures)
            return Checked("not_a_gap", report, warnings, tuple(dict.fromkeys(validation.failures)))
        return await self._duplicates_and_pointer(site, validation.report, warnings)

    def _verdict(self, report: GapReport, site: Site) -> Checked:
        """A report that is not a gap: ``not_a_gap``, ``not_spec``, or ``duplicate`` of an existing gap."""

        if report.verdict in _VERDICT_OUTCOMES:
            reason = fit(report.not_spec_reason or "", NOT_SPEC_REASON_CHARS) or ""
            kept = report.model_copy(update={"options": (), "not_spec_reason": reason or None})
            return Checked(_VERDICT_OUTCOMES[report.verdict], kept)
        known = self.by_id.get(report.duplicate_of or "")
        if known is None or known.gap_id == site.gap_id:  # a raised gap is never a duplicate of itself
            return Checked("not_a_gap", report, ("duplicate_unknown",), ("duplicate_of",))
        return Checked("duplicate", report.model_copy(update={"options": ()}), gap_id=known.gap_id)

    async def _repair(self, view: SiteView, report: GapReport, validation: Validation) -> GapReport:
        messages = repair_messages(view, report, validation.failures, validation.warnings)
        self.usage.add(INVESTIGATE_ROLE, messages)
        turn: FinalTurn = await self.run.gateway.structured(
            REPAIR_CALL_CLASS, None, messages, FinalTurn, prompt=REPAIR_PROMPT
        )
        return report_of(turn.report)

    async def _duplicates_and_pointer(self, site: Site, report: GapReport, warnings: tuple[str, ...]) -> Checked:
        signature = self.run.signature(site, report) if self.run.signature is not None else ""
        if (
            signature
            and signature not in self.by_signature
            and signature in await self.run.records.suppressed(self.run.project_id, [signature])
        ):
            return Checked("duplicate", report, (*warnings, "suppressed"))
        if signature not in self.by_signature:
            similar = await self._similar(site, report)
            if similar is not None:
                return Checked("duplicate", report, warnings, gap_id=similar)
        new = site.gap_id is None and signature not in self.by_signature
        seed_kinds = tuple(seed.kind for seed in site.seeds)
        if report.pointer is None or "contradiction" in {report.kind, *seed_kinds}:
            return await self._ai_answer(Checked("gap", report, warnings, new=new), seed_kinds)
        try:
            judge = await self._judge(report)
        except (LLMError, ValidationError) as error:
            return Checked("deferred", warnings=(*warnings, _code(error)))
        confirmed = judge.entailed and judge.consistent and judge.confidence >= self.run.min_confidence
        checked = Checked(
            "gap",
            report,
            warnings if confirmed else (*warnings, "pointer_not_confirmed"),
            judge=judge.model_dump(mode="json"),
            pointer=confirmed,
            new=new,
        )
        return checked if confirmed else await self._ai_answer(checked, seed_kinds)

    async def _ai_answer(self, checked: Checked, seed_kinds: Sequence[str]) -> Checked:
        """The second check of an AI answer when the gap is an AI candidate (module docstring); else ``checked``."""

        run, report = self.run, checked.report
        if (
            run.gates is None
            or report is None
            or not ai_candidate(
                report,
                run.snapshot,
                gates=run.gates,
                ai_kinds=run.ai_kinds,
                ai_answers=run.ai_answers,
                new=checked.new,
                seed_kinds=seed_kinds,
            )
        ):
            return checked
        try:
            answer = await self._ask_judge(report, ai_answer_messages, AI_ANSWER_PROMPT)
        except (LLMError, ValidationError) as error:
            return replace(checked, warnings=(*checked.warnings, _code(error)), ai_judge_failed=True)
        return replace(checked, ai_judge=answer.model_dump(mode="json"))

    async def _similar(self, site: Site, report: GapReport) -> str | None:
        """The open gap whose ``ABOUT`` overlaps and whose question reads the same (cosine at least
        ``DUPLICATE_COSINE``), or None; an embedding failure finds none."""

        about = set(report.about)
        candidates = [
            gap
            for gap in self.run.gaps
            if gap.status in OPEN_STATUSES
            and gap.gap_id != site.gap_id
            and set(gap.about_ids) & about
            and str(gap.props.get("question") or "").strip()
        ]
        if not candidates:
            return None
        texts = [report.question, *(str(gap.props["question"]) for gap in candidates)]
        try:
            vectors = await self.run.gateway.embed(texts)
        except GraphError as error:
            log_event(logger, WARNING, "graph.gap.check.embedding_failed", run_id=self.run.run_id, code=error.code)
            return None
        scored = [(cosine(vectors[0], vector), gap.gap_id) for vector, gap in zip(vectors[1:], candidates, strict=True)]
        best = max(scored, default=(0.0, ""))
        return best[1] if best[0] >= DUPLICATE_COSINE else None

    async def _judge(self, report: GapReport) -> JudgeAnswer:
        return await self._ask_judge(report, judge_messages, JUDGE_PROMPT)

    async def _ask_judge(self, report: GapReport, build: JudgeMessages, prompt: str) -> JudgeAnswer:
        """One second check, and once more with the lines of its one ``find_fragments`` read when it asks for one."""

        messages = build(report, None)
        self.usage.add(JUDGE_ROLE, messages)
        answer: JudgeAnswer = await self.run.gateway.structured(
            JUDGE_CALL_CLASS, None, messages, JudgeAnswer, prompt=prompt
        )
        words = (answer.find_words or "").strip()
        if not words or self.run.reader is None:
            return answer
        found = await GraphSiteReads(self.run.reader, self.run.project_id, run_id=self.run.run_id).find_fragments(words)
        messages = build(report, found)
        self.usage.add(JUDGE_ROLE, messages)
        final: JudgeAnswer = await self.run.gateway.structured(
            JUDGE_CALL_CLASS, None, messages, JudgeAnswer, prompt=prompt
        )
        return final.model_copy(update={"find_words": words})

    def _log_rejected(self, site: Site, failures: Collection[str]) -> None:
        log_event(
            logger,
            WARNING,
            "graph.gap.check.rejected",
            project_id=self.run.project_id,
            run_id=self.run.run_id,
            site_id=site.site_id,
            checks=",".join(sorted(set(failures))),
        )


def _code(error: Exception) -> str:
    return error.code if isinstance(error, GraphError) and error.code else "llm_malformed"
