"""``GraphReader.paths``: at most three shortest directed chains between two spec nodes, walked by the trace's impact
roles over trusted edges, first ``based_on`` then ``affects``, a spine root inside a chain passed over its hub roles
only; without a chain, the reason and the shortest undirected ``link`` (api.md, trace.md)."""

from __future__ import annotations

from typing import Any

import pytest

from graph.core import ConfigurationError
from graph.core import paths as paths_module
from graph.core.paths import LINK_HOPS, MAX_CHAINS, MAX_PATH_HOPS, PathResult
from graph_reads_support import PROJECT, ReadsExecutor
from graph_trace_support import TraceGraph, shop_graph


def chains(result: PathResult | None) -> list[list[tuple[str, str, str]]]:
    assert result is not None
    return [[(step.spec_id, step.relation, step.step) for step in chain.steps] for chain in result.chains]


async def paths(from_id: str, to_id: str, graph: TraceGraph | None = None, **options: Any) -> PathResult | None:
    return await ReadsExecutor(graph or shop_graph()).reader().paths(PROJECT, from_id, to_id, **options)


async def test_a_chain_where_the_first_node_depends_on_the_second_is_based_on_with_its_steps() -> None:
    found = await paths("MOD::PAYMENTS_SERVICE", "MOD::STRIPE")

    assert found is not None and found.direction == "based_on"
    assert [chain.hops for chain in found.chains] == [2]
    step = found.chains[0].steps[0]
    assert (step.spec_id, step.label, step.name, step.role, step.basis) == (
        "IFC::STRIPE_API",
        "Interface",
        "Stripe API",
        "used_by",
        "stated",
    )
    assert chains(found) == [
        [("IFC::STRIPE_API", "CONSUMED_BY", "backward"), ("MOD::STRIPE", "PROVIDED_BY", "forward")]
    ]


async def test_without_a_based_on_chain_the_reverse_is_affects() -> None:
    forward = await paths("FEAT::CHECKOUT", "FEAT::CARD_PAYMENT")
    reverse = await paths("FEAT::CARD_PAYMENT", "FEAT::CHECKOUT")

    assert forward is not None and forward.direction == "based_on"
    assert reverse is not None and reverse.direction == "affects"
    assert chains(reverse) == [[("FEAT::CHECKOUT", "DEPENDS_ON", "backward")]]


def link(result: PathResult | None) -> list[tuple[str, str, str, str]]:
    assert result is not None and result.link is not None
    return [(step.spec_id, step.relation, step.step, step.role) for step in result.link.steps]


async def test_a_spine_root_inside_a_chain_is_passed_over_its_hub_roles_as_the_trace_does() -> None:
    # trace.md worked example: CON::STRIPE is a rule the Stripe module owns directly (used_by), reached at hop 3
    found = await paths("MOD::PAYMENTS_SERVICE", "CON::STRIPE")

    assert found is not None and (found.direction, found.reason, found.link) == ("based_on", None, None)
    assert chains(found) == [
        [
            ("IFC::STRIPE_API", "CONSUMED_BY", "backward"),
            ("MOD::STRIPE", "PROVIDED_BY", "forward"),
            ("CON::STRIPE", "PART_OF", "backward"),
        ]
    ]


async def test_a_shared_provider_is_no_chain_and_the_link_shows_the_module_between() -> None:
    # both interfaces are provided by MOD::STRIPE; PROVIDED_BY is coupled, never walked from a reached module
    found = await paths("IFC::STRIPE_API", "IFC::INTEGRATED")

    assert found is not None and (found.direction, found.chains, found.reason) == (None, (), "no_directed_chain")
    assert found.link is not None and found.link.hops == 2
    assert link(found) == [
        ("MOD::STRIPE", "PROVIDED_BY", "forward", "coupled"),
        ("IFC::INTEGRATED", "PROVIDED_BY", "backward", "coupled"),
    ]


async def test_the_link_goes_up_through_owners_and_takes_weak_edges() -> None:
    # FEAT::CHECKOUT sits in the web shop; CON::EU_REGION relates to it: an association, not a dependency
    found = await paths("FEAT::CHECKOUT", "CON::EU_REGION")

    assert found is not None and found.reason == "no_directed_chain"
    assert link(found) == [
        ("MOD::WEB_SHOP", "PART_OF", "forward", "contains"),
        ("CON::EU_REGION", "RELATES_TO", "backward", "related"),
    ]


