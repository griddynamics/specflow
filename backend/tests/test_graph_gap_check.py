"""The check and route steps: report validation, repair, duplicates, the second check and routing
(docs/graph/gap/investigation.md "Checks", "Second check", "Pointer answers"; answers.md "Severity", "Routing";
FR-GINV-05 to FR-GINV-07, FR-GRTE-01, FR-GRTE-02, FR-GRTE-06)."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

from gap_acceptance_support import RecordedGapGateway
from gap_detection_support import PROJECT, MiniGraph
from gap_port_fakes import MemoryGapRecords
from graph.core import load_ontology
from graph.core.errors import GraphError, LLMError
from graph.gap.gaps import StoredGap, StoredLink
from graph.gap.models import (
    Edit,
    EditTarget,
    GapReport,
    InvestigationResult,
    Quote,
    ReadResult,
    ReportOption,
    SuppressionRow,
    Witness,
    WitnessRef,
)
from graph.gap.prompts import JUDGE_PROMPT, REPAIR_PROMPT
from graph.gap.rules import load_gap_rules
from graph.gap.steps.check import (
    Checked,
    Checker,
    CheckRun,
    SiteView,
    anchor_line,
    check_edit,
    cosine,
    fit,
    judge_messages,
    snaps,
    validate_report,
)
from graph.gap.steps.detect import Seed
from graph.gap.steps.investigate import Investigated
from graph.gap.steps.record import report_signature
from graph.gap.steps.route import Candidate, hold_back, route_report, routed_payload
from graph.gap.steps.sites import Site
from graph_support import ScriptedRequest

LINE = "Orders over 1,000 EUR are reviewed."
OTHER = "Payments over 500 EUR are held for review."
AT = "2026-11-01T00:00:00+00:00"


def graph() -> tuple[MiniGraph, str, str]:
    mini = MiniGraph()
    orders = mini.section("Orders", LINE, "Orders are shipped once paid.")
    mini.node("REQ::BR::REVIEW", "BusinessRule", "Review", quote=LINE)
    payments = mini.section("Payments", OTHER, document="payments.md")
    mini.node("REQ::BR::HOLD", "BusinessRule", "Hold", quote=OTHER, fragment=payments)
    mini.node("REQ::FR::HIDDEN", "FunctionalRequirement", "Hidden")
    mini.node("MOD::LEGACY", "Module", "Legacy", in_scope="no")
    return mini, orders, payments


def site(orders: str, payments: str, *, gap_id: str | None = None) -> Site:
    seed = Seed(
        detector="CT-VALUE",
        kind="contradiction",
        family="contradiction",
        strength="strong",
        about=("REQ::BR::REVIEW", "REQ::BR::HOLD"),
        witness=(
            Witness(fragment_id=orders, quote=LINE),
            Witness(fragment_id=payments, quote=OTHER),
            Witness(fact="refused dedup"),
        ),
    )
    return Site("SITE::1", (seed,), seed.about, (), "hash-1", 1.0, 0, gap_id=gap_id)


def option(
    label: str, fragment: str, anchor: str, *, recommended: bool = False, operation: str = "replace"
) -> ReportOption:
    edit = Edit(
        placement="amend",
        target=EditTarget(fragment_id=fragment, anchor_quote=anchor),
        operation=operation,
        text="Orders over 500 EUR are reviewed.",
    )
    return ReportOption(label=label, recommended=recommended, edits=(edit,))


def report(orders: str, payments: str, **changes: Any) -> GapReport:
    base = GapReport(
        verdict="gap",
        kind="contradiction",
        severity="major",
        title="Two review limits",
        question="Which amount sends an order to review?",
        why="The two documents disagree.",
        about=("REQ::BR::REVIEW", "REQ::BR::HOLD"),
        witness=(WitnessRef(fragment_id=orders, quote=LINE), WitnessRef(fragment_id=payments, quote=OTHER)),
        options=(
            option("A: 500 EUR", orders, LINE, recommended=True),
            option("B: 1,000 EUR", payments, OTHER),
        ),
    )
    return base.model_copy(update=changes)


def view_of(mini: MiniGraph, orders: str, payments: str, **shown: Any) -> SiteView:
    limits = load_gap_rules(ontology=load_ontology()).limits
    investigated = Investigated(InvestigationResult(), shown_fragment_ids=(orders, payments), **shown)
    return SiteView.of(mini.snapshot(), site(orders, payments), investigated, limits)


def checker(mini: MiniGraph, gateway: Any = None, **fields: Any) -> Checker:
    return Checker(
        CheckRun(
            project_id=PROJECT,
            run_id="run-1",
            snapshot=mini.snapshot(),
            limits=load_gap_rules(ontology=load_ontology()).limits,
            records=fields.pop("records", MemoryGapRecords()),
            gateway=gateway or RecordedGapGateway(recordings={}),
            reader=mini.reader(),
            signature=report_signature,
            **fields,
        )
    )


def result(mini: MiniGraph, orders: str, payments: str, found: GapReport | None, **extra: Any) -> Investigated:
    del mini
    return Investigated(InvestigationResult(report=found, **extra), shown_fragment_ids=(orders, payments))


# Pure helpers


def test_fit_cuts_at_a_sentence_or_word_only_when_a_little_over() -> None:
    assert fit("  short  ", (1, 10)) == "short" and fit("ab", (3, 10)) is None
    assert fit("One two. Three four", (1, 18)) == "One two."
    assert fit("aaaa bbbbbbbbbb", (1, 14)) == "aaaa"
    assert fit("x" * 30, (1, 10)) is None and fit("abcdefghijk", (5, 10)) is None
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0 and cosine([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_quotes_snap_and_anchors_name_one_line() -> None:
    mini, orders, _ = graph()
    snapshot = mini.snapshot()

    assert snaps(snapshot, orders, "  Orders over 1,000 EUR  are reviewed. ")
    assert (
        not snaps(snapshot, orders, "Not there.")
        and not snaps(snapshot, None, LINE)
        and not snaps(snapshot, orders, " ")
    )
    assert anchor_line(snapshot, orders, LINE) == LINE and anchor_line(snapshot, orders, "1,000 EUR") == LINE
    assert anchor_line(snapshot, orders, "Orders") is None and anchor_line(snapshot, orders, "  ") is None


def test_edits_are_checked_against_their_target() -> None:
    mini, orders, payments = graph()
    view = view_of(mini, orders, payments)
    gap = report(orders, payments)

    def failed(**fields: Any) -> str | None:
        base = {
            "placement": "amend",
            "target": EditTarget(fragment_id=orders, anchor_quote=LINE),
            "operation": "replace",
            "text": "New text.",
        }
        return check_edit(view, Edit.model_validate({**base, **fields}), gap)[1]

    assert failed() is None
    assert failed(text="") == "edit_text_length" and failed(text="# Heading") == "edit_text_heading"
    assert failed(placement="document", operation="append_section", target=EditTarget(heading="Limits")) is None
    assert failed(placement="document", operation="append_section", target=EditTarget()) == "edit_document_target"
    assert failed(operation="append_section") == "edit_operation"
    assert failed(target=EditTarget(fragment_id="FRG::NONE", anchor_quote=LINE)) == "edit_target"
    assert failed(target=EditTarget(fragment_id=orders, anchor_quote="No such line.")) == "edit_anchor"
    shipped = EditTarget(fragment_id=orders, anchor_quote="Orders are shipped once paid.")
    assert failed(target=shipped) == "edit_replace_outside_gap"
    assert failed(target=shipped, operation="insert_after") is None
    gap_about_only = gap.model_copy(update={"witness": (WitnessRef(fact=0),)})
    assert (
        check_edit(
            view,
            Edit(
                placement="amend",
                target=EditTarget(fragment_id=orders, anchor_quote=LINE),
                operation="replace",
                text="x",
            ),
            gap_about_only,
        )[1]
        is None
    )


def test_a_valid_report_keeps_what_holds_and_drops_the_rest() -> None:
    mini, orders, payments = graph()
    view = view_of(mini, orders, payments)
    loose = report(
        orders,
        payments,
        about=("REQ::BR::REVIEW", "REQ::BR::HOLD", "REQ::NONE", "REQ::FR::HIDDEN"),
        impacts=("REQ::BR::REVIEW", "MOD::LEGACY"),
        witness=(
            *report(orders, payments).witness,
            WitnessRef(fragment_id=orders, quote="Not in the text."),
            WitnessRef(fact=0),
            WitnessRef(fact=5),
        ),
        pointer=Quote(fragment_id=orders, quote="Not there."),
        kind="missing",
        title="Two limits " * 15,
    )

    checked = validate_report(view, loose)

    assert checked.failures == [] and checked.report is not None
    assert checked.report.about == ("REQ::BR::REVIEW", "REQ::BR::HOLD") and checked.report.impacts == ()
    assert len(checked.report.witness) == 3 and checked.report.pointer is None and len(checked.report.title) <= 160
    assert {"about_unresolved", "about_not_shown", "witness_dropped", "pointer_dropped", "kind_changed"} <= set(
        checked.warnings
    )


def test_each_defect_fails_its_check() -> None:
    mini, orders, payments = graph()
    view = view_of(mini, orders, payments)
    good = report(orders, payments)

    def failures(**changes: Any) -> list[str]:
        return validate_report(view, good.model_copy(update=changes)).failures

    assert failures(kind=None) == ["kind_missing"]
    assert failures(about=tuple(f"REQ::BR::REVIEW{n}" for n in range(30))) == ["about_empty"]
    assert failures(witness=(WitnessRef(fragment_id=orders, quote="Nope."),)) == ["witness_empty"]
    assert failures(question="Which amount sends an order to review.") == ["question_form"]
    assert failures(title="short") == ["title_length"]
    assert failures(options=good.options[:1]) == ["options_count"]
    assert failures(options=tuple(item.model_copy(update={"recommended": True}) for item in good.options)) == [
        "options_recommended"
    ]
    assert failures(options=(good.options[0], good.options[1].model_copy(update={"label": "a: 500 eur"}))) == [
        "options_labels"
    ]
    twice = good.options[0].model_copy(update={"edits": good.options[0].edits * 2})
    assert failures(options=(twice, good.options[1])) == ["option_dropped", "options_count", "options_recommended"]
    long_label = good.options[1].model_copy(update={"label": "x" * 200})
    assert "option_dropped" in failures(options=(good.options[0], long_label))
    no_edits = good.options[1].model_copy(update={"edits": ()})
    assert "option_dropped" in failures(options=(good.options[0], no_edits))
    note = validate_report(
        view, good.model_copy(update={"severity": "info", "about": (), "witness": (), "options": ()})
    )
    assert note.failures == [] and note.report is not None
    many = view_of(mini, orders, payments, shown_spec_ids=tuple(f"REQ::BR::REVIEW{n}" for n in range(30)))
    assert validate_report(many, good.model_copy(update={"about": ("REQ::BR::REVIEW",) * 1})).failures == []


# The checker


async def test_the_checker_decides_verdicts_failures_and_duplicate_of() -> None:
    mini, orders, payments = graph()
    known = StoredGap(gap_id="GAP::1", props={"status": "open", "signature": "s"})
    check = checker(mini, gaps=[known])
    at = site(orders, payments)

    deferred = await check.check(at, Investigated(InvestigationResult(error="llm_exhausted")))
    not_spec = await check.check(
        at, result(mini, orders, payments, GapReport(verdict="not_spec", not_spec_reason="A ticket."))
    )
    duplicate = await check.check(
        at, result(mini, orders, payments, GapReport(verdict="duplicate_of", duplicate_of="GAP::1"))
    )
    unknown = await check.check(
        at, result(mini, orders, payments, GapReport(verdict="duplicate_of", duplicate_of="GAP::2"))
    )
    gap = await check.check(at, result(mini, orders, payments, report(orders, payments)))

    assert (deferred.outcome, deferred.warnings) == ("deferred", ("llm_exhausted",))
    assert (
        not_spec.outcome == "not_spec"
        and not_spec.report is not None
        and not_spec.report.not_spec_reason == "A ticket."
    )
    assert (duplicate.outcome, duplicate.gap_id) == ("duplicate", "GAP::1")
    assert (unknown.outcome, unknown.rejected) == ("not_a_gap", ("duplicate_of",))
    assert gap.outcome == "gap" and Checked.from_payload(gap.payload()) == gap


async def test_a_repairable_report_gets_one_repair_call() -> None:
    mini, orders, payments = graph()
    good, broken = report(orders, payments), report(orders, payments)
    broken = broken.model_copy(update={"options": broken.options[:1]})
    gateway = RecordedGapGateway(recordings={}).handle(
        REPAIR_PROMPT, lambda _request: {"action": "report", "report": good.model_dump(mode="json")}
    )
    check = checker(mini, gateway)

    repaired = await check.check(site(orders, payments), result(mini, orders, payments, broken))

    assert repaired.outcome == "gap" and "repaired" in repaired.warnings
    (call,) = gateway.calls_of("gap_investigate")
    assert "options_count" in str(call.messages[1]["content"]) and check.usage.calls == {"gap_investigate": 1}

    failing = RecordedGapGateway(recordings={}).handle(REPAIR_PROMPT, _raise)
    deferred = await checker(mini, failing).check(site(orders, payments), result(mini, orders, payments, broken))
    assert deferred.outcome == "deferred" and "llm_exhausted" in deferred.warnings

    still = RecordedGapGateway(recordings={}).handle(
        REPAIR_PROMPT, lambda _request: {"action": "report", "report": broken.model_dump(mode="json")}
    )
    rejected = await checker(mini, still).check(site(orders, payments), result(mini, orders, payments, broken))
    assert rejected.outcome == "not_a_gap" and rejected.rejected == ("options_count",)


def _raise(request: ScriptedRequest) -> Any:
    raise LLMError("exhausted", prompt=request.prompt)


async def test_duplicates_by_suppression_and_by_a_similar_question() -> None:
    mini, orders, payments = graph()
    good = report(orders, payments)
    at = site(orders, payments)
    records = MemoryGapRecords()
    await records.suppress(
        PROJECT,
        SuppressionRow(
            signature=report_signature(at, good),
            question="q",
            reason="not a gap",
            dismissed_by="u",
            dismissed_at=datetime.fromisoformat(AT),
        ),
    )
    suppressed = await checker(mini, records=records).check(at, result(mini, orders, payments, good))
    assert (suppressed.outcome, suppressed.gap_id) == ("duplicate", None) and "suppressed" in suppressed.warnings

    link = StoredLink("ABOUT", "REQ::BR::REVIEW", "u", "BusinessRule", "Review", "k")
    open_gap = StoredGap(
        gap_id="GAP::OPEN", props={"status": "open", "question": "What is the review limit?"}, about=(link,)
    )
    gateway = RecordedGapGateway(recordings={}).relate(open_gap.props["question"], 0.95, texts=[good.question])
    similar = await checker(mini, gateway, gaps=[open_gap]).check(at, result(mini, orders, payments, good))
    assert (similar.outcome, similar.gap_id) == ("duplicate", "GAP::OPEN")

    distant = RecordedGapGateway(recordings={}).relate(open_gap.props["question"], 0.5, texts=[good.question])
    assert (
        await checker(mini, distant, gaps=[open_gap]).check(at, result(mini, orders, payments, good))
    ).outcome == "gap"

    class NoEmbeddings(RecordedGapGateway):
        async def embed(self, texts: Any) -> Any:
            del texts
            raise GraphError("llm_exhausted")

    failed = await checker(mini, NoEmbeddings(recordings={}), gaps=[open_gap]).check(
        at, result(mini, orders, payments, good)
    )
    assert failed.outcome == "gap"
    joined = await checker(mini, gateway, gaps=[open_gap]).check(
        site(orders, payments, gap_id="GAP::OPEN"), result(mini, orders, payments, good)
    )
    assert joined.outcome == "gap"


async def test_a_pointer_goes_to_the_second_check() -> None:
    mini, orders, payments = graph()
    pointer = Quote(fragment_id=orders, quote=LINE)
    missing = report(orders, payments, kind="missing", pointer=pointer, options=())
    contradicted = site(orders, payments)
    at = dataclasses.replace(contradicted, seeds=(dataclasses.replace(contradicted.seeds[0], kind="missing"),))
    asks: list[ScriptedRequest] = []

    def judge(request: ScriptedRequest) -> Any:
        asks.append(request)
        if len(asks) == 1:
            return {"entailed": False, "consistent": False, "confidence": 0, "reason": "", "find_words": "review limit"}
        return {"entailed": True, "consistent": True, "confidence": 0.9, "reason": "Stated.", "find_words": None}

    gateway = RecordedGapGateway(recordings={}).handle(JUDGE_PROMPT, judge)
    check = checker(mini, gateway)

    confirmed = await check.check(at, result(mini, orders, payments, missing))

    assert confirmed.pointer and confirmed.judge is not None and confirmed.judge["find_words"] == "review limit"
    assert len(asks) == 2 and "retrieved lines" in str(asks[1].messages[1]["content"])
    assert check.usage.calls == {"gap_judge": 2}
    low = RecordedGapGateway(recordings={}).handle(
        JUDGE_PROMPT,
        lambda _r: {"entailed": True, "consistent": True, "confidence": 0.84, "reason": "", "find_words": None},
    )
    unconfirmed = await checker(mini, low).check(at, result(mini, orders, payments, missing))
    assert not unconfirmed.pointer and "pointer_not_confirmed" in unconfirmed.warnings
    failing = RecordedGapGateway(recordings={}).handle(JUDGE_PROMPT, _raise)
    assert (await checker(mini, failing).check(at, result(mini, orders, payments, missing))).outcome == "deferred"
    contradiction = report(orders, payments, pointer=pointer)
    no_judge = RecordedGapGateway(recordings={})
    assert not (await checker(mini, no_judge).check(at, result(mini, orders, payments, contradiction))).pointer
    relabelled = await checker(mini, no_judge).check(contradicted, result(mini, orders, payments, missing))
    assert not relabelled.pointer  # a contradiction seed gets no pointer, whatever kind the report names
    assert no_judge.calls_of("gap_judge") == []
    found = ReadResult(read="find_fragments", items=({"text": LINE},))
    assert "retrieved lines" in str(judge_messages(missing, found)[1]["content"])


# Routing


def test_hold_back_ranks_new_questions_by_severity_reach_and_score() -> None:
    candidates = [
        Candidate("S1", "minor", 0, 0.5),
        Candidate("S2", "major", 0, 0.1),
        Candidate("S3", "minor", 3, 0.1),
        Candidate("S4", "blocker", 0, 0.0),
    ]

    assert hold_back(candidates, 2) == {"S1", "S3"}
    assert hold_back(candidates, 3) == {"S1"} and hold_back(candidates, 9) == frozenset()
    mini, orders, payments = graph()
    routed = route_report(report(orders, payments, severity="minor", about=("MOD::LEGACY",)), mini.snapshot())
    assert routed.route is None and routed.severity == "info"
    assert routed_payload(routed, held_back=False) == {
        "severity": "info",
        "route": None,
        "mode": None,
        "reasons": ["info"],
        "gates": {},
        "confidence": None,
        "held_back": False,
    }
