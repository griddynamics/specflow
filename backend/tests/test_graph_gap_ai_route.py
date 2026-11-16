"""The AI branch of routing: the second check of an AI answer, the gates, the valve and the AI answer the record step
writes (docs/graph/gap/answers.md "Routing", "Gates", "The valve", "AI answers"; investigation.md "Second check";
FR-GRTE-01, FR-GRTE-03, FR-GRTE-05)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gap_acceptance_support import Planted, RecordedGapGateway, gap_corpus_graph
from gap_detection_support import PROJECT, MiniGraph
from gap_port_fakes import MemoryGapRecords
from gap_run_support import corpus_world
from graph.core import load_ontology
from graph.core.errors import GraphWriteError, LLMError
from graph.gap.models import Edit, EditTarget, GapReport, InvestigationResult, Quote, ReportOption, Witness, WitnessRef
from graph.gap.prompts import AI_ANSWER_PROMPT, JUDGE_PROMPT
from graph.gap.rules import Gates, load_gap_rules
from graph.gap.steps.check import Checked, Checker, CheckRun, ai_answer_messages
from graph.gap.steps.detect import Seed
from graph.gap.steps.investigate import Investigated
from graph.gap.steps.record import RecordRun, SiteDecision, record_sites, report_signature
from graph.gap.steps.route import (
    ValveItem,
    ai_candidate,
    apply_valve,
    gate_results,
    local,
    route_report,
    routed_payload,
)
from graph.gap.steps.sites import ExistingGap, Site
from graph.gap.steps.snapshot import GraphSnapshot, read_snapshot
from graph_support import ScriptedRequest

LINE = "Customers can export their orders as CSV."
RULE = "Orders over 1,000 EUR are reviewed."
OTHER = "Payments over 500 EUR are held for review."
KINDS = ("missing", "ambiguous", "inconsistency")
GATES = Gates(local_max_nodes=5, local_max_areas=1, min_confidence=0.85)
AT = datetime(2026, 11, 1, tzinfo=UTC)
SURE = {"entailed": True, "consistent": True, "confidence": 0.95, "reason": "Stated.", "find_words": None}


def graph() -> tuple[MiniGraph, str, str]:
    mini = MiniGraph()
    export = mini.section("Order export", LINE)
    mini.node("REQ::FR::EXPORT", "FunctionalRequirement", "Export orders", quote=LINE)
    mini.node("MOD::ORDERS", "Module", "Orders")
    mini.node("MOD::PAYMENTS", "Module", "Payments")
    review = mini.section("Order review", RULE)
    mini.node("REQ::BR::REVIEW", "BusinessRule", "Review", quote=RULE, fragment=review)
    rules = mini.section("Payments", OTHER, document="payments.md")
    mini.node("REQ::BR::HOLD", "BusinessRule", "Hold", quote=OTHER, fragment=rules)
    mini.edge("REQ::BR::REVIEW", "PART_OF", "MOD::ORDERS")
    mini.edge("REQ::BR::HOLD", "PART_OF", "MOD::PAYMENTS")
    return mini, export, rules


def edit(fragment: str) -> Edit:
    return Edit(
        placement="amend",
        target=EditTarget(fragment_id=fragment, anchor_quote=LINE),
        operation="insert_after",
        text="The export holds the orders of the customer who exports them.",
    )


def report(fragment: str, **changes: Any) -> GapReport:
    basis = (Quote(fragment_id=fragment, quote=LINE),)
    base = GapReport(
        verdict="gap",
        kind="missing",
        severity="minor",
        title="Whose orders the export holds",
        question="Which orders does the CSV export contain?",
        why="No acceptance criteria.",
        about=("REQ::FR::EXPORT",),
        witness=(WitnessRef(fragment_id=fragment, quote=LINE),),
        options=(
            ReportOption(label="A: Own orders", recommended=True, edits=(edit(fragment),), basis=basis),
            ReportOption(label="B: A date range", recommended=False, edits=(edit(fragment),)),
        ),
    )
    return base.model_copy(update=changes)


def site(fragment: str, *, gap_id: str | None = None) -> Site:
    seed = Seed(
        detector="MS-CRITERIA",
        kind="missing",
        family="structure",
        strength="weak",
        about=("REQ::FR::EXPORT",),
        witness=(Witness(fragment_id=fragment, quote=LINE),),
    )
    return Site("SITE::1", (seed,), seed.about, (), "hash-1", 1.0, 0, gap_id=gap_id)


def checker(mini: MiniGraph, gateway: Any, **fields: Any) -> Checker:
    return Checker(
        CheckRun(
            project_id=PROJECT,
            run_id="run-1",
            snapshot=mini.snapshot(),
            limits=load_gap_rules(ontology=load_ontology()).limits,
            records=fields.pop("records", None) or MemoryGapRecords(),
            gateway=gateway,
            reader=mini.reader(),
            signature=report_signature,
            gates=fields.pop("gates", GATES),
            ai_kinds=fields.pop("ai_kinds", KINDS),
            ai_answers=fields.pop("ai_answers", True),
            **fields,
        )
    )


def investigated(fragment: str, found: GapReport) -> Investigated:
    return Investigated(InvestigationResult(report=found), shown_fragment_ids=(fragment,))


# The gates


def test_the_local_gate_counts_nodes_and_owning_areas() -> None:
    mini, export, _ = graph()
    snapshot = mini.snapshot()
    one = report(export)
    two_areas = report(export, about=("REQ::BR::REVIEW", "REQ::BR::HOLD"))
    one_area = report(export, about=("REQ::BR::REVIEW", "MOD::ORDERS"))

    assert local(one, snapshot, GATES)
    assert not local(two_areas, snapshot, GATES)
    assert local(one_area, snapshot, GATES)
    assert not local(one_area, snapshot, Gates(local_max_nodes=1, local_max_areas=1, min_confidence=0.85))


def test_each_gate_reads_the_report_and_the_second_check() -> None:
    mini, export, _ = graph()
    snapshot = mini.snapshot()
    found = report(export)

    assert gate_results(found, snapshot, GATES, SURE) == {
        "entailed": True,
        "local": True,
        "consistent": True,
        "confident": True,
    }
    assert gate_results(found, snapshot, GATES, None) == {
        "entailed": False,
        "local": True,
        "consistent": False,
        "confident": False,
    }
    no_basis = report(export, options=tuple(option.model_copy(update={"basis": ()}) for option in found.options))
    assert not gate_results(no_basis, snapshot, GATES, SURE)["entailed"]
    unsure = gate_results(found, snapshot, GATES, SURE | {"consistent": False, "confidence": 0.85})
    assert (unsure["consistent"], unsure["confident"]) == (False, True)


def test_only_a_new_minor_gap_of_an_ai_kind_with_a_basis_is_a_candidate() -> None:
    mini, export, _ = graph()
    snapshot = mini.snapshot()
    found = report(export)

    def candidate(item: GapReport, **changes: Any) -> bool:
        given: dict[str, Any] = {"gates": GATES, "ai_kinds": KINDS, "ai_answers": True, "new": True} | changes
        return ai_candidate(item, snapshot, **given)

    assert candidate(found)
    assert not candidate(found, ai_answers=False)
    assert not candidate(found, new=False)
    assert not candidate(report(export, severity="major"))
    for kind in ("contradiction", "open_question", "assumption"):
        assert not candidate(report(export, kind=kind))
    assert not candidate(
        report(export, options=tuple(option.model_copy(update={"basis": ()}) for option in found.options))
    )
    assert not candidate(report(export, options=(found.options[1],)))
    assert not candidate(report(export, about=("REQ::BR::REVIEW", "REQ::BR::HOLD")))


def test_the_route_is_ai_only_when_every_rule_and_gate_holds() -> None:
    mini, export, _ = graph()
    snapshot = mini.snapshot()
    found = report(export)
    given: dict[str, Any] = {"ai_kinds": KINDS, "gates": GATES, "judge": SURE}

    routed = route_report(found, snapshot, **given)
    assert (routed.route, routed.mode, routed.reasons, routed.confidence) == ("ai", "ai", ("gates_hold",), 0.95)
    assert route_report(found, snapshot, **given, origin="development").reasons == ("origin",)
    assert route_report(found, snapshot, **given, new=False).reasons == ("existing",)
    assert route_report(found, snapshot, **given, ai_answers=False).reasons == ("ai_answers_off",)
    failed = route_report(found, snapshot, ai_kinds=KINDS, gates=GATES, judge_failed=True)
    assert failed.route == "ask" and failed.reasons[0] == "judge_failed"
    assert route_report(found, snapshot, ai_kinds=KINDS).reasons == ("gates_off",)
    for kind in ("contradiction", "open_question", "assumption"):
        never = route_report(report(export, kind=kind), snapshot, **given)
        assert never.route == "ask" and "kind" in never.reasons
    pointer = route_report(report(export, kind="open_question"), snapshot, **given, pointer=True)
    assert (pointer.route, pointer.mode) == ("ai", "pointer")
    payload = routed_payload(routed, held_back=False)
    assert payload["gates"] == dict(routed.gates) and payload["confidence"] == 0.95


# The valve


def test_the_valve_stops_at_the_run_limit_and_the_node_share_in_rank_order() -> None:
    mini, export, _ = graph()
    snapshot = mini.snapshot()
    ai = route_report(report(export), snapshot, ai_kinds=KINDS, gates=GATES, judge=SURE)
    pointer = route_report(report(export, kind="open_question"), snapshot, pointer=True)
    decided = {f"S{index}": ai for index in range(5)} | {"P": pointer}
    items = [ValveItem(f"S{index}", float(index), frozenset({f"N{index}"})) for index in range(5)]
    items.append(ValveItem("P", 9.0, frozenset({"N9"})))

    limited = apply_valve(decided, items, limit=2, max_share=1.0, spec_nodes=10)
    assert [key for key, value in limited.items() if value.route == "ai" and value.mode == "ai"] == ["S3", "S4"]
    assert limited["S0"].reasons == ("ai_limit",) and limited["S0"].gates == ai.gates
    assert limited["P"] == pointer
    shared = apply_valve(decided, items, limit=20, max_share=0.25, spec_nodes=10)
    assert [key for key, value in shared.items() if value.mode == "ai"] == ["S2", "S3", "S4"]
    assert all(
        value.route == "ask"
        for key, value in apply_valve(decided, items, limit=20, max_share=0.05, spec_nodes=0).items()
        if key != "P"
    )


# The second check of an AI answer


def test_the_ai_answer_check_sees_witness_basis_and_edits_only() -> None:
    _, export, _ = graph()
    found = report(export, ai_reason="SECRET REASONING")

    messages = ai_answer_messages(found)

    content = str(messages[1]["content"])
    assert messages[0]["content"].startswith("You check one answer that would settle")
    assert LINE in content and "The export holds the orders" in content and '"mode": "ai"' in content
    assert "A: Own orders" not in content and "SECRET REASONING" not in content and "date range" not in content


async def test_a_candidate_gets_one_second_check_and_one_read_when_it_asks() -> None:
    mini, export, _ = graph()
    asks: list[ScriptedRequest] = []

    def judge(request: ScriptedRequest) -> Any:
        asks.append(request)
        if len(asks) == 1:
            return SURE | {"entailed": False, "consistent": False, "confidence": 0, "find_words": "export orders"}
        return SURE

    gateway = RecordedGapGateway(recordings={}).handle(AI_ANSWER_PROMPT, judge)
    check = checker(mini, gateway)

    checked = await check.check(site(export), investigated(export, report(export)))

    assert checked.outcome == "gap" and checked.new and checked.ai_judge is not None
    assert checked.ai_judge["confidence"] == 0.95 and checked.ai_judge["find_words"] == "export orders"
    assert len(asks) == 2 and "retrieved lines" in str(asks[1].messages[1]["content"])
    assert check.usage.calls == {"gap_judge": 2}
    assert Checked.from_payload(checked.payload()) == checked


async def test_no_second_check_without_a_candidate_and_a_failed_one_asks_a_person() -> None:
    mini, export, _ = graph()
    silent = RecordedGapGateway(recordings={})

    off = await checker(mini, silent, ai_answers=False).check(site(export), investigated(export, report(export)))
    major = await checker(mini, silent).check(site(export), investigated(export, report(export, severity="major")))
    joined = await checker(mini, silent).check(site(export, gap_id="GAP::1"), investigated(export, report(export)))
    no_gates = await checker(mini, silent, gates=None).check(site(export), investigated(export, report(export)))

    assert silent.calls_of("gap_judge") == []
    assert off.ai_judge is None and major.ai_judge is None and no_gates.ai_judge is None
    assert not joined.new

    def fail(_request: ScriptedRequest) -> Any:
        raise LLMError("exhausted")

    failing = RecordedGapGateway(recordings={}).handle(AI_ANSWER_PROMPT, fail)
    failed = await checker(mini, failing).check(site(export), investigated(export, report(export)))
    assert (failed.outcome, failed.ai_judge_failed, failed.ai_judge) == ("gap", True, None)
    assert failed.warnings[-1] == "llm_exhausted"


async def test_an_unconfirmed_pointer_still_gets_the_ai_answer_check() -> None:
    mini, export, _ = graph()
    gateway = RecordedGapGateway(recordings={})
    gateway.handle(JUDGE_PROMPT, lambda _r: SURE | {"confidence": 0.5})
    gateway.handle(AI_ANSWER_PROMPT, lambda _r: SURE)
    found = report(export, pointer=Quote(fragment_id=export, quote=LINE))

    checked = await checker(mini, gateway).check(site(export), investigated(export, found))

    assert not checked.pointer and checked.judge is not None and checked.ai_judge is not None


# The AI answer the record step writes


async def csv_decision() -> tuple[Any, GraphSnapshot, SiteDecision]:
    world = await corpus_world()
    snapshot = await read_snapshot(world.reader, PROJECT, world.ontology)
    fragment = gap_corpus_graph().fragment("account-spec.md", "Order export").fragment_id
    seed = Seed(
        detector="MS-CRITERIA",
        kind="missing",
        family="structure",
        strength="weak",
        about=(Planted.OWNERLESS_CSV_EXPORT,),
        witness=(Witness(fragment_id=fragment, quote=LINE),),
    )
    decision = SiteDecision(
        site=Site("SITE::CSV", (seed,), seed.about, (), "hash-csv", 1.0, 0),
        outcome="gap",
        report=report(fragment, about=(Planted.OWNERLESS_CSV_EXPORT,)),
        severity="minor",
        route="ai",
        mode="ai",
        reasons=("gates_hold",),
        gates={"entailed": True, "local": True, "consistent": True, "confident": True},
        confidence=0.93,
        judge=SURE,
    )
    return world, snapshot, decision


def record_run(world: Any, **changes: Any) -> RecordRun:
    fields: dict[str, Any] = {
        "source": world.executor,
        "executor": world.executor,
        "ontology": world.ontology,
        "records": world.records,
        "project_id": PROJECT,
        "run_id": "run-1",
        "now": lambda: AT,
        "amendments": world.amendments,
    }
    return RecordRun(**(fields | changes))


async def test_an_ai_answer_is_written_answered_with_its_clarification_and_pending_rows() -> None:
    world, snapshot, decision = await csv_decision()

    outcome = await record_sites(record_run(world), snapshot, (), [decision])

    ((gap, props),) = world.gap_nodes().items()
    assert (props["status"], props["route"], outcome.ai_answered) == ("answered", "ai", 1)
    (clarification,) = [found for (label, _), found in world.executor.nodes.items() if label == "Clarification"]
    assert (clarification["mode"], clarification["answered_by"], clarification["status"]) == ("ai", "ai", "pending")
    assert (clarification["option_label"], clarification["confidence"]) == ("A: Own orders", 0.93)
    assert clarification["placement"] == "amend" and LINE in clarification["basis"]
    (row,) = world.amendments.rows[PROJECT].values()
    assert (row.status, row.clr_id, row.lineage) == ("pending", clarification["clr_id"], "upload:account-spec")
    assert row.text.endswith(f"> Clarified by {gap}")
    assert len(await world.records.options(PROJECT, gap)) == 2
    events = {event.event: event for event in await world.records.events(PROJECT, gap)}
    assert events["answered"].actor == "ai" and events["answered"].clr_id == clarification["clr_id"]
    assert events["routed"].detail["gates"]["confident"] and events["routed"].detail["confidence"] == 0.93


async def test_without_an_amendments_port_an_ai_route_is_recorded_as_a_question() -> None:
    world, snapshot, decision = await csv_decision()

    outcome = await record_sites(record_run(world, amendments=None), snapshot, (), [decision])

    ((gap, props),) = world.gap_nodes().items()
    assert (props["status"], props["route"], outcome.ai_answered) == ("open", "ask", 0)
    (routed,) = [event for event in await world.records.events(PROJECT, gap) if event.event == "routed"]
    assert routed.detail["reasons"] == ["amendments_unavailable"] and routed.detail["mode"] is None


async def test_a_failed_graph_write_removes_the_ai_answer_rows() -> None:
    world, snapshot, decision = await csv_decision()
    world.executor.fail_on = "upsert_node_Clarification"

    with pytest.raises(GraphWriteError):
        await record_sites(record_run(world), snapshot, (), [decision])

    assert not world.amendments.rows[PROJECT]


def test_a_gap_answered_by_the_run_ai_is_not_an_input_of_its_site() -> None:
    assert not ExistingGap("GAP::1", "sig", "answered", route="ai").hashed
    assert ExistingGap("GAP::1", "sig", "answered", route="ask").hashed
    assert not ExistingGap("GAP::1", "sig", "open").hashed
    assert ExistingGap("GAP::1", "sig", "applying", route="ai").hashed
