"""Answered elsewhere: Conform drops an open question another document answers, keeps a partly answered one, caps fresh
calls, remembers verdicts until the question or a candidate changes, and does nothing without the ontology's flag;
run.py wiring.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from graph.build import FileStores, open_file_stores
from graph.build import run as runner
from graph.build.answered import AnsweredMemory, Context, Passage, Question, own_context, question_context
from graph.build.conform import DEDUP_PROMPT, ConformJudging, conform
from graph.build.models import (
    AnsweredElsewhere,
    AnsweredElsewhereRef,
    CanonicalNode,
    EdgesArtifact,
    FragmentRec,
    PlanArtifact,
    RawEdge,
    ResolveArtifact,
    TargetGraph,
)
from graph.build.options import BuildContext, BuildOptions, StepServices
from graph.build.vectors import VectorCache
from graph.core.errors import LLMError
from graph.core.identity import SpecIdRow
from graph.core.ontology import Ontology
from graph_conform_support import canonical, conform_plan
from graph_extraction_store_support import MemoryStore
from graph_support import OfflineExecutor, ScriptedGateway, ScriptedRequest, frozen_ontology, shipped_ontology

pytest_plugins = ["graph_support"]

ONT = shipped_ontology()
FLAG = ONT.conform.answered_elsewhere if ONT.conform else None
PROMPT = "judge_answered_elsewhere.3"
PROJECT = "p-answered"
OQ = "OpenQuestion"
QUESTION = "Which currency is used for refunds?"
SEARCH = f"{OQ}: {QUESTION}"
ANSWER = "Refunds are always paid in the order's original currency."
Factory = Callable[..., ScriptedGateway]


def ontology_with(**changes: Any) -> Ontology:
    """The shipped ontology with its answered-elsewhere flag changed (``None``: no flag)."""

    assert ONT.conform is not None and FLAG is not None
    flag = None if changes.get("off") else dataclasses.replace(FLAG, **changes)
    return dataclasses.replace(ONT, conform=dataclasses.replace(ONT.conform, answered_elsewhere=flag))


def plan_of(*documents: tuple[str, Sequence[str]]) -> PlanArtifact:
    return conform_plan(*documents)


def node(cid: str, name: str, label: str, fragment: str) -> CanonicalNode:
    return canonical(cid, name, label, [fragment], quote=name)


def question(cid: str = "q", name: str = QUESTION, fragment: str = "spec#s0") -> CanonicalNode:
    return node(cid, name, OQ, fragment)


SPEC = ("spec", [f"TBD: {QUESTION}", "Customers can request refunds online."])
FAQ = ("faq", [ANSWER])


def verdict(answer: str, confidence: float = 0.9, quote: str | None = ANSWER, fragment: str = "faq#s0") -> Any:
    return lambda _: {"answer": answer, "fragment": fragment, "quote": quote, "confidence": confidence}


async def run(
    plan: PlanArtifact,
    nodes: Sequence[CanonicalNode],
    gateway: ScriptedGateway,
    *,
    edges: Sequence[RawEdge] = (),
    ontology: Ontology = ONT,
    store: MemoryStore | None = None,
    memory: AnsweredMemory | None = None,
) -> TargetGraph:
    return await conform(
        plan,
        ResolveArtifact(canonical=tuple(sorted(nodes, key=lambda item: item.identity_key))),
        EdgesArtifact(raw_edges=tuple(edges)),
        services=StepServices(gateway=gateway, ontology=ontology, options=BuildOptions()),
        vectors=VectorCache(gateway, store or MemoryStore(), PROJECT),
        at="2026-01-01T00:00:00+00:00",
        judging=ConformJudging(answered_memory=memory),
    )


def asked(gateway: ScriptedGateway) -> list[Any]:
    return [call for call in gateway.calls if call.prompt == PROMPT]


def gateway_near(factory: Factory, *texts: str, cosine: float = 0.8) -> ScriptedGateway:
    """A gateway whose question search text lies at ``cosine`` to each of ``texts`` (fragment texts)."""

    return factory().relate(SEARCH, cosine, texts=texts)


# Drop, keep


async def test_an_answered_question_is_dropped_with_its_edges_and_listed(scripted_gateway: Factory) -> None:
    gateway = gateway_near(scripted_gateway, ANSWER).handle(PROMPT, verdict("answered"))
    refunds = node("r", "Customers can request refunds online", "FunctionalRequirement", "spec#s1")
    graph = await run(
        plan_of(SPEC, FAQ),
        [question(), refunds],
        gateway,
        edges=[
            RawEdge(relation="RELATES_TO", source_cid="q", target_cid="r", fragment_ids=("spec#s0",), unit_id="spec@u0")
        ],
    )
    assert [item.name for item in graph.nodes] == ["Customers can request refunds online"]
    assert graph.edges == ()
    (listed,) = graph.answered_elsewhere
    assert (listed.fragment, listed.quote) == ("faq#s0", ANSWER)
    assert "which currency is used for refunds" in listed.key
    counters = graph.counters
    assert (counters.oq_answer_judged, counters.oq_answered_dropped, counters.oq_partly_kept) == (1, 1, 0)
    (call,) = asked(gateway)
    assert call.call_class == "small"
    user = call.messages[-1]["content"]
    assert QUESTION in user and ANSWER in user and "Customers can request refunds online" not in user


@pytest.mark.parametrize(
    ("answer", "partly"),
    [
        (verdict("partly"), 1),
        (verdict("open", quote=None), 0),
        (verdict("answered", confidence=0.79), 0),  # below the floor (0.8)
        (verdict("answered", quote="Refunds are paid in EUR"), 0),  # quote not in the fragment
        (verdict("answered", fragment="spec#s0"), 0),  # not a candidate fragment
        (lambda _: LLMError("exhausted"), 0),  # a failed call
    ],
    ids=["partly", "open", "below-floor", "invalid-quote", "foreign-fragment", "failed"],
)
async def test_anything_but_a_sure_quoted_answer_keeps_the_question(
    scripted_gateway: Factory, answer: Callable[[ScriptedRequest], Any], partly: int
) -> None:
    gateway = gateway_near(scripted_gateway, ANSWER).handle(PROMPT, answer)
    graph = await run(plan_of(SPEC, FAQ), [question()], gateway)
    assert [item.name for item in graph.nodes] == [QUESTION]
    assert graph.answered_elsewhere == ()
    counters = graph.counters
    assert (counters.oq_answer_judged, counters.oq_answered_dropped, counters.oq_partly_kept) == (1, 0, partly)


async def test_the_question_fragment_its_neighbours_and_far_fragments_are_no_candidates(
    scripted_gateway: Factory,
) -> None:
    """The question's own fragment and the fragments next to it in its document are never candidates
    however close; one below ``min_cosine`` is not either; a question without a candidate is not
    judged (no call)."""

    near_own = "Refund currency: the order's original currency is used."
    gateway = scripted_gateway().relate(SEARCH, 0.9, texts=[f"TBD: {QUESTION}", near_own])
    gateway.relate(SEARCH, 0.3, texts=[ANSWER])  # below min_cosine 0.35
    graph = await run(plan_of(("spec", [f"TBD: {QUESTION}", near_own]), FAQ), [question()], gateway)
    assert asked(gateway) == []
    assert [item.name for item in graph.nodes] == [QUESTION]
    assert (graph.counters.oq_answer_judged, graph.counters.oq_answered_dropped) == (0, 0)


async def test_a_later_part_of_the_question_document_can_answer_it(scripted_gateway: Factory) -> None:
    """Questions in part 1 of a file, answers in part 2 of the same file and unit: beyond the neighbours, the question's
    own unit offers candidates.
    """

    texts = [f"Part 1. {QUESTION}", "Part 1. Who approves refunds?", "Part 2. Call notes.", ANSWER]
    gateway = gateway_near(scripted_gateway, texts[1], ANSWER).handle(PROMPT, verdict("answered", fragment="spec#s3"))
    graph = await run(plan_of(("spec", texts)), [question()], gateway)
    (call,) = asked(gateway)
    user = call.messages[-1]["content"]
    assert ANSWER in user and "Who approves refunds" not in user  # spec#s1 is the question's neighbour
    assert graph.nodes == ()
    assert [(item.fragment, item.quote) for item in graph.answered_elsewhere] == [("spec#s3", ANSWER)]


async def test_at_most_the_flagged_number_of_candidates_is_shown_best_first(scripted_gateway: Factory) -> None:
    texts = [f"Refund rule {index}: refunds use the original currency." for index in range(4)]
    gateway = scripted_gateway()
    for index, text in enumerate(texts):
        gateway.relate(SEARCH, 0.6 + index * 0.05, texts=[text])
    gateway.handle(PROMPT, verdict("open", quote=None))
    await run(plan_of(SPEC, ("faq", texts)), [question()], gateway, ontology=ontology_with(candidates=2))
    (call,) = asked(gateway)
    user = call.messages[-1]["content"]
    assert [text in user for text in texts] == [False, False, True, True]


async def test_fresh_calls_stop_at_the_cap_and_the_rest_stay(scripted_gateway: Factory) -> None:
    other = "Which carrier ships refunds?"
    gateway = scripted_gateway()
    gateway.relate(SEARCH, 0.6, texts=[ANSWER]).relate(f"{OQ}: {other}", 0.6, texts=[ANSWER])
    gateway.handle(PROMPT, verdict("answered"))
    graph = await run(
        plan_of(("spec", [f"TBD: {QUESTION}", f"TBD: {other}"]), FAQ),
        [question(), question("q2", other, "spec#s1")],
        gateway,
        ontology=ontology_with(cap=1),
    )
    assert len(asked(gateway)) == 1
    assert len(graph.answered_elsewhere) == 1 and len(graph.nodes) == 1
    assert graph.counters.oq_answer_judged == 1


OPEN_ISSUES = (
    "### Open issues\n\n"
    "| ID | Open issue | Owner | Default if unanswered |\n"
    "|---|---|---|---|\n"
    "| OI-001 | Refund currency for large merchants | Finance | Original currency only |\n"
    "| OI-002 | Refund carrier | Logistics | Post only |"
)
ROW = "| OI-001 | Refund currency for large merchants | Finance | Original currency only |"
ROW_QUESTION = "Which refund currency do large merchants get?"


def fragment(fragment_id: str, document_id: str, heading: tuple[str, ...], text: str, ordinal: int) -> Passage:
    return Passage(
        FragmentRec(
            fragment_id=fragment_id,
            document_id=document_id,
            heading_path=heading,
            ordinal=ordinal,
            line_start=ordinal * 10 + 1,
            line_end=ordinal * 10 + 1 + text.count("\n"),
            text=text,
            content_hash=f"hash-{fragment_id}",
            tokens=10,
        )
    )


def test_fragments_of_the_question_section_are_no_candidates_beyond_the_neighbours() -> None:
    """The question's own section (same document and heading path, such as the rest of its table) is
    its own context however far; the same heading in another document is not."""

    table = ("Plan", "36.3 Open issues")
    passages = [
        fragment("spec#a", "spec", table, "| OI-001 | ... |", 0),
        fragment("spec#b", "spec", ("Plan", "36.4 Decisions"), "Decisions.", 1),
        fragment("spec#c", "spec", ("Plan", "37 Glossary"), "Terms.", 2),
        fragment("spec#d", "spec", table, "| OI-009 | ... |", 3),
        fragment("faq#a", "faq", table, "Answers.", 4),
    ]
    asked_here = Question("k", ROW_QUESTION, ROW, ROW_QUESTION, frozenset({"spec#a"}))
    assert own_context(asked_here, passages) == {"spec#a", "spec#b", "spec#d"}


def test_a_question_context_is_its_section_and_for_a_table_row_the_header_and_default() -> None:
    passages = [fragment("spec#a", "spec", ("Plan", "Open issues"), OPEN_ISSUES, 0)]
    row = Question("k", ROW_QUESTION, ROW, ROW_QUESTION, frozenset({"spec#a"}))
    assert question_context(row, passages) == Context(
        "Plan > Open issues",
        "| ID | Open issue | Owner | Default if unanswered |",
        "OI-001",
        "Original currency only",
    )
    prose = Question("k2", "Is it open?", "### Open issues", "x", frozenset({"spec#a"}))
    assert question_context(prose, passages) == Context("Plan > Open issues")
    assert question_context(Question("k3", "?", ROW, "x", frozenset({"other#a"})), passages) == Context()


@pytest.mark.parametrize(
    ("quote", "drops"),
    [
        ("Refunds are always paid in the order's original currency only.", 0),
        ("OI-001 is decided: large merchants are refunded in EUR.", 1),
    ],
)
async def test_an_open_issue_row_with_a_default_drops_only_for_an_answer_naming_it(
    scripted_gateway: Factory, quote: str, drops: int
) -> None:
    """A row of a table with a default column is listed as open with its default: an answer that does not name the row
    (its first cell) restates that default or the current design and keeps it; one naming the row drops it.
    """

    search = f"{OQ}: {ROW_QUESTION}"
    gateway = scripted_gateway().relate(search, 0.8, texts=[quote])
    gateway.handle(PROMPT, verdict("answered", quote=quote, fragment="faq#s0"))
    oq = canonical("q", ROW_QUESTION, OQ, ["spec#s0"], quote=ROW)
    graph = await run(plan_of(("spec", [OPEN_ISSUES]), ("faq", [quote])), [oq], gateway)
    (call,) = asked(gateway)
    assert "Default if unanswered" in call.messages[-1]["content"]
    assert [item.name for item in graph.nodes] == [ROW_QUESTION][drops:]
    assert len(graph.answered_elsewhere) == drops


# Memory


async def test_a_stored_verdict_replaces_the_call_until_the_question_or_a_candidate_changes(
    scripted_gateway: Factory,
) -> None:
    store = MemoryStore()  # the decisions; every run gets fresh vectors
    memory = AnsweredMemory(store, PROJECT, PROMPT, "small-model")
    first = gateway_near(scripted_gateway, ANSWER).handle(PROMPT, verdict("answered"))
    dropped = await run(plan_of(SPEC, FAQ), [question()], first, memory=memory)
    assert len(asked(first)) == 1

    again = gateway_near(scripted_gateway, ANSWER)  # unscripted: a call would fail the test
    reused = await run(plan_of(SPEC, FAQ), [question()], again, memory=memory)
    assert asked(again) == []
    assert reused.answered_elsewhere == dropped.answered_elsewhere
    assert reused.counters.oq_answer_judged == 1 and reused.counters.oq_answered_dropped == 1

    edited = "Refunds are always paid in the order's original currency, within 14 days."
    changed = gateway_near(scripted_gateway, edited).handle(PROMPT, verdict("partly", quote=edited))
    kept = await run(plan_of(SPEC, ("faq", [edited])), [question()], changed, memory=memory)
    assert len(asked(changed)) == 1  # the candidate's content changed: the verdict expired
    assert kept.answered_elsewhere == () and kept.counters.oq_partly_kept == 1


# Off


async def test_without_the_flag_nothing_is_judged_and_the_counts_are_none(scripted_gateway: Factory) -> None:
    gateway = gateway_near(scripted_gateway, ANSWER)
    graph = await run(plan_of(SPEC, FAQ), [question()], gateway, ontology=ontology_with(off=True))
    assert asked(gateway) == []
    assert [item.name for item in graph.nodes] == [QUESTION]
    counters = graph.counters
    assert (counters.oq_answer_judged, counters.oq_answered_dropped, counters.oq_partly_kept) == (None,) * 3


def test_the_frozen_ontology_has_no_flag() -> None:
    frozen = frozen_ontology()
    assert frozen.conform is None or frozen.conform.answered_elsewhere is None


# run.py wiring


@pytest.fixture
def stores(tmp_path: Path) -> FileStores:
    return open_file_stores(tmp_path / "stores", corpus_folder=tmp_path / "corpus")


def context(stores: FileStores, ontology: Ontology = ONT) -> BuildContext:
    return BuildContext(
        project_id=PROJECT,
        ontology=ontology,
        driver=None,
        gateway=ScriptedGateway(),
        registry=stores.registry,
        corpus=stores.corpus,
        extraction=stores.extraction,
        builds=stores.builds,
        executor=OfflineExecutor(None),
    )


def test_conform_small_calls_other_than_dedup_count_as_the_answered_check() -> None:
    assert runner._judge_step("conform", PROMPT, "small") == "answered_check"
    assert runner._judge_step("conform", DEDUP_PROMPT, "small") == "requirement_dedup"
    assert runner._judge_step("conform", "judge_owner_conflict.2", "judge") == "conform_judge"
    assert "answered_check" in runner.JUDGING_STEPS["conform"]


def test_the_conform_step_remembers_verdicts_only_with_the_flag(stores: FileStores) -> None:
    memory = runner._answered_memory(context(stores))
    assert memory is not None
    assert memory.fingerprint == f"{PROMPT}|{ScriptedGateway().config.model_for('small')}"
    assert memory.key("k") == f"{PROJECT}|answered|k"
    assert runner._answered_memory(context(stores, frozen_ontology())) is None


async def test_the_result_lists_answered_questions_by_spec_id_only(stores: FileStores) -> None:
    """A persisted question the build deleted keeps its issued ``spec_id``; one never persisted has
    none; identity keys never leave the artifact."""

    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    persisted, fresh = "requirement|span|secret persisted question", "requirement|span|secret new question"
    await stores.registry.record(PROJECT, "earlier", [SpecIdRow("QST::OLD", persisted, "uuid-old", "retired")])
    target = TargetGraph(
        answered_elsewhere=(
            AnsweredElsewhere(key=persisted, fragment="faq#s0", quote=ANSWER),
            AnsweredElsewhere(key=fresh, fragment="faq#s1", quote="Shipping is free."),
        )
    )
    await runner.store_artifact(ctx, "conform", target)
    result = await runner._finish(ctx, runner._RunStats())
    assert result.answered_elsewhere == (
        AnsweredElsewhereRef(spec_id="QST::OLD", fragment_id="faq#s0", quote=ANSWER),
        AnsweredElsewhereRef(spec_id=None, fragment_id="faq#s1", quote="Shipping is free."),
    )
    assert not any(key in result.model_dump_json() for key in (persisted, fresh))
