"""Acceptance tests of graph.gap: Severity and routing (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from typing import Any

from gap_acceptance_support import Planted, assert_all_recorded, gap_corpus_graph
from gap_detection_support import PROJECT, TracingReader
from gap_investigation_support import loop_world
from gap_port_fakes import SeedInvestigator, seed_report
from graph.core import load_ontology
from graph.gap import GapService, run_gap
from graph.gap.models import (
    Edit,
    EditTarget,
    GapReport,
    InvestigationResult,
    Quote,
    ReportOption,
    SitePack,
    WitnessRef,
)
from graph.gap.prompts import AI_ANSWER_PROMPT
from graph.gap.rules import load_gap_rules
from graph.gap.settings import GapSettings
from graph.gap.steps.route import route_report
from graph.gap.steps.snapshot import GraphSnapshot, read_snapshot
from test_graph_gap_run import interfaces, mini_world

CSV_FRAGMENT = "upload:account-spec#account-specification/order-export#145ce11f"


async def corpus_snapshot() -> GraphSnapshot:
    ontology = load_ontology()
    return await read_snapshot(TracingReader(gap_corpus_graph().reader, ontology), PROJECT, ontology)


def judged(confidence: float, *, entailed: bool = True, consistent: bool = True) -> dict[str, Any]:
    """A second check's answer of an AI answer."""

    return {"entailed": entailed, "consistent": consistent, "confidence": confidence, "reason": "", "find_words": None}


def report(**fields: Any) -> GapReport:
    base: dict[str, Any] = {
        "verdict": "gap",
        "kind": "missing",
        "question": "What is missing here?",
        "about": (Planted.PLACE_ORDER,),
        "witness": (WitnessRef(fact=0),),
    }
    return GapReport.model_validate({**base, **fields})


async def test_fr_grte_01_code_decide_route_kind_severity_gates_model() -> None:
    """Inspection and test: a report with `ai_answerable: true` on a contradiction is routed `ask`."""

    snapshot = await corpus_snapshot()
    ai_kinds = ("missing", "ambiguous", "inconsistency")
    contradiction = report(
        kind="contradiction",
        severity="minor",
        ai_answerable=True,
        about=(Planted.REVIEW_LIMIT_ORDERS, Planted.REVIEW_LIMIT_PAYMENTS),
    )

    routed = route_report(contradiction, snapshot, ai_kinds=ai_kinds)
    pointed = route_report(contradiction, snapshot, ai_kinds=ai_kinds, pointer=True)

    assert routed.route == "ask" and routed.severity == "major" and "kind" in routed.reasons
    assert pointed.route == "ask"
    minor = report(severity="minor", ai_answerable=True)
    assert route_report(minor, snapshot, ai_kinds=ai_kinds).route == "ask"  # no gates looked at: never AI
    gates = load_gap_rules(ontology=load_ontology()).routing.gates
    judged_contradiction = route_report(contradiction, snapshot, ai_kinds=ai_kinds, gates=gates, judge=judged(1.0))
    assert judged_contradiction.route == "ask" and "kind" in judged_contradiction.reasons
    world = await loop_world()
    await run_gap(world.context(), mode="full")
    contradictions = [props for props in world.gap_nodes().values() if props["kind"] == "contradiction"]
    assert [props["route"] for props in contradictions] == ["ask"]
    assert_all_recorded(world.gateway)


async def test_fr_grte_02_blocker_major_gap_always_routed_ask() -> None:
    """Given reports of each severity, then blockers and majors are `ask`."""

    snapshot = await corpus_snapshot()
    kinds = ("missing", "ambiguous", "inconsistency")
    planned = {Planted.PLACE_ORDER}
    for severity in ("blocker", "major"):
        routed = route_report(report(severity=severity, ai_answerable=True), snapshot, planned=planned, ai_kinds=kinds)
        assert (routed.severity, routed.route) == (severity, "ask")
        assert "severity" in routed.reasons
        pointed = route_report(report(severity=severity), snapshot, planned=planned, ai_kinds=kinds, pointer=True)
        assert pointed.route == "ai" and pointed.mode == "pointer"  # nothing new is decided (answers.md "Routing")
    assert route_report(report(severity="minor"), snapshot, ai_kinds=kinds).route == "ask"
    assert route_report(report(severity="info"), snapshot, ai_kinds=kinds).route is None
    off = route_report(report(severity="major"), snapshot, pointer=True, ai_answers=False)
    assert off.route == "ask" and "ai_answers_off" in off.reasons


