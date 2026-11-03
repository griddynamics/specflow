"""Conform, and D6: stated edges own first, so a ``derive`` draft on an owner or exclusive pair yields to a kept stated
edge of its relation; and each raw edge with an end from another unit's catalog part gets one outcome, counted per entry
path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import pytest

from graph.build.conform import _EdgeAcc, _Final, _foreign_outcome, _Legal, conform
from graph.build.models import (
    AttributeValue,
    CanonicalNode,
    CatalogParts,
    EdgesArtifact,
    PlanArtifact,
    RawEdge,
    ResolveArtifact,
    TargetGraph,
)
from graph.build.normalise import Normalised, NormCounters, Quarantined
from graph.build.options import BuildOptions, StepServices
from graph.build.vectors import VectorCache
from graph.core.ontology import NodeView, Ontology, QuarantineReason
from graph_conform_support import canonical, conform_plan
from graph_extraction_store_support import MemoryStore
from graph_support import ScriptedGateway, frozen_ontology, shipped_ontology

pytest_plugins = ["graph_support"]

ONT = shipped_ontology()
FR = "FunctionalRequirement"
Factory = Callable[..., ScriptedGateway]


def plan_of(*documents: tuple[str, Sequence[str]]) -> PlanArtifact:
    return conform_plan(*documents)


def node(cid: str, name: str, label: str, fragments: Sequence[str], quote: str | None = None) -> CanonicalNode:
    return canonical(cid, name, label, fragments, quote=quote, hubs=ONT.hub_labels)


def edge(relation: str, source: str, target: str, fact: str, fragments: Sequence[str], *, unit: str) -> RawEdge:
    return RawEdge(
        relation=relation,
        source_cid=source,
        target_cid=target,
        fact=fact,
        fragment_ids=tuple(fragments),
        unit_id=unit,
    )


async def run(
    plan: PlanArtifact,
    nodes: Sequence[CanonicalNode],
    gateway: ScriptedGateway,
    *,
    edges: Sequence[RawEdge] = (),
    attributes: Mapping[str, Mapping[str, AttributeValue]] | None = None,
    catalog_parts: Mapping[str, CatalogParts] | None = None,
    locality: str = "grounded",
    ontology: Ontology = ONT,
) -> TargetGraph:
    return await conform(
        plan,
        ResolveArtifact(canonical=tuple(sorted(nodes, key=lambda item: item.identity_key))),
        EdgesArtifact(
            raw_edges=tuple(edges),
            attributes={cid: dict(values) for cid, values in (attributes or {}).items()},
            catalog_parts=dict(catalog_parts) if catalog_parts is not None else None,
        ),
        services=StepServices(gateway=gateway, ontology=ontology, options=BuildOptions(locality=locality)),
        vectors=VectorCache(gateway, MemoryStore(), "p-precedence"),
        at="2026-01-01T00:00:00+00:00",
    )


def triples(graph: TargetGraph) -> set[tuple[str, str, str, str]]:
    names = {item.identity_key: item.name for item in graph.nodes}
    return {(names[e.source_key], e.relation, names[e.target_key], e.basis) for e in graph.edges}


# D5: stated edges own first

EXPORT = "Customers can export order history as CSV."
SPEC = (
    "spec",
    [
        f"Order export feature.\n{EXPORT}",
        "The Reporting service and the Billing service run the back office.",
        "The Payments API is provided by the Billing service.",
    ],
)


async def test_a_stated_owner_edge_suppresses_the_derived_owner_edge_of_its_relation(
    scripted_gateway: Factory,
) -> None:
    """A requirement stated PART_OF a feature takes no derived PART_OF its module (both owner pairs)."""

    plan = plan_of(SPEC)
    nodes = [
        node("r", "Customer can export order history as CSV", FR, ["spec#s0"], EXPORT),
        node("f", "Order export", "Feature", ["spec#s0"]),
        node("m", "Reporting service", "Module", ["spec#s1"]),
    ]
    graph = await run(
        plan,
        nodes,
        scripted_gateway(),
        edges=[edge("PART_OF", "r", "f", EXPORT, ["spec#s0"], unit="spec@u0")],
        attributes={"r": {"module": "Reporting service"}},
    )
    assert triples(graph) == {("Customer can export order history as CSV", "PART_OF", "Order export", "stated")}
    assert graph.counters.derive_suppressed_by_edge == 1


async def test_without_a_stated_edge_the_derived_owner_edge_stays(scripted_gateway: Factory) -> None:
    plan = plan_of(SPEC)
    nodes = [
        node("r", "Customer can export order history as CSV", FR, ["spec#s0"], EXPORT),
        node("m", "Reporting service", "Module", ["spec#s1"]),
    ]
    graph = await run(plan, nodes, scripted_gateway(), attributes={"r": {"module": "Reporting service"}})
    assert triples(graph) == {("Customer can export order history as CSV", "PART_OF", "Reporting service", "implied")}
    assert graph.counters.derive_suppressed_by_edge == 0


async def test_a_derived_edge_equal_to_the_stated_one_merges_and_is_not_suppressed(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(SPEC)
    nodes = [
        node("r", "Customer can export order history as CSV", FR, ["spec#s0"], EXPORT),
        node("m", "Reporting service", "Module", ["spec#s1"]),
    ]
    graph = await run(
        plan,
        nodes,
        scripted_gateway(),
        edges=[edge("PART_OF", "r", "m", EXPORT, ["spec#s0"], unit="spec@u0")],
        attributes={"r": {"module": "Reporting service"}},
    )
    assert triples(graph) == {("Customer can export order history as CSV", "PART_OF", "Reporting service", "stated")}
    assert graph.counters.derive_suppressed_by_edge == 0


async def test_a_stated_provider_suppresses_the_provider_attribute_on_the_exclusive_pair(
    scripted_gateway: Factory,
) -> None:
    plan = plan_of(SPEC)
    nodes = [
        node("i", "Payments API", "Interface", ["spec#s2"]),
        node("b", "Billing service", "Module", ["spec#s1", "spec#s2"]),
        node("m", "Reporting service", "Module", ["spec#s1"]),
    ]
    fact = "The Payments API is provided by the Billing service."
    graph = await run(
        plan,
        nodes,
        scripted_gateway(),
        edges=[edge("PROVIDED_BY", "i", "b", fact, ["spec#s2"], unit="spec@u0")],
        attributes={"i": {"provider": "Reporting service"}},
    )
    assert triples(graph) == {("Payments API", "PROVIDED_BY", "Billing service", "stated")}
    assert graph.counters.derive_suppressed_by_edge == 1


async def test_a_derived_edge_on_a_non_exclusive_pair_stays_beside_a_stated_one(
    scripted_gateway: Factory,
) -> None:
    """REALIZED_BY capability -> module is neither owner nor exclusive: the derived edge stays."""

    plan = plan_of(("caps", ["Order handling is realized by the Billing service and the Reporting service."]))
    nodes = [
        node("c", "Order handling", "Capability", ["caps#s0"]),
        node("b", "Billing service", "Module", ["caps#s0"]),
        node("m", "Reporting service", "Module", ["caps#s0"]),
    ]
    fact = "Order handling is realized by the Billing service and the Reporting service."
    graph = await run(
        plan,
        nodes,
        scripted_gateway(),
        edges=[edge("REALIZED_BY", "c", "b", fact, ["caps#s0"], unit="caps@u0")],
        attributes={"c": {"module": "Reporting service"}},
    )
    assert triples(graph) == {
        ("Order handling", "REALIZED_BY", "Billing service", "stated"),
        ("Order handling", "REALIZED_BY", "Reporting service", "implied"),
    }
    assert graph.counters.derive_suppressed_by_edge == 0


async def test_a_stated_owner_edge_strict_locality_refuses_suppresses_nothing(scripted_gateway: Factory) -> None:
    """The stated PART_OF the feature of another document is quarantined ``locality``; the derived
    PART_OF the module of the requirement's own unit stays."""

    plan = plan_of(SPEC, ("other", ["Order export feature."]))
    nodes = [
        node("r", "Customer can export order history as CSV", FR, ["spec#s0"], EXPORT),
        node("f", "Order export", "Feature", ["other#s0"]),
        node("m", "Reporting service", "Module", ["spec#s1"]),
    ]
    graph = await run(
        plan,
        nodes,
        scripted_gateway(),
        edges=[edge("PART_OF", "r", "f", EXPORT, ["spec#s0"], unit="spec@u0")],
        attributes={"r": {"module": "Reporting service"}},
        locality="strict",
    )
    assert triples(graph) == {("Customer can export order history as CSV", "PART_OF", "Reporting service", "implied")}
    assert graph.counters.derive_suppressed_by_edge == 0
    assert [(entry.reason, entry.relation) for entry in graph.quarantine] == [("locality", "PART_OF")]


