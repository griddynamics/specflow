"""The investigate step: the read menu, the context pack and the structured loop (docs/graph/gap/investigation.md
"Read menu", "Context pack", "Structured loop adapter", "Failures"; FR-GINV-01 to FR-GINV-04, FR-GINV-08)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gap_acceptance_support import RecordedGapGateway
from gap_detection_support import PROJECT, MiniGraph, TracingReader
from gap_port_fakes import NO_READS, MemoryPlanView
from graph.core import load_ontology
from graph.core.errors import GraphError, LLMError
from graph.core.llm.gateway import UNTRUSTED_OPEN
from graph.gap.gaps import StoredGap, StoredLink
from graph.gap.models import InvestigationResult, ReadResult, SiteBudget, SitePack, TaskRef, Witness
from graph.gap.prompts import INVESTIGATE_PROMPT
from graph.gap.rules import load_gap_rules
from graph.gap.steps.detect import Seed
from graph.gap.steps.investigate import (
    FinalTurn,
    Investigated,
    InvestigateRun,
    LoopInvestigator,
    LoopTurn,
    WireRead,
    investigate_site,
    pack_message,
    primary_seed,
    reads_message,
    run_read,
    site_budget,
    summary_message,
)
from graph.gap.steps.pack import PackRun, build_pack, pack_tokens
from graph.gap.steps.reads import UNKNOWN_ID, GraphSiteReads, cut_items
from graph.gap.steps.sites import Site
from graph_support import ScriptedRequest

LINE = "Orders over 1,000 EUR are reviewed."
REPORT = {"verdict": "not_a_gap", "why": "Settled."}


def mini() -> tuple[MiniGraph, str]:
    graph = MiniGraph()
    fragment = graph.section("Orders", "Before the rule.", LINE, "After the rule.")
    graph.node("REQ::BR::REVIEW", "BusinessRule", "Review large orders", quote=LINE, in_scope="yes")
    graph.node("MOD::ORDERS", "Module", "Orders service")
    graph.node("REQ::FR::FAR", "FunctionalRequirement", "Far away")
    graph.edge("REQ::BR::REVIEW", "PART_OF", "MOD::ORDERS")
    return graph, fragment


def seed(fragment: str, *, fact: str | None = None) -> Seed:
    witness = (Witness(fact=fact),) if fact else (Witness(fragment_id=fragment, quote=LINE, line_start=4, line_end=4),)
    return Seed(
        detector="CT-VALUE",
        kind="contradiction",
        family="contradiction",
        strength="strong",
        about=("REQ::BR::REVIEW",),
        witness=witness,
        context=("MOD::ORDERS",),
    )


def site(
    fragment: str, *, seeds: tuple[Seed, ...] | None = None, about: tuple[str, ...] = ("REQ::BR::REVIEW",)
) -> Site:
    return Site(
        site_id="SITE::1",
        seeds=seeds or (seed(fragment), seed(fragment, fact="a graph fact")),
        about=about,
        context=("MOD::ORDERS",),
        input_hash="hash-1",
        rank_score=1.0,
        reach=0,
    )


def run(reader: Any, **fields: Any) -> PackRun:
    limits = load_gap_rules(ontology=load_ontology()).limits
    return PackRun(reader=reader, project_id=PROJECT, run_id="run-1", mode="full", limits=limits, **fields)


class Failing:
    """A reader whose every call raises a ``GraphError``."""

    def __getattr__(self, name: str) -> Any:
        async def fail(*_: Any, **__: Any) -> Any:
            raise GraphError("store_unavailable", read=name)

        return fail


# The read menu


async def test_the_read_menu_reads_the_bound_project_and_remembers_what_it_showed() -> None:
    graph, fragment = mini()
    reads = GraphSiteReads(graph.reader(), PROJECT, run_id="run-1")

    node = await reads.node("REQ::BR::REVIEW")
    evidence = await reads.evidence("REQ::BR::REVIEW")
    neighbours = await reads.neighbours("REQ::BR::REVIEW", ["PART_OF"])
    trace = await reads.trace("REQ::BR::REVIEW", "based_on", 9)
    paths = await reads.paths("REQ::BR::REVIEW", "MOD::ORDERS")
    similar = await reads.similar("REQ::BR::REVIEW", 50)
    words = await reads.similar("orders service", 3)
    nodes = await reads.find_nodes("Orders", ["Module"])
    fragments = await reads.find_fragments("large orders reviewed")

    assert node.items[0]["spec_id"] == "REQ::BR::REVIEW" and evidence.items[0]["quote"] == LINE
    assert neighbours.items[0]["relation"] == "PART_OF" and trace.reason is None
    assert paths.items[0]["reason"] == "not_connected" and similar.items == ()
    assert words.items and nodes.items[0]["node"]["spec_id"] == "MOD::ORDERS"
    assert fragments.items == () and fragments.reason is None
    assert {"REQ::BR::REVIEW", "MOD::ORDERS"} <= reads.shown_spec_ids and fragment in reads.shown_fragment_ids
    for unknown in (
        await reads.node("REQ::NONE"),
        await reads.evidence("REQ::NONE"),
        await reads.neighbours("REQ::NONE"),
        await reads.trace("REQ::NONE", "affects", 2),
        await reads.paths("REQ::NONE", "MOD::ORDERS"),
    ):
        assert unknown.items == () and unknown.reason == UNKNOWN_ID
    for empty in (await reads.similar(" ", 3), await reads.find_nodes(""), await reads.find_fragments(" ")):
        assert empty.reason == "empty_argument"


async def test_a_refused_read_is_an_empty_result_with_the_code_and_results_are_cut() -> None:
    reads = GraphSiteReads(Failing(), PROJECT)

    refused = await reads.node("REQ::BR::REVIEW")

    assert refused == ReadResult(read="node", reason="store_unavailable")
    items, cut = cut_items([{"text": "x" * 400}] * 40, budget=500)
    assert cut and 0 < len(items) < 40
    assert cut_items([{"a": 1}]) == (({"a": 1},), False)


async def test_run_read_maps_each_request_and_reports_missing_arguments() -> None:
    graph, _ = mini()
    reads = GraphSiteReads(graph.reader(), PROJECT)
    requests = [
        WireRead(read="node", spec_id="REQ::BR::REVIEW"),
        WireRead(read="evidence", spec_id="REQ::BR::REVIEW"),
        WireRead(read="neighbours", spec_id="REQ::BR::REVIEW"),
        WireRead(read="trace", spec_id="REQ::BR::REVIEW"),
        WireRead(read="paths", spec_id="REQ::BR::REVIEW", target="MOD::ORDERS"),
        WireRead(read="similar", words="orders"),
        WireRead(read="find_nodes", words="orders"),
        WireRead(read="find_fragments", words="reviewed"),
    ]

    results = [await run_read(reads, request) for request in requests]

    assert [result.read for result in results] == [request.read for request in requests]
    assert all(result.reason != "missing_argument" for result in results)
    for bare in ("node", "evidence", "neighbours", "trace", "paths", "similar", "find_nodes", "find_fragments"):
        assert (await run_read(reads, WireRead(read=bare))).reason == "missing_argument"
    message = reads_message(1, requests[:2], results[:2])
    assert UNTRUSTED_OPEN in message and json.loads(message.split("\n\n")[1])["round"] == 1
    summary = summary_message(1, requests[:2], results[:2])
    assert summary.startswith("Read results of round 1") and LINE not in summary


# The context pack


async def test_the_pack_holds_anchors_evidence_trace_gaps_plan_and_nearby_within_its_budget() -> None:
    graph, fragment = mini()
    reader = graph.reader()
    reads = GraphSiteReads(reader, PROJECT)
    gap = StoredGap(
        gap_id="GAP::1",
        props={"kind": "missing", "status": "open", "question": "Which limit?"},
        about=(StoredLink("ABOUT", "REQ::BR::REVIEW", "u", "BusinessRule", "Review", "k"),),
    )
    plan = MemoryPlanView({"REQ::BR::REVIEW": [TaskRef(task_id="TASK::1", status="todo")]})

    pack = await build_pack(site(fragment), graph.snapshot(), reads, run(reader, gaps=[gap], plan=plan))

    context = pack.context
    assert [item["spec_id"] for item in context["anchors"]] == ["REQ::BR::REVIEW", "MOD::ORDERS"]
    assert context["anchors"][0]["attributes"] == {"in_scope": "yes"}
    (evidence,) = context["evidence"]
    assert [line for line, _ in evidence["lines"]] == [1, 2, 3, 4]
    assert context["trace"] and context["gaps"][0]["gap_id"] == "GAP::1"
    assert context["plan"] == [
        {"spec_id": "REQ::BR::REVIEW", "tasks": [{"task_id": "TASK::1", "story_id": None, "status": "todo"}]}
    ]
    assert "REQ::FR::FAR" not in reads.shown_spec_ids and fragment in reads.shown_fragment_ids
    assert pack_tokens(context, pack.seeds) <= run(reader).limits.pack_tokens
    message = pack_message(pack, site_budget(run(reader).limits))
    payload = json.loads(message.split("\n\n")[1])
    assert payload["site_id"] == "SITE::1" and {"nodes_block", "trace_block", "gaps_block"} <= set(payload)
    assert [item["witness"][0] for item in payload["seeds"]] == [
        {"fragment_id": fragment, "quote_block": f"Q1 {fragment} lines 4-4"},
        {"fact_block": "F1 fact of seed 1"},
    ]
    assert "Which limit?" not in message.split("\n\n")[1]


async def test_a_pack_survives_refused_reads_and_a_site_without_nodes() -> None:
    graph, fragment = mini()
    snapshot = graph.snapshot()

    refused = await build_pack(site(fragment), snapshot, GraphSiteReads(Failing(), PROJECT), run(Failing()))
    empty = await build_pack(site(fragment, about=()), snapshot, GraphSiteReads(Failing(), PROJECT), run(Failing()))

    assert refused.context["trace"] == [] and refused.context["nearby"] == []
    assert empty.context["trace"] == [] and [item["role"] for item in empty.context["anchors"]] == ["context"]


# The structured loop


def pack_of() -> SitePack:
    return SitePack(project_id=PROJECT, site_id="SITE::1", input_hash="h", mode="full", seeds=())


async def test_the_loop_reports_in_one_round_and_counts_calls_and_tokens() -> None:
    gateway = RecordedGapGateway(recordings={}).handle(
        INVESTIGATE_PROMPT, lambda _request: {"action": "report", "report": REPORT}
    )

    result = await LoopInvestigator(gateway).investigate(
        pack_of(), NO_READS, SiteBudget(rounds=2, reads_per_round=1, context_tokens=100)
    )

    assert result.report is not None and result.report.verdict == "not_a_gap"
    assert (result.rounds_used, result.model_calls) == (1, 1) and result.tokens > 0


@pytest.mark.parametrize(
    ("failing_round", "error"),
    [(1, "llm_exhausted"), (3, "llm_exhausted"), (0, "llm_report_missing")],
)
async def test_a_failed_or_empty_call_ends_the_investigation_without_a_report(failing_round: int, error: str) -> None:
    def answer(request: ScriptedRequest) -> Any:
        number = 1 + sum(1 for message in request.messages if message["role"] == "assistant")
        if failing_round == 0:
            return {"action": "report", "report": None}
        if number == failing_round:
            raise LLMError("exhausted", prompt=request.prompt)
        return {"action": "read", "reads": [{"read": "node", "spec_id": "X"}]}

    gateway = RecordedGapGateway(recordings={}).handle(INVESTIGATE_PROMPT, answer)

    result = await LoopInvestigator(gateway).investigate(
        pack_of(), NO_READS, SiteBudget(rounds=2, reads_per_round=1, context_tokens=100)
    )

    assert result.report is None and result.error == error


async def test_the_context_is_compacted_and_the_last_call_reports_only() -> None:
    def answer(request: ScriptedRequest) -> Any:
        if request.output_type is FinalTurn:
            return {"action": "report", "report": REPORT}
        return {"action": "read", "reads": [{"read": "find_fragments", "words": "orders"}]}

    graph, _ = mini()
    gateway = RecordedGapGateway(recordings={}).handle(INVESTIGATE_PROMPT, answer)
    reads = GraphSiteReads(graph.reader(), PROJECT)

    result = await LoopInvestigator(gateway).investigate(
        pack_of(), reads, SiteBudget(rounds=3, reads_per_round=2, context_tokens=50)
    )

    final = gateway.calls_of("gap_investigate")[-1]
    assert final.output_type is FinalTurn and result.rounds_used == 3
    assert sum("summarised to save space" in str(message["content"]) for message in final.messages) == 3
    assert [call.output_type for call in gateway.calls_of("gap_investigate")] == [LoopTurn] * 3 + [FinalTurn]

    def broken(request: ScriptedRequest) -> Any:
        if request.output_type is FinalTurn:
            raise LLMError("malformed", prompt=request.prompt)
        return {"action": "read", "reads": []}

    failing = RecordedGapGateway(recordings={}).handle(INVESTIGATE_PROMPT, broken)
    failed = await LoopInvestigator(failing).investigate(
        pack_of(), reads, SiteBudget(rounds=1, reads_per_round=1, context_tokens=50)
    )
    assert failed.error == "llm_malformed" and failed.report is None


async def test_investigate_site_turns_a_raised_error_into_a_deferred_result() -> None:
    graph, fragment = mini()

    class Raising:
        async def investigate(self, *_: Any) -> InvestigationResult:
            raise GraphError("llm_exhausted")

    class Empty:
        async def investigate(self, *_: Any) -> InvestigationResult:
            raise GraphError("")

    limits = load_gap_rules(ontology=load_ontology()).limits
    config = InvestigateRun(project_id=PROJECT, run_id="run-1", mode="full", limits=limits, reader=graph.reader())

    found = await investigate_site(site(fragment), Raising(), graph.snapshot(), config)
    empty = await investigate_site(site(fragment), Empty(), graph.snapshot(), config)

    assert found.result.error == "llm_exhausted" and "REQ::BR::REVIEW" in found.shown_spec_ids
    assert empty.result.error == "investigator_failed"
    assert Investigated.from_payload(json.loads(json.dumps(found.payload()))) == found
    assert primary_seed([seed(fragment, fact="f"), seed(fragment)]).witness[0].fact == "f"


def test_the_tracing_reader_stays_the_offline_reader() -> None:
    assert isinstance(MiniGraph().reader(), TracingReader)