def csv_gap(world: Any) -> tuple[str, dict[str, Any]]:
    return next(
        (gap, props) for gap, props in world.gap_nodes().items() if Planted.OWNERLESS_CSV_EXPORT in world.about(gap)
    )


async def test_fr_grte_03_minor_gap_routed_ai_kind_ai_kinds() -> None:
    """Given a minor missing-information gap with confidence 0.84, then it is `ask`; with 0.9 and all gates, then
    `ai`.

    The shop corpus's CSV export requirement (MS-CRITERIA): the recorded report is a minor ``missing`` gap whose
    recommended option rests on the line "Customers can export their orders as CSV." The recorded second check
    answers entailed, consistent, 0.92: AI answers it and it appears in the review list. A second check of 0.84 routes
    it to a person, and the routed event names the failed gate.
    """

    snapshot = await corpus_snapshot()
    gates = load_gap_rules(ontology=load_ontology()).routing.gates
    basis = (Quote(fragment_id=CSV_FRAGMENT, quote=Planted.OWNERLESS_CSV_EXPORT_LINE),)
    edit = Edit(
        placement="amend",
        target=EditTarget(fragment_id=CSV_FRAGMENT, anchor_quote=Planted.OWNERLESS_CSV_EXPORT_LINE),
        operation="insert_after",
        text="The CSV export contains the orders of the customer who exports them.",
    )
    option = ReportOption(label="The customer's own orders", recommended=True, edits=(edit,), basis=basis)
    minor = report(severity="minor", about=(Planted.OWNERLESS_CSV_EXPORT,), options=(option,))
    kinds = ("missing", "ambiguous", "inconsistency")
    sure = route_report(minor, snapshot, ai_kinds=kinds, gates=gates, judge=judged(0.9))
    unsure = route_report(minor, snapshot, ai_kinds=kinds, gates=gates, judge=judged(0.84))
    assert (sure.route, sure.mode, dict(sure.gates)) == (
        "ai",
        "ai",
        {"entailed": True, "local": True, "consistent": True, "confident": True},
    )
    assert (unsure.route, unsure.reasons, unsure.gates["confident"]) == ("ask", ("gate_confident",), False)

    world = await loop_world()
    result = await run_gap(world.context(), mode="full")

    gap, props = csv_gap(world)
    assert (props["status"], props["route"], props["severity"]) == ("answered", "ai", "minor")
    assert result.counts["ai_answered"] == 1 and result.counts["routes"]["ai"] >= 1
    routed = next(event for event in await world.records.events(PROJECT, gap) if event.event == "routed")
    assert routed.detail["gates"] == {"entailed": True, "local": True, "consistent": True, "confident": True}
    assert routed.detail["confidence"] == 0.92 and routed.detail["mode"] == "ai"
    service = GapService(world.executor, world.reader, world.ontology, world.rules, world.records, world.amendments)
    (listed,) = (await service.ai_answers(PROJECT)).answers
    assert (listed.gap_id, listed.clarification.mode, listed.clarification.status) == (gap, "ai", "pending")
    assert listed.clarification.answered_by == "ai" and listed.clarification.confidence == 0.92
    assert [item.quote for item in listed.clarification.basis] == [Planted.OWNERLESS_CSV_EXPORT_LINE]
    (row,) = world.amendments.rows[PROJECT].values()
    assert (row.status, row.clr_id, row.operation) == ("pending", listed.clarification.clr_id, "insert_after")
    assert_all_recorded(world.gateway)

    doubtful = await loop_world()
    doubtful.gateway.handle(AI_ANSWER_PROMPT, lambda _request: judged(0.84))
    await run_gap(doubtful.context(), mode="full")

    gap, props = csv_gap(doubtful)
    assert (props["status"], props["route"]) == ("open", "ask")
    routed = next(event for event in await doubtful.records.events(PROJECT, gap) if event.event == "routed")
    assert routed.detail["reasons"] == ["gate_confident"] and routed.detail["confidence"] == 0.84
    assert not doubtful.amendments.rows[PROJECT] and len(await doubtful.records.options(PROJECT, gap)) == 2
    assert_all_recorded(doubtful.gateway)


