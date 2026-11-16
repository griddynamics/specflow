"""Live: the close check and a dismissal that stays quiet on the selected graph store (docs/graph/gap/lifecycle.md
"Close check", "Dismissals and suppression"; FR-GCLS-01, FR-GCOR-03, FR-GDET-07, NFR-GPORT-01).

A full run (seed investigator, no model) on the shop corpus, then an own answer to the cancel-paid-order gap placed in
the clarifications document and applied. The update build is written to the store by hand: the clarifications
document read from the live graph becomes a document with one fragment per section, and the build's answered-elsewhere
check drops the question node. The next update run grounds the clarification in its section (``GROUNDED_IN``),
places it in the document (``PLACED_IN``) and resolves the gap. A dismissed gap stays quiet in a full run that
investigates every site again.
"""

from __future__ import annotations

import copy
from typing import Any

from api.graph_build.corpus import CorpusAnswersRead, with_answers
from gap_acceptance_support import Planted, corpus_revisions, gap_corpus_graph, write_gap_corpus_graph
from gap_close_support import _resegment, seed_investigator
from gap_port_fakes import MemoryBuildRequests
from gap_run_support import GapWorld
from graph.core import load_ontology
from graph.core.store import as_executor
from graph.gap import GapService, run_gap
from graph.gap.corpus import clarifications_lineage, document_clarifications
from graph.gap.models import AnswerInput, BuildFacts, GapChangeReport
from graph.gap.rules import load_gap_rules
from graph.gap.settings import GapSettings
from graph_plan_support import fixture_graph, write_spec_graph
from graph_probes import requires_live_graph
from graph_support import GraphProject

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

MEMBER = "user-member-2"
QUESTION_NODE_UUID = "gap-node-0024"  # QST::CANCEL_PAID_ORDER in graph.yaml


async def _world(graph_project: GraphProject) -> tuple[GapWorld, GapService]:
    project_id = graph_project.project_id
    ontology = load_ontology()
    await write_gap_corpus_graph(graph_project, project_id)
    world = GapWorld(
        reader=graph_project.reader(ontology=ontology),
        executor=graph_project.store,
        ontology=ontology,
        rules=load_gap_rules(ontology=ontology),
        project_id=project_id,
    )
    await run_gap(world.context(), mode="full")
    service = GapService(
        graph_project.store,
        world.reader,
        ontology,
        world.rules,
        world.records,
        world.amendments,
        builds=MemoryBuildRequests(),
    )
    return world, service


async def _cancel_gap(service: GapService, project_id: str) -> Any:
    page = await service.list_gaps(project_id, about=[Planted.STATED_QUESTION])
    (gap,) = page.gaps
    return gap


async def test_a_document_answer_is_grounded_placed_and_resolved_on_the_live_store(graph_project: GraphProject) -> None:
    project_id = graph_project.project_id
    world, service = await _world(graph_project)
    gap = await _cancel_gap(service, project_id)
    text = "Support decides each cancellation of a paid order."
    answer = AnswerInput(gap_id=gap.gap_id, own_text=text, placement="document", answered_by=MEMBER)
    result = await service.answer_gap(project_id, answer)
    await service.apply_now(project_id, by=MEMBER)

    executor = as_executor(graph_project.store)
    read = CorpusAnswersRead(clarifications=await document_clarifications(executor, project_id, world.ontology))
    (document,) = [
        item
        for item in with_answers(project_id, corpus_revisions(), read)
        if item.lineage_id == clarifications_lineage(project_id)
    ]
    data = copy.deepcopy(dict(gap_corpus_graph().data))
    moved = _resegment(data, document, "build-2")
    await write_spec_graph(graph_project, project_id, fixture_graph(data, world.ontology), world.ontology)
    writer = graph_project.writer(project_id=project_id, ontology=world.ontology)
    await writer.delete_node("OpenQuestion", f"{project_id}-{QUESTION_NODE_UUID}")
    report = GapChangeReport(build_id="build-2", removed=(Planted.STATED_QUESTION,))

    seed_investigator(world)
    run = await run_gap(world.context(changes=[report], build=BuildFacts(build_id="build-2")))

    after = await service.get_gap(project_id, gap.gap_id)
    assert (after.status, after.resolution) == ("resolved", "answered")
    assert after.clarification is not None and after.clarification.status == "applied"
    assert run.counts["close"]["resolved"] == 1
    (fragment,) = [new for _, new in moved.values()]
    grounded = await graph_project.edges("GROUNDED_IN")
    assert {(edge.source.props["clr_id"], edge.target.props["fragment_id"]) for edge in grounded} >= {
        (result.clr_id, fragment)
    }
    placed = await graph_project.edges("PLACED_IN")
    assert (result.clr_id, clarifications_lineage(project_id)) in {
        (edge.source.props["clr_id"], edge.target.props["document_id"]) for edge in placed
    }


async def test_a_dismissed_gap_stays_quiet_on_the_live_store(graph_project: GraphProject) -> None:
    project_id = graph_project.project_id
    world, service = await _world(graph_project)
    gap = await _cancel_gap(service, project_id)

    await service.dismiss_gap(project_id, gap.gap_id, reason="not a gap", by=MEMBER)
    run = await run_gap(world.context(settings=GapSettings(ignore_cache=True)), mode="full")

    assert run.counts["seeds_by_detector"]["OQ-STATED"]["suppressed"] == 1
    assert (await service.list_gaps(project_id, about=[Planted.STATED_QUESTION])).gaps == ()