async def test_exclusive_pairs_suppress_only_on_the_same_pair(scripted_gateway: Factory) -> None:
    """The frozen v1.3 ontology has no owner pairs: requirement -> feature and requirement -> module are
    two exclusive PART_OF pairs, one target each, so a stated feature leaves the derived module."""

    frozen = frozen_ontology()
    plan = plan_of(SPEC)
    nodes = [
        node("r", "Customer can export order history as CSV", FR, ["spec#s0"], EXPORT),
        node("f", "Order export", "Feature", ["spec#s0"]),
        node("m", "Reporting service", "Module", ["spec#s1"]),
    ]
    graph = await run(
        plan,
        nodes,
        scripted_gateway(),
        edges=[edge("PART_OF", "r", "f", EXPORT, ["spec#s0"], unit="spec@u0")],
        attributes={"r": {"module": "Reporting service"}},
        ontology=frozen,
    )
    assert triples(graph) == {
        ("Customer can export order history as CSV", "PART_OF", "Order export", "stated"),
        ("Customer can export order history as CSV", "PART_OF", "Reporting service", "implied"),
    }
    assert graph.counters.derive_suppressed_by_edge == 0


# D6: foreign edges per entry path

ALPHA = "Customers can export orders, part of Alpha export."
BETA = "Customers can export orders, part of Beta export."
FOREIGN = (
    ("a", [f"{ALPHA}\n{BETA}", "Imports run nightly in Alpha export."]),
    ("b", ["Alpha export and Beta export are features.", "Gamma export is a feature."]),
)


