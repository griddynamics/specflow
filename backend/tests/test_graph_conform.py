"""Conform unit tests (plan 2.T3; build.md step 5; FR-BLD-08, 09, 18, 19, 20, 24).

Plans, canonical nodes, raw edges and attributes are built by hand. Embeddings are the gateway's
deterministic fakes (texts declared synonyms share a vector, cosine 1); ``AngleGateway`` pins chosen
vectors to reach exact cosines. The small model answers ``dedup_adjudicate.2`` through handlers.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest

from graph.build.conform import DEDUP_PROMPT, _refusal, _Signature, check_invariants, conform
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
    RawEdge,
    ResolveArtifact,
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
from graph_extraction_store_support import MemoryStore
from graph_support import (
    ScriptedGateway,
    ScriptedRequest,
    llm_config,
    shipped_ontology,
)

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

ONT = shipped_ontology()
PROJECT = "p-conform"
STAMP = "2026-01-01T00:00:00+00:00"
FR = "FunctionalRequirement"
QR = "QualityRequirement"
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


async def run_conform(
    plan: PlanArtifact,
    nodes: Sequence[CanonicalNode],
    gateway: ScriptedGateway,
    *,
    edges: Sequence[RawEdge] = (),
    attributes: Mapping[str, Mapping[str, AttributeValue]] | None = None,
    locality: str = "grounded",
    persisted: Mapping[str, PersistedIdentity] | None = None,
    store: MemoryStore | None = None,
) -> TargetGraph:
    return await conform(
        plan,
        ResolveArtifact(canonical=tuple(sorted(nodes, key=lambda item: item.identity_key))),
        EdgesArtifact(
            raw_edges=tuple(edges),
            attributes={cid: dict(values) for cid, values in (attributes or {}).items()},
        ),
        services=StepServices(gateway=gateway, ontology=ONT, options=BuildOptions(locality=locality)),
        vectors=VectorCache(gateway, store or MemoryStore(), PROJECT),
        at=STAMP,
        persisted=persisted,
    )


def by_name(graph: TargetGraph) -> dict[str, TargetNode]:
    return {item.name: item for item in graph.nodes}


def lines(fragment_id: str, start: int, end: int | None = None) -> EvidenceLines:
    return EvidenceLines(
        fragment_id=fragment_id,
        line_start=start,
        line_end=start if end is None else end,
    )


def same(*, answer: bool) -> Callable[[ScriptedRequest], dict[str, bool]]:
    return lambda _: {"same": answer}


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


# 2. Requirement dedup (FR-BLD-18, FR-BLD-24)


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
    graph = await dedup_case(gateway, first, second)
    assert len(graph.nodes) == 2
    assert graph.counters.dedup_candidates == int(candidate)
    assert len(gateway.calls_for(DEDUP_PROMPT)) == int(candidate)
    assert gateway.unscripted == []


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
        lambda _: verdict if isinstance(verdict, LLMError) else {"same": verdict},
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


# 3. Implied edges (derive, id_references; FR-BLD-20)


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
    assert "module" not in graph.nodes[0].attributes  # store: false


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


# 4. Legalisation (FR-BLD-09) and edge keys (FR-BLD-19)


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
        locality=locality,
    )
    assert len(graph.edges) == int(kept)
    assert graph.counters.dropped_edges == ({} if kept else {"RELATES_TO": {"locality": 1}})


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


# 7. Invariants (FR-BLD-08)


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
