"""Live: a gap run, its record step and the gap reads on the shop corpus in the selected graph store give what the
offline run gives (docs/graph/gap/architecture.md "Run steps"; NFR-GPORT-01, FR-GOWN-01, FR-GOWN-02, FR-GRUN-03)."""

from __future__ import annotations

from gap_acceptance_support import Planted, write_gap_corpus_graph
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
    assert view.history is not None and [event.event for event in view.history.events] == ["created"]
    counts = await service.open_counts(project_id, [Planted.STATED_QUESTION])
    assert counts == {Planted.STATED_QUESTION: 1}
    document = await service.list_gaps(project_id, document="upload:orders-spec", limit=200)
    assert question.gap_id in {item.gap_id for item in document.gaps}

    again = await run_gap(world.context())

    assert again.counts["sites"]["investigated"] == 0 and again.counts["gaps_new"]["total"] == 0
    assert len(await read_gaps(as_executor(graph_project.store), project_id, ontology)) == len(stored)
