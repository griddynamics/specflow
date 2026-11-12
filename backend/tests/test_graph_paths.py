"""``GraphReader.paths``: at most three shortest directed chains between two spec nodes, walked by the trace's impact
roles over trusted edges, first ``based_on`` then ``affects``, a spine root never inside a chain (api.md, trace.md)."""

from __future__ import annotations

from typing import Any

import pytest

from graph.core import ConfigurationError
from graph.core.paths import MAX_CHAINS, PathResult
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


async def test_a_spine_root_ends_a_chain_but_is_never_inside_one() -> None:
    # the trace reaches CON::STRIPE at hop 3 through MOD::STRIPE (a module); a chain may not pass through it
    found = await paths("MOD::PAYMENTS_SERVICE", "CON::STRIPE")

    assert found == PathResult()


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
    assert await paths("FEAT::A", "FEAT::C", graph, max_hops=1) == PathResult()


async def test_aliases_resolve_one_node_is_no_chain_and_an_unknown_end_is_none() -> None:
    graph = shop_graph().alias("FEAT::CHECKOUT", "FEAT::PAY_AND_GO")

    aliased = await paths("FEAT::PAY_AND_GO", "FEAT::CARD_PAYMENT", graph)
    same = await paths("FEAT::PAY_AND_GO", "FEAT::CHECKOUT", graph)

    assert aliased is not None and aliased.direction == "based_on" and len(aliased.chains) == 1
    assert same == PathResult()
    assert await paths("FEAT::NOPE", "FEAT::CHECKOUT") is None
    assert await paths("FEAT::CHECKOUT", "FEAT::NOPE") is None


@pytest.mark.parametrize("max_hops", [0, 5, True, "2"])
async def test_max_hops_outside_one_to_four_is_refused_before_any_read(max_hops: Any) -> None:
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
