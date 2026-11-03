"""The shared validator."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np
import pytest

from graph.build.validator import (
    CHECKS,
    InMemoryGraphView,
    ProposalChecker,
    ViewEdge,
    ViewNode,
    Violation,
    simple_cycles,
    violations,
)
from graph.core.ontology import Ontology
from graph_support import shipped_ontology

FR = "FunctionalRequirement"
NFR = "QualityRequirement"
CON = "Constraint"
BR = "BusinessRule"
INT = "IntegrationRequirement"
DATA = "DataRequirement"
FEAT = "Feature"
MOD = "Module"
CAP = "Capability"
IFC = "Interface"
ENT = "DataEntity"
QST = "OpenQuestion"

PART_OF = "PART_OF"
GROUPED_BY = "GROUPED_BY"
REALIZED_BY = "REALIZED_BY"
INTEGRATES_WITH = "INTEGRATES_WITH"
DEPLOYED_TO = "DEPLOYED_TO"
PROVIDED_BY = "PROVIDED_BY"
CONSUMED_BY = "CONSUMED_BY"
STORED_IN = "STORED_IN"
CARRIES = "CARRIES"
DEPENDS_ON = "DEPENDS_ON"
RELATES_TO = "RELATES_TO"

Edge = tuple[str, str, str] | tuple[str, str, str, str]


@pytest.fixture(scope="module")
def ontology() -> Ontology:
    return shipped_ontology()


def node(node_id: str, label: str, **attributes: Any) -> ViewNode:
    return ViewNode(node_id, label, attributes)


def internal(node_id: str, kind: str = "backend") -> ViewNode:
    return node(node_id, MOD, external="no", type=kind)


def external(node_id: str, kind: str = "other") -> ViewNode:
    return node(node_id, MOD, external="yes", type=kind)


def edge(spec: Edge) -> ViewEdge:
    source, relation, target, *basis = spec
    return ViewEdge(relation, source, target, basis[0] if basis else None)


def view(ontology: Ontology, nodes: Iterable[ViewNode], edges: Iterable[Edge]) -> InMemoryGraphView:
    return InMemoryGraphView(nodes, [edge(spec) for spec in edges], ontology)


def found(ontology: Ontology, nodes: Iterable[ViewNode], edges: Iterable[Edge]) -> list[tuple[str, str, str, str]]:
    return [(v.check, v.source, v.relation, v.target) for v in violations(view(ontology, nodes, edges), ontology)]


# Example graphs checked against the shipped ontology


def e1() -> tuple[list[ViewNode], list[Edge]]:
    nodes = [
        node("feat-checkout", FEAT),
        internal("mod-checkout"),
        internal("mod-web", "frontend"),
        node("fr-review", FR),
        node("fr-pay", FR),
        node("ifc-checkout-api", IFC, kind="api"),
        node("int-place-order", INT),
    ]
    edges: list[Edge] = [
        ("feat-checkout", PART_OF, "mod-checkout"),
        ("fr-review", PART_OF, "feat-checkout"),
        ("fr-review", RELATES_TO, "mod-web"),
        ("fr-pay", PART_OF, "feat-checkout"),
        ("ifc-checkout-api", PROVIDED_BY, "mod-checkout"),
        ("ifc-checkout-api", CONSUMED_BY, "mod-web"),
        ("int-place-order", PART_OF, "ifc-checkout-api"),
        ("mod-web", INTEGRATES_WITH, "mod-checkout", "implied"),
    ]
    return nodes, edges


def e2() -> tuple[list[ViewNode], list[Edge]]:
    nodes = [
        node("ifc-stripe-api", IFC, kind="api"),
        node("ifc-webhook", IFC, kind="api"),
        external("mod-stripe"),
        internal("mod-checkout"),
        node("con-rate", CON),
        node("int-status", INT),
    ]
    edges: list[Edge] = [
        ("ifc-stripe-api", PROVIDED_BY, "mod-stripe"),
        ("ifc-stripe-api", CONSUMED_BY, "mod-checkout"),
        ("con-rate", PART_OF, "mod-stripe"),
        ("ifc-webhook", PROVIDED_BY, "mod-checkout"),
        ("ifc-webhook", CONSUMED_BY, "mod-stripe"),
        ("int-status", PART_OF, "ifc-webhook"),
    ]
    return nodes, edges


def e3() -> tuple[list[ViewNode], list[Edge]]:
    nodes = [
        node("ifc-order-paid", IFC, kind="event"),
        internal("mod-orders"),
        internal("mod-warehouse"),
        internal("mod-email"),
        node("ent-order-paid", ENT),
        node("data-order-paid", DATA),
    ]
    edges: list[Edge] = [
        ("ifc-order-paid", PROVIDED_BY, "mod-orders"),
        ("ifc-order-paid", CONSUMED_BY, "mod-warehouse"),
        ("ifc-order-paid", CONSUMED_BY, "mod-email"),
        ("ifc-order-paid", CARRIES, "ent-order-paid"),
        ("data-order-paid", PART_OF, "ent-order-paid"),
        ("mod-warehouse", INTEGRATES_WITH, "mod-orders", "implied"),
        ("mod-email", INTEGRATES_WITH, "mod-orders", "implied"),
    ]
    return nodes, edges


def e4() -> tuple[list[ViewNode], list[Edge]]:
    nodes = [
        node("ent-purchase-order", ENT),
        internal("mod-orders-db", "datastore"),
        node("data-doc-a", DATA),
        node("data-doc-b", DATA),
        node("fr-cancel", FR),
    ]
    edges: list[Edge] = [
        ("ent-purchase-order", STORED_IN, "mod-orders-db"),
        ("data-doc-a", PART_OF, "ent-purchase-order"),
        ("data-doc-b", PART_OF, "ent-purchase-order"),
        ("fr-cancel", RELATES_TO, "ent-purchase-order"),
    ]
    return nodes, edges


def e5() -> tuple[list[ViewNode], list[Edge]]:
    nodes = [node("int-erp", INT), internal("mod-checkout"), external("mod-erp")]
    edges: list[Edge] = [
        ("int-erp", PART_OF, "mod-checkout"),
        ("int-erp", RELATES_TO, "mod-erp"),
        ("mod-checkout", INTEGRATES_WITH, "mod-erp"),
    ]
    return nodes, edges


def e6() -> tuple[list[ViewNode], list[Edge]]:
    nodes = [
        node("feat-sso", FEAT),
        internal("mod-identity"),
        internal("mod-admin", "frontend"),
        node("cap-auth", CAP),
        node("fr-tokens", FR),
        node("fr-admin", FR),
    ]
    edges: list[Edge] = [
        ("feat-sso", PART_OF, "mod-identity"),
        ("feat-sso", GROUPED_BY, "cap-auth"),
        ("fr-tokens", GROUPED_BY, "cap-auth"),
        ("fr-admin", PART_OF, "mod-admin"),
        ("fr-admin", GROUPED_BY, "cap-auth"),
        ("cap-auth", REALIZED_BY, "mod-identity", "inferred"),
        ("cap-auth", REALIZED_BY, "mod-admin", "inferred"),
    ]
    return nodes, edges


E7_REJECTED: Edge = ("feat-cancellation", DEPENDS_ON, "feat-refunds")


def e7() -> tuple[list[ViewNode], list[Edge]]:
    nodes = [
        node("feat-refunds", FEAT),
        node("feat-cancellation", FEAT),
        node("fr-refund", FR),
        node("fr-cancel", FR),
    ]
    edges: list[Edge] = [
        ("feat-refunds", DEPENDS_ON, "feat-cancellation"),
        ("fr-refund", PART_OF, "feat-refunds"),
        ("fr-cancel", PART_OF, "feat-cancellation"),
        ("fr-refund", DEPENDS_ON, "fr-cancel"),
    ]
    return nodes, edges


def e8() -> tuple[list[ViewNode], list[Edge]]:
    nodes = [node("fr-export", FR), node("feat-export", FEAT), internal("mod-orders"), internal("mod-reporting")]
    edges: list[Edge] = [
        ("fr-export", PART_OF, "feat-export"),
        ("feat-export", PART_OF, "mod-orders"),
        ("fr-export", RELATES_TO, "mod-reporting"),
    ]
    return nodes, edges


def e9() -> tuple[list[ViewNode], list[Edge]]:
    nodes = [
        internal("mod-orders-api"),
        internal("mod-orders-db", "datastore"),
        internal("mod-orders"),
        internal("mod-staging", "deployment"),
        internal("mod-production", "deployment"),
        node("con-eu", CON),
    ]
    edges: list[Edge] = [
        ("mod-orders-api", PART_OF, "mod-orders"),
        ("mod-orders-db", PART_OF, "mod-orders"),
        ("mod-orders", DEPLOYED_TO, "mod-staging"),
        ("mod-orders", DEPLOYED_TO, "mod-production"),
        ("con-eu", PART_OF, "mod-production"),
    ]
    return nodes, edges


def e10() -> tuple[list[ViewNode], list[Edge]]:
    nodes = [
        node("feat-coupons", FEAT, release="Phase 2"),
        internal("mod-checkout"),
        node("br-one-coupon", BR),
        node("fr-stack", FR, in_scope="no"),
        node("feat-gift-cards", FEAT, in_scope="no"),
        node("qst-provider", QST),
    ]
    edges: list[Edge] = [
        ("feat-coupons", PART_OF, "mod-checkout"),
        ("br-one-coupon", PART_OF, "feat-coupons"),
        ("fr-stack", PART_OF, "feat-coupons"),
        ("qst-provider", RELATES_TO, "feat-coupons"),
    ]
    return nodes, edges


EXAMPLES = {"E1": e1, "E2": e2, "E3": e3, "E4": e4, "E5": e5, "E6": e6, "E7": e7, "E8": e8, "E9": e9, "E10": e10}


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_proposal_examples_are_valid(ontology: Ontology, name: str) -> None:
    nodes, edges = EXAMPLES[name]()
    assert found(ontology, nodes, edges) == []


def test_e7_cycle_is_the_one_violation(ontology: Ontology) -> None:
    nodes, edges = e7()
    result = violations(view(ontology, nodes, [*edges, E7_REJECTED]), ontology)
    assert result == [
        Violation(
            check="acyclic",
            relation=DEPENDS_ON,
            source="feat-refunds",
            target="feat-cancellation",
            basis="stated",
            cycle=("feat-cancellation", "feat-refunds"),
        )
    ]


def prefixed(name: str, edge: Edge) -> Edge:
    """``edge`` with both ends prefixed ``<name>:``."""

    if len(edge) == 3:
        return (f"{name}:{edge[0]}", edge[1], f"{name}:{edge[2]}")
    return (f"{name}:{edge[0]}", edge[1], f"{name}:{edge[2]}", edge[3])


def test_all_examples_together_are_valid(ontology: Ontology) -> None:
    nodes: dict[str, ViewNode] = {}
    edges: list[Edge] = []
    for name, build in EXAMPLES.items():
        example_nodes, example_edges = build()
        nodes.update({f"{name}:{n.id}": ViewNode(f"{name}:{n.id}", n.label, n.attributes) for n in example_nodes})
        edges += [prefixed(name, edge) for edge in example_edges]
    assert found(ontology, nodes.values(), edges) == []


# illegal_pair


def test_illegal_pair(ontology: Ontology) -> None:
    nodes = [node("feat-a", FEAT), node("feat-b", FEAT), internal("mod-a"), internal("mod-b"), node("fr", FR)]
    assert found(ontology, nodes, [("feat-a", PART_OF, "mod-a"), ("fr", PART_OF, "feat-a")]) == []
    assert found(
        ontology,
        nodes,
        [
            ("feat-a", PART_OF, "feat-b"),  # removed in v2.0 (T4)
            ("mod-a", RELATES_TO, "mod-b"),  # module <-> module RELATES_TO is not allowed
            ("feat-a", RELATES_TO, "fr"),  # feature RELATES_TO nothing but an open question
        ],
    ) == [
        ("illegal_pair", "feat-a", PART_OF, "feat-b"),
        ("illegal_pair", "feat-a", RELATES_TO, "fr"),
        ("illegal_pair", "mod-a", RELATES_TO, "mod-b"),
    ]


def test_illegal_pair_unknown_relation_or_node(ontology: Ontology) -> None:
    nodes = [node("feat", FEAT), internal("mod")]
    result = violations(view(ontology, nodes, [("feat", "USED_BY", "mod"), ("feat", PART_OF, "gone")]), ontology)
    assert [(v.check, v.relation, v.detail) for v in result] == [
        ("illegal_pair", PART_OF, "unknown_node"),
        ("illegal_pair", "USED_BY", "unknown_relation"),
    ]


# where


def test_where(ontology: Ontology) -> None:
    nodes = [
        node("feat", FEAT),
        node("fr", FR),
        node("con", CON),
        external("mod-ext"),
        internal("mod-deploy", "deployment"),
        node("mod-unknown", MOD),
    ]
    # A constraint may be owned by an external module; a null value without a default holds.
    assert found(ontology, nodes, [("con", PART_OF, "mod-ext"), ("feat", PART_OF, "mod-unknown")]) == []
    result = violations(
        view(ontology, nodes, [("fr", PART_OF, "mod-ext"), ("feat", PART_OF, "mod-deploy")]),
        ontology,
    )
    assert [(v.check, v.source, v.target, v.detail) for v in result] == [
        ("where", "feat", "mod-deploy", "target_where"),
        ("where", "fr", "mod-ext", "target_where"),
    ]


def test_where_on_both_ends(ontology: Ontology) -> None:
    nodes = [internal("mod-deploy-a", "deployment"), internal("mod-deploy-b", "deployment")]
    result = violations(view(ontology, nodes, [("mod-deploy-a", INTEGRATES_WITH, "mod-deploy-b")]), ontology)
    assert [(v.check, v.detail) for v in result] == [("where", "source_where,target_where")]


# owner, D6


def test_owner(ontology: Ontology) -> None:
    nodes = [node("fr", FR), node("feat", FEAT), internal("mod-orders"), internal("mod-reporting")]
    valid: list[Edge] = [("fr", PART_OF, "feat"), ("feat", PART_OF, "mod-orders"), ("fr", RELATES_TO, "mod-reporting")]
    assert found(ontology, nodes, valid) == []
    # E8 before normalisation: the requirement keeps a second and a third owner.
    tampered: list[Edge] = [*valid, ("fr", PART_OF, "mod-reporting"), ("fr", PART_OF, "mod-orders")]
    assert found(ontology, nodes, tampered) == [
        ("owner", "fr", PART_OF, "feat"),
        ("owner", "fr", PART_OF, "mod-orders"),
        ("owner", "fr", PART_OF, "mod-reporting"),
    ]


def test_owner_counts_targets_not_edges(ontology: Ontology) -> None:
    nodes = [node("fr", FR), node("feat", FEAT)]
    assert found(ontology, nodes, [("fr", PART_OF, "feat", "stated"), ("fr", PART_OF, "feat", "inferred")]) == []


# exclusive, D6


def test_exclusive(ontology: Ontology) -> None:
    nodes = [node("ifc", IFC), internal("mod-a"), internal("mod-b"), internal("mod-c")]
    valid: list[Edge] = [("ifc", PROVIDED_BY, "mod-a"), ("ifc", CONSUMED_BY, "mod-b"), ("ifc", CONSUMED_BY, "mod-c")]
    assert found(ontology, nodes, valid) == []
    assert found(ontology, nodes, [*valid, ("ifc", PROVIDED_BY, "mod-b")]) == [
        ("exclusive", "ifc", PROVIDED_BY, "mod-a"),
        ("exclusive", "ifc", PROVIDED_BY, "mod-b"),
    ]
    two_parents: list[Edge] = [("mod-a", PART_OF, "mod-b"), ("mod-a", PART_OF, "mod-c")]
    assert [check for check, *_ in found(ontology, nodes, two_parents)] == ["exclusive", "exclusive"]


# depth


def test_depth(ontology: Ontology) -> None:
    nodes = [internal("mod-a"), internal("mod-b"), internal("mod-c"), internal("mod-d")]
    assert found(ontology, nodes, [("mod-a", PART_OF, "mod-c"), ("mod-b", PART_OF, "mod-c")]) == []
    chain: list[Edge] = [("mod-a", PART_OF, "mod-b"), ("mod-b", PART_OF, "mod-c"), ("mod-c", PART_OF, "mod-d")]
    result = violations(view(ontology, nodes, chain), ontology)
    # The deepest edges are reported: every edge that starts a chain longer than max_depth 1.
    assert [(v.check, v.source, v.target, v.detail) for v in result] == [
        ("depth", "mod-a", "mod-b", "max_depth=1"),
        ("depth", "mod-b", "mod-c", "max_depth=1"),
    ]


def test_depth_counts_only_the_pair(ontology: Ontology) -> None:
    # A requirement owned by a contained module is not a deeper containment.
    nodes = [node("fr", FR), internal("mod-a"), internal("mod-b")]
    assert found(ontology, nodes, [("fr", PART_OF, "mod-a"), ("mod-a", PART_OF, "mod-b")]) == []


# across


def test_across(ontology: Ontology) -> None:
    nodes = [internal("mod-a"), internal("mod-b"), external("mod-ext"), node("mod-unknown", MOD)]
    assert found(ontology, nodes, [("mod-a", PART_OF, "mod-b"), ("mod-unknown", PART_OF, "mod-ext")]) == []
    result = violations(view(ontology, nodes, [("mod-a", PART_OF, "mod-ext")]), ontology)
    assert [(v.check, v.source, v.target, v.detail) for v in result] == [("across", "mod-a", "mod-ext", "external")]


# acyclic, D7


def test_acyclic(ontology: Ontology) -> None:
    nodes = [node(f"feat-{name}", FEAT) for name in "abcd"]
    chain: list[Edge] = [("feat-a", DEPENDS_ON, "feat-b"), ("feat-b", DEPENDS_ON, "feat-c")]
    assert found(ontology, nodes, chain) == []
    result = violations(view(ontology, nodes, [*chain, ("feat-c", DEPENDS_ON, "feat-a")]), ontology)
    assert [(v.check, v.source, v.target, v.cycle) for v in result] == [
        ("acyclic", "feat-c", "feat-a", ("feat-a", "feat-b", "feat-c"))
    ]


def test_acyclic_reports_each_cycle_once(ontology: Ontology) -> None:
    nodes = [node(f"feat-{name}", FEAT) for name in "abc"]
    edges: list[Edge] = [
        ("feat-a", DEPENDS_ON, "feat-b"),
        ("feat-b", DEPENDS_ON, "feat-a"),
        ("feat-a", DEPENDS_ON, "feat-c"),
        ("feat-c", DEPENDS_ON, "feat-b"),
        ("feat-c", DEPENDS_ON, "feat-c"),
    ]
    result = violations(view(ontology, nodes, edges), ontology)
    assert [v.cycle for v in result] == [("feat-a", "feat-b"), ("feat-a", "feat-c", "feat-b"), ("feat-c",)]
    assert [(v.source, v.target) for v in result] == [
        ("feat-b", "feat-a"),
        ("feat-b", "feat-a"),
        ("feat-c", "feat-c"),
    ]


def test_simple_cycles() -> None:
    graph = {"a": {"b"}, "b": {"c", "a"}, "c": {"a", "d"}, "d": {"b"}}
    assert simple_cycles(graph) == [("a", "b"), ("a", "b", "c"), ("b", "c", "d")]
    assert simple_cycles({"a": {"b"}, "b": {"c"}}) == []
    complete = {name: {other for other in "abcd" if other != name} for name in "abcd"}
    assert len(simple_cycles(complete)) == 20
    assert len(simple_cycles(complete, limit=5)) == 5


# stated_only


def test_stated_only(ontology: Ontology) -> None:
    nodes = [node("feat-a", FEAT), node("feat-b", FEAT), node("feat-c", FEAT)]
    valid: list[Edge] = [("feat-a", DEPENDS_ON, "feat-b", "stated"), ("feat-b", DEPENDS_ON, "feat-c")]
    assert found(ontology, nodes, valid) == []
    result = violations(
        view(
            ontology, nodes, [("feat-a", DEPENDS_ON, "feat-b", "inferred"), ("feat-a", DEPENDS_ON, "feat-c", "implied")]
        ),
        ontology,
    )
    assert [(v.check, v.target, v.basis) for v in result] == [
        ("stated_only", "feat-b", "inferred"),
        ("stated_only", "feat-c", "implied"),
    ]


# Normalising rules are not violations


def test_distinct_owner_and_redundancy_are_not_violations(ontology: Ontology) -> None:
    nodes = [node("feat", FEAT), node("fr-a", FR), node("fr-b", FR), node("cap", CAP)]
    edges: list[Edge] = [
        ("fr-a", PART_OF, "feat"),
        ("fr-b", PART_OF, "feat"),
        ("fr-a", DEPENDS_ON, "fr-b"),  # distinct_owner: same owner
        ("fr-a", RELATES_TO, "fr-b"),  # distinct_owner: same owner
        ("feat", GROUPED_BY, "cap"),
        ("fr-a", GROUPED_BY, "cap"),  # redundant_via_owner
    ]
    assert found(ontology, nodes, edges) == []


# Proposals


def test_proposals_are_checked_in_order(ontology: Ontology) -> None:
    nodes = [node("fr", FR), node("feat", FEAT), internal("mod-a"), internal("mod-b")]
    graph = view(ontology, nodes, [])
    proposals = [edge(("fr", PART_OF, "mod-a", "inferred")), edge(("fr", PART_OF, "mod-b", "inferred"))]
    result = violations(graph, ontology, proposals=proposals)
    # The first is accepted; the second gives the source a second owner.
    assert [(v.check, v.target) for v in result] == [("owner", "mod-a"), ("owner", "mod-b")]


def test_proposals_ignore_existing_violations(ontology: Ontology) -> None:
    nodes = [node("feat-a", FEAT), node("feat-b", FEAT), internal("mod-a"), node("fr", FR)]
    graph = view(ontology, nodes, [("feat-a", PART_OF, "feat-b")])
    assert [v.check for v in violations(graph, ontology)] == ["illegal_pair"]
    assert violations(graph, ontology, proposals=[edge(("fr", PART_OF, "feat-a", "inferred"))]) == []


def test_invalid_proposal_is_not_accepted(ontology: Ontology) -> None:
    nodes = [node("feat", FEAT), external("mod-ext"), internal("mod-a")]
    checker = ProposalChecker(view(ontology, nodes, []), ontology)
    # INF-CONT-02: an external candidate fails the validator.
    assert [(v.check, v.detail) for v in checker.offer(edge(("feat", PART_OF, "mod-ext", "inferred")))] == [
        ("where", "target_where")
    ]
    assert checker.offer(edge(("feat", PART_OF, "mod-a", "inferred"))) == []
    assert [v.check for v in checker.check(edge(("feat", PART_OF, "mod-ext", "inferred")))] == [
        "where",
        "owner",
        "owner",
    ]


def test_proposal_check_names_the_edge_it_breaks(ontology: Ontology) -> None:
    nodes = [internal("mod-a"), internal("mod-b"), internal("mod-c")]
    checker = ProposalChecker(view(ontology, nodes, [("mod-a", PART_OF, "mod-b")]), ontology)
    proposal = edge(("mod-b", PART_OF, "mod-c", "inferred"))
    assert [(v.check, v.source, v.target) for v in checker.check(proposal)] == [("depth", "mod-a", "mod-b")]
    # check does not add the edge; accept does.
    closing = edge(("mod-c", PART_OF, "mod-a", "inferred"))
    assert [(v.source, v.target) for v in checker.check(closing)] == [("mod-c", "mod-a")]
    checker.accept(proposal)
    assert [(v.source, v.target) for v in checker.check(closing)] == [("mod-b", "mod-c"), ("mod-c", "mod-a")]


def test_proposal_closing_a_cycle(ontology: Ontology) -> None:
    nodes, edges = e7()
    checker = ProposalChecker(view(ontology, nodes, edges), ontology)
    result = checker.check(edge(E7_REJECTED))
    assert [v.check for v in result] == ["acyclic"]


# The view


def test_in_memory_view(ontology: Ontology) -> None:
    records_nodes: list[Mapping[str, object]] = [
        {"id": "fr", "label": FR},
        {"id": "feat", "label": FEAT, "attributes": {"release": "MVP"}},
        {"id": "mod-a", "label": MOD, "attributes": {"external": "no"}},
        {"id": "mod-b", "label": MOD},
    ]
    records_edges: list[Mapping[str, object]] = [
        {"relation": PART_OF, "source": "fr", "target": "mod-b"},
        {"relation": PART_OF, "source": "fr", "target": "feat", "basis": "inferred"},
        {"relation": PART_OF, "source": "feat", "target": "mod-a", "basis": "stated"},
    ]
    graph = InMemoryGraphView.from_records(records_nodes, records_edges, ontology)
    assert [n.id for n in graph.nodes(MOD)] == ["mod-a", "mod-b"]
    assert [e.target for e in graph.edges(PART_OF)] == ["mod-b", "feat", "mod-a"]
    assert graph.attrs("feat") == {"release": "MVP"}
    assert graph.attrs("gone") == {}
    assert graph.node("gone") is None
    # The lowest owner_rank wins (feature 1, module 2); a node without an owner pair edge has none.
    assert (graph.owner("fr"), graph.owner("feat"), graph.owner("mod-a")) == ("feat", "mod-a", None)
    assert [e.effective_basis for e in graph.edges()] == ["stated", "inferred", "stated"]
    assert [v.check for v in violations(graph, ontology)] == ["owner", "owner"]
    assert violations(graph.with_edges([edge(("feat", PART_OF, "mod-b"))]), ontology)[-1].check == "owner"


# Determinism


def test_deterministic_order(ontology: Ontology) -> None:
    nodes = [
        node("fr", FR),
        node("feat", FEAT),
        node("feat-b", FEAT),
        internal("mod-a"),
        internal("mod-b"),
        external("mod-ext"),
        node("ifc", IFC),
    ]
    edges: list[Edge] = [
        ("fr", PART_OF, "feat"),
        ("fr", PART_OF, "mod-a"),
        ("feat", PART_OF, "mod-ext"),
        ("ifc", PROVIDED_BY, "mod-a"),
        ("ifc", PROVIDED_BY, "mod-b"),
        ("mod-a", PART_OF, "mod-ext"),
        ("feat", DEPENDS_ON, "feat-b", "inferred"),
        ("feat-b", DEPENDS_ON, "feat"),
        ("feat", PART_OF, "feat-b"),
    ]
    expected = violations(view(ontology, nodes, edges), ontology)
    assert {v.check for v in expected} == set(CHECKS) - {"depth"}
    assert [v.sort_key() for v in expected] == sorted(v.sort_key() for v in expected)
    rng = np.random.default_rng(7)
    for _ in range(5):
        nodes = [nodes[index] for index in rng.permutation(len(nodes))]
        edges = [edges[index] for index in rng.permutation(len(edges))]
        assert violations(view(ontology, nodes, edges), ontology) == expected