def foreign_nodes() -> list[CanonicalNode]:
    return [
        node("r", "Customers can export orders", FR, ["a#s0"], "Customers can export orders"),
        node("alpha", "Alpha export", "Feature", ["b#s0"]),
        node("beta", "Beta export", "Feature", ["b#s0"]),
        node("gamma", "Gamma export", "Feature", ["b#s1"]),
    ]


async def test_foreign_edges_are_counted_per_entry_path_and_outcome(scripted_gateway: Factory) -> None:
    """The kept owner (lowest key wins the conflict) is ``kept``, the loser (no legal fallback relation:
    quarantined without a judge call) ``owner_conflict``, an illegal hub self-edge
    ``quarantined:<reason>``."""

    graph = await run(
        plan_of(*FOREIGN),
        foreign_nodes(),
        scripted_gateway(),
        edges=[
            edge("PART_OF", "r", "alpha", ALPHA, ["a#s0"], unit="a@u0"),
            edge("PART_OF", "r", "beta", BETA, ["a#s0"], unit="a@u0"),
            edge("PART_OF", "gamma", "gamma", "Gamma export is a feature.", ["b#s1"], unit="b@u0"),
        ],
        catalog_parts={
            "a@u0": CatalogParts(mentioned=("alpha",), near=("beta",)),
            "b@u0": CatalogParts(heading=("gamma",)),
        },
    )
    assert ("Customers can export orders", "PART_OF", "Alpha export", "stated") in triples(graph)
    assert graph.counters.foreign_edges == {
        "mentioned": {"kept": 1},
        "heading": {"quarantined:illegal_pair": 1},
        "near": {"owner_conflict": 1},
    }


