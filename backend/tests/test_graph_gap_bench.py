"""The graph.gap detection benchmark (examples/gap_detect_bench.py, NFR-GPERF-01): a small generated graph runs the
detection steps without a model call, and the bench command parses."""

from __future__ import annotations

import graph_bench
from gap_detect_bench import NODES_PER_MODULE, generate, run_gap_detect_bench


async def test_a_generated_graph_runs_detection_without_a_model_call() -> None:
    summary = await run_gap_detect_bench(10 * NODES_PER_MODULE)

    assert summary["ok"] is True
    assert summary["nodes"] == 10 * NODES_PER_MODULE
    assert summary["model_calls"] == 0
    assert summary["detector_failures"] == {}
    assert summary["seeds"] > 0 and summary["sites"]["formed"] > 0


def test_the_generated_graph_has_every_planted_defect() -> None:
    graph = generate(2 * NODES_PER_MODULE)

    assert len(graph.nodes) == 2 * NODES_PER_MODULE
    assert graph.refused and graph.cuts
    assert any(node.attributes.get("is_assumption") == "yes" for node in graph.nodes)


def test_the_gap_detect_command_parses() -> None:
    args = graph_bench.parse_args(["gap-detect", "--nodes", "100"])

    assert (args.command, args.nodes) == ("gap-detect", 100)
