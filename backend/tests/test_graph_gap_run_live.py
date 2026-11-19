"""Live: a gap run, its record step and the gap reads on the shop corpus in the selected graph store give what the
offline run gives (docs/graph/gap/architecture.md "Run steps"; NFR-GPORT-01, FR-GOWN-01, FR-GOWN-02, FR-GRUN-03)."""

from __future__ import annotations

from typing import Any

from gap_acceptance_support import Planted, RecordedGapGateway, write_gap_corpus_graph
from gap_investigation_support import answers_by_site, loop_world_of, pack_payload, round_of
from gap_run_support import GapWorld, corpus_world
from graph.core import load_ontology
from graph.core.store import as_executor
from graph.gap import GapService, run_gap
from graph.gap.gaps import read_gaps
from graph.gap.rules import load_gap_rules
from graph_probes import requires_live_graph
from graph_support import GraphProject

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()


async def test_a_run_records_and_reads_the_planted_gaps_on_the_live_store(graph_project: GraphProject) -> None:
    project_id = graph_project.project_id
    ontology = load_ontology()
    await write_gap_corpus_graph(graph_project, project_id)
    world = GapWorld(
        reader=graph_project.reader(ontology=ontology),
        executor=graph_project.store,  # the live store: OwnedWriter writes, the gap queries read
        ontology=ontology,
        rules=load_gap_rules(ontology=ontology),
        project_id=project_id,
    )
    offline = await corpus_world()
    expected = await run_gap(offline.context(), mode="full")

    result = await run_gap(world.context(), mode="full")

    stored = await read_gaps(as_executor(graph_project.store), project_id, ontology)
    assert {gap.gap_id for gap in stored} == set(offline.gap_nodes())
    assert result.counts["gaps_new"] == expected.counts["gaps_new"]
    question = next(gap for gap in stored if gap.props["kind"] == "open_question")
    assert set(question.about_ids) == {Planted.STATED_QUESTION, Planted.PLACE_ORDER}
    assert [cite.quote for cite in question.cites] == [Planted.STATED_QUESTION_LINE]
    assert question.cites[0].line_start is not None and question.cites[0].document_id == "upload:orders-spec"
    assert all(link.label and link.uuid for gap in stored for link in (*gap.about, *gap.impacts))

    service = GapService(graph_project.store, world.reader, ontology, world.rules, world.records, world.amendments)
    page = await service.list_gaps(project_id, about=[Planted.STATED_QUESTION])
    assert [view.gap_id for view in page.gaps] == [question.gap_id]
    view = await service.get_gap(project_id, question.gap_id)
    assert view.history is not None and [event.event for event in view.history.events] == ["created", "routed"]
    assert len(view.options) == 2 and sum(option.recommended for option in view.options) == 1
    counts = await service.open_counts(project_id, [Planted.STATED_QUESTION])
    assert counts == {Planted.STATED_QUESTION: 1}
    document = await service.list_gaps(project_id, document="upload:orders-spec", limit=200)
    assert question.gap_id in {item.gap_id for item in document.gaps}

    again = await run_gap(world.context())

    assert again.counts["sites"]["investigated"] == 0 and again.counts["gaps_new"]["total"] == 0
    assert len(await read_gaps(as_executor(graph_project.store), project_id, ontology)) == len(stored)


async def test_the_structured_loop_writes_gaps_options_and_a_pointer_on_the_live_store(
    graph_project: GraphProject,
) -> None:
    """The loop over the core reader (pack and reads on the live store) with the authored answers by site; the
    refund-to-card site points at the line that answers it, and the judge confirms it (FR-GINV-07 on a live store)."""

    project_id = graph_project.project_id
    ontology = load_ontology()
    await write_gap_corpus_graph(graph_project, project_id)
    authored = answers_by_site()
    refunds = "upload:payments-spec#payments-service-specification/refunds#56c00bc8"
    pointer = {
        "verdict": "gap",
        "kind": "missing",
        "severity": "minor",
        "title": "How long a refund to the card takes",
        "question": "How long does a refund to the original card take?",
        "why": "The requirement has no acceptance criteria.",
        "about": ["REQ::FR::REFUND_TO_ORIGINAL_CARD"],
        "witness": [{"fragment_id": None, "quote": None, "fact": 0}],
        "pointer": {"fragment_id": refunds, "quote": Planted.POINTER_ANSWER_LINE},
    }

    def investigate(request: Any) -> Any:
        site = pack_payload(request)["site_id"]
        answer = dict(authored[(site, round_of(request))][1])
        if "REQ::FR::REFUND_TO_ORIGINAL_CARD" in str(pack_payload(request)["seeds"]):
            answer = {"action": "report", "reads": [], "report": pointer}
        return answer

    gateway = RecordedGapGateway()
    gateway.handle("gap.investigate.1", investigate)
    gateway.handle(
        "gap.judge.1",
        lambda _request: {"entailed": True, "consistent": True, "confidence": 0.95, "reason": "", "find_words": None},
    )
    world = loop_world_of(
        GapWorld(
            reader=graph_project.reader(ontology=ontology),
            executor=graph_project.store,
            ontology=ontology,
            rules=load_gap_rules(ontology=ontology),
            project_id=project_id,
            gateway=gateway,
        )
    )

    result = await run_gap(world.context(), mode="full")

    stored = {gap.gap_id: gap for gap in await read_gaps(as_executor(graph_project.store), project_id, ontology)}
    assert result.status == "completed" and result.counts["rejected"] == 0 and result.counts["pointers"] == 1
    resolved = [gap for gap in stored.values() if gap.status == "resolved"]
    assert len(resolved) == 1 and resolved[0].props["resolution"] == "pointer"
    (clarification,) = resolved[0].clarifications
    assert clarification["mode"] == "pointer" and clarification["status"] == "applied"
    question = next(gap for gap in stored.values() if gap.props["kind"] == "open_question")
    assert set(question.about_ids) == {Planted.STATED_QUESTION, Planted.PLACE_ORDER}
    service = GapService(graph_project.store, world.reader, ontology, world.rules, world.records, world.amendments)
    view = await service.get_gap(project_id, question.gap_id)
    assert [option.label[:2] for option in view.options] == ["A:", "B:", "C:"] and view.options[0].recommended
    pointed = await service.get_gap(project_id, resolved[0].gap_id)
    assert pointed.clarification is not None and pointed.clarification.mode == "pointer"