async def test_nodes_joined_only_by_an_open_question_or_nothing_are_not_connected() -> None:
    # OQ::PAYMENT_PROVIDERS relates to ENT::ORDER over a pair whose role is none: never walked, also not by the link
    graph = shop_graph().node("FEAT::ALONE", "Feature")

    question = await paths("OQ::PAYMENT_PROVIDERS", "ENT::ORDER", graph)
    alone = await paths("FEAT::ALONE", "FEAT::CHECKOUT", graph)

    assert question == alone == PathResult(reason="not_connected")


def line(count: int) -> TraceGraph:
    """FEAT::N0 depends on FEAT::N1, ... on FEAT::N<count>."""

    graph = TraceGraph()
    for index in range(count + 1):
        graph.node(f"FEAT::N{index}", "Feature")
    for index in range(count):
        graph.edge("DEPENDS_ON", f"FEAT::N{index}", f"FEAT::N{index + 1}")
    return graph


async def test_six_hops_find_a_six_edge_chain_and_the_link_stops_at_six_edges() -> None:
    six = await paths("FEAT::N0", "FEAT::N6", line(6), max_hops=MAX_PATH_HOPS)
    short = await paths("FEAT::N0", "FEAT::N6", line(6), max_hops=4)
    seven = await paths("FEAT::N0", "FEAT::N7", line(7), max_hops=MAX_PATH_HOPS)

    assert six is not None and [chain.hops for chain in six.chains] == [6]
    assert short is not None and (short.chains, short.reason) == ((), "no_directed_chain")
    assert short.link is not None and short.link.hops == LINK_HOPS
    assert seven == PathResult(reason="not_connected")


def diamond() -> TraceGraph:
    graph = TraceGraph()
    for spec_id in ("FEAT::A", "FEAT::B", "FEAT::C", "FEAT::D", "FEAT::E", "FEAT::F", "FEAT::Z"):
        graph.node(spec_id, "Feature")
    for source, target in (
        ("FEAT::A", "FEAT::E"),
        ("FEAT::A", "FEAT::D"),
        ("FEAT::A", "FEAT::C"),
        ("FEAT::A", "FEAT::B"),
        ("FEAT::B", "FEAT::Z"),
        ("FEAT::C", "FEAT::Z"),
        ("FEAT::D", "FEAT::Z"),
        ("FEAT::E", "FEAT::Z"),
        ("FEAT::A", "FEAT::F"),
    ):
        graph.edge("DEPENDS_ON", source, target, basis="stated")
    graph.edge("DEPENDS_ON", "FEAT::F", "FEAT::Z", basis="inferred", confidence=0.9, inference_rule="R-1")
    return graph


async def test_at_most_three_shortest_chains_ordered_by_the_ids_along_them_and_inferred_edges_are_not_walked() -> None:
    found = await paths("FEAT::A", "FEAT::Z", diamond())

    assert found is not None and found.direction == "based_on"
    assert len(found.chains) == MAX_CHAINS
    assert [[step.spec_id for step in chain.steps] for chain in found.chains] == [
        ["FEAT::B", "FEAT::Z"],
        ["FEAT::C", "FEAT::Z"],
        ["FEAT::D", "FEAT::Z"],
    ]


async def test_max_hops_bounds_the_chains() -> None:
    graph = TraceGraph()
    for spec_id in ("FEAT::A", "FEAT::B", "FEAT::C"):
        graph.node(spec_id, "Feature")
    graph.edge("DEPENDS_ON", "FEAT::A", "FEAT::B").edge("DEPENDS_ON", "FEAT::B", "FEAT::C")

    assert chains(await paths("FEAT::A", "FEAT::C", graph, max_hops=2)) == [
        [("FEAT::B", "DEPENDS_ON", "forward"), ("FEAT::C", "DEPENDS_ON", "forward")]
    ]
    one = await paths("FEAT::A", "FEAT::C", graph, max_hops=1)
    assert one is not None and (one.chains, one.reason) == ((), "no_directed_chain")


