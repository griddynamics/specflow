"""Offline tests of ``GraphReader.trace`` (trace.md "Walk", "Library", "Worked example"; FR-READ-11 to 17).

The walk runs over the shop fixture through ``TraceExecutor``, an in-memory executor answering the three
queries a trace reads; ``test_graph_trace_live.py`` runs the worked example against a live store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from graph.core import ConfigurationError, GraphReader, TraceResult
from graph.core.trace import MAX_SPEC_IDS
from graph_support import frozen_ontology, shipped_ontology
from graph_trace_support import TraceExecutor, TraceGraph, load_edited, shipped_data, shop_graph, without_impact

PROJECT = "offline-trace"
SEEDS = ["FEAT::CARD_PAYMENT", "REQ::FR::CARD_PAYMENT"]
OWNER = "MOD::PAYMENTS_SERVICE"

# The tables of trace.md "Worked example": (hops, spec_id, parent, relation, step, via_owner).
BASED_ON_DEPTH_3 = [
    (1, "IFC::STRIPE_API", OWNER, "CONSUMED_BY", "backward", True),
    (1, "IFC::UNSHARED", OWNER, "CONSUMED_BY", "backward", True),
    (2, "MOD::STRIPE", "IFC::STRIPE_API", "PROVIDED_BY", "forward", True),
    (2, "REQ::INT::CALL_STRIPE", "IFC::STRIPE_API", "PART_OF", "scope", True),
    (3, "CON::STRIPE", "MOD::STRIPE", "PART_OF", "backward", True),
]
AFFECTS_DEPTH_2 = [
    (1, "FEAT::CHECKOUT", "FEAT::CARD_PAYMENT", "DEPENDS_ON", "backward", False),
    (1, "MOD::STRIPE", OWNER, "INTEGRATES_WITH", "backward", True),
    (1, "MOD::WEB_SHOP", OWNER, "INTEGRATES_WITH", "backward", True),
    (2, "REQ::FR::CHECKOUT", "FEAT::CHECKOUT", "PART_OF", "scope", False),
    (2, "REQ::Q::CHECKOUT_SPEED", "FEAT::CHECKOUT", "PART_OF", "scope", False),
]


def rows(result: TraceResult) -> list[tuple[int, str, str, str, str, bool]]:
    return [(n.hops, n.spec_id, n.parent, n.relation, n.step, n.via_owner) for n in result.nodes]


def reader(graph: TraceGraph | None = None, ontology: Any = None) -> tuple[GraphReader, TraceExecutor]:
    executor = TraceExecutor(graph or shop_graph())
    return GraphReader(executor, ontology or shipped_ontology()), executor


async def trace(graph: TraceGraph | None = None, spec_ids: Any = None, **kwargs: Any) -> TraceResult:
    found, _ = reader(graph)
    return await found.trace(PROJECT, SEEDS if spec_ids is None else spec_ids, **kwargs)


# Worked example (trace.md)


async def test_based_on_returns_the_worked_example() -> None:
    result = await trace(direction="based_on", depth=3)
    assert rows(result) == BASED_ON_DEPTH_3
    assert result.sources == tuple(SEEDS)
    assert [(item.spec_id, item.label, item.name) for item in result.context] == [(OWNER, "Module", "Payments service")]
    assert (result.direction, result.unknown, result.truncated) == ("based_on", (), False)
    roles = {node.spec_id: node.role for node in result.nodes}
    assert roles == {
        "IFC::STRIPE_API": "used_by",
        "IFC::UNSHARED": "used_by",
        "MOD::STRIPE": "coupled",
        "REQ::INT::CALL_STRIPE": "contains",
        "CON::STRIPE": "used_by",
    }
    stripe = next(node for node in result.nodes if node.spec_id == "MOD::STRIPE")
    assert (stripe.label, stripe.name, stripe.spine_root, stripe.basis, stripe.weak) == (
        "Module",
        "Stripe",
        True,
        "stated",
        False,
    )
    assert stripe.confidence is None and stripe.rule is None


async def test_affects_returns_the_worked_example() -> None:
    result = await trace(direction="affects", depth=2)
    assert rows(result) == AFFECTS_DEPTH_2
    assert [item.spec_id for item in result.context] == [OWNER]
    assert not result.truncated


async def test_without_the_owner_start_based_on_is_empty_and_affects_keeps_checkout() -> None:
    based_on = await trace(direction="based_on", depth=3, include_owner=False)
    affects = await trace(direction="affects", depth=2, include_owner=False)
    assert based_on.nodes == ()
    assert [item.spec_id for item in based_on.context] == [OWNER]
    assert rows(affects) == [item for item in AFFECTS_DEPTH_2 if not item[5]]


async def test_the_worked_example_never_reaches_a_stored_entity_or_a_related_constraint() -> None:
    reached = {node.spec_id for node in (await trace(direction="based_on", depth=3)).nodes}
    assert {"ENT::DUAL_STORED", "CON::EU_REGION", "FEAT::GIFT_CARDS"}.isdisjoint(reached)


async def test_the_same_graph_gives_the_same_answer() -> None:
    first = await trace(direction="affects", depth=3)
    graph = shop_graph()
    graph.edges.reverse()
    assert await trace(graph, direction="affects", depth=3) == first


# Walk rules (FR-READ-11 to 16)


async def test_an_edge_is_walked_only_the_way_its_role_allows() -> None:
    """FR-READ-11: checkout depends on card payment."""

    based_on = await trace(spec_ids=["FEAT::CHECKOUT"], direction="based_on", depth=1, include_owner=False)
    affects = await trace(spec_ids=["FEAT::CHECKOUT"], direction="affects", depth=1, include_owner=False)
    assert [(n.spec_id, n.relation, n.step) for n in based_on.nodes if n.step != "scope"] == [
        ("FEAT::CARD_PAYMENT", "DEPENDS_ON", "forward")
    ]
    assert [n.spec_id for n in affects.nodes if n.step != "scope"] == []


async def test_context_lists_every_owner_up_to_the_spine_root_and_is_not_walked() -> None:
    """FR-READ-12: contains edges are never walked upward."""

    result = await trace(spec_ids=["REQ::FR::CARD_PAYMENT"], direction="affects", depth=3, include_owner=False)
    assert [item.spec_id for item in result.context] == ["FEAT::CARD_PAYMENT", OWNER]
    assert result.nodes == ()


async def test_scope_reaches_the_members_of_a_seed_and_of_a_reached_node() -> None:
    """FR-READ-13."""

    result = await trace(spec_ids=["FEAT::CHECKOUT"], direction="affects", depth=1)
    assert [(n.hops, n.spec_id, n.step, n.role) for n in result.nodes] == [
        (1, "REQ::FR::CHECKOUT", "scope", "contains"),
        (1, "REQ::Q::CHECKOUT_SPEED", "scope", "contains"),
    ]


async def test_from_a_spine_root_only_uses_and_used_by_edges_are_walked() -> None:
    """FR-READ-14: a store reached over STORED_IN does not fan out to every entity it stores."""

    result = await trace(spec_ids=["ENT::ORDER"], direction="based_on", depth=3)
    assert [(n.hops, n.spec_id, n.step) for n in result.nodes] == [
        (1, "MOD::ORDERS_DATABASE", "forward"),
        (1, "REQ::DATA::ORDER_FIELDS", "scope"),
    ]


async def test_a_seed_that_is_a_spine_root_has_no_context_and_no_members() -> None:
    result = await trace(spec_ids=["MOD::WEB_SHOP"], direction="based_on", depth=1)
    assert result.context == ()
    assert [(n.spec_id, n.relation, n.via_owner) for n in result.nodes] == [
        ("IFC::ORDERS_API", "CONSUMED_BY", False),
        (OWNER, "INTEGRATES_WITH", False),
    ]


async def test_grouping_and_realisation_edges_are_never_walked() -> None:
    result = await trace(spec_ids=["MOD::ORDERS_SERVICE"], direction="affects", depth=3)
    assert "CAP::AUTHENTICATION" not in {node.spec_id for node in result.nodes}


async def test_owner_start_marks_what_it_reaches_first() -> None:
    """FR-READ-15."""

    with_owner = await trace(direction="based_on", depth=1)
    without = await trace(direction="based_on", depth=1, include_owner=False)
    assert [(n.spec_id, n.via_owner) for n in with_owner.nodes] == [
        ("IFC::STRIPE_API", True),
        ("IFC::UNSHARED", True),
    ]
    assert without.nodes == ()


async def test_trusted_basis_skips_inferred_edges_and_any_carries_their_rule() -> None:
    """FR-READ-16."""

    graph = shop_graph().edge(
        "DEPENDS_ON", "FEAT::PLACE_ORDER", "FEAT::CARD_PAYMENT", basis="inferred", confidence=0.8, inference_rule="R-1"
    )
    trusted = await trace(graph, spec_ids=["FEAT::CARD_PAYMENT"], direction="affects", depth=1, include_owner=False)
    every = await trace(
        graph, spec_ids=["FEAT::CARD_PAYMENT"], direction="affects", depth=1, include_owner=False, basis="any"
    )
    assert "FEAT::PLACE_ORDER" not in {node.spec_id for node in trusted.nodes}
    inferred = next(node for node in every.nodes if node.spec_id == "FEAT::PLACE_ORDER")
    assert (inferred.basis, inferred.confidence, inferred.rule) == ("inferred", 0.8, "R-1")


async def test_an_edge_without_basis_reads_as_stated_and_implied_is_trusted() -> None:
    graph = shop_graph()
    graph.edge("DEPENDS_ON", "FEAT::PLACE_ORDER", "FEAT::CARD_PAYMENT")
    graph.edge("DEPENDS_ON", "FEAT::ORDER_HISTORY_EXPORT", "FEAT::CARD_PAYMENT", basis="implied")
    result = await trace(graph, spec_ids=["FEAT::CARD_PAYMENT"], direction="affects", depth=1, include_owner=False)
    assert {node.spec_id: node.basis for node in result.nodes if node.step != "scope"} == {
        "FEAT::CHECKOUT": "stated",
        "FEAT::ORDER_HISTORY_EXPORT": "implied",
        "FEAT::PLACE_ORDER": "stated",
    }


async def test_a_related_edge_marks_the_path_weak() -> None:
    graph = shop_graph()
    graph.edge("RELATES_TO", "REQ::FR::CHECKOUT", "REQ::FR::PLACE_ORDER")
    graph.edge("DEPENDS_ON", "REQ::FR::PLACE_ORDER", "REQ::FR::A")
    result = await trace(graph, spec_ids=["REQ::FR::CHECKOUT"], direction="based_on", depth=2, include_owner=False)
    assert [(n.hops, n.spec_id, n.role, n.weak) for n in result.nodes] == [
        (1, "REQ::FR::PLACE_ORDER", "related", True),
        (2, "REQ::FR::A", "uses", True),
    ]


@pytest.mark.parametrize("stored", ["requirement_first", "interface_first"])
async def test_a_symmetric_edge_takes_the_role_of_the_pair_matching_its_stored_ends(stored: str) -> None:
    ends = ("REQ::FR::CHECKOUT", "IFC::ORDERS_API")
    graph = shop_graph().edge("RELATES_TO", *(ends if stored == "requirement_first" else ends[::-1]))
    based_on = await trace(graph, spec_ids=["REQ::FR::CHECKOUT"], direction="based_on", depth=1, include_owner=False)
    affects = await trace(graph, spec_ids=["REQ::FR::CHECKOUT"], direction="affects", depth=1, include_owner=False)
    assert [(n.spec_id, n.weak) for n in based_on.nodes] == [("IFC::ORDERS_API", False)]
    assert affects.nodes == ()
    expected = ("uses", "forward") if stored == "requirement_first" else ("used_by", "backward")
    assert (based_on.nodes[0].role, based_on.nodes[0].step) == expected


async def test_open_question_edges_are_never_walked() -> None:
    result = await trace(spec_ids=["ENT::ORDER"], direction="affects", depth=3, include_owner=False)
    assert "OQ::PAYMENT_PROVIDERS" not in {node.spec_id for node in result.nodes}


async def test_relations_restrict_the_steps_but_not_the_context() -> None:
    result = await trace(direction="affects", depth=3, relations=["DEPENDS_ON"])
    assert [n.spec_id for n in result.nodes] == ["FEAT::CHECKOUT"]
    assert [item.spec_id for item in result.context] == [OWNER]


# Parents, order and limits: FR-READ-17


async def test_the_parent_is_the_lowest_spec_id_and_a_seed_path_beats_an_owner_path() -> None:
    graph = shop_graph()
    graph.edge("DEPENDS_ON", "FEAT::CHECKOUT", "FEAT::PLACE_ORDER")
    graph.edge("RELATES_TO", "REQ::FR::CARD_PAYMENT", "MOD::WEB_SHOP")
    result = await trace(
        graph,
        spec_ids=["FEAT::PLACE_ORDER", "FEAT::CARD_PAYMENT", "REQ::FR::CARD_PAYMENT"],
        direction="affects",
        depth=1,
    )
    parents = {node.spec_id: (node.parent, node.relation, node.via_owner, node.weak) for node in result.nodes}
    assert parents["FEAT::CHECKOUT"] == ("FEAT::CARD_PAYMENT", "DEPENDS_ON", False, False)
    assert parents["MOD::WEB_SHOP"] == ("REQ::FR::CARD_PAYMENT", "RELATES_TO", False, True)
    assert parents["MOD::STRIPE"] == (OWNER, "INTEGRATES_WITH", True, False)


def fan_graph(count: int) -> TraceGraph:
    graph = TraceGraph().node("FEAT::HUB", "Feature")
    for index in range(count):
        graph.node(f"FEAT::F{index:02d}", "Feature").edge("DEPENDS_ON", f"FEAT::F{index:02d}", "FEAT::HUB")
    return graph


async def test_limit_cuts_by_hop_then_spec_id_and_sets_truncated() -> None:
    first = await trace(fan_graph(30), spec_ids=["FEAT::HUB"], direction="affects", limit=10)
    again = await trace(fan_graph(30), spec_ids=["FEAT::HUB"], direction="affects", limit=10)
    assert [node.spec_id for node in first.nodes] == [f"FEAT::F{index:02d}" for index in range(10)]
    assert first.truncated and first == again


async def test_a_limit_equal_to_what_is_reachable_is_not_truncated() -> None:
    result = await trace(fan_graph(5), spec_ids=["FEAT::HUB"], direction="affects", limit=5)
    assert len(result.nodes) == 5 and not result.truncated


async def test_a_full_hop_past_the_limit_stops_the_walk() -> None:
    graph = fan_graph(3).node("FEAT::LEAF", "Feature").edge("DEPENDS_ON", "FEAT::LEAF", "FEAT::F00")
    found, executor = reader(graph)
    result = await found.trace(PROJECT, ["FEAT::HUB"], direction="affects", depth=3, limit=2)
    assert [node.spec_id for node in result.nodes] == ["FEAT::F00", "FEAT::F01"] and result.truncated
    # One read of the seed's owners, one hop: hop 1 already holds more than the limit.
    assert executor.names().count("read_trace_hop") == 2


async def test_depth_bounds_the_hops() -> None:
    graph = fan_graph(1).node("FEAT::LEAF", "Feature").edge("DEPENDS_ON", "FEAT::LEAF", "FEAT::F00")
    shallow = await trace(graph, spec_ids=["FEAT::HUB"], direction="affects", depth=1)
    deep = await trace(graph, spec_ids=["FEAT::HUB"], direction="affects", depth=3)
    assert [n.spec_id for n in shallow.nodes] == ["FEAT::F00"]
    assert [(n.hops, n.spec_id) for n in deep.nodes] == [(1, "FEAT::F00"), (2, "FEAT::LEAF")]


async def test_a_hop_takes_at_most_two_queries() -> None:
    """One read of the owners per level, then per hop one read for the spine roots and one for the rest."""

    graph = shop_graph()
    uuid = {spec_id: node["uuid"] for spec_id, node in graph.nodes.items()}
    found, executor = reader(graph)
    await found.trace(PROJECT, SEEDS, direction="affects", depth=1)
    frontiers = [sorted(params["frontier"]) for name, params in executor.calls if name == "read_trace_hop"]
    assert frontiers == [
        sorted(uuid[spec_id] for spec_id in SEEDS),  # owners of the seeds
        [uuid[OWNER]],  # hop 1: the spine root
        sorted(uuid[spec_id] for spec_id in SEEDS),  # hop 1: the other nodes
    ]


async def test_a_spine_root_is_read_with_the_hub_relations_only() -> None:
    found, executor = reader()
    await found.trace(PROJECT, ["MOD::WEB_SHOP"], direction="affects", depth=1)
    (params,) = [params for name, params in executor.calls if name == "read_trace_hop"]
    hub = {"CARRIES", "CONSUMED_BY", "DEPENDS_ON", "DEPLOYED_TO", "INTEGRATES_WITH", "PART_OF", "RELATES_TO"}
    assert set(params["relations"]) == hub


# Seeds


async def test_unknown_seeds_are_listed_and_aliases_resolve() -> None:
    graph = shop_graph().alias("FEAT::CHECKOUT", "FEAT::PAY_AND_GO")
    result = await trace(graph, spec_ids=["FEAT::PAY_AND_GO", "FEAT::NOPE", "FEAT::PAY_AND_GO"], direction="based_on")
    assert result.sources == ("FEAT::CHECKOUT",)
    assert result.unknown == ("FEAT::NOPE",)
    assert "FEAT::CARD_PAYMENT" in {node.spec_id for node in result.nodes}


async def test_a_trace_where_no_seed_resolves_is_empty() -> None:
    found, executor = reader()
    result = await found.trace(PROJECT, "FEAT::NOPE", direction="affects")
    assert result == TraceResult(direction="affects", unknown=("FEAT::NOPE",))
    assert executor.names() == ["read_node_uuids_by_spec_id"]


async def test_an_ontology_without_impact_walks_only_the_scope_step(tmp_path: Path) -> None:
    """An older ontology (no ``impact``): every role is ``none``; owners and members still come from owner pairs."""

    found, _ = reader(ontology=load_edited(tmp_path, without_impact(shipped_data())))
    result = await found.trace(PROJECT, SEEDS, direction="affects", depth=3)
    assert [(n.spec_id, n.step, n.role) for n in result.nodes] == []
    checkout = await found.trace(PROJECT, ["FEAT::CHECKOUT"], direction="affects", depth=3)
    assert [(n.spec_id, n.step, n.role) for n in checkout.nodes] == [
        ("REQ::FR::CHECKOUT", "scope", "none"),
        ("REQ::Q::CHECKOUT_SPEED", "scope", "none"),
    ]
    assert [item.spec_id for item in result.context] == [OWNER]


async def test_an_ontology_without_owner_pairs_has_no_context_and_walks_nothing() -> None:
    graph = TraceGraph().node("MOD::M", "Module").node("FEAT::F", "Feature").node("REQ::FR::R", "FunctionalRequirement")
    graph.edge("PART_OF", "FEAT::F", "MOD::M").edge("PART_OF", "REQ::FR::R", "FEAT::F")
    found, _ = reader(graph, frozen_ontology())
    result = await found.trace(PROJECT, ["FEAT::F"], direction="affects", depth=3)
    assert (result.sources, result.context, result.nodes) == (("FEAT::F",), (), ())


# Arguments


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"spec_ids": []}, "invalid_spec_ids"),
        ({"spec_ids": [f"FEAT::F{index}" for index in range(MAX_SPEC_IDS + 1)]}, "invalid_spec_ids"),
        ({"spec_ids": ["FEAT::CHECKOUT", 7]}, "invalid_spec_ids"),
        ({"direction": "sideways"}, "invalid_direction"),
        ({"depth": 0}, "invalid_depth"),
        ({"depth": 4}, "invalid_depth"),
        ({"depth": True}, "invalid_depth"),
        ({"basis": "maybe"}, "invalid_basis"),
        ({"relations": ["NOT_A_RELATION"]}, "unknown_relation"),
        ({"relations": ["CONTAINS"]}, "unknown_relation"),
        ({"limit": 0}, "invalid_limit"),
        ({"limit": 501}, "invalid_limit"),
    ],
)
async def test_invalid_arguments_raise_a_configuration_error(kwargs: dict[str, Any], code: str) -> None:
    found, executor = reader()
    arguments: dict[str, Any] = {"spec_ids": SEEDS, "direction": "affects", **kwargs}
    with pytest.raises(ConfigurationError) as raised:
        await found.trace(PROJECT, arguments.pop("spec_ids"), **arguments)
    assert raised.value.code == code
    assert executor.calls == []


async def test_fifty_seeds_are_accepted() -> None:
    result = await trace(spec_ids=[f"FEAT::F{index}" for index in range(MAX_SPEC_IDS)], direction="affects")
    assert len(result.unknown) == MAX_SPEC_IDS
