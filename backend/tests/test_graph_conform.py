"""Conform unit tests."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from graph.build.conform import (
    DEDUP_PROMPT,
    DEDUP_RICH_FLOOR,
    ConformJudging,
    DedupMemory,
    GroundingStats,
    _refusal,
    _Signature,
    _surviving_refusals,
    check_invariants,
    conform,
    dedup_candidate_text,
    grounding_stats,
)
from graph.build.corpus import segment
from graph.build.models import (
    AttributeValue,
    CanonicalNode,
    ChangeSet,
    DocumentRec,
    EdgesArtifact,
    EvidenceLines,
    FragmentRec,
    PlanArtifact,
    QuarantinedTargetEdge,
    RawEdge,
    RefusedDedupPair,
    ResolveArtifact,
    SourceQuote,
    TargetGraph,
    TargetNode,
    Unit,
)
from graph.build.options import BuildOptions, StepServices
from graph.build.ports import CorpusRevision
from graph.build.resolve import PersistedIdentity, hub_surface, persisted_identities
from graph.build.vectors import VectorCache
from graph.core.errors import ConformanceError, LLMError
from graph.core.identity import bounded_key_value, normalise_surface
from graph.core.llm.gateway import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from graph.core.ontology import Ontology, load_ontology
from graph_extraction_store_support import MemoryStore
from graph_support import (
    FROZEN_ONTOLOGY_FILE,
    ScriptedGateway,
    ScriptedRequest,
    copy_frozen_prompts,
    frozen_ontology,
    llm_config,
    shipped_ontology,
)

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, graph_project) live in graph_support.
pytest_plugins = ["graph_support"]

# Mechanics tests: pinned to the frozen v1.3 ontology (graph_support.FROZEN_ONTOLOGY_FILE), not the shipped one.
ONT = frozen_ontology()
PROJECT = "p-conform"
STAMP = "2026-01-01T00:00:00+00:00"
FR = "FunctionalRequirement"
QR = "QualityRequirement"
QUOTE = "quote"
Factory = Callable[..., ScriptedGateway]


# Local helpers


class AngleGateway(ScriptedGateway):
    """A ``ScriptedGateway`` whose listed texts embed to pinned vectors."""

    def __init__(self, pinned: Mapping[str, Sequence[float]], **options: Any) -> None:
        super().__init__(**options)
        self.pinned = {text: list(vector) for text, vector in pinned.items()}

    def vector(self, text: str) -> list[float]:
        return self.pinned.get(text) or super().vector(text)


def at_cosine(cosine: float) -> tuple[list[float], list[float]]:
    """Two unit vectors of the offline dimension with the given cosine."""

    dim = llm_config().embedding_dim
    first = [1.0] + [0.0] * (dim - 1)
    second = [cosine, math.sqrt(1 - cosine * cosine)] + [0.0] * (dim - 2)
    return first, second


def fragments_of(document_id: str, texts: Sequence[str]) -> list[FragmentRec]:
    result = []
    line = 1
    for index, text in enumerate(texts):
        count = len(text.split("\n"))
        result.append(
            FragmentRec(
                fragment_id=f"{document_id}#s{index}",
                document_id=document_id,
                heading_path=(f"Section {index}",),
                ordinal=index,
                line_start=line,
                line_end=line + count - 1,
                text=text,
                content_hash=f"hash-{document_id}-{index}",
                tokens=max(1, len(text) // 4),
            )
        )
        line += count + 1
    return result


def plan_from(fragments: Sequence[FragmentRec]) -> PlanArtifact:
    """A plan of the given fragments: one document and one unit per document id."""

    documents: dict[str, list[FragmentRec]] = {}
    for fragment in fragments:
        documents.setdefault(fragment.document_id, []).append(fragment)
    return PlanArtifact(
        mode="first",
        change_set=ChangeSet(new=tuple(documents)),
        documents=tuple(
            DocumentRec(
                document_id=document_id,
                revision_id=f"{document_id}-r1",
                version=1,
                path=f"{document_id}.md",
                title=document_id.title(),
            )
            for document_id in documents
        ),
        fragments=tuple(fragments),
        dirty_ids=tuple(fragment.fragment_id for fragment in fragments),
        units=tuple(
            Unit(
                unit_id=f"{document_id}@u0",
                document_id=document_id,
                fragment_ids=tuple(fragment.fragment_id for fragment in own),
                tokens=10,
                content_hash=f"unit-{document_id}",
                windows=((0, len(own)),),
            )
            for document_id, own in documents.items()
        ),
    )


def plan_of(*documents: tuple[str, Sequence[str]]) -> PlanArtifact:
    return plan_from([fragment for document_id, texts in documents for fragment in fragments_of(document_id, texts)])


def node(
    cid: str,
    name: str,
    label: str,
    fragments: Sequence[str],
    *,
    explicit: str | None = None,
    aliases: Sequence[str] = (),
) -> CanonicalNode:
    """A canonical node keyed as Resolve keys it: explicit id, hub surface, or provisional span."""

    if explicit is not None:
        key, provisional = f"requirement|id|{explicit}", False
    elif label in ONT.hub_labels:
        key, provisional = f"{label}|surface|{hub_surface(name)}", False
    else:
        key, provisional = f"requirement|span|{normalise_surface(name)}", True
    return CanonicalNode(
        cid=cid,
        identity_key=key,
        identity_aliases=tuple(aliases),
        provisional=provisional,
        name=name,
        label=label,
        member_ids=(f"raw-{cid}",),
        explicit_ids=(explicit,) if explicit else (),
        fragment_ids=tuple(fragments),
        name_embedding_text=f"{label}: {name}",
    )


def edge(
    relation: str,
    source: str,
    target: str,
    fact: str | None,
    fragments: Sequence[str],
    *,
    unit: str,
) -> RawEdge:
    return RawEdge(
        relation=relation,
        source_cid=source,
        target_cid=target,
        fact=fact,
        fragment_ids=tuple(fragments),
        unit_id=unit,
    )


@dataclass(frozen=True)
class Setup:
    """The ontology and locality option of one ``run_conform``."""

    locality: str = "grounded"
    ontology: Ontology = ONT
    quotes_from_attributes: bool = True
    """A ``quote`` attribute becomes the node's one source quote (``with_quotes``); False: passed on as given."""
    judging: ConformJudging | None = None


async def run_conform(
    plan: PlanArtifact,
    nodes: Sequence[CanonicalNode],
    gateway: ScriptedGateway,
    *,
    edges: Sequence[RawEdge] = (),
    attributes: Mapping[str, Mapping[str, AttributeValue]] | None = None,
    setup: Setup | None = None,
    persisted: Mapping[str, PersistedIdentity] | None = None,
    store: MemoryStore | None = None,
) -> TargetGraph:
    """Conform over ``nodes``; a ``quote`` among a node's ``attributes`` becomes its one source quote
    over all its fragments (``with_quotes``), as Resolve hands it over, and leaves the attributes
    (``Setup.quotes_from_attributes`` false: the attributes are passed on as given)."""

    setup = setup or Setup()
    if setup.quotes_from_attributes:
        nodes, values = with_quotes(nodes, attributes or {})
    else:
        values = {cid: dict(found) for cid, found in (attributes or {}).items()}
    return await conform(
        plan,
        ResolveArtifact(canonical=tuple(sorted(nodes, key=lambda item: item.identity_key))),
        EdgesArtifact(raw_edges=tuple(edges), attributes=values),
        services=StepServices(gateway=gateway, ontology=setup.ontology, options=BuildOptions(locality=setup.locality)),
        vectors=VectorCache(gateway, store or MemoryStore(), PROJECT),
        at=STAMP,
        persisted=persisted,
        judging=setup.judging,
    )


def with_quotes(
    nodes: Sequence[CanonicalNode], attributes: Mapping[str, Mapping[str, AttributeValue]]
) -> tuple[list[CanonicalNode], dict[str, dict[str, AttributeValue]]]:
    """``nodes`` with the ``quote`` of their attributes as their one source quote (over all their
    fragments) unless they carry quotes already, and the attributes without it."""

    quoted = []
    for given in nodes:
        quote = (attributes.get(given.cid) or {}).get(QUOTE)
        if isinstance(quote, str) and quote and not given.quotes:
            source = SourceQuote(quote=quote, fragment_ids=given.fragment_ids)
            quoted.append(given.model_copy(update={"quotes": (source,)}))
        else:
            quoted.append(given)
    values = {cid: {name: value for name, value in found.items() if name != QUOTE} for cid, found in attributes.items()}
    return quoted, {cid: found for cid, found in values.items() if found}


def by_name(graph: TargetGraph) -> dict[str, TargetNode]:
    return {item.name: item for item in graph.nodes}


def lines(fragment_id: str, start: int, end: int | None = None) -> EvidenceLines:
    return EvidenceLines(
        fragment_id=fragment_id,
        line_start=start,
        line_end=start if end is None else end,
    )


def same(*, answer: bool, confidence: str = "high") -> Callable[[ScriptedRequest], dict[str, Any]]:
    return lambda _: {"same": answer, "confidence": confidence}


# 1. Quote snapping


async def test_quote_snaps_to_its_line_and_becomes_the_span_key(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        (
            "doc",
            ["Intro line.\n- Customers can export order history as CSV.\nOther line."],
        )
    )
    graph = await run_conform(
        plan,
        [node("c1", "Customer can export order history as CSV", FR, ["doc#s0"])],
        scripted_gateway(),
        attributes={"c1": {"quote": "Customers can export order history as CSV"}},
    )
    (target,) = graph.nodes
    assert target.evidence == (lines("doc#s0", 2),)
    assert target.quote == "Customers can export order history as CSV."  # verbatim line, list marker cut
    assert target.identity_key == "requirement|span|customers can export order history as csv"
    assert graph.counters.quote_fallbacks == 0


@pytest.mark.parametrize(
    ("quote", "snaps"),
    [
        (
            "customers CAN export order-history, as csv",
            True,
        ),  # case, punctuation and spacing ignored
        (
            "Customers can export order history as CSV files",
            True,
        ),  # 7 of 8 tokens: 0.875
        (
            "Customers can export the order history as JSON files",
            False,
        ),  # 6 of 9 tokens
    ],
)
async def test_quote_snaps_at_token_overlap_of_at_least_085(
    scripted_gateway: Factory, quote: str, *, snaps: bool
) -> None:
    plan = plan_of(
        (
            "doc",
            ["Intro line.\nCustomers can export order history as CSV.\nOther line."],
        )
    )
    graph = await run_conform(
        plan,
        [node("c1", "Customer can export order history as CSV", FR, ["doc#s0"])],
        scripted_gateway(),
        attributes={"c1": {"quote": quote}},
    )
    (target,) = graph.nodes
    if snaps:
        assert target.evidence == (lines("doc#s0", 2),)
        assert target.quote == "Customers can export order history as CSV."
    else:
        assert target.evidence == (lines("doc#s0", 1, 3),)
        assert target.quote is None
        assert target.identity_key == "requirement|span|customer can export order history as csv"  # provisional stays
        assert graph.counters.quote_fallbacks == 1