async def test_fr_grte_04_info_report_become_run_note_graph_node() -> None:
    """Given an info report, then a `gap_note` row exists and no `Gap` node."""

    world = await loop_world()

    result = await run_gap(world.context(), mode="full")

    titles = {"No security requirement in the spec", "No data protection requirement in the spec"}
    notes = [note for note in world.records.notes[PROJECT] if note.run_id == result.run_id and note.text in titles]
    assert len(notes) == 2 and all(note.detector == "MS-COVERAGE" for note in notes)
    assert not any(props["title"] in titles for props in world.gap_nodes().values())
    assert result.counts["notes"] >= 2
    assert_all_recorded(world.gateway)


class EligibleInvestigator:
    """The seed report as a minor gap whose recommended option states the interface's provider after its own line
    and rests on that line: AI-eligible."""

    prompt_version = "eligible.1"

    async def investigate(self, pack: SitePack, reads: Any, budget: Any) -> InvestigationResult:
        del reads, budget
        found = seed_report(pack)
        (spec_id,) = found.about
        line = f"Interface number {int(spec_id[-2:])} carries events."
        fragment = next(item["fragment_id"] for item in pack.context["evidence"] if item["spec_id"] == spec_id)
        stated = Edit(
            placement="amend",
            target=EditTarget(fragment_id=fragment, anchor_quote=line),
            operation="insert_after",
            text=f"The Events service provides interface {int(spec_id[-2:])}.",
        )
        basis = (Quote(fragment_id=fragment, quote=line),)
        options = (
            ReportOption(label="The Events service", recommended=True, edits=(stated,), basis=basis),
            *(option.model_copy(update={"recommended": False}) for option in found.options[1:]),
        )
        return InvestigationResult(report=found.model_copy(update={"severity": "minor", "options": options}))


async def eligible_world(count: int) -> Any:
    world = await mini_world(interfaces(count))
    world.investigator = EligibleInvestigator()
    world.gateway.handle(AI_ANSWER_PROMPT, lambda _request: judged(0.95))
    return world


async def test_fr_grte_05_ai_answers_stop_at_run_limit_or_node_share() -> None:
    """Given 25 AI-eligible gaps, then 20 are answered by AI and 5 are `ask`.

    The node share is lifted for the per-run limit (25 one-node gaps of a 25-node graph would pass 5 % after the
    second answer); with the shipped share of 5 %, the third answer finds two of the 25 nodes (8 %) already touched.
    """

    world = await eligible_world(25)

    result = await run_gap(world.context(settings=GapSettings(limits={"ai_answers_max_node_share": 1.0})), mode="full")

    routes = [props["route"] for props in world.gap_nodes().values()]
    assert (routes.count("ai"), routes.count("ask")) == (20, 5)
    assert result.counts["ai_answered"] == 20 and len(world.gateway.calls_of("gap_judge")) == 25
    limited = [
        event
        for gap, props in world.gap_nodes().items()
        if props["route"] == "ask"
        for event in await world.records.events(PROJECT, gap)
        if event.event == "routed"
    ]
    assert len(limited) == 5 and all(event.detail["reasons"] == ["ai_limit"] for event in limited)
    assert {props["status"] for props in world.gap_nodes().values() if props["route"] == "ai"} == {"answered"}

    shared = await eligible_world(25)
    await run_gap(shared.context(), mode="full")

    routes = [props["route"] for props in shared.gap_nodes().values()]
    assert (routes.count("ai"), routes.count("ask")) == (2, 23)


async def test_fr_grte_06_every_new_question_shows_in_the_inbox() -> None:
    """Given 30 new `ask` gaps, then the inbox lists all 30 (in pages). A `held_back` mark that an earlier version
    stored on a gap hides nothing."""

    world = await mini_world(interfaces(30))
    world.investigator = SeedInvestigator()
    service = GapService(world.executor, world.reader, world.ontology, world.rules, world.records, world.amendments)

    result = await run_gap(world.context(), mode="full")

    assert result.counts["gaps_new"]["total"] == result.counts["routes"]["ask"] == 30
    assert "held_back" not in result.counts
    first = await service.list_gaps(PROJECT, route="ask", limit=20)
    rest = await service.list_gaps(PROJECT, route="ask", cursor=first.next, limit=20)
    assert (len(first.gaps), len(rest.gaps), rest.next) == (20, 10, None)
    listed = (*first.gaps, *rest.gaps)
    assert len({gap.gap_id for gap in listed}) == 30 and {gap.status for gap in listed} == {"open"}
    assert all("held_back" not in props for props in world.gap_nodes().values())

    for props in list(world.gap_nodes().values())[:5]:
        props["held_back"] = "yes"

    assert len((await service.list_gaps(PROJECT, route="ask")).gaps) == 30