async def test_aliases_resolve_one_node_is_no_chain_and_an_unknown_end_is_none() -> None:
    graph = shop_graph().alias("FEAT::CHECKOUT", "FEAT::PAY_AND_GO")

    aliased = await paths("FEAT::PAY_AND_GO", "FEAT::CARD_PAYMENT", graph)
    same = await paths("FEAT::PAY_AND_GO", "FEAT::CHECKOUT", graph)

    assert aliased is not None and aliased.direction == "based_on" and len(aliased.chains) == 1
    assert same == PathResult(reason="same_node")
    assert await paths("FEAT::NOPE", "FEAT::CHECKOUT") is None
    assert await paths("FEAT::CHECKOUT", "FEAT::NOPE") is None


@pytest.mark.parametrize("max_hops", [0, MAX_PATH_HOPS + 1, True, "2"])
async def test_max_hops_outside_one_to_six_is_refused_before_any_read(max_hops: Any) -> None:
    executor = ReadsExecutor(shop_graph())

    with pytest.raises(ConfigurationError) as error:
        await executor.reader().paths(PROJECT, "FEAT::CHECKOUT", "FEAT::CARD_PAYMENT", max_hops=max_hops)

    assert error.value.code == "invalid_max_hops" and executor.calls == []


async def test_of_parallel_edges_a_chain_steps_over_the_lowest_relation() -> None:
    graph = TraceGraph()
    for spec_id in ("FEAT::A", "FEAT::B"):
        graph.node(spec_id, "Feature")
    graph.edge("RELATES_TO", "FEAT::A", "FEAT::B", basis="stated").edge("DEPENDS_ON", "FEAT::A", "FEAT::B")

    assert chains(await paths("FEAT::A", "FEAT::B", graph)) == [[("FEAT::B", "DEPENDS_ON", "forward")]]


async def test_a_link_search_cut_at_its_node_cap_is_a_search_limit_not_proof_of_no_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QA F29: the 2000-node cap of the link search made a cut-short search read as ``not_connected``."""

    monkeypatch.setattr(paths_module, "LINK_MAX_NODES", 3)

    far = await paths("FEAT::N0", "FEAT::N7", line(7), max_hops=MAX_PATH_HOPS)
    near = await paths("FEAT::N0", "FEAT::N2", line(7), max_hops=1)

    assert far == PathResult(reason="search_limit")  # the cap was passed after the third hop
    assert near is not None and near.reason == "no_directed_chain" and near.link is not None  # found before the cap


def landing() -> TraceGraph:
    """MOD::M is reached at hop 2 from MOD::START by a related edge (from CON::A) and by a used_by edge (from IFC::B);
    MOD::M2 integrates with MOD::M."""

    graph = TraceGraph()
    for spec_id, label in (
        ("MOD::START", "Module"),
        ("CON::A", "Constraint"),
        ("IFC::B", "Interface"),
        ("MOD::M", "Module"),
        ("MOD::M2", "Module"),
    ):
        graph.node(spec_id, label)
    graph.edge("RELATES_TO", "CON::A", "MOD::START").edge("PROVIDED_BY", "IFC::B", "MOD::START")
    graph.edge("RELATES_TO", "CON::A", "MOD::M").edge("CONSUMED_BY", "IFC::B", "MOD::M")
    return graph.edge("INTEGRATES_WITH", "MOD::M2", "MOD::M")


async def test_a_chain_walks_on_from_a_node_over_the_same_parent_edge_as_the_trace() -> None:
    """QA F29: the chain search walked on from a node when any edge allowed it; the trace decides by the edge it
    keeps (the lowest parent): CON::A's related edge lands in the module, so neither goes on to MOD::M2."""

    graph = landing()
    reader = ReadsExecutor(graph).reader()

    found = await reader.paths(PROJECT, "MOD::START", "MOD::M2", max_hops=MAX_PATH_HOPS)
    traced = await reader.trace(PROJECT, ["MOD::START"], direction="affects", depth=3)

    assert "MOD::M2" not in [node.spec_id for node in traced.nodes]
    assert found is not None and found.chains == () and found.reason == "no_directed_chain"