async def test_quote_spanning_lines_gives_a_contiguous_span(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(("doc", ["Customers can export\norder history as CSV.\nOther line."]))
    graph = await run_conform(
        plan,
        [node("c1", "Customer can export order history as CSV", FR, ["doc#s0"])],
        scripted_gateway(),
        attributes={"c1": {"quote": "Customers can export order history as CSV"}},
    )
    (target,) = graph.nodes
    assert target.evidence == (lines("doc#s0", 1, 2),)
    assert target.quote == "Customers can export\norder history as CSV."
    assert target.identity_key == "requirement|span|customers can export order history as csv"


async def test_quote_found_outside_the_attributed_fragments_is_searched_in_the_unit(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        ("doc", ["Nothing here.", "Customers can export order history as CSV."]),
        ("other", ["x"]),
    )
    graph = await run_conform(
        plan,
        [node("c1", "Customer can export order history as CSV", FR, ["doc#s0"])],
        scripted_gateway(),
        attributes={"c1": {"quote": "Customers can export order history as CSV"}},
    )
    (target,) = graph.nodes
    assert target.evidence == (lines("doc#s1", 3),)
    assert [statement.fragment_id for statement in target.statements] == ["doc#s1"]


async def test_masked_lines_keep_numbering_and_never_match(
    scripted_gateway: Factory,
) -> None:
    markdown = (
        "# Orders\n\nCustomers can export order history as CSV.\n<!-- page 2 marker -->\nAdmins can purge exports.\n"
    )
    fragments = segment(
        CorpusRevision(
            lineage_id="doc",
            revision_id="r1",
            version=1,
            path="doc.md",
            title="Orders",
            markdown=markdown,
        ),
        BuildOptions(),
    )
    (fragment,) = fragments
    assert fragment.text.split("\n")[5 - fragment.line_start] == "Admins can purge exports."
    assert "page" not in fragment.text
    plan = plan_from(fragments)
    graph = await run_conform(
        plan,
        [
            node("c1", "Admin can purge exports", FR, [fragment.fragment_id]),
            node("c2", "Page marker", FR, [fragment.fragment_id]),
        ],
        scripted_gateway(),
        attributes={
            "c1": {"quote": "Admins can purge exports"},
            "c2": {"quote": "page 2 marker"},
        },
    )
    nodes = by_name(graph)
    assert nodes["Admin can purge exports"].evidence == (lines(fragment.fragment_id, 5),)
    assert nodes["Page marker"].quote is None
    assert nodes["Page marker"].evidence == (lines(fragment.fragment_id, fragment.line_start, fragment.line_end),)


async def test_node_without_quote_is_grounded_in_its_fragments_and_states_the_line_naming_it(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(("doc", ["Intro.\nThe Ledger keeps totals.", "Second fragment."]))
    graph = await run_conform(plan, [node("c1", "Ledger", "Module", ["doc#s0", "doc#s1"])], scripted_gateway())
    (target,) = graph.nodes
    assert target.quote is None
    assert target.evidence == (lines("doc#s0", 2), lines("doc#s1", 4))
    # A hub states only the fragments that name it (F1); doc#s1 does not.
    assert [statement.text for statement in target.statements] == [
        "The Ledger keeps totals.",
    ]


# Grounding per source and its statistics


def quoted(item: CanonicalNode, *quotes: tuple[str, Sequence[str]]) -> CanonicalNode:
    return item.model_copy(
        update={"quotes": tuple(SourceQuote(quote=quote, fragment_ids=tuple(ids)) for quote, ids in quotes)}
    )


TWO_DOCS = plan_of(
    ("a", ["FR-001: Customers can export orders as CSV.", "Other text."]),
    ("b", ["Intro text.", "Downloads.\nFR-001 lets customers download their orders as a CSV file."]),
)


async def test_an_id_requirement_worded_differently_in_two_documents_keeps_evidence_from_both(
    scripted_gateway: Factory,
) -> None:
    item = quoted(
        node("c1", "Customers can export orders as CSV", FR, ["a#s0", "b#s1"], explicit="FR-001"),
        ("lets customers download their orders as a CSV file", ["b#s1"]),
        ("Customers can export orders as CSV", ["a#s0"]),
    )
    graph = await run_conform(TWO_DOCS, [item], scripted_gateway())
    (target,) = graph.nodes
    assert target.evidence == (lines("a#s0", 1), lines("b#s1", 4))
    assert [(statement.document_id, statement.text) for statement in target.statements] == [
        ("a", "FR-001: Customers can export orders as CSV."),
        ("b", "FR-001 lets customers download their orders as a CSV file."),
    ]
    # the primary span is the best-ranked one: equal overlap and length, the earlier fragment
    assert target.quote == "FR-001: Customers can export orders as CSV."
    counters = graph.counters
    assert (counters.unsnapped_source_quotes, counters.attributed_fragments_dropped) == (0, 0)
    assert (counters.nodes_missing_source_documents, counters.quote_fallbacks) == (0, 0)
    assert counters.unsnapped_quote_nodes == {}


async def test_an_unsnapped_quote_falls_back_to_an_id_line_else_its_fragment_is_dropped_and_counted(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        ("a", ["FR-001: Customers can export orders as CSV."]),
        ("b", ["Background.\nSee FR-001 for the file format."]),
        ("c", ["Nothing about it here."]),
    )
    item = quoted(
        node("c1", "Customers can export orders as CSV", FR, ["a#s0", "b#s0", "c#s0"], explicit="FR-001"),
        ("Customers can export orders as CSV", ["a#s0"]),
        ("the export writes one row per order", ["b#s0"]),
        ("exports are kept for a year", ["c#s0"]),
    )
    graph = await run_conform(plan, [item], scripted_gateway())
    (target,) = graph.nodes
    # b: the line holding the node's id is secondary evidence; c: dropped
    assert target.evidence == (lines("a#s0", 1), lines("b#s0", 2))
    assert [statement.text for statement in target.statements] == [
        "FR-001: Customers can export orders as CSV.",
        "See FR-001 for the file format.",
    ]
    assert target.quote == "FR-001: Customers can export orders as CSV."
    counters = graph.counters
    assert (counters.unsnapped_source_quotes, counters.attributed_fragments_dropped) == (2, 1)
    assert (counters.nodes_missing_source_documents, counters.quote_fallbacks) == (1, 0)


async def test_an_id_line_never_becomes_the_primary_span_nor_grounds_a_hub(scripted_gateway: Factory) -> None:
    plan = plan_of(("a", ["See FR-001 for the file format.\nCheckout service handles payment."]))
    requirement = quoted(
        node("c1", "Customers can export orders as CSV", FR, ["a#s0"], explicit="FR-001"),
        ("Customers can export orders as CSV", ["a#s0"]),
    )
    hub = quoted(node("c2", "Checkout service", "Module", ["a#s0"]), ("the service that bills orders", ["a#s0"]))
    graph = await run_conform(plan, [requirement, hub], scripted_gateway())
    nodes = by_name(graph)
    target = nodes["Customers can export orders as CSV"]
    assert target.evidence == (lines("a#s0", 1),)
    assert target.quote is None  # an id line is evidence, not the node's quote
    assert nodes["Checkout service"].evidence == (lines("a#s0", 2),)  # its name line, not the id line
    assert graph.counters.unsnapped_quote_nodes == {"Module": 1, FR: 1}


async def test_without_any_span_a_node_keeps_its_fragments_whole_and_its_provisional_key(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(("a", ["Customers export.", "Other line."]))
    item = quoted(node("c1", "Customers can download invoices", FR, ["a#s0", "a#s1"]), ("invoices as PDF", ["a#s0"]))
    graph = await run_conform(plan, [item], scripted_gateway())
    (target,) = graph.nodes
    assert target.evidence == (lines("a#s0", 1), lines("a#s1", 3))
    assert target.identity_key == item.identity_key  # still provisional: nothing snapped
    counters = graph.counters
    assert (counters.provisional_keys_left, counters.unsnapped_source_quotes) == (1, 1)
    assert (counters.attributed_fragments_dropped, counters.nodes_missing_source_documents) == (0, 0)


async def test_conform_never_reads_a_quote_attribute(scripted_gateway: Factory) -> None:
    plan = plan_of(("doc", ["Customers can export order history as CSV."]))
    graph = await run_conform(
        plan,
        [node("c1", "Customer can export order history as CSV", FR, ["doc#s0"])],
        scripted_gateway(),
        attributes={"c1": {"quote": "Customers can export order history as CSV"}},
        setup=Setup(quotes_from_attributes=False),
    )
    (target,) = graph.nodes
    assert target.quote is None and "quote" not in target.attributes
    assert graph.counters.quote_fallbacks == 1


async def test_grounding_stats_give_conforms_counts_and_read_older_artifacts_by_their_quote_attribute(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        ("a", ["FR-001: Customers can export orders as CSV."]),
        ("b", ["FR-001 lets customers download their orders as a CSV file."]),
    )
    item = node("c1", "Customers can export orders as CSV", FR, ["a#s0", "b#s0"], explicit="FR-001")
    lone = node("c2", "Admins can purge exports", FR, ["a#s0"])
    current = [
        quoted(item, ("Customers can export orders as CSV", ["a#s0"]), ("download their orders", ["b#s0"])),
        quoted(lone, ("Admins can purge exports", ["a#s0"])),
    ]
    graph = await run_conform(plan, current, scripted_gateway())
    stats = grounding_stats(plan, ResolveArtifact(canonical=tuple(current)), EdgesArtifact(), ONT)
    counters = graph.counters
    assert stats == GroundingStats(
        unsnapped_quote_nodes=counters.unsnapped_quote_nodes,
        unsnapped_source_quotes=counters.unsnapped_source_quotes,
        provisional_keys_left=counters.provisional_keys_left,
        attributed_fragments_dropped=counters.attributed_fragments_dropped,
        nodes_missing_source_documents=counters.nodes_missing_source_documents,
    )
    assert stats == GroundingStats({FR: 1}, 1, 1, 0, 0)  # the purge quote is nowhere; FR-001 by its id line
    # An artifact written before per-source quotes (no ``quotes`` field): its one quote attribute over
    # all the node's fragments, no id lines, source quotes n/a.
    older = ResolveArtifact.model_validate(
        {"canonical": [node.model_dump(mode="json", exclude={"quotes"}) for node in (item, lone)]}
    )
    attributes = EdgesArtifact(attributes={"c1": {"quote": "Customers can export orders as CSV"}})
    assert grounding_stats(plan, older, attributes, ONT) == GroundingStats(
        unsnapped_quote_nodes={FR: 1},
        unsnapped_source_quotes=None,
        provisional_keys_left=1,
        attributed_fragments_dropped=1,  # b: the one quote snaps in a only
        nodes_missing_source_documents=1,
    )


async def test_a_fragment_without_its_own_quote_is_grounded_by_the_nodes_other_quotes(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        ("a", ["Customers can export orders as CSV."]),
        ("b", ["Recap: customers can export orders as CSV."]),
        ("c", ["Again: customers can export orders as CSV."]),
    )
    # a: the quote's own fragment; b: a member without a quote; c: a member whose quote snaps nowhere
    item = quoted(
        node("c1", "Customers can export orders as CSV", FR, ["a#s0", "b#s0", "c#s0"]),
        ("Customers can export orders as CSV", ["a#s0"]),
        ("exports are kept for a year", ["c#s0"]),
    )
    graph = await run_conform(plan, [item], scripted_gateway())
    (target,) = graph.nodes
    assert [lines.fragment_id for lines in target.evidence] == ["a#s0", "b#s0", "c#s0"]
    assert (graph.counters.unsnapped_source_quotes, graph.counters.attributed_fragments_dropped) == (1, 0)


async def test_of_two_quotes_on_the_same_lines_the_better_snapped_one_names_the_span(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(("a", ["Customers can export orders. Exports run nightly."]))
    item = quoted(
        node("c1", "Customers can export orders", FR, ["a#s0"]),
        ("Exports run nightly and customers can export", ["a#s0"]),  # 6 of 7 tokens: snaps, not located
        ("Customers can export orders", ["a#s0"]),  # all tokens, located: its sentence is the key
    )
    graph = await run_conform(plan, [item], scripted_gateway())
    (target,) = graph.nodes
    assert target.identity_key == "requirement|span|customers can export orders"


async def test_an_update_keeps_the_span_key_a_persisted_node_holds(scripted_gateway: Factory) -> None:
    plan = plan_of(("a", ["Customers can export orders."]), ("b", ["Customers export every order."]))
    item = quoted(
        node("c1", "Customers can export orders", FR, ["a#s0", "b#s0"]),
        ("Customers can export orders", ["a#s0"]),
        ("Customers export every order", ["b#s0"]),
    )
    first = await run_conform(plan, [item], scripted_gateway())
    assert [target.identity_key for target in first.nodes] == ["requirement|span|customers can export orders"]
    kept = "requirement|span|customers export every order"
    again = await run_conform(
        plan.model_copy(update={"mode": "update"}),
        [item],
        scripted_gateway(),
        persisted=persisted_identities([(kept, (), FR)]),
    )
    (target,) = again.nodes
    assert target.identity_key == kept
    assert target.quote == "Customers export every order."
    assert [lines.fragment_id for lines in target.evidence] == ["a#s0", "b#s0"]


# T12 (limited): the spine exclude value of merged nodes


@pytest.mark.parametrize(
    ("change_set", "in_scope", "wins"),
    [
        (ChangeSet(new=("a", "b")), "yes", 0),  # first build: every document is new, the first value stays
        (ChangeSet(unchanged=("a",), revised=("b",)), "no", 1),  # b revised: its member's value wins
        (ChangeSet(revised=("a",), unchanged=("b",)), "yes", 0),  # the first member is the fresh one
        (ChangeSet(revised=("a", "b")), "yes", 0),  # both fresh: the first value stays
    ],
)
async def test_merged_members_that_disagree_on_scope_take_the_value_grounded_in_a_dirty_document(
    scripted_gateway: Factory, change_set: ChangeSet, in_scope: str, wins: int
) -> None:
    shipped = shipped_ontology()
    plan = plan_of(
        ("a", ["Customers can export orders as CSV."]), ("b", ["Customers can export orders as CSV."])
    ).model_copy(update={"mode": "first" if change_set.new else "update", "change_set": change_set})
    nodes = [
        node("c1", "Customers can export orders as CSV", FR, ["a#s0"]),
        node("c2", "Customers export orders as CSV", FR, ["b#s0"]),
    ]
    graph = await run_conform(
        plan,
        nodes,
        scripted_gateway(),
        attributes={
            "c1": {"quote": "Customers can export orders as CSV", "in_scope": "yes"},
            "c2": {"quote": "Customers can export orders as CSV", "in_scope": "no"},
        },
        setup=Setup(ontology=shipped),
    )
    (target,) = graph.nodes  # one span key: the two members merge
    assert {statement.document_id for statement in target.statements} == {"a", "b"}
    assert target.attributes["in_scope"] == in_scope
    assert (graph.counters.in_scope_disagreements, graph.counters.in_scope_dirty_wins) == (1, wins)


async def test_a_value_a_fresh_member_set_stays_in_a_later_build_without_fresh_members(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(("a", ["Customers can export orders as CSV."]), ("b", ["Customers can export orders as CSV."]))
    nodes = [
        node("c1", "Customers can export orders as CSV", FR, ["a#s0"]),
        node("c2", "Customers export orders as CSV", FR, ["b#s0"]),
    ]
    attributes: dict[str, dict[str, AttributeValue]] = {
        "c1": {"quote": "Customers can export orders as CSV", "in_scope": "yes"},
        "c2": {"quote": "Customers can export orders as CSV", "in_scope": "no"},
    }
    setup = Setup(ontology=shipped_ontology())

    async def build(
        changes: ChangeSet, persisted: Mapping[str, PersistedIdentity] | None = None, mode: str = "update"
    ) -> TargetGraph:
        update = plan.model_copy(update={"mode": mode, "change_set": changes})
        return await run_conform(
            update, nodes, scripted_gateway(), attributes=attributes, setup=setup, persisted=persisted
        )

    revised = await build(ChangeSet(unchanged=("a",), revised=("b",)))  # b revised: its value wins
    (target,) = revised.nodes
    assert (target.attributes["in_scope"], revised.counters.in_scope_dirty_wins) == ("no", 1)
    stored = persisted_identities([(target.identity_key, (), FR)], {target.identity_key: {"in_scope": "no"}})
    later = await build(ChangeSet(unchanged=("a", "b")), stored)  # no fresh member: the persisted value stays
    assert (later.nodes[0].attributes["in_scope"], later.counters.in_scope_dirty_wins) == ("no", 0)
    assert later.counters.in_scope_disagreements == 1
    unknown = await build(ChangeSet(unchanged=("a", "b")))  # nothing persisted: the first value
    assert unknown.nodes[0].attributes["in_scope"] == "yes"
    rebuilt = await build(ChangeSet(unchanged=("a",), revised=("b",)), mode="rebuild")  # fresh only in updates
    assert (rebuilt.nodes[0].attributes["in_scope"], rebuilt.counters.in_scope_dirty_wins) == ("yes", 0)


# Edge facts


async def test_fact_snaps_to_the_source_line_else_keeps_the_attributed_fragments(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        (
            "doc",
            [
                "The Ledger is a component of the Billing service.\nBilling calls Stripe.",
                "Stripe is external.",
            ],
        )
    )
    nodes = [
        node("c1", "Ledger", "Module", ["doc#s0"]),
        node("c2", "Billing service", "Module", ["doc#s0"]),
        node("c3", "Stripe", "Module", ["doc#s0", "doc#s1"]),
    ]
    graph = await run_conform(
        plan,
        nodes,
        scripted_gateway(),
        edges=[
            edge(
                "PART_OF",
                "c1",
                "c2",
                "Ledger is a component of Billing service",
                ["doc#s0"],
                unit="doc@u0",
            ),
            edge(
                "INTEGRATES_WITH",
                "c2",
                "c3",
                "Billing service uses the Stripe payment API for charges",
                ["doc#s1"],
                unit="doc@u0",
            ),
            edge("INTEGRATES_WITH", "c1", "c3", None, [], unit="doc@u0"),
        ],
    )
    edges = {(item.relation, item.source_key, item.target_key): item for item in graph.edges}
    part_of = edges[("PART_OF", "Module|surface|ledger", "Module|surface|billing service")]
    assert part_of.fact == "The Ledger is a component of the Billing service."
    assert part_of.fragment_ids == ("doc#s0",)
    paraphrase = edges[("INTEGRATES_WITH", "Module|surface|billing service", "Module|surface|stripe")]
    assert paraphrase.fact is None and paraphrase.fragment_ids == ("doc#s1",)
    unattributed = edges[("INTEGRATES_WITH", "Module|surface|ledger", "Module|surface|stripe")]
    assert unattributed.fact is None and unattributed.fragment_ids == (
        "doc#s0",
        "doc#s1",
    )
    assert graph.counters.fact_fallbacks == 2
    assert all(item.basis == "stated" for item in graph.edges)


async def test_raw_edge_naming_an_unknown_node_is_a_conformance_error(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(("doc", ["The Ledger."]))
    with pytest.raises(ConformanceError) as raised:
        await run_conform(
            plan,
            [node("c1", "Ledger", "Module", ["doc#s0"])],
            scripted_gateway(),
            edges=[edge("PART_OF", "c1", "c99", None, ["doc#s0"], unit="doc@u0")],
        )
    assert raised.value.code == "conform_unknown_cid"


async def test_node_without_a_groundable_fragment_is_a_conformance_error(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(("doc", ["The Ledger."]))
    with pytest.raises(ConformanceError) as raised:
        await run_conform(plan, [node("c1", "Ledger", "Module", ["gone#s0"])], scripted_gateway())
    assert raised.value.code == "conform_node_without_fragments"


# Final keys: defining line and span keys


async def test_span_key_is_the_sentence_holding_the_quote(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(("doc", ["Orders are kept. Export completes within 5 s. Retries happen."]))
    graph = await run_conform(
        plan,
        [node("c1", "Order export completes within 5 s", QR, ["doc#s0"])],
        scripted_gateway(),
        attributes={"c1": {"quote": "export completes within 5 s"}},
    )
    assert graph.nodes[0].identity_key == "requirement|span|export completes within 5 s"


async def test_quote_across_two_sentences_keys_both_and_a_reworded_quote_keys_the_lines(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        (
            "doc",
            [
                "Orders are kept. Export completes within 5 s. Retries happen.",
                "Admins purge exports nightly.",
            ],
        )
    )
    graph = await run_conform(
        plan,
        [
            node("c1", "Order export completes within 5 s", QR, ["doc#s0"]),
            node("c2", "Admin can purge exports", FR, ["doc#s1"]),
        ],
        scripted_gateway(),
        attributes={
            "c1": {"quote": "kept. Export completes within 5 s"},
            "c2": {"quote": "nightly exports admins purge"},  # snaps by tokens, not found as text
        },
    )
    keys = {item.name: item.identity_key for item in graph.nodes}
    assert keys["Order export completes within 5 s"] == "requirement|span|orders are kept. export completes within 5 s"
    assert keys["Admin can purge exports"] == "requirement|span|admins purge exports nightly"


@pytest.mark.parametrize(
    ("text", "quote", "persisted"),
    [
        pytest.param(
            "Orders are kept. Export completes within 5 s. Retries happen.",
            "export completes within 5 s",
            "Export completes within 5 s.",
            id="one-sentence-of-three",
        ),
        pytest.param(
            "Orders are kept. Export completes within 5 s. Retries happen.",
            "kept. Export completes within 5 s",
            "Orders are kept. Export completes within 5 s.",
            id="two-sentences",
        ),
        pytest.param(
            "Retries happen. Export completes within 5 s, e.g. for small orders.",
            "export completes within 5 s e.g. for small orders",
            "Export completes within 5 s, e.g. for small orders.",
            id="lower-case-after-a-period-is-no-sentence-end",
        ),
        pytest.param(
            "Export of orders is fast. It completes within 5 s.",
            "export completes within 5 s",
            "Export of orders is fast. It completes within 5 s.",
            id="no-single-sentence-holds-the-quote-whole-line",
        ),
        pytest.param(
            "- Export completes within 5 s.",
            "export completes within 5 s",
            "Export completes within 5 s.",
            id="one-sentence-line",
        ),
        pytest.param(
            "Exports follow the layout per section No. 7. Retries happen.",
            "exports follow the layout per section No. 7",
            "Exports follow the layout per section No. 7.",
            id="no-sentence-end-after-an-abbreviation",
        ),
        pytest.param(
            "Retries happen. Charts follow Fig. 3 and use a cache, e.g. Redis. Admins purge.",
            "charts follow fig 3 and use a cache e.g. Redis",
            "Charts follow Fig. 3 and use a cache, e.g. Redis.",
            id="fig-and-eg-before-a-capital",
        ),
    ],
)
async def test_the_persisted_quote_is_the_sentences_of_its_line_that_hold_the_quote(
    scripted_gateway: Factory, text: str, quote: str, persisted: str
) -> None:
    """evidence stays line-ranged, the persisted ``quote`` is the fewest whole sentences of
    the primary span holding the source quote, verbatim (else the line); the span key is unchanged."""

    graph = await run_conform(
        plan_of(("doc", ["Intro.", text])),
        [node("c1", "Order export completes within 5 s", QR, ["doc#s1"])],
        scripted_gateway(),
        attributes={"c1": {"quote": quote}},
    )
    (target,) = graph.nodes
    assert target.quote == persisted
    assert persisted in text
    assert target.evidence == (lines("doc#s1", 3),)


async def test_id_leading_the_defining_line_becomes_the_key_and_its_family_the_label(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        ("doc", ["FR-CHK-009: Customer can cancel an order."]),
        ("other", ["FR-CHK-009 again."]),
    )
    graph = await run_conform(
        plan,
        [
            node("c1", "Customer can cancel an order", QR, ["doc#s0"]),
            node("c2", "Customer cancels orders", FR, ["other#s0"], explicit="FR-CHK-009"),
        ],
        scripted_gateway(),
        attributes={"c1": {"quote": "Customer can cancel an order"}},
    )
    (target,) = graph.nodes  # equal final keys merge; the non-provisional node survives
    assert target.identity_key == "requirement|id|FR-CHK-009"
    assert target.label == FR
    assert target.explicit_ids == ("FR-CHK-009",)
    assert target.name == "Customer cancels orders"
    assert {item.fragment_id for item in target.evidence} == {"doc#s0", "other#s0"}
    assert graph.counters.span_merges == 1


async def test_alike_nodes_on_one_span_key_merge(scripted_gateway: Factory) -> None:
    plan = plan_of(("doc", ["Customers can export orders as CSV."]))
    graph = await run_conform(
        plan,
        [
            node("c1", "Customer can export orders as CSV", FR, ["doc#s0"]),
            node("c2", "Customer can export the orders as CSV", FR, ["doc#s0"]),
        ],
        scripted_gateway(),
        edges=[edge("RELATES_TO", "c1", "c2", None, ["doc#s0"], unit="doc@u0")],
        attributes={
            "c1": {"quote": "Customers can export orders as CSV"},
            "c2": {"quote": "can export orders as CSV"},
        },
    )
    (target,) = graph.nodes
    assert target.identity_key == "requirement|span|customers can export orders as csv"
    assert graph.counters.span_merges == 1 and graph.counters.span_splits == 0
    assert graph.edges == ()
    assert graph.counters.dropped_edges == {"RELATES_TO": {"self_loop": 1}}
    assert graph.quarantine == ()  # a self-loop is counted only


async def test_names_with_close_vectors_are_alike(scripted_gateway: Factory) -> None:
    gateway = scripted_gateway(synonyms=[(f"{FR}: Customer can export orders", f"{FR}: Shopper downloads purchases")])
    plan = plan_of(("doc", ["Customers can export orders as CSV."]))
    graph = await run_conform(
        plan,
        [
            node("c1", "Customer can export orders", FR, ["doc#s0"]),
            node("c2", "Shopper downloads purchases", FR, ["doc#s0"]),
        ],
        gateway,
        attributes={
            "c1": {"quote": "Customers can export orders as CSV"},
            "c2": {"quote": "Customers can export orders"},
        },
    )
    assert len(graph.nodes) == 1


async def test_a_data_requirement_and_a_functional_requirement_of_one_sentence_stay_two(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        (
            "doc",
            ["Purchase order holds id and total, and the customer can download it."],
        )
    )
    sentence = "requirement|span|purchase order holds id and total, and the customer can download it"
    graph = await run_conform(
        plan,
        [
            node("c1", "Purchase order holds id, total", "DataRequirement", ["doc#s0"]),
            node("c2", "Customer can download purchase order", FR, ["doc#s0"]),
        ],
        scripted_gateway(),
        attributes={
            "c1": {"quote": "Purchase order holds id and total"},
            "c2": {"quote": "the customer can download it"},
        },
    )
    keys = {item.label: item.identity_key for item in graph.nodes}
    assert keys == {
        "DataRequirement": f"{sentence}|purchase order holds id, total",
        FR: f"{sentence}|customer can download purchase order",
    }
    assert graph.counters.span_splits == 2


async def test_unlike_names_split_and_a_clash_of_names_adds_the_label(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(("doc", ["Order export runs nightly and archives are purged."]))
    sentence = "requirement|span|order export runs nightly and archives are purged"
    graph = await run_conform(
        plan,
        [
            node("c1", "Order export", FR, ["doc#s0"]),
            node("c2", "Order export", QR, ["doc#s0"]),
            node("c3", "Admin purges archives", FR, ["doc#s0"]),
        ],
        scripted_gateway(),
        attributes={cid: {"quote": "Order export runs nightly and archives are purged"} for cid in ("c1", "c2", "c3")},
    )
    keys = sorted(item.identity_key for item in graph.nodes)
    assert keys == sorted(
        [
            f"{sentence}|order export",
            f"{sentence}|order export|{QR}",
            f"{sentence}|admin purges archives",
        ]
    )
    assert graph.counters.span_splits == 3


async def test_long_span_values_are_bounded(scripted_gateway: Factory) -> None:
    sentence = "Customer can export " + "very " * 120 + "large files"
    plan = plan_of(("doc", [sentence + "."]))
    graph = await run_conform(
        plan,
        [node("c1", "Customer can export large files", FR, ["doc#s0"])],
        scripted_gateway(),
        attributes={"c1": {"quote": "Customer can export very very very"}},
    )
    value = graph.nodes[0].identity_key.removeprefix("requirement|span|")
    assert value == bounded_key_value(normalise_surface(sentence))
    assert len(value) == 500 + 1 + 12 and value[500] == "#"


# Persisted labels for the keys Conform gives (update builds; Resolve step 6 again)

EXPORT_LINE = "Intro line.\n- Customers can export order history as CSV.\nOther line."
EXPORT_FINAL = "requirement|span|customers can export order history as csv"
EXPORT_PROVISIONAL = "requirement|span|customer can export order history as csv"


async def export_node(
    gateway: ScriptedGateway, persisted: Mapping[str, PersistedIdentity], **update: Any
) -> TargetNode:
    values: dict[str, Any] = {"name": "Customer can export order history as CSV", "label": FR}
    values.update(update)
    graph = await run_conform(
        plan_of(("doc", [EXPORT_LINE])),
        [node("c1", values["name"], values["label"], ["doc#s0"], explicit=values.get("explicit"))],
        gateway,
        attributes={"c1": {"quote": "Customers can export order history as CSV"}},
        persisted=persisted,
    )
    (target,) = graph.nodes
    return target


async def test_the_final_span_key_of_a_persisted_node_keeps_its_label(scripted_gateway: Factory) -> None:
    # only the final key (from the snapped line) names the persisted node, not the provisional one
    persisted = persisted_identities([(EXPORT_FINAL, (), QR)])
    assert EXPORT_PROVISIONAL not in persisted
    target = await export_node(scripted_gateway(), persisted)
    assert (target.identity_key, target.label, target.label_disagreement) == (EXPORT_FINAL, QR, FR)
    assert target.identity_aliases == ()  # the key stays
    # the texts are those of the kept label (Diff compares vectors under the persisted label)
    assert target.name_embedding_text == f"{QR}: Customer can export order history as CSV"
    assert target.embedding_text is not None and target.embedding_text.startswith(f"{QR}: ")


async def test_a_persisted_alias_names_the_node_too(scripted_gateway: Factory) -> None:
    persisted = persisted_identities([("requirement|span|older wording", (EXPORT_FINAL,), QR)])
    target = await export_node(scripted_gateway(), persisted)
    assert (target.identity_key, target.label, target.label_disagreement) == (EXPORT_FINAL, QR, FR)


@pytest.mark.parametrize(
    ("persisted", "label"),
    [
        ([(EXPORT_FINAL, (), FR)], FR),  # the same label: no disagreement
        ([(EXPORT_PROVISIONAL, (), QR)], FR),  # only the provisional key: not the node's key any more
        ([(EXPORT_FINAL, (), "Module")], FR),  # a hub label is never taken by a requirement
        ([(EXPORT_FINAL, (), "Document")], FR),  # not an extracted (here: undeclared) label
    ],
)
async def test_a_persisted_label_is_not_taken_otherwise(
    scripted_gateway: Factory, persisted: list[tuple[str, tuple[str, ...], str]], label: str
) -> None:
    target = await export_node(scripted_gateway(), persisted_identities(persisted))
    assert (target.identity_key, target.label, target.label_disagreement) == (EXPORT_FINAL, label, None)


async def test_an_explicit_id_fixes_the_label_against_a_persisted_one(scripted_gateway: Factory) -> None:
    key = "requirement|id|FR-CHK-001"
    target = await export_node(scripted_gateway(), persisted_identities([(key, (), QR)]), explicit="FR-CHK-001")
    assert (target.identity_key, target.label, target.label_disagreement) == (key, FR, None)


async def test_the_persisted_label_is_kept_before_equal_keys_merge(scripted_gateway: Factory) -> None:
    """Two restatements of one sentence under two labels: the persisted label is given before the
    merge of equal keys, so they are alike (one label) and merge instead of splitting."""

    graph = await run_conform(
        plan_of(("doc", [EXPORT_LINE])),
        [
            node("c1", "Customer can export order history as CSV", FR, ["doc#s0"]),
            node("c2", "Customer can export order history as CSV", QR, ["doc#s0"]),
        ],
        scripted_gateway(),
        attributes={cid: {"quote": "Customers can export order history as CSV"} for cid in ("c1", "c2")},
        persisted=persisted_identities([(EXPORT_FINAL, (), QR)]),
    )
    (target,) = graph.nodes
    assert (target.identity_key, target.label, target.label_disagreement) == (EXPORT_FINAL, QR, FR)
    assert graph.counters.span_merges == 1 and graph.counters.span_splits == 0


async def test_a_split_key_naming_a_persisted_node_keeps_its_label(scripted_gateway: Factory) -> None:
    """Split keys exist only after the merge of equal keys, so the label is kept again there."""

    plan = plan_of(("doc", ["Purchase order holds id and total, and the customer can download it."]))
    sentence = "requirement|span|purchase order holds id and total, and the customer can download it"
    split = f"{sentence}|customer can download purchase order"
    graph = await run_conform(
        plan,
        [
            node("c1", "Purchase order holds id, total", "DataRequirement", ["doc#s0"]),
            node("c2", "Customer can download purchase order", FR, ["doc#s0"]),
        ],
        scripted_gateway(),
        attributes={
            "c1": {"quote": "Purchase order holds id and total"},
            "c2": {"quote": "the customer can download it"},
        },
        persisted=persisted_identities([(split, (), QR)]),
    )
    found = {item.identity_key: (item.label, item.label_disagreement) for item in graph.nodes}
    assert found == {
        f"{sentence}|purchase order holds id, total": ("DataRequirement", None),
        split: (QR, FR),
    }


# 2. Requirement dedup


async def dedup_case(
    gateway: ScriptedGateway,
    first: str,
    second: str,
    *,
    label: str = FR,
    quotes: tuple[str, str] | None = None,
    attributes: Mapping[str, Mapping[str, AttributeValue]] | None = None,
) -> TargetGraph:
    """Two id-less requirements, one per document; each quote is its document's line."""

    quote_a, quote_b = quotes or (first, second)
    plan = plan_of(("a", [f"{quote_a}."]), ("b", [f"{quote_b}."]))
    values: dict[str, dict[str, AttributeValue]] = {
        "c1": {"quote": quote_a},
        "c2": {"quote": quote_b},
    }
    for cid, extra in (attributes or {}).items():
        values[cid].update(extra)
    return await run_conform(
        plan,
        [node("c1", first, label, ["a#s0"]), node("c2", second, label, ["b#s0"])],
        gateway,
        attributes=values,
    )


async def test_near_identical_restatements_merge_directly_into_the_lowest_key(
    scripted_gateway: Factory,
) -> None:
    first, second = (
        "Customer can export order history as CSV",
        "Customer can export the order history as CSV",
    )
    gateway = scripted_gateway(synonyms=[(f"{FR}: {first}", f"{FR}: {second}")])  # no dedup script: no call
    graph = await dedup_case(gateway, first, second, attributes={"c2": {"stmt": "Customers export history."}})
    (target,) = graph.nodes
    keys = sorted(
        [
            f"requirement|span|{normalise_surface(first)}",
            f"requirement|span|{normalise_surface(second)}",
        ]
    )
    assert target.identity_key == keys[0]
    assert target.identity_aliases == (keys[1],)
    assert [statement.document_id for statement in target.statements] == ["a", "b"]
    assert [item.fragment_id for item in target.evidence] == ["a#s0", "b#s0"]
    assert target.quote == f"{first}."  # the survivor's quote stays
    assert target.attributes["stmt"] == "Customers export history."  # missing attributes come from the others
    assert target.embedding_text == f"{FR}: {first}. Customers export history."
    counters = graph.counters
    assert (
        counters.dedup_candidates,
        counters.dedup_merges,
        counters.dedup_adjudications,
    ) == (1, 1, 0)


@pytest.mark.parametrize(
    ("cosine", "direct"),
    [(1.0, True), (0.96, True), (0.94, False)],
)
async def test_direct_merge_needs_cosine_095_and_token_jaccard_07(cosine: float, *, direct: bool) -> None:
    first, second = (
        "Customer can export order history as CSV",
        "Customer can export the order history as CSV",
    )
    a, b = at_cosine(cosine)
    gateway = AngleGateway({f"{FR}: {first}": a, f"{FR}: {second}": b})
    gateway.handle(DEDUP_PROMPT, same(answer=True))
    graph = await dedup_case(gateway, first, second)
    assert len(graph.nodes) == 1
    assert len(gateway.calls_for(DEDUP_PROMPT)) == (0 if direct else 1)
    assert gateway.unscripted == []


@pytest.mark.parametrize(("cosine", "candidate"), [(0.86, True), (0.84, False)])
async def test_dedup_candidates_need_cosine_085(cosine: float, *, candidate: bool) -> None:
    first, second = (
        "Customer can export order history",
        "Shopper downloads purchase list",
    )
    a, b = at_cosine(cosine)
    gateway = AngleGateway({f"{FR}: {first}": a, f"{FR}: {second}": b})
    gateway.handle(DEDUP_PROMPT, same(answer=False))
    # Quotes other than the names: candidate texts of their own, far apart (no rich candidate).
    quotes = ("Customers export the order history from the portal", "Shoppers download a purchase list")
    graph = await dedup_case(gateway, first, second, quotes=quotes)
    assert len(graph.nodes) == 2
    assert graph.counters.dedup_candidates == int(candidate)
    assert graph.counters.dedup_candidates_rich_only == 0
    assert len(gateway.calls_for(DEDUP_PROMPT)) == int(candidate)
    assert gateway.unscripted == []


RICH_QUOTES: Final = ("Invoices are downloadable as PDF files", "Past invoices can be fetched in PDF")


def rich_case_gateway(name_cosine: float, rich_cosine: float, first: str, second: str) -> AngleGateway:
    """Names and candidate texts (``dedup_candidate_text``, quotes ``RICH_QUOTES``) at the given cosines."""

    names = at_cosine(name_cosine)
    rich = at_cosine(rich_cosine)
    texts = [
        dedup_candidate_text(FR, name, "", quote) for name, quote in zip((first, second), RICH_QUOTES, strict=True)
    ]
    return AngleGateway({f"{FR}: {first}": names[0], f"{FR}: {second}": names[1], texts[0]: rich[0], texts[1]: rich[1]})


def test_the_dedup_candidate_text_holds_label_name_stmt_and_a_bounded_new_quote() -> None:
    assert dedup_candidate_text(FR, "Export orders", "Users export orders.", "Users can export orders") == (
        f"{FR}: Export orders. Users export orders. Users can export orders"
    )
    # A quote (or stmt) equal to an earlier part, case and punctuation aside, is left out; so is an empty one.
    assert dedup_candidate_text(FR, "Export orders", "", "export orders.") == f"{FR}: Export orders"
    assert dedup_candidate_text(FR, "Export orders", "Users export", "users  EXPORT") == (
        f"{FR}: Export orders. Users export"
    )
    long = "word " * 100
    assert dedup_candidate_text(FR, "N", "", long) == f"{FR}: N. " + " ".join(long[:300].split())


@pytest.mark.parametrize(
    ("rich_cosine", "candidate"), [(DEDUP_RICH_FLOOR + 0.01, True), (DEDUP_RICH_FLOOR - 0.01, False)]
)
async def test_a_rich_only_candidate_is_found_and_always_judged(rich_cosine: float, *, candidate: bool) -> None:
    """D7: names far apart (0.5) but candidate texts close: a candidate of the rich vectors only, at
    cosine >= ``DEDUP_RICH_FLOOR``; it is never merged directly, the small model decides."""

    first, second = INVOICES
    gateway = rich_case_gateway(0.5, rich_cosine, first, second)
    gateway.handle(DEDUP_PROMPT, same(answer=True))
    graph = await dedup_case(gateway, first, second, quotes=RICH_QUOTES)
    counters = graph.counters
    assert (counters.dedup_candidates, counters.dedup_candidates_rich_only) == (int(candidate), int(candidate))
    assert len(gateway.calls_for(DEDUP_PROMPT)) == counters.dedup_adjudications == int(candidate)
    assert len(graph.nodes) == (1 if candidate else 2)


async def test_a_rich_only_candidate_goes_through_the_guards_and_is_listed_when_refused() -> None:
    first, second = "Invoice export completes within 5 s", "Invoice export completes within 10 s"
    gateway = rich_case_gateway(0.5, 0.9, first, second)  # no dedup script: a call fails the test
    graph = await dedup_case(gateway, first, second, label=FR, quotes=RICH_QUOTES)
    assert len(graph.nodes) == 2
    assert graph.counters.dedup_guard_refusals == graph.counters.dedup_candidates_rich_only == 1
    (refused,) = graph.dedup_refused
    assert (refused.guard, refused.label, round(refused.name_cosine, 3)) == ("numbers", FR, 0.5)
    assert refused.rich_cosine is not None and round(refused.rich_cosine, 3) == 0.9


def span_key(name: str) -> str:
    return f"requirement|span|{normalise_surface(name)}"


async def test_a_guard_refused_pair_is_listed_by_identity_key(scripted_gateway: Factory) -> None:
    """D8: "5 s" / "10 s" is refused by the numbers guard and kept in the artifact, ends by key (lower
    first), with its name and candidate cosines."""

    first, second = "Export completes within 5 s", "Export completes within 10 s"
    gateway = scripted_gateway(synonyms=[(f"{QR}: {first}", f"{QR}: {second}")])  # no dedup call allowed
    graph = await dedup_case(gateway, first, second, label=QR)
    assert len(graph.nodes) == 2
    (refused,) = graph.dedup_refused
    assert (refused.first_key, refused.second_key) == (span_key(second), span_key(first))
    assert (refused.label, refused.guard) == (QR, "numbers")
    # The quote is the name, so the candidate text is the name's: one vector for all four.
    assert (round(refused.name_cosine, 6), round(refused.rich_cosine or 0, 6)) == (1.0, 1.0)
    assert "dedup_refused" in TargetGraph.model_validate_json(graph.model_dump_json()).model_fields_set


@pytest.mark.parametrize(
    ("first", "second", "merged"),
    [
        # Two commitments extraction split from one source line: refused, never asked.
        ("User can export orders", "Admin can delete old orders", False),
        # Near-identical names (the direct-merge rule): the exception, merged directly.
        ("Customer can export order history as CSV", "Customer can export the order history as CSV", True),
    ],
    ids=["siblings-refused", "near-identical-merge"],
)
async def test_statements_of_one_source_sentence_stay_apart_unless_their_names_are_near_identical(
    scripted_gateway: Factory, first: str, second: str, *, merged: bool
) -> None:
    """Statements whose primary spans overlap in one fragment stay apart unless their names are near identical."""

    gateway = scripted_gateway(synonyms=[(f"{FR}: {first}", f"{FR}: {second}")])  # no dedup script: no call
    line = f"{first}; {second}."
    graph = await run_conform(
        plan_of(("doc", [line])),
        [node("c1", first, FR, ["doc#s0"]), node("c2", second, FR, ["doc#s0"])],
        gateway,
        attributes={"c1": {"quote": first}, "c2": {"quote": second}},
    )
    assert len(graph.nodes) == (1 if merged else 2)
    assert gateway.calls_for(DEDUP_PROMPT) == []
    assert [pair.guard for pair in graph.dedup_refused] == ([] if merged else ["same_sentence"])
    assert graph.counters.dedup_guard_refusals == int(not merged)


async def test_an_equal_quote_marks_one_sentence_across_fragments() -> None:
    """One quote in two documents: the span key splits by name (names at 0.88: not alike), then the two
    are a dedup candidate that ``same_sentence`` refuses."""

    quote = "Users export orders and admins delete old orders"
    first, second = "User can export orders", "Admin can delete old orders"
    a, b = at_cosine(0.88)
    gateway = AngleGateway({f"{FR}: {first}": a, f"{FR}: {second}": b})
    graph = await run_conform(
        plan_of(("a", [f"Intro. {quote}."]), ("b", [f"{quote}. Outro."])),
        [node("c1", first, FR, ["a#s0"]), node("c2", second, FR, ["b#s0"])],
        gateway,
        attributes={"c1": {"quote": quote}, "c2": {"quote": quote}},
    )
    assert len(graph.nodes) == 2
    assert graph.counters.span_splits == 2
    assert [pair.guard for pair in graph.dedup_refused] == ["same_sentence"]


async def three_exports(scripted_gateway: Factory, middle: str) -> TargetGraph:
    """ "5 s", ``middle`` and "10 s" quality requirements, one vector, every verdict "same"."""

    names = ["Export completes within 5 s", middle, "Export completes within 10 s"]
    gateway = scripted_gateway(synonyms=[tuple(f"{QR}: {name}" for name in names)])
    gateway.handle(DEDUP_PROMPT, same(answer=True))
    plan = plan_of(*((f"d{index}", [f"{name}."]) for index, name in enumerate(names)))
    return await run_conform(
        plan,
        [node(f"c{index}", name, QR, [f"d{index}#s0"]) for index, name in enumerate(names)],
        gateway,
        attributes={f"c{index}": {"quote": name} for index, name in enumerate(names)},
    )


async def test_both_refusals_of_a_merged_node_name_its_survivor(scripted_gateway: Factory) -> None:
    """ "finishes" ~ "completes within 5 s" merge: both refusals of "10 s" name the survivor, one stays."""

    graph = await three_exports(scripted_gateway, "Export finishes within 5 s")
    keys = {item.identity_key for item in graph.nodes}
    assert [(pair.first_key, pair.second_key, pair.collapsed) for pair in graph.dedup_refused] == [
        (span_key("Export completes within 10 s"), span_key("Export completes within 5 s"), False)
    ]
    assert all(pair.first_key in keys and pair.second_key in keys for pair in graph.dedup_refused)
    assert graph.counters.dedup_guard_refusals == 2


async def test_refused_ends_never_join_through_a_third_statement(scripted_gateway: Factory) -> None:
    """Review MEDIUM 2: "5 s" ~ "quickly" and "quickly" ~ "10 s" are both confirmed, but "5 s" / "10 s" is a
    cannot-link: the second union is refused and counted, so no node holds both values."""

    graph = await three_exports(scripted_gateway, "Export completes quickly")
    assert len(graph.nodes) == 2
    for item in graph.nodes:
        values = {key for key in (item.identity_key, *item.identity_aliases) if "within" in key}
        assert len(values) <= 1, values
    counters = graph.counters
    assert (counters.dedup_merges, counters.dedup_union_refused, counters.dedup_refused_collapsed) == (1, 1, 0)
    (refused,) = graph.dedup_refused
    assert refused.guard == "numbers" and not refused.collapsed
    keys = {item.identity_key for item in graph.nodes}
    assert {refused.first_key, refused.second_key} <= keys


def test_a_refusal_whose_ends_end_in_one_node_is_kept_and_flagged_collapsed() -> None:
    """Review MEDIUM 3: a refused pair whose ends end in one node keeps its own keys, marked collapsed; two
    refusals that now name the same survivors keep the closer one."""

    def refused(first: str, second: str, cosine: float) -> RefusedDedupPair:
        return RefusedDedupPair(first_key=first, second_key=second, label=QR, guard="numbers", name_cosine=cosine)

    owner = {"k|b": "k|a", "k|c": "k|a", "k|e": "k|d"}
    kept = _surviving_refusals(
        [
            refused("k|c", "k|b", 0.9),
            refused("k|b", "k|x", 0.8),
            refused("k|e", "k|x", 0.95),
            refused("k|d", "k|x", 0.7),
        ],
        owner,
    )
    assert [(pair.first_key, pair.second_key, pair.collapsed, pair.name_cosine) for pair in kept] == [
        ("k|a", "k|x", False, 0.8),
        ("k|b", "k|c", True, 0.9),
        ("k|d", "k|x", False, 0.95),
    ]


@pytest.mark.parametrize(("verdict", "nodes"), [(True, 1), (False, 2), (LLMError("malformed"), 2)])
async def test_other_candidates_merge_only_on_the_small_models_word(
    scripted_gateway: Factory, verdict: Any, nodes: int
) -> None:
    first, second = (
        "Customer can download invoices as PDF",
        "Shopper fetches past invoices in PDF format",
    )
    gateway = scripted_gateway(synonyms=[(f"{FR}: {first}", f"{FR}: {second}")])
    gateway.handle(
        DEDUP_PROMPT,
        lambda _: verdict if isinstance(verdict, LLMError) else {"same": verdict, "confidence": "high"},
    )
    graph = await dedup_case(gateway, first, second)
    assert len(graph.nodes) == nodes
    (call,) = gateway.calls_for(DEDUP_PROMPT)
    assert call.call_class == "small"
    user = call.messages[-1]["content"]
    assert user.startswith(f"{UNTRUSTED_OPEN} statements>>>") and user.endswith(UNTRUSTED_CLOSE)
    assert first in user and second in user
    assert first not in call.messages[0]["content"]
    assert graph.counters.dedup_adjudications == 1


INVOICES: Final = ("Customer can download invoices as PDF", "Shopper fetches past invoices in PDF format")


async def remembered_dedup(
    gateway: ScriptedGateway,
    store: MemoryStore,
    *,
    attributes: Mapping[str, Mapping[str, AttributeValue]] | None = None,
) -> TargetGraph:
    """The invoice pair of ``INVOICES`` (one vector: the small model is asked) with a dedup memory in ``store``."""

    first, second = INVOICES
    plan = plan_of(("a", [f"{first}."]), ("b", [f"{second}."]))
    values: dict[str, dict[str, AttributeValue]] = {"c1": {"quote": first}, "c2": {"quote": second}}
    for cid, extra in (attributes or {}).items():
        values[cid].update(extra)
    memory = DedupMemory(store, PROJECT, "small-model")
    return await run_conform(
        plan,
        [node("c1", first, FR, ["a#s0"]), node("c2", second, FR, ["b#s0"])],
        gateway,
        attributes=values,
        setup=Setup(judging=ConformJudging(dedup_memory=memory)),
        store=store,
    )


def invoice_gateway(factory: Factory, handler: Callable[[ScriptedRequest], Any] | None) -> ScriptedGateway:
    gateway = factory(synonyms=[(f"{FR}: {INVOICES[0]}", f"{FR}: {INVOICES[1]}")])
    if handler is not None:
        gateway.handle(DEDUP_PROMPT, handler)
    return gateway


async def test_a_dedup_verdict_is_stored_with_its_evidence_and_reused_while_it_matches(
    scripted_gateway: Factory,
) -> None:
    store = MemoryStore()
    one = await remembered_dedup(invoice_gateway(scripted_gateway, same(answer=True, confidence="medium")), store)
    assert len(one.nodes) == 1
    ((_, payload),) = store.of("decisions").values()
    assert {name: value for name, value in payload.items() if name != "evidence"} == {
        "same": True,
        "confidence": "medium",
    }
    gateway = invoice_gateway(scripted_gateway, None)  # no script: a call fails the test
    two = await remembered_dedup(gateway, store)
    assert len(two.nodes) == 1
    assert gateway.calls_for(DEDUP_PROMPT) == []
    assert (two.counters.dedup_verdicts_reused, two.counters.dedup_verdicts_expired) == (1, 0)


async def test_a_dedup_verdict_on_changed_evidence_expires_and_is_asked_again(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    await remembered_dedup(invoice_gateway(scripted_gateway, same(answer=True)), store)
    gateway = invoice_gateway(scripted_gateway, same(answer=False))
    graph = await remembered_dedup(gateway, store, attributes={"c2": {"stmt": "Shoppers fetch invoices."}})
    assert len(gateway.calls_for(DEDUP_PROMPT)) == 1
    assert len(graph.nodes) == 2
    counters = graph.counters
    assert (counters.dedup_verdicts_expired, counters.dedup_verdicts_reused, counters.dedup_adjudications) == (1, 0, 1)
    ((_, payload),) = store.of("decisions").values()
    assert payload["same"] is False


async def test_an_unsure_dedup_verdict_merges_nothing_and_is_not_stored(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    graph = await remembered_dedup(invoice_gateway(scripted_gateway, same(answer=True, confidence="low")), store)
    assert len(graph.nodes) == 2
    assert (graph.counters.dedup_unsure, graph.counters.dedup_merges) == (1, 0)
    assert store.of("decisions") == {}


async def test_a_dedup_payload_without_evidence_is_a_miss(scripted_gateway: Factory) -> None:
    store = MemoryStore()
    memory = DedupMemory(store, PROJECT, "small-model")
    keys = sorted(f"requirement|span|{normalise_surface(name)}" for name in INVOICES)
    await store.put("decisions", memory.key(*keys), memory.fingerprint, {"same": True})
    gateway = invoice_gateway(scripted_gateway, same(answer=False))
    graph = await remembered_dedup(gateway, store)
    assert len(gateway.calls_for(DEDUP_PROMPT)) == 1
    assert len(graph.nodes) == 2
    assert (graph.counters.dedup_verdicts_reused, graph.counters.dedup_verdicts_expired) == (0, 0)


@pytest.mark.parametrize(
    ("first", "second", "label", "quotes"),
    [
        ("Export completes within 5 s", "Export completes within 10 s", QR, None),
        (
            "Discount: never exceeds 30%",
            "Discount: never exceeds 40%",
            "BusinessRule",
            None,
        ),
        ("System can export orders", "System cannot export orders", FR, None),
        ("System can export orders", "System never exports orders", FR, None),
        (
            "User can export orders",
            "User can export orders",
            FR,
            ("Users must export orders", "Users should export orders"),
        ),
        (
            "Admin can bulk-export orders",
            "Admin can bulk-export orders",
            FR,
            ("Bulk export reuses FR-012", "Bulk export reuses FR-013"),
        ),
    ],
    ids=["numbers", "percent", "negation", "never", "modality", "ids"],
)
async def test_guards_refuse_a_merge(
    scripted_gateway: Factory,
    first: str,
    second: str,
    label: str,
    quotes: tuple[str, str] | None,
) -> None:
    gateway = scripted_gateway(synonyms=[(f"{label}: {first}", f"{label}: {second}")])  # no dedup call allowed
    graph = await dedup_case(gateway, first, second, label=label, quotes=quotes)
    assert len(graph.nodes) == 2
    assert graph.counters.dedup_guard_refusals == 1
    assert graph.counters.dedup_merges == 0


@pytest.mark.parametrize(
    ("first", "second", "label"),
    [
        ("Export completes within 5 s", "Export completes within 5000 ms", QR),
        (
            "Export completes within 5 s",
            "Export of 100 orders completes within 5 s",
            QR,
        ),
        ("Export completes within 5 s", "Export completes within 5 s or 10 s", QR),
        ("Export completes within 5 s", "Export completes no later than 5 s", QR),
        ("System can retry a call", "System can retry a call no more than 3 times", FR),
        (
            "Order total: sum of line totals",
            "Order total: sum of line totals plus 20% tax",
            "BusinessRule",
        ),
    ],
    ids=[
        "equal-units",
        "extra-kind",
        "subset",
        "comparative",
        "bound",
        "one-sided-percent",
    ],
)
async def test_guards_let_compatible_restatements_through(
    scripted_gateway: Factory, first: str, second: str, label: str
) -> None:
    gateway = scripted_gateway(synonyms=[(f"{label}: {first}", f"{label}: {second}")])
    gateway.handle(DEDUP_PROMPT, same(answer=True))
    graph = await dedup_case(gateway, first, second, label=label)
    assert len(graph.nodes) == 1
    assert graph.counters.dedup_guard_refusals == 0


_RELEASE_GATE: Final = (
    "Releases require a pass rate of at least 4 of 5 runs, recall of at least 0.90, faithfulness of at "
    "least 0.85 (sample 20, batches of 5), 100% plan coverage; the judge writes an edge only at 0.7 or more"
)


async def test_a_statement_is_compared_by_its_own_number_not_its_source_sentences(
    scripted_gateway: Factory,
) -> None:
    """A table row "recall | every specification | >=0.90 | Section 19.7" and the
    release sentence holding five statements' numbers state one gate; each name states its own number,
    so the sentence's other numbers never refuse the pair: the judge is asked."""

    first, second = "Every specification has a recall of at least 0.90", "Release needs recall of at least 0.90"
    gateway = scripted_gateway(synonyms=[(f"{QR}: {first}", f"{QR}: {second}")])
    gateway.handle(DEDUP_PROMPT, same(answer=True))
    quotes = ("| Recall | Every specification | >=0.90 | Section 19.7 |", _RELEASE_GATE)
    graph = await dedup_case(gateway, first, second, label=QR, quotes=quotes)
    assert graph.counters.dedup_guard_refusals == 0
    assert len(gateway.calls_for(DEDUP_PROMPT)) == 1
    assert len(graph.nodes) == 1


@pytest.mark.parametrize(
    ("first", "second", "quotes"),
    [
        # siblings of one sentence: each name's own number differs
        (
            "Recall is at least 0.90",
            "Faithfulness is at least 0.85",
            (_RELEASE_GATE, "Gates: recall of at least 0.90 and faithfulness of at least 0.85"),
        ),
        # a name without a number of that kind is compared by its quote's
        ("Export completes within 5 s", "Export completes quickly", ("Export within 5 s", "Export within 10 s")),
        # a kind the name does not state still comes from the quote
        ("Retry a call 3 times", "Retry a call 3 times", ("Retry 3 times within 10 s", "Retry 3 times within 30 s")),
    ],
    ids=["sibling-numbers", "quote-number", "quote-kind"],
)
async def test_own_numbers_still_refuse_different_values(
    scripted_gateway: Factory, first: str, second: str, quotes: tuple[str, str]
) -> None:
    gateway = scripted_gateway(synonyms=[(f"{QR}: {first}", f"{QR}: {second}")])  # no dedup call allowed
    graph = await dedup_case(gateway, first, second, label=QR, quotes=quotes)
    assert graph.counters.dedup_guard_refusals == 1
    assert [refused.guard for refused in graph.dedup_refused] == ["numbers"]
    assert len(graph.nodes) == 2


async def test_a_decision_row_restating_a_rule_is_not_refused_by_its_date(scripted_gateway: Factory) -> None:
    """A decision-log row and the ADR row it cites state one rule; their record dates
    are no values of the rule, so the numbers guard leaves them to the judge."""

    first, second = "System can skip failed fragments and notify", "System can skip failed fragments"
    quotes = (
        "|DEC-016 | 2026-10-02 | Skip failed fragment, notify | Platform Owner | ADR-014|",
        "| ADR-014 | 2026-09-29 | One fragment must not block a lane | Skip failed fragment with notification |",
    )
    gateway = scripted_gateway(synonyms=[(f"{FR}: {first}", f"{FR}: {second}")])
    gateway.handle(DEDUP_PROMPT, same(answer=True))
    graph = await dedup_case(gateway, first, second, quotes=quotes)
    assert graph.counters.dedup_guard_refusals == 0
    assert len(graph.nodes) == 1


async def test_two_different_decision_records_stay_apart_by_their_ids(scripted_gateway: Factory) -> None:
    first, second = "System can run agents in the worker", "System can run agents through the SDK"
    quotes = (
        "|DEC-004 | 2026-09-10 | Walking skeleton first | Platform Owner | none|",
        "|DEC-023 | 2026-10-07 | Agents SDK via Bifrost | Engineering Lead | ADR-005|",
    )
    gateway = scripted_gateway(synonyms=[(f"{FR}: {first}", f"{FR}: {second}")])  # no dedup call allowed
    graph = await dedup_case(gateway, first, second, quotes=quotes)
    assert [refused.guard for refused in graph.dedup_refused] == ["ids"]
    assert len(graph.nodes) == 2


async def test_dedup_leaves_explicit_ids_and_other_labels_alone(
    scripted_gateway: Factory,
) -> None:
    name = "Customer can export order history as CSV"
    gateway = scripted_gateway(synonyms=[(f"{FR}: {name}", f"{QR}: {name}", f"{FR}: Customer exports history")])
    plan = plan_of(
        ("a", [f"{name}."]),
        ("b", [f"{name} quickly."]),
        ("c", ["FR-12: Customer exports history."]),
    )
    graph = await run_conform(
        plan,
        [
            node("c1", name, FR, ["a#s0"]),
            node("c2", name, QR, ["b#s0"]),
            node("c3", "Customer exports history", FR, ["c#s0"], explicit="FR-12"),
        ],
        gateway,
        attributes={"c1": {"quote": name}, "c2": {"quote": f"{name} quickly"}},
    )
    assert len(graph.nodes) == 3
    assert graph.counters.dedup_candidates == 0


async def test_dedup_clusters_hold_at_most_twelve_nodes(
    scripted_gateway: Factory,
) -> None:
    words = ["red", "blue", "green", "black", "white", "pink", "gold", "grey", "brown", "teal", "navy", "plum", "lime"]
    names = [f"Customer can export order history as CSV {word}" for word in words]
    gateway = scripted_gateway(synonyms=[tuple(f"{FR}: {name}" for name in names)])
    plan = plan_of(("doc", [f"{name}." for name in names]))
    graph = await run_conform(
        plan,
        [node(f"c{index:02d}", name, FR, [f"doc#s{index}"]) for index, name in enumerate(names)],
        gateway,
        attributes={f"c{index:02d}": {"quote": name} for index, name in enumerate(names)},
    )
    assert len(graph.nodes) >= 2
    assert sum(1 + len(item.identity_aliases) for item in graph.nodes) == len(names)
    assert max(1 + len(item.identity_aliases) for item in graph.nodes) <= 12
    assert graph.counters.dedup_guard_refusals >= 1


async def test_a_statement_without_modality_never_joins_must_and_should_into_one_cluster(
    scripted_gateway: Factory,
) -> None:
    """must ~ none and none ~ should both pass the pair guard; the cluster guard stops the chain."""

    name = "User can export orders"
    quotes = {"a": "Users must export orders", "b": "Users export orders", "c": "Users should export orders"}
    gateway = scripted_gateway()  # one name: one vector, every pair direct (no dedup call)
    plan = plan_of(*((document, [f"{quote}."]) for document, quote in quotes.items()))
    graph = await run_conform(
        plan,
        [node(f"c{document}", name, FR, [f"{document}#s0"]) for document in quotes],
        gateway,
        attributes={f"c{document}": {"quote": quote} for document, quote in quotes.items()},
    )
    clusters = [{statement.document_id for statement in target.statements} for target in graph.nodes]
    assert len(graph.nodes) == 2
    assert all(not {"a", "c"} <= cluster for cluster in clusters)
    assert sorted(map(len, clusters)) == [1, 2]
    counters = graph.counters
    assert (counters.dedup_candidates, counters.dedup_merges, counters.dedup_adjudications) == (3, 1, 0)
    assert counters.dedup_guard_refusals == 2  # the must/should pair, then the chain through "none"


# 3. Implied edges


async def test_derive_turns_a_module_reference_into_an_implied_part_of(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        (
            "doc",
            ["Customer can export order history as CSV.\nThe Orders service keeps history."],
        )
    )
    graph = await run_conform(
        plan,
        [
            node("c1", "Customer can export order history as CSV", FR, ["doc#s0"]),
            node("c2", "Orders service", "Module", ["doc#s0"]),
        ],
        scripted_gateway(),
        attributes={
            "c1": {
                "quote": "Customer can export order history as CSV",
                "module": "the orders services",
            }
        },
    )
    (implied,) = graph.edges
    assert (implied.relation, implied.basis, implied.fact) == (
        "PART_OF",
        "implied",
        None,
    )
    assert implied.source_key == "requirement|span|customer can export order history as csv"
    assert implied.target_key == "Module|surface|order service"
    assert implied.fragment_ids == ("doc#s0",)
    assert graph.counters.implied_edges == 1
    assert graph.derive_fallback_edges == ()  # resolved by its name, not by the fallback
    assert "module" not in graph.nodes[0].attributes  # store: false


@pytest.mark.parametrize(
    ("reference", "modules", "target"),
    [
        pytest.param("Orders", ["Orders service", "Billing service"], "Orders service", id="starts-with"),
        pytest.param("Orders", ["Legacy orders service", "Billing service"], "Legacy orders service", id="holds"),
        pytest.param("Orders", ["Orders service", "Legacy orders store"], "Orders service", id="start-beats-hold"),
        pytest.param("Orders", ["Orders service", "Orders database"], None, id="two-start-none"),
        pytest.param("Order", ["Reorder service"], None, id="whole-words-only"),
        pytest.param("Workflow orchestration", ["Orders service"], None, id="a-heading-names-nothing"),
        pytest.param("the Orders", ["Orders service", "Billing service"], "Orders service", id="article-dropped"),
        pytest.param("Checkout", ["Checkout service"], None, id="names-a-hub-of-another-label"),
    ],
)
async def test_an_unmatched_reference_falls_back_to_the_one_hub_holding_its_words(
    scripted_gateway: Factory, reference: str, modules: list[str], target: str | None
) -> None:
    """a reference that names no node by its name resolves to the one hub of the rule's target labels whose name starts
    with its words, else holds them as whole words (a leading article dropped); never to one of several, and never when
    it is the exact name of a hub of another label (the feature "Checkout").
    """

    text = "Customer can export order history as CSV.\n" + "\n".join(f"The {name} runs." for name in modules)
    text += "\nCheckout is a feature."
    graph = await run_conform(
        plan_of(("doc", [text])),
        [
            node("c1", "Customer can export order history as CSV", FR, ["doc#s0"]),
            node("f1", "Checkout", "Feature", ["doc#s0"]),
            *(node(f"m{index}", name, "Module", ["doc#s0"]) for index, name in enumerate(modules)),
        ],
        scripted_gateway(),
        attributes={"c1": {"quote": "Customer can export order history as CSV", "module": reference}},
    )
    names = {item.identity_key: item.name for item in graph.nodes}
    assert [(edge.relation, names[edge.target_key]) for edge in graph.edges] == (
        [("PART_OF", target)] if target else []
    )
    assert graph.counters.derive_fallback_matches == (1 if target else 0)
    # The fallback edge is marked by its edge key in the artifact, so a review can sample it.
    assert graph.derive_fallback_edges == tuple(edge.edge_key for edge in graph.edges)


async def test_an_interface_reference_naming_the_provider_reaches_the_providers_interface(
    scripted_gateway: Factory,
) -> None:
    """The reference names the provider ("Stripe"), the interface is "Stripe API": the integration
    requirement is PART_OF that interface, the one interface whose name starts with the reference."""

    shipped = shipped_ontology()
    text = "Checkout service calls Stripe to take card payments.\nThe Stripe API takes card payments."
    integration = node("c1", "Checkout service calls Stripe to take card payments", FR, ["doc#s0"])
    interface = node("c2", "Stripe API", FR, ["doc#s0"])
    graph = await run_conform(
        plan_of(("doc", [text])),
        [
            integration.model_copy(update={"label": "IntegrationRequirement"}),
            interface.model_copy(
                update={
                    "label": "Interface",
                    "identity_key": f"Interface|surface|{hub_surface('Stripe API')}",
                    "provisional": False,
                }
            ),
        ],
        scripted_gateway(),
        attributes={
            "c1": {"quote": "Checkout service calls Stripe to take card payments", "interface": "Stripe"},
            "c2": {"quote": "The Stripe API takes card payments"},
        },
        setup=Setup(ontology=shipped),
    )
    (part_of,) = graph.edges
    assert (part_of.relation, part_of.basis, part_of.target_key) == (
        "PART_OF",
        "implied",
        "Interface|surface|stripe api",
    )


@pytest.mark.parametrize(
    ("requirement_text", "reference", "target", "heading"),
    [
        pytest.param("The API shall return orders as JSON.", "API", None, None, id="api-client-not-named-no-edge"),
        pytest.param(
            "The API client shall show orders as JSON.", "API", "API client", None, id="api-client-named-mentioned-in"
        ),
        pytest.param(
            "The Orders service shall return orders as JSON.", "Orders", "Orders service", None, id="grounded"
        ),
        pytest.param(
            "The SpecFlow API shall return orders as JSON.",
            "API",
            "SpecFlow API",
            None,
            id="unnamed-start-passed-over-for-a-named-hold",
        ),
        pytest.param(
            "The API shall return orders as JSON.",
            "API",
            "API client",
            ("Architecture", "API client"),
            id="named-in-the-heading-path",
        ),
    ],
)
async def test_the_derive_fallback_needs_its_hub_named_in_the_sources_evidence(
    scripted_gateway: Factory,
    requirement_text: str,
    reference: str,
    target: str | None,
    heading: tuple[str, ...] | None,
) -> None:
    """a reference resolved by the starts-with/holds fallback (not by its exact name) reaches the hub only when the hub
    is named in one of the requirement's evidence fragments (it may be grounded elsewhere: the fragment is then a
    ``MENTIONED_IN`` one) or in their heading path.
    """

    texts = [
        requirement_text,
        "The API client runs in the browser.",
        "The Orders service runs.",
        "The SpecFlow API serves requests.",
    ]
    fragments = fragments_of("doc", texts)
    if heading is not None:
        fragments[0] = fragments[0].model_copy(update={"heading_path": heading})
    graph = await run_conform(
        plan_from(fragments),
        [
            node("c1", "API returns orders as JSON", FR, ["doc#s0"]),
            node("m1", "API client", "Module", ["doc#s1"]),
            node("m2", "Orders service", "Module", ["doc#s0", "doc#s2"]),
            node("m3", "SpecFlow API", "Module", ["doc#s3"]),
        ],
        scripted_gateway(),
        attributes={"c1": {"quote": requirement_text.removesuffix("."), "module": reference}},
    )
    names = {item.identity_key: item.name for item in graph.nodes}
    assert [(edge.relation, names[edge.target_key]) for edge in graph.edges] == (
        [("PART_OF", target)] if target else []
    )
    assert graph.counters.derive_fallback_matches == (1 if target else 0)


@pytest.mark.parametrize("named", [True, False])
async def test_stripe_reaches_the_stripe_api_only_when_its_fragment_names_it(
    scripted_gateway: Factory, *, named: bool
) -> None:
    """The integration requirement's reference "Stripe" reaches the interface "Stripe API" (grounded in
    another document) when the requirement's own fragment names "Stripe API"; else no edge."""

    text = "Checkout calls Stripe to take card payments" + (" through the Stripe API." if named else ".")
    interface = node("c2", "Stripe API", FR, ["b#s0"])
    graph = await run_conform(
        plan_of(("a", [text]), ("b", ["The Stripe API takes card payments."])),
        [
            node("c1", "Checkout calls Stripe to take card payments", FR, ["a#s0"]).model_copy(
                update={"label": "IntegrationRequirement"}
            ),
            interface.model_copy(
                update={
                    "label": "Interface",
                    "identity_key": f"Interface|surface|{hub_surface('Stripe API')}",
                    "provisional": False,
                }
            ),
        ],
        scripted_gateway(),
        attributes={
            "c1": {"quote": "Checkout calls Stripe to take card payments", "interface": "Stripe"},
            "c2": {"quote": "The Stripe API takes card payments"},
        },
        setup=Setup(ontology=shipped_ontology()),
    )
    assert [(edge.relation, edge.target_key) for edge in graph.edges] == (
        [("PART_OF", "Interface|surface|stripe api")] if named else []
    )
    assert by_name(graph)["Stripe API"].mentions == ({"a#s0": 1} if named else {})


async def test_a_named_requirement_id_relates_two_requirements_without_merging_them(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        ("a", ["FR-012: Customer can export order history as CSV."]),
        (
            "b",
            ["Bulk export reuses FR-012 for admins.\nThe Orders service implements FR-012."],
        ),
    )
    graph = await run_conform(
        plan,
        [
            node(
                "c1",
                "Customer can export order history as CSV",
                FR,
                ["a#s0"],
                explicit="FR-012",
            ),
            node("c2", "Admin can bulk-export orders", FR, ["b#s0"]),
            node("c3", "Orders service", "Module", ["b#s0"]),
        ],
        scripted_gateway(),
        attributes={
            "c1": {"quote": "FR-012: Customer can export order history as CSV"},
            "c2": {"quote": "Bulk export reuses FR-012 for admins"},
            "c3": {"quote": "The Orders service implements FR-012"},
        },
    )
    assert len(graph.nodes) == 3
    (related,) = graph.edges  # none from the module, none from FR-012 to itself
    assert (related.relation, related.basis) == ("RELATES_TO", "implied")
    assert related.source_key == "requirement|id|FR-012"
    assert related.target_key == "requirement|span|bulk export reuses fr-012 for admins"
    assert related.fact == "Bulk export reuses FR-012 for admins."
    assert related.fragment_ids == ("b#s0",)


async def test_an_id_in_stmt_relates_too_and_a_stated_edge_beats_an_implied_one(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        ("a", ["FR-012: Customer can export order history."]),
        ("b", ["Admins export in bulk."]),
    )
    nodes = [
        node("c1", "Customer can export order history", FR, ["a#s0"], explicit="FR-012"),
        node("c2", "Admin can bulk-export orders", FR, ["b#s0"]),
    ]
    attributes = {
        "c2": {
            "quote": "Admins export in bulk",
            "stmt": "Admins reuse FR-012 to export in bulk.",
        }
    }
    graph = await run_conform(plan, nodes, scripted_gateway(), attributes=attributes)
    (implied,) = graph.edges
    assert (implied.basis, implied.fact) == ("implied", None)

    stated = edge("RELATES_TO", "c2", "c1", "Admins export in bulk", ["b#s0"], unit="b@u0")
    graph = await run_conform(plan, nodes, scripted_gateway(), attributes=attributes, edges=[stated])
    (merged,) = graph.edges
    assert (merged.basis, merged.fact) == ("stated", "Admins export in bulk.")
    assert merged.source_key == "requirement|id|FR-012"


# 4. Legalisation and edge keys


LEGALISE_LINES = [
    "Customer can export order history as CSV.",
    "The Orders service holds the export.",
    "The Ledger is a component of the Billing service.",
    "Billing service and Ledger exchange totals.",
    "Authentication and Audit logging overlap.",
]


def legalise_scenario() -> tuple[
    PlanArtifact,
    list[CanonicalNode],
    list[RawEdge],
    dict[str, dict[str, AttributeValue]],
]:
    plan = plan_of(("doc", ["\n".join(LEGALISE_LINES)]))
    nodes = [
        node("c1", "Customer can export order history as CSV", FR, ["doc#s0"]),
        node("c2", "Orders service", "Module", ["doc#s0"]),
        node("c3", "Ledger", "Module", ["doc#s0"]),
        node("c4", "Billing service", "Module", ["doc#s0"]),
        node("c5", "Authentication", "Capability", ["doc#s0"]),
        node("c6", "Audit logging", "Capability", ["doc#s0"]),
    ]
    raw = [
        edge("PART_OF", "c2", "c1", LEGALISE_LINES[1], ["doc#s0"], unit="doc@u0"),  # reversed: flipped
        edge("GROUPED_BY", "c3", "c4", LEGALISE_LINES[3], ["doc#s0"], unit="doc@u0"),  # module pair default
        edge("PART_OF", "c3", "c4", LEGALISE_LINES[2], ["doc#s0"], unit="doc@u0"),  # cue present: kept
        edge("PART_OF", "c4", "c3", LEGALISE_LINES[3], ["doc#s0"], unit="doc@u0"),  # no cue: default
        edge("PART_OF", "c5", "c6", LEGALISE_LINES[4], ["doc#s0"], unit="doc@u0"),  # illegal: dropped
        edge("LIKES", "c1", "c2", LEGALISE_LINES[1], ["doc#s0"], unit="doc@u0"),  # unknown: dropped
    ]
    attributes: dict[str, dict[str, AttributeValue]] = {"c1": {"quote": LEGALISE_LINES[0]}}
    return plan, nodes, raw, attributes


async def test_legalise_flips_defaults_honours_cues_and_counts_drops(
    scripted_gateway: Factory,
) -> None:
    plan, nodes, raw, attributes = legalise_scenario()
    graph = await run_conform(plan, nodes, scripted_gateway(), edges=raw, attributes=attributes)
    fr = "requirement|span|customer can export order history as csv"
    got = {(item.source_key, item.relation, item.target_key) for item in graph.edges}
    assert got == {
        (fr, "PART_OF", "Module|surface|order service"),
        ("Module|surface|ledger", "INTEGRATES_WITH", "Module|surface|billing service"),
        ("Module|surface|ledger", "PART_OF", "Module|surface|billing service"),
        ("Module|surface|billing service", "INTEGRATES_WITH", "Module|surface|ledger"),
    }
    assert graph.counters.flipped_edges == 1
    assert graph.counters.dropped_edges == {
        "LIKES": {"unknown_relation": 1},
        "PART_OF": {"illegal_pair": 1},
    }
    assert all(item.edge_key == f"{item.source_key}|{item.relation}|{item.target_key}" for item in graph.edges)
    # Each drop is quarantined with the extracted relation and direction, ordered by reason.
    assert graph.quarantine == (
        quarantined("illegal_pair", "PART_OF", ("Capability", "authentication"), ("Capability", "audit logging")),
        quarantined("unknown_relation", "LIKES", (FR, fr), ("Module", "order service")),
    )


def quarantined(
    reason: str, relation: str, source: tuple[str, str], target: tuple[str, str], fragments: Sequence[str] = ("doc#s0",)
) -> QuarantinedTargetEdge:
    """A stated quarantine entry; an end is ``(label, key)``, a hub's key given by its surface."""

    def key(label: str, value: str) -> str:
        return value if "|" in value else f"{label}|surface|{value}"

    return QuarantinedTargetEdge.model_validate(
        {
            "reason": reason,
            "relation": relation,
            "source_label": source[0],
            "target_label": target[0],
            "basis": "stated",
            "fragment_ids": tuple(fragments),
            "source_key": key(*source),
            "target_key": key(*target),
        }
    )


def without_module_default(directory: Path) -> Ontology:
    """The frozen ontology without its module -> module default (INTEGRATES_WITH): a module PART_OF a
    module then needs the relation's cue, else it is dropped ``cue_missing``."""

    text = FROZEN_ONTOLOGY_FILE.read_text(encoding="utf-8")
    default = "- {from: module, to: module, default: true}"
    assert text.count(default) == 1
    path = directory / FROZEN_ONTOLOGY_FILE.name
    path.write_text(text.replace(default, "- {from: module, to: module}"), encoding="utf-8")
    copy_frozen_prompts(directory)
    return load_ontology(path, submodule_files=[])


async def test_a_cue_required_pair_without_its_cue_is_quarantined_cue_missing(
    scripted_gateway: Factory, tmp_path: Path
) -> None:
    plan, nodes, raw, attributes = legalise_scenario()
    graph = await run_conform(
        plan,
        nodes,
        scripted_gateway(),
        edges=raw,
        attributes=attributes,
        setup=Setup(ontology=without_module_default(tmp_path)),
    )
    ledger, billing = ("Module", "ledger"), ("Module", "billing service")
    assert graph.counters.dropped_edges == {
        "GROUPED_BY": {"illegal_pair": 1},
        "LIKES": {"unknown_relation": 1},
        "PART_OF": {"cue_missing": 1, "illegal_pair": 1},
    }
    assert [(entry.reason, entry.relation) for entry in graph.quarantine] == [
        ("cue_missing", "PART_OF"),
        ("illegal_pair", "PART_OF"),  # by reason, then source key
        ("illegal_pair", "GROUPED_BY"),
        ("unknown_relation", "LIKES"),
    ]
    assert graph.quarantine[0] == quarantined("cue_missing", "PART_OF", billing, ledger)
    assert graph.quarantine[2] == quarantined("illegal_pair", "GROUPED_BY", ledger, billing)


async def test_repeated_drops_of_one_edge_are_one_quarantine_entry_with_joined_evidence(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(("a", ["Authentication and Audit logging overlap."]), ("b", ["Audit logging and Authentication."]))
    nodes = [
        node("c5", "Authentication", "Capability", ["a#s0", "b#s0"]),
        node("c6", "Audit logging", "Capability", ["a#s0", "b#s0"]),
    ]
    graph = await run_conform(
        plan,
        nodes,
        scripted_gateway(),
        edges=[
            edge("PART_OF", "c5", "c6", None, ["b#s0"], unit="b@u0"),
            edge("PART_OF", "c5", "c6", None, ["a#s0"], unit="a@u0"),
        ],
    )
    assert graph.counters.dropped_edges["PART_OF"] == {"illegal_pair": 2}
    auth, audit = ("Capability", "authentication"), ("Capability", "audit logging")
    assert graph.quarantine == (quarantined("illegal_pair", "PART_OF", auth, audit, ("a#s0", "b#s0")),)


async def test_symmetric_edges_take_canonical_order_and_merge_their_evidence(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        ("a", ["Customer can export order history.\nThe export links to bulk export."]),
        ("b", ["Admins export in bulk.\nBulk export builds on the customer export."]),
    )
    nodes = [
        node("c1", "Customer can export order history", FR, ["a#s0"]),
        node("c2", "Admin can bulk-export orders", FR, ["b#s0"]),
    ]
    graph = await run_conform(
        plan,
        nodes,
        scripted_gateway(),
        edges=[
            edge(
                "RELATES_TO",
                "c2",
                "c1",
                "The export links to bulk export",
                ["a#s0"],
                unit="a@u0",
            ),
            edge(
                "RELATES_TO",
                "c1",
                "c2",
                "Bulk export builds on the customer export",
                ["b#s0"],
                unit="b@u0",
            ),
        ],
    )
    (related,) = graph.edges
    assert related.source_key == "requirement|span|admin can bulk-export orders"
    assert related.source_key < related.target_key
    assert related.fragment_ids == ("a#s0", "b#s0")
    assert related.fact == "The export links to bulk export."  # the first stated fact


@pytest.mark.parametrize(("locality", "kept"), [("grounded", True), ("strict", False)])
async def test_locality(scripted_gateway: Factory, locality: str, *, kept: bool) -> None:
    plan = plan_of(
        (
            "a",
            ["Customer can export order history.\nCustomer export applies to the Orders service."],
        ),
        ("b", ["The Orders service keeps history."]),
    )
    graph = await run_conform(
        plan,
        [
            node("c1", "Customer can export order history", FR, ["a#s0"]),
            node("c2", "Orders service", "Module", ["b#s0"]),
        ],
        scripted_gateway(),
        edges=[
            edge(
                "RELATES_TO",
                "c1",
                "c2",
                "Customer export applies to the Orders service",
                ["a#s0"],
                unit="a@u0",
            )
        ],
        setup=Setup(locality=locality),
    )
    assert len(graph.edges) == int(kept)
    assert graph.counters.dropped_edges == ({} if kept else {"RELATES_TO": {"locality": 1}})
    requirement = (FR, "requirement|span|customer can export order history")
    # RELATES_TO is symmetric: its ends in canonical order, as a kept edge's.
    entry = quarantined("locality", "RELATES_TO", ("Module", "order service"), requirement, ("a#s0",))
    assert graph.quarantine == (() if kept else (entry,))


# 6. Hub grounding, statements and mentions


async def test_hub_is_grounded_in_every_attributed_fragment(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        (
            "doc",
            [
                "The Orders service keeps orders.",
                "Exports run in the orders services cluster.",
                "Nothing relevant here.\nSecond line.",
            ],
        )
    )
    graph = await run_conform(
        plan,
        [node("c1", "Orders service", "Module", ["doc#s0", "doc#s1", "doc#s2"])],
        scripted_gateway(),
        attributes={"c1": {"quote": "The Orders service keeps orders"}},
    )
    (hub,) = graph.nodes
    assert hub.evidence == (
        lines("doc#s0", 1),
        lines("doc#s1", 3),
        lines("doc#s2", 5, 6),
    )
    assert hub.quote == "The Orders service keeps orders."
    # The hub is grounded in doc#s2 without being named there, so that fragment gives no statement (F1).
    assert [statement.fragment_id for statement in hub.statements] == [
        "doc#s0",
        "doc#s1",
    ]
    assert all(statement.at == STAMP and statement.revision_id == "doc-r1" for statement in hub.statements)


async def test_a_hubs_primary_quote_is_a_span_naming_it_before_a_better_ranked_context_line(
    scripted_gateway: Factory,
) -> None:
    """the hub "SpecFlow Web" quoted with a context line of an earlier fragment and with a line naming it: the line
    naming it gives the persisted quote, although the other ranks first.
    """

    plan = plan_of(("doc", ["Membership confers edit rights.", "SpecFlow Web serves the browser screens."]))
    hub = quoted(
        node("c1", "SpecFlow Web", "Module", ["doc#s0", "doc#s1"]),
        ("Membership confers edit rights", ["doc#s0"]),
        ("SpecFlow Web serves the browser screens", ["doc#s1"]),
    )
    graph = await run_conform(plan, [hub], scripted_gateway())
    (target,) = graph.nodes
    assert target.quote == "SpecFlow Web serves the browser screens."
    assert target.evidence == (lines("doc#s0", 1), lines("doc#s1", 3))


async def test_a_hubs_quote_keeps_the_sentence_naming_it(scripted_gateway: Factory) -> None:
    """Trimmed to sentences, a hub's quote keeps the sentence that names it,
    though a shorter run holds the quote ("It stores carts in Redis")."""

    plan = plan_of(("doc", ["Intro.", "Checkout service handles payments. It stores carts in Redis."]))
    graph = await run_conform(
        plan,
        [node("c1", "Checkout service", "Module", ["doc#s1"])],
        scripted_gateway(),
        attributes={"c1": {"quote": "It stores carts in Redis"}},
    )
    (target,) = graph.nodes
    assert target.quote == "Checkout service handles payments. It stores carts in Redis."


async def test_a_dropped_hub_is_left_out_of_the_mentions_counted_once_before_normalisation(
    scripted_gateway: Factory,
) -> None:
    """Hub names are counted once (before normalisation); a hub normalisation drops (a DataEntity
    without a supporting edge) is no target node and has no mentions, the others keep theirs."""

    plan = plan_of(("doc", ["The Orders service runs.\nCart is a record.", "Orders service saves the Cart."]))
    graph = await run_conform(
        plan,
        [node("m1", "Orders service", "Module", ["doc#s0"]), node("d1", "Cart", "DataEntity", ["doc#s0"])],
        scripted_gateway(),
        setup=Setup(ontology=shipped_ontology()),
    )
    assert graph.counters.unsupported_nodes == 1
    assert {item.name: item.mentions for item in graph.nodes} == {"Orders service": {"doc#s1": 1}}


async def test_mentions_are_counted_for_hubs_only_longest_name_first(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(
        (
            "doc",
            [
                "The Orders service and Orders store data.\nCustomer can pay by card.",
                "Ask the Orders service, then the orders. Customer can pay by card.",
            ],
        )
    )
    graph = await run_conform(
        plan,
        [
            node("c1", "Orders service", "Module", ["doc#s0"]),
            node("c2", "Orders", "Module", ["doc#s0"]),
            node("c3", "Customer can pay by card", FR, ["doc#s0"]),
        ],
        scripted_gateway(),
    )
    nodes = by_name(graph)
    assert nodes["Orders service"].mentions == {"doc#s1": 1}
    assert nodes["Orders"].mentions == {"doc#s1": 1}
    assert nodes["Customer can pay by card"].mentions == {}


async def test_hub_states_only_the_fragments_that_name_it(
    scripted_gateway: Factory,
) -> None:
    """F1: a fragment that grounds a hub without naming it (by name or alias, as ``MENTIONED_IN``
    counts) gives it no statement; a fragment naming it gives the line naming it. A requirement
    grounded the same way still states the fragment's head."""

    plan = plan_of(
        (
            "doc",
            [
                "# Storage\nRows are kept for a year.",
                "Overview.\nThe Orders DB holds every order.",
                "Overview.\nAlso: the Order Store is backed up nightly.",
            ],
        )
    )
    graph = await run_conform(
        plan,
        [
            node(
                "c1",
                "Order Store",
                "Module",
                ["doc#s0", "doc#s1", "doc#s2"],
                aliases=(f"Module|surface|{hub_surface('Orders DB')}",),
            ),
            node("c2", "Rows are kept for one year", FR, ["doc#s0"]),
        ],
        scripted_gateway(),
    )
    nodes = by_name(graph)
    hub = nodes["Order Store"]
    assert [lines_.fragment_id for lines_ in hub.evidence] == ["doc#s0", "doc#s1", "doc#s2"]
    assert [(statement.fragment_id, statement.text) for statement in hub.statements] == [
        ("doc#s1", "The Orders DB holds every order."),
        ("doc#s2", "Also: the Order Store is backed up nightly."),
    ]
    requirement = nodes["Rows are kept for one year"]
    assert [statement.text for statement in requirement.statements] == ["# Storage\nRows are kept for a year."]


async def test_hub_statements_of_an_unchanged_corpus_are_the_same_and_cost_no_call(
    scripted_gateway: Factory,
) -> None:
    """M3 churn rule: Conform again over the same plan and answers (a no-change update) gives the same
    hub statements and search texts and makes no call, so Diff finds nothing to change."""

    plan = plan_of(
        (
            "doc",
            ["# Storage\nRows are kept for a year.", "The Order Store holds every order."],
        )
    )
    nodes = [node("c1", "Order Store", "Module", ["doc#s0", "doc#s1"])]
    store = MemoryStore()
    first = await run_conform(plan, nodes, scripted_gateway(), store=store)
    gateway = scripted_gateway()
    again = await run_conform(plan, nodes, gateway, store=store)
    assert again == first
    assert [statement.fragment_id for statement in again.nodes[0].statements] == ["doc#s1"]
    assert gateway.calls == []


async def test_hub_named_only_by_a_persisted_alias_states_the_line_naming_it(
    scripted_gateway: Factory,
) -> None:
    """F1: a name the hub continues (a persisted ``surface`` alias, as ``MENTIONED_IN`` reads it) makes
    a fragment name it; the statement is the first line naming it, never the fragment's head."""

    key = f"Module|surface|{hub_surface('Order Store')}"
    plan = plan_of(("doc", ["# Data\nNightly jobs run.\nThe Sales Ledger is replicated."]))
    graph = await run_conform(
        plan,
        [node("c1", "Order Store", "Module", ["doc#s0"])],
        scripted_gateway(),
        persisted=persisted_identities([(key, (f"Module|surface|{hub_surface('Sales Ledger')}",), "Module")]),
    )
    (hub,) = graph.nodes
    assert [statement.text for statement in hub.statements] == ["The Sales Ledger is replicated."]


async def test_hub_named_nowhere_has_no_statement(scripted_gateway: Factory) -> None:
    """F1: a hub whose name and aliases occur in no fragment, even where its quote snaps, has no
    statement; its search text is its label and name with its ``stmt``, as for any node."""

    plan = plan_of(
        (
            "doc",
            [
                "Rates are stored in a PostgreSQL database.",
                "# Backend\nThe API reads rates.",
            ],
        )
    )
    graph = await run_conform(
        plan,
        [node("c1", "Rate Database", "Module", ["doc#s0", "doc#s1"])],
        scripted_gateway(),
        attributes={"c1": {"quote": "stored in a PostgreSQL database"}},
    )
    (hub,) = graph.nodes
    assert hub.quote == "Rates are stored in a PostgreSQL database."
    assert [lines_.fragment_id for lines_ in hub.evidence] == ["doc#s0", "doc#s1"]
    assert hub.statements == ()
    assert hub.mentions == {}
    assert hub.embedding_text == "Module: Rate Database"


# 7. Invariants


@pytest.fixture
async def valid(scripted_gateway: Factory) -> tuple[TargetGraph, PlanArtifact]:
    plan = plan_of(
        (
            "a",
            ["FR-012: Customer can export order history.\nThe Orders service keeps history."],
        ),
        ("b", ["Admins export in bulk, reusing FR-012."]),
    )
    graph = await run_conform(
        plan,
        [
            node(
                "c1",
                "Customer can export order history",
                FR,
                ["a#s0"],
                explicit="FR-012",
            ),
            node("c2", "Admin can bulk-export orders", FR, ["b#s0"]),
            node("c3", "Orders service", "Module", ["a#s0"]),
        ],
        scripted_gateway(),
        edges=[
            edge(
                "PART_OF",
                "c1",
                "c3",
                "The Orders service keeps history",
                ["a#s0"],
                unit="a@u0",
            )
        ],
        attributes={"c2": {"quote": "Admins export in bulk, reusing FR-012"}},
    )
    check_invariants(graph, plan, ONT)
    assert {item.relation for item in graph.edges} == {"PART_OF", "RELATES_TO"}
    return graph, plan


def _edge(graph: TargetGraph, relation: str) -> int:
    return next(index for index, item in enumerate(graph.edges) if item.relation == relation)


def _replace(items: tuple[Any, ...], index: int, **update: Any) -> tuple[Any, ...]:
    return (*items[:index], items[index].model_copy(update=update), *items[index + 1 :])


def _tamper_nodes(nodes: tuple[Any, ...], case: str) -> tuple[Any, ...]:
    if case == "unknown_label":
        return _replace(nodes, 0, label="Bogus")
    if case == "duplicate_node":
        return (*nodes, nodes[0])
    if case == "alias_is_a_key":
        return _replace(nodes, 0, identity_aliases=(nodes[1].identity_key,))
    if case == "evidence_outside":
        return _replace(
            nodes,
            0,
            evidence=(EvidenceLines(fragment_id="a#s0", line_start=1, line_end=40),),
        )
    if case == "no_evidence":
        return _replace(nodes, 0, evidence=())
    return nodes


def _tamper_edges(graph: TargetGraph, case: str) -> tuple[Any, ...]:
    edges = graph.edges
    part_of, related = _edge(graph, "PART_OF"), _edge(graph, "RELATES_TO")
    reversed_ends = {"illegal_pair": ("PART_OF", part_of), "symmetric_order": ("RELATES_TO", related)}
    if case in reversed_ends:
        relation, index = reversed_ends[case]
        item = edges[index]
        return _replace(
            edges,
            index,
            source_key=item.target_key,
            target_key=item.source_key,
            edge_key=f"{item.target_key}|{relation}|{item.source_key}",
        )
    if case == "edge_key":
        return _replace(edges, part_of, edge_key="x|PART_OF|y")
    if case == "unknown_endpoint":
        return _replace(
            edges,
            part_of,
            target_key="Module|surface|nope",
            edge_key=f"{edges[part_of].source_key}|PART_OF|Module|surface|nope",
        )
    if case == "duplicate_edge":
        return (*edges, edges[0])
    fragments = {"edge_unknown_fragment": ("nowhere#s0",), "edge_without_fragments": ()}
    if case in fragments:
        return _replace(edges, part_of, fragment_ids=fragments[case])
    return edges


def tamper(graph: TargetGraph, plan: PlanArtifact, case: str) -> tuple[TargetGraph, PlanArtifact]:
    nodes, edges = _tamper_nodes(graph.nodes, case), _tamper_edges(graph, case)
    if case == "retired_fragment":
        plan = plan.model_copy(
            update={
                "fragments": tuple(
                    f.model_copy(update={"status": "retired"}) if f.fragment_id == "b#s0" else f for f in plan.fragments
                )
            }
        )
    return graph.model_copy(update={"nodes": nodes, "edges": edges}), plan


@pytest.mark.parametrize(
    ("case", "invariant"),
    [
        ("unknown_label", "D1"),
        ("illegal_pair", "D2"),
        ("edge_key", "D2"),
        ("symmetric_order", "D2"),
        ("unknown_endpoint", "D2"),
        ("duplicate_node", "D3"),
        ("alias_is_a_key", "D3"),
        ("duplicate_edge", "D3"),
        ("evidence_outside", "D4"),
        ("no_evidence", "D4"),
        ("edge_unknown_fragment", "D4"),
        ("edge_without_fragments", "D4"),
        ("retired_fragment", "D4"),
    ],
)
def test_tampered_graphs_violate_invariants(valid: tuple[TargetGraph, PlanArtifact], case: str, invariant: str) -> None:
    graph, plan = tamper(*valid, case)
    with pytest.raises(ConformanceError) as raised:
        check_invariants(graph, plan, ONT)
    assert raised.value.code == "conform_invariant_violated"
    assert raised.value.fields["invariant"] == invariant


# Determinism


async def test_conform_is_deterministic(scripted_gateway: Factory) -> None:
    async def once() -> str:
        plan, nodes, raw, attributes = legalise_scenario()
        return (await run_conform(plan, nodes, scripted_gateway(), edges=raw, attributes=attributes)).model_dump_json()

    first, second = await once(), await once()
    assert first == second
    graph = TargetGraph.model_validate_json(first)
    assert [item.identity_key for item in graph.nodes] == sorted(item.identity_key for item in graph.nodes)
    assert [item.edge_key for item in graph.edges] == sorted(item.edge_key for item in graph.edges)


def test_dedup_ids_guard_refuses_only_conflicting_reference_sets() -> None:
    """A referenced id on one side only does not refuse a merge; disjoint reference sets do."""

    def signature(references: frozenset[str]) -> _Signature:
        return _Signature(numbers={}, negated=False, modal=frozenset(), references=references)

    assert _refusal(signature(frozenset({"FR-012"})), signature(frozenset())) is None
    assert _refusal(signature(frozenset({"FR-012"})), signature(frozenset({"FR-012", "FR-013"}))) is None
    assert _refusal(signature(frozenset({"FR-012"})), signature(frozenset({"FR-013"}))) == "ids"