async def test_a_foreign_edge_strict_locality_refuses_is_quarantined_locality(scripted_gateway: Factory) -> None:
    graph = await run(
        plan_of(*FOREIGN),
        foreign_nodes(),
        scripted_gateway(),
        edges=[edge("PART_OF", "r", "alpha", ALPHA, ["a#s0"], unit="a@u0")],
        catalog_parts={"a@u0": CatalogParts(referenced=("alpha",))},
        locality="strict",
    )
    assert graph.edges == ()
    assert graph.counters.foreign_edges == {"referenced": {"quarantined:locality": 1}}


def test_a_foreign_edge_takes_the_quarantine_reason_of_its_own_relation_only() -> None:
    """Normalisation quarantined DEPENDS_ON r-s (``cycle_broken``) and RELATES_TO r-t
    (``judge_rejected``): a raw RELATES_TO r-s it dropped is ``dropped``, not quarantined for the
    other relation; RELATES_TO r-t is ``quarantined:judge_rejected``."""

    def quarantined(reason: QuarantineReason, relation: str, source: str, target: str) -> Quarantined:
        return Quarantined(reason, relation, source, target, "stated", frozenset())

    views = {key: NodeView(FR, {}) for key in ("r", "s", "t")}
    normalised = Normalised(
        edges=[],
        dropped_nodes=frozenset(),
        quarantine=[
            quarantined("cycle_broken", "DEPENDS_ON", "r", "s"),
            quarantined("judge_rejected", "RELATES_TO", "t", "r"),
        ],
        counted={},
        counters=NormCounters(),
        resolved=True,
    )
    legal = _Legal(
        edges={
            "r|RELATES_TO|s": _EdgeAcc("RELATES_TO", "r", "s", "stated", None, set()),
            "r|RELATES_TO|t": _EdgeAcc("RELATES_TO", "r", "t", "stated", None, set()),
        }
    )
    final = _Final(normalised, views, ONT)
    assert _foreign_outcome("r|RELATES_TO|s", legal, set(), final) == "dropped"
    assert _foreign_outcome("r|RELATES_TO|t", legal, set(), final) == "quarantined:judge_rejected"


async def test_foreign_edges_are_not_measured_without_catalog_parts(scripted_gateway: Factory) -> None:
    """An edges artifact without entry paths (one call per unit) leaves the count None;
    one with parts but no foreign edge counts nothing."""

    edges = [edge("PART_OF", "r", "alpha", ALPHA, ["a#s0"], unit="a@u0")]
    before = await run(plan_of(*FOREIGN), foreign_nodes(), scripted_gateway(), edges=edges)
    assert before.counters.foreign_edges is None
    local = await run(
        plan_of(*FOREIGN),
        foreign_nodes(),
        scripted_gateway(),
        edges=edges,
        catalog_parts={"a@u0": CatalogParts()},
    )
    assert local.counters.foreign_edges == {}


@pytest.mark.parametrize("ontology", [ONT], ids=["shipped"])
def test_the_shipped_ontology_has_the_pairs_these_tests_rely_on(ontology: Ontology) -> None:
    part_of = {(pair.source, pair.target): pair for pair in ontology.pairs("PART_OF")}
    assert part_of[(FR, "Feature")].owner and part_of[(FR, "Module")].owner
    (provided,) = ontology.pairs("PROVIDED_BY")
    assert provided.exclusive
    assert all(not pair.owner and not pair.exclusive for pair in ontology.pairs("REALIZED_BY"))
