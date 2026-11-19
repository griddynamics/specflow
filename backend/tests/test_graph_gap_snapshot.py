"""The detection snapshot of graph.gap: sections, lines, attribute defaults and spine owners
(docs/graph/gap/detection.md "Relevance filter", "Detector catalogue")."""

from __future__ import annotations

from gap_acceptance_support import Planted, gap_corpus_graph
from gap_detection_support import PROJECT, MiniGraph, TracingReader
from graph.core import load_ontology
from graph.core.reader import GraphFragment
from graph.gap.steps.snapshot import GraphSnapshot, heading_path_of, read_snapshot


def test_heading_paths_come_as_tuples_json_lists_or_plain_text() -> None:
    def fragment(value: object) -> GraphFragment:
        return GraphFragment.model_validate({"fragment_id": "f", "document_id": "d", "heading_path": value})

    assert heading_path_of(fragment(("A", "B"))) == ("A", "B")
    assert heading_path_of(fragment('["A", "B"]')) == ("A", "B")
    assert heading_path_of(fragment("A > B")) == ("A > B",)
    assert heading_path_of(fragment('{"a": 1}')) == ('{"a": 1}',)
    assert heading_path_of(fragment(None)) == ()


def test_inactive_documents_and_fragments_and_dangling_edges_are_left_out() -> None:
    graph = MiniGraph()
    graph.section("Orders", "Orders are kept.")
    graph.section("Old", "Gone.", document="old.md")
    graph.documents["upload:old"] = graph.documents["upload:old"].model_copy(update={"status": "deleted"})
    graph.node("MOD::A", "Module", "A")
    graph.edge("MOD::A", "INTEGRATES_WITH", "MOD::MISSING")

    snapshot = graph.snapshot()

    assert [section.heading_path[-1] for section in snapshot.sections] == ["Orders"]
    assert snapshot.edges == ()
    assert snapshot.fragment_lines("missing") == ()
    lines = snapshot.fragment_lines(graph.fragments[0].fragment_id)
    assert [(line.line_start, line.text) for line in lines] == [(1, "## Orders"), (2, "Orders are kept.")]
    assert snapshot.sections[0].label == "spec.md \u203a Shop specification \u203a Orders"


def test_values_read_the_declared_default() -> None:
    graph = MiniGraph()
    node = graph.node("REQ::FR::A", "FunctionalRequirement", "A")
    snapshot = graph.snapshot()

    assert snapshot.value(node, "in_scope") == "yes"
    assert not snapshot.out_of_spine(node)
    assert snapshot.matches(node, {"in_scope": ["yes"]})
    assert not snapshot.matches(node, {})


def test_spine_owners_follow_the_spine_paths_and_leave_out_excluded_roots() -> None:
    graph = MiniGraph()
    graph.node("MOD::ORDERS", "Module", "Orders")
    graph.node("MOD::STRIPE", "Module", "Stripe", external="yes")
    graph.node("MOD::LATER", "Module", "Later", in_scope="no")
    graph.node("FEAT::PLACE", "Feature", "Place")
    graph.node("REQ::FR::PLACE", "FunctionalRequirement", "Place")
    graph.node("REQ::FR::PAY", "FunctionalRequirement", "Pay")
    graph.node("REQ::FR::LATER", "FunctionalRequirement", "Later")
    graph.node("IFC::STRIPE", "Interface", "Stripe API")
    graph.node("REQ::INT::CAPTURE", "IntegrationRequirement", "Capture")
    graph.edge("REQ::FR::PLACE", "PART_OF", "FEAT::PLACE")
    graph.edge("FEAT::PLACE", "PART_OF", "MOD::ORDERS")
    graph.edge("REQ::FR::PAY", "RELATES_TO", "MOD::STRIPE")
    graph.edge("REQ::FR::LATER", "PART_OF", "MOD::LATER")
    graph.edge("REQ::INT::CAPTURE", "PART_OF", "IFC::STRIPE")
    graph.edge("IFC::STRIPE", "PROVIDED_BY", "MOD::STRIPE")
    graph.edge("IFC::STRIPE", "CONSUMED_BY", "MOD::ORDERS")
    snapshot = graph.snapshot()
    internal = {"external": ["yes"]}

    assert snapshot.spine_owners("REQ::FR::PLACE") == {"MOD::ORDERS"}
    assert snapshot.spine_owners("REQ::FR::PAY") == {"MOD::STRIPE"}
    assert snapshot.spine_owners("REQ::FR::PAY", internal) == frozenset()
    assert snapshot.spine_owners("REQ::FR::LATER") == frozenset()
    assert snapshot.spine_owners("REQ::INT::CAPTURE", internal) == {"MOD::ORDERS"}
    assert snapshot.spine_owners("REQ::FR::PAY", without=["RELATES_TO"]) == frozenset()
    assert snapshot.spine_owners("MOD::ORDERS") == {"MOD::ORDERS"}
    assert snapshot.spine_owners("REQ::FR::UNKNOWN") == frozenset()


async def test_the_corpus_snapshot_reads_every_part_once() -> None:
    corpus = gap_corpus_graph()
    snapshot = await read_snapshot(TracingReader(corpus.reader, load_ontology()), PROJECT, load_ontology())

    assert isinstance(snapshot, GraphSnapshot)
    assert len(snapshot.nodes) == len(corpus.nodes)
    assert len(snapshot.sections) == len(corpus.fragments)
    question = snapshot.nodes[Planted.STATED_QUESTION]
    assert [line.text for line in snapshot.lines(question)] == [Planted.STATED_QUESTION_LINE]
    assert Planted.STATED_QUESTION_LINE in snapshot.text(question)
    assert snapshot.related(Planted.STATED_QUESTION) == {Planted.PLACE_ORDER}


def test_a_provenance_without_a_quote_reads_its_lines_from_the_fragment() -> None:
    graph = MiniGraph()
    graph.section("Orders", "Orders are kept.", "  For ten years.  ")
    node = graph.node("REQ::FR::KEEP", "FunctionalRequirement", "Keep", quote="Orders are kept.")
    item = node.provenance[0]
    ranged = node.model_copy(
        update={"provenance": (item.model_copy(update={"quote": None, "line_end": item.line_start + 1}),)}
    )
    lost = node.model_copy(update={"provenance": (item.model_copy(update={"quote": None, "line_start": None}),)})
    graph.nodes[-1] = ranged
    snapshot = graph.snapshot()

    assert [line.text for line in snapshot.lines(ranged)] == ["Orders are kept. For ten years."]
    assert snapshot.lines(lost) == ()
