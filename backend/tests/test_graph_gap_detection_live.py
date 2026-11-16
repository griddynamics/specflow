"""Live: detection on the shop gap corpus through the core reader of the selected graph store (snapshot reads and
the trace behind site reach) gives the seeds the offline fixture reader gives (docs/graph/gap/detection.md)."""

from __future__ import annotations

from gap_acceptance_support import Planted, assert_all_recorded, write_gap_corpus_graph
from gap_detection_support import run_detection
from graph.core import load_ontology
from graph_probes import requires_live_graph
from graph_support import GraphProject

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()


async def test_detection_through_the_core_reader_matches_the_fixture_reader(graph_project: GraphProject) -> None:
    await write_gap_corpus_graph(graph_project, graph_project.project_id)
    reader = graph_project.reader(ontology=load_ontology())

    live = await run_detection(reader, project_id=graph_project.project_id)
    offline = await run_detection()

    assert {seed.signature for seed in live.detection.seeds} == {seed.signature for seed in offline.detection.seeds}
    assert {site.site_id for site in live.sites.sites} == {site.site_id for site in offline.sites.sites}
    question = live.site_of(Planted.STATED_QUESTION)
    assert question is not None and question.reach >= 1
    assert live.relevance_of("roadmap.md", "Release plan") == "mixed"
    assert live.gateway is not None
    assert_all_recorded(live.gateway)
