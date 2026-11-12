"""``GraphReader.hops``: fewest trusted edges between spec nodes, a spine root never inside a chain (api.md)."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

import pytest

from graph.core import ConfigurationError, GraphReader
from graph.core.hops import hop_distances
from graph_reads_support import PROJECT, EdgeRead, ReadsExecutor
from graph_support import OfflineExecutor, shipped_ontology
from graph_trace_support import TraceGraph

CHAIN = [("A", "B"), ("B", "C"), ("C", "D")]


async def distances(
    edges: list[tuple[str, str]], roots: Collection[str], sources: list[str], targets: list[str], max_hops: int
) -> dict[str, int]:
    read = EdgeRead(edges, roots)
    return await hop_distances(read, [read.node(spec_id) for spec_id in sources], targets, max_hops)


async def test_a_source_is_zero_and_others_count_edges_either_way() -> None:
    assert await distances(CHAIN, (), ["A"], ["A", "B", "C", "D"], 3) == {"A": 0, "B": 1, "C": 2, "D": 3}
    assert await distances(CHAIN, (), ["D"], ["A"], 3) == {"A": 3}


async def test_ids_beyond_max_hops_are_left_out_and_the_nearest_source_counts() -> None:
    assert await distances(CHAIN, (), ["A"], ["C", "D"], 2) == {"C": 2}
    assert await distances(CHAIN, (), ["A", "D"], ["C"], 3) == {"C": 1}


async def test_a_spine_root_ends_a_chain_but_is_never_walked_through() -> None:
    edges = [("F1", "MOD"), ("MOD", "F2"), ("F2", "F3")]

    assert await distances(edges, ["MOD"], ["F1"], ["MOD", "F2", "F3"], 4) == {"MOD": 1}
    assert await distances(edges, ["MOD"], ["MOD"], ["F1", "F2"], 4) == {"F1": 1, "F2": 1}  # a root source starts
    assert await distances(edges, [], ["F1"], ["F2"], 4) == {"F2": 2}


async def test_unknown_and_unlinked_ids_are_not_reached() -> None:
    assert await distances(CHAIN, (), ["A"], ["Z"], 3) == {}
    assert await distances(CHAIN, (), ["Q"], ["A"], 3) == {}


async def test_the_walk_reads_one_frontier_per_hop_and_stops_once_every_target_is_found() -> None:
    read = EdgeRead([*CHAIN, ("D", "E")])

    assert await hop_distances(read, [read.node("A")], ["B"], 4) == {"B": 1}
    assert read.calls == [["A"]]  # B found at hop 1: no further read

    read.calls.clear()
    assert await hop_distances(read, [read.node("A")], ["Z"], 4) == {}
    assert read.calls == [["A"], ["B"], ["C"], ["D"]]  # never more reads than hops, each the new frontier only


def hop_graph() -> TraceGraph:
    graph = TraceGraph()
    for spec_id in ("FEAT::A", "FEAT::B", "FEAT::C", "FEAT::D"):
        graph.node(spec_id, "Feature")
    graph.node("MOD::M", "Module")
    graph.edge("DEPENDS_ON", "FEAT::A", "FEAT::B", basis="stated")
    graph.edge("DEPENDS_ON", "FEAT::B", "MOD::M", basis="stated")
    graph.edge("DEPENDS_ON", "MOD::M", "FEAT::C")  # no basis reads as stated
    return graph


async def test_hops_walks_the_trusted_edges_frontier_by_frontier_and_never_through_a_module() -> None:
    executor = ReadsExecutor(hop_graph())

    found = await executor.reader().hops(PROJECT, ["FEAT::A"], ["FEAT::B", "MOD::M", "FEAT::C"])

    assert found == {"FEAT::B": 1, "MOD::M": 2}  # the module is a hub: FEAT::C is not reached through it
    names = [name for name, _ in executor.calls]
    assert names == ["read_node_uuids_by_spec_id", "read_trace_hop", "read_trace_hop"]  # no project-wide read
    assert executor.calls[2][1]["frontier"] == [executor.graph.nodes["FEAT::B"]["uuid"]]  # the module is not read


async def test_hops_leaves_out_inferred_edges_but_walks_implied_ones() -> None:
    graph = TraceGraph()
    for spec_id in ("FEAT::A", "FEAT::B", "FEAT::C"):
        graph.node(spec_id, "Feature")
    graph.edge("DEPENDS_ON", "FEAT::A", "FEAT::B", basis="inferred", confidence=0.9, inference_rule="r")
    graph.edge("DEPENDS_ON", "FEAT::B", "FEAT::C", basis="implied")
    reader = ReadsExecutor(graph).reader()

    assert await reader.hops(PROJECT, ["FEAT::A"], ["FEAT::B", "FEAT::C"]) == {}
    assert await reader.hops(PROJECT, ["FEAT::B"], ["FEAT::C"]) == {"FEAT::C": 1}


def reader(rows: dict[str, list[dict[str, Any]]]) -> tuple[GraphReader, OfflineExecutor]:
    executor = OfflineExecutor(rows)
    return GraphReader(executor, shipped_ontology()), executor


@pytest.mark.parametrize(
    ("sources", "ids", "max_hops", "code"),
    [
        ([], ["A"], 3, "invalid_spec_ids"),
        (["A"], [], 3, "invalid_spec_ids"),
        (["A"] * 501, ["A"], 3, "invalid_spec_ids"),
        ("A", ["A"], 3, "invalid_spec_ids"),
        (["A"], ["A"], 0, "invalid_max_hops"),
        (["A"], ["A"], 5, "invalid_max_hops"),
        (["A"], ["A"], True, "invalid_max_hops"),
    ],
)
async def test_hops_checks_its_arguments_before_any_read(sources: Any, ids: Any, max_hops: Any, code: str) -> None:
    graph, executor = reader({})

    with pytest.raises(ConfigurationError) as error:
        await graph.hops("p1", sources, ids, max_hops=max_hops)

    assert error.value.code == code and executor.reads == []
