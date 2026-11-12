"""``GraphReader.nodes_by_spec_ids``: nodes by own id or alias, keyed by the current id, in one read (api.md)."""

from __future__ import annotations

from graph_reads_support import PROJECT, ReadsExecutor
from graph_trace_support import TraceGraph


def graph() -> TraceGraph:
    found = TraceGraph()
    found.node("FEAT::A", "Feature")
    found.node("FEAT::B", "Feature", aliases=["FEAT::OLD_B"])
    return found


async def test_ids_naming_no_node_are_left_out() -> None:
    reader = ReadsExecutor(graph()).reader()

    found = await reader.nodes_by_spec_ids(PROJECT, ["FEAT::A", "FEAT::NOPE"])

    assert list(found) == ["FEAT::A"]
    assert await reader.nodes_by_spec_ids(PROJECT, ["FEAT::NOPE"]) == {}


async def test_an_alias_returns_the_current_node_keyed_by_its_current_spec_id() -> None:
    reader = ReadsExecutor(graph()).reader()

    found = await reader.nodes_by_spec_ids(PROJECT, ["FEAT::OLD_B", "FEAT::A"])

    assert sorted(found) == ["FEAT::A", "FEAT::B"]  # not "FEAT::OLD_B": the key is the current spec_id
    assert found["FEAT::B"].spec_id == "FEAT::B"


async def test_empty_input_is_an_empty_dict_without_a_query() -> None:
    executor = ReadsExecutor(graph())

    assert await executor.reader().nodes_by_spec_ids(PROJECT, []) == {}
    assert executor.calls == []


async def test_many_ids_are_one_id_read_and_one_node_read() -> None:
    executor = ReadsExecutor(graph())

    await executor.reader().nodes_by_spec_ids(PROJECT, ["FEAT::A", "FEAT::OLD_B"])

    assert [name for name, _ in executor.calls] == ["read_node_uuids_by_spec_id", "read_nodes_by_uuid"]
