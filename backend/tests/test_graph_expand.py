"""``GraphReader.expand``: what to read next around seed nodes, personalised PageRank near them mixed with stored
vector scores, cut at a token budget, without a model call (api.md "Read and search", A40)."""

from __future__ import annotations

import math
import time
from typing import Any

import pytest

from graph.core import ConfigurationError
from graph.core.expand import (
    HUB_PASS,
    ITERATIONS,
    MAX_RANKED,
    SIMILAR_ONLY_BAND,
    SIMILAR_ONLY_MAX,
    SUBGRAPH_MAX_NODES,
    ExpansionItem,
    Subgraph,
    adjacency,
    estimate_tokens,
    mean_vector,
    personalised_pagerank,
    ranked_scores,
    subgraph,
    top_scores,
    within_budget,
)
from graph_reads_support import MODEL, PROJECT, EdgeRead, Gateway, ReadsExecutor
from graph_trace_support import TraceGraph, shop_graph

SEEDS = ["FEAT::CHECKOUT", "REQ::FR::CHECKOUT"]


def item(spec_id: str, tokens: int, score: float = 1.0) -> ExpansionItem:
    return ExpansionItem(
        spec_id=spec_id, label="Feature", name=spec_id, score=score, via="graph", text="", tokens=tokens
    )


# --- pure parts -----------------------------------------------------------------------------------------------


def test_tokens_are_characters_divided_by_four_rounded_up() -> None:
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcd", "e") == 2
    assert estimate_tokens("", "") == 0


def test_items_are_taken_in_rank_order_while_each_fits_in_what_is_left_of_the_budget() -> None:
    found = within_budget([item("A", 40), item("B", 70), item("C", 10), item("D", 60)], 100)

    assert [taken.spec_id for taken in found.items] == ["A", "C"]  # B does not fit; C still does, D no longer
    assert (found.tokens, found.left_out) == (50, 2)
    assert within_budget([item("A", 40), item("B", 60)], 100).left_out == 0


async def near(edges: list[tuple[str, str]], seeds: list[str], roots: set[str]) -> tuple[Subgraph, EdgeRead]:
    read = EdgeRead(edges, roots)
    return await subgraph(read, [read.node(spec_id) for spec_id in seeds]), read


async def test_the_subgraph_is_three_hops_nearest_first_and_never_leaves_a_root_that_is_no_seed() -> None:
    edges = [("S", "A"), ("A", "B"), ("B", "C"), ("C", "D"), ("S", "R"), ("R", "M1"), ("R", "M2"), ("S", "S")]

    found, read = await near(edges, ["S"], {"R"})
    from_root, _ = await near(edges, ["R"], {"R"})

    assert found.nodes == ("S", "A", "R", "B", "C") and found.roots == {"R"}
    assert from_root.nodes == ("R", "M1", "M2", "S", "A", "B") and from_root.roots == frozenset()  # a seed root starts
    assert "S" not in found.neighbours["S"]  # a self loop is no neighbour
    assert found.neighbours["C"] == {"B"}  # D is outside the subgraph
    # one read per hop; the last reads the farthest nodes and the root, never walked on
    assert read.calls == [["S"], ["A"], ["B"], ["C", "R"]]


async def test_the_subgraph_sees_the_edge_between_two_roots_that_are_no_seeds() -> None:
    found, _ = await near([("S", "R1"), ("S", "R2"), ("R1", "R2"), ("R1", "M")], ["S"], {"R1", "R2"})

    assert found.nodes == ("S", "R1", "R2") and found.neighbours["R1"] == {"S", "R2"}


async def test_the_subgraph_keeps_at_most_2000_nodes_nearest_first() -> None:
    edges = [("S", f"N{index:05d}") for index in range(SUBGRAPH_MAX_NODES + 10)]

    found, read = await near([*edges, ("N00000", "FAR")], ["S"], set())

    assert len(found.nodes) == SUBGRAPH_MAX_NODES and found.nodes[:3] == ("S", "N00000", "N00001")
    assert len(read.calls) == 2 and "FAR" not in found.nodes  # full after hop 1: the last read is of the kept nodes


async def test_pagerank_restarts_at_the_seeds_and_a_hub_passes_on_a_fifth_of_its_rank() -> None:
    edges = [("S", "X"), ("S", "R"), *(("R", f"M{index}") for index in range(5))]
    found, _ = await near(edges, ["S"], set())
    nodes, neighbours = found.nodes, found.neighbours

    damped = personalised_pagerank(nodes, neighbours, ["S"], {"R"})
    open_hub = personalised_pagerank(nodes, neighbours, ["S"], set())

    assert max(damped, key=damped.__getitem__) == "S"
    assert damped["X"] > damped["M0"] * 5  # what the hub spreads to its members is cut to 0.2
    assert damped["M0"] < open_hub["M0"] * HUB_PASS < open_hub["M0"]
    assert math.isclose(sum(damped.values()), 1.0, rel_tol=1e-9)
    assert personalised_pagerank(nodes, neighbours, ["NOPE"], set()) == {}


def test_the_centroid_is_the_mean_of_the_seed_vectors() -> None:
    assert mean_vector([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]) == [0.5, 0.5]
    assert mean_vector([[1.0, 0.0], [-1.0, 0.0]]) is None  # no direction
    assert mean_vector([[1.0], [1.0, 0.0]]) is None and mean_vector([]) is None


def test_pagerank_matches_the_closed_form_of_two_linked_nodes() -> None:
    # S - X, seed S: S(n) = 0.15 + 0.85 X(n-1), X(n) = 0.85 S(n-1), so S(n) = 0.15 + 0.7225 S(n-2) from S(0) = 1
    # and S(1) = 0.15. After 30 iterations (15 double steps from S(0), 14 from S(1)):
    fixed = 0.15 / (1 - 0.85**2)
    s_30 = fixed + 0.7225**15 * (1 - fixed)
    s_29 = fixed + 0.7225**14 * (0.15 - fixed)

    rank = personalised_pagerank(["S", "X"], adjacency([("S", "X")]), ["S"], set())

    assert rank == {"S": pytest.approx(s_30, abs=1e-12), "X": pytest.approx(0.85 * s_29, abs=1e-12)}


def test_scores_mix_graph_and_vector_and_say_which_found_each_node() -> None:
    ranked = ranked_scores({"A": 0.2, "B": 0.1, "Z": 0.0}, {"B": 0.9, "C": 0.86})

    assert ranked == [
        ("A", pytest.approx(0.7), "graph"),
        ("B", pytest.approx(0.7 * 0.5 + 0.3 * 0.9), "both"),
        ("C", pytest.approx(0.3 * 0.86), "similar"),
    ]  # Z scores 0 and is left out
    ties = [f"N{i:03d}" for i in range(MAX_RANKED + 5)]
    kept = [spec_id for spec_id, _, _ in ranked_scores(dict.fromkeys(reversed(ties), 1.0), {})]
    assert kept == ties[:MAX_RANKED]  # N000..N199: equal scores by spec_id


def test_a_node_found_by_its_vector_alone_is_kept_only_near_the_best_vector_score_and_at_most_ten() -> None:
    """QA I07: the vector-only tail (0.62 to 0.73 under a best of 0.80) filled the budget with unrelated nodes."""

    best = 0.8
    tail = {f"T{index:02d}": best - SIMILAR_ONLY_BAND / 20 * index for index in range(30)}
    vector = {"G": 0.62, "FAR": best - SIMILAR_ONLY_BAND - 0.01, **tail, "BEST": best}

    ranked = {spec_id: via for spec_id, _, via in ranked_scores({"G": 1.0}, vector)}

    # a graph node keeps its vector score however low it is; of the others the 10 best within the band
    assert ranked == {"G": "both", "BEST": "similar", **{f"T{index:02d}": "similar" for index in range(9)}}
    assert len(ranked) == 1 + SIMILAR_ONLY_MAX


def test_equal_scores_rank_by_spec_id_whatever_the_order_they_come_in() -> None:
    forward = {"A": 0.5, "C": 0.5, "B": 0.5}
    backward = dict(reversed(forward.items()))

    for graph in (forward, backward):
        assert [spec_id for spec_id, _, _ in ranked_scores(graph, dict(reversed(graph.items())))] == ["A", "B", "C"]
        assert list(top_scores(graph, 2)) == ["A", "B"]


# --- the read -------------------------------------------------------------------------------------------------


def shop_executor() -> ReadsExecutor:
    executor = ReadsExecutor(shop_graph())
    executor.attributes["FEAT::CARD_PAYMENT"] = {"stmt": "Customers pay by card when they check out."}
    executor.provenance["MOD::PAYMENTS_SERVICE"] = [
        {"fragment_id": "f1", "document_id": "d", "path": "a.md", "line_start": 1, "line_end": 1}
    ]
    executor.attributes["MOD::PAYMENTS_SERVICE"] = {
        "statements": '[{"fragment_id": "f1", "text": "Payments service takes card payments."}]'
    }
    executor.vectors = {
        "FEAT::CHECKOUT": (MODEL, (1.0, 0.0)),
        "FEAT::CHECKOUT_REVIEW": (MODEL, (0.9, 0.2)),
        "ENT::ORDER": (MODEL, (0.1, 1.0)),  # score below the vector floor
        "FEAT::GIFT_CARDS": ("other/model", (1.0, 0.0)),
    }
    return executor


async def test_expand_ranks_nodes_near_the_seeds_in_graph_and_meaning_without_seeds_or_excluded_nodes() -> None:
    executor = shop_executor()

    found = await executor.reader(Gateway()).expand(PROJECT, SEEDS, exclude=["REQ::Q::CHECKOUT_SPEED"])

    ids = [entry.spec_id for entry in found.items]
    assert ids[0] == "FEAT::CARD_PAYMENT"  # one domain edge from the seed
    assert not {*SEEDS, "REQ::Q::CHECKOUT_SPEED", "ENT::ORDER", "FEAT::GIFT_CARDS"} & set(ids)
    vias = {entry.spec_id: entry.via for entry in found.items}
    assert vias["FEAT::CHECKOUT_REVIEW"] == "similar" and vias["FEAT::CARD_PAYMENT"] == "graph"
    scores = [entry.score for entry in found.items]
    assert scores == sorted(scores, reverse=True) and scores[0] == pytest.approx(0.7)
    card = found.items[0]
    assert (card.text, card.tokens) == (
        "Customers pay by card when they check out.",
        estimate_tokens("Card payment", "Customers pay by card when they check out."),
    )
    payments = next(entry for entry in found.items if entry.spec_id == "MOD::PAYMENTS_SERVICE")
    assert payments.text == "Payments service takes card payments."  # the quote of its first provenance record
    assert (payments.document, card.document) == ("a.md", None)  # the path of its first source document
    assert found.tokens == sum(entry.tokens for entry in found.items) and found.left_out == 0
    assert "embed" not in [name for name, _ in executor.calls]


async def test_expand_cuts_at_the_budget_and_counts_what_it_left_out() -> None:
    executor = shop_executor()
    for spec_id in ("FEAT::CARD_PAYMENT", "MOD::WEB_SHOP", "REQ::Q::CHECKOUT_SPEED", "MOD::PAYMENTS_SERVICE"):
        executor.attributes[spec_id] = {"stmt": f"{spec_id} " * 20}
    reader = executor.reader(Gateway())
    every = await reader.expand(PROJECT, SEEDS, budget_tokens=20000)
    budget = max(100, every.items[0].tokens + every.items[1].tokens + every.items[2].tokens - 1)

    cut = await reader.expand(PROJECT, SEEDS, budget_tokens=budget)

    assert every.left_out == 0 and len(every.items) > 3
    assert cut.items[:2] == every.items[:2] and every.items[2] not in cut.items
    assert cut.tokens <= budget and cut.items == within_budget(every.items, budget).items
    assert cut.left_out == len(every.items) - len(cut.items) > 0


async def test_expand_is_graph_only_without_a_gateway_and_the_same_graph_gives_the_same_answer() -> None:
    executor = shop_executor()

    first = await executor.reader().expand(PROJECT, SEEDS)
    second = await executor.reader().expand(PROJECT, SEEDS)

    assert first == second and {entry.via for entry in first.items} == {"graph"}
    assert "read_node_vectors" not in [name for name, _ in executor.calls]


async def test_expand_never_spreads_through_a_module_to_its_other_members() -> None:
    found = await shop_executor().reader().expand(PROJECT, ["FEAT::PLACE_ORDER"], budget_tokens=20000)

    # the Orders service module is one hop away; the walk stops there, and without vectors its other members are
    # not lent either (test_graph_expand_owners.py)
    assert [entry.spec_id for entry in found.items] == ["MOD::ORDERS_SERVICE", "REQ::FR::PLACE_ORDER"]


async def test_unknown_seeds_answer_an_empty_expansion() -> None:
    found = await shop_executor().reader(Gateway()).expand(PROJECT, ["FEAT::NOPE"])

    assert (found.items, found.tokens, found.left_out) == ((), 0, 0)


@pytest.mark.parametrize(
    ("spec_ids", "budget", "code"),
    [
        ([], 3000, "invalid_spec_ids"),
        ("FEAT::CHECKOUT", 3000, "invalid_spec_ids"),
        (["X"] * 501, 3000, "invalid_spec_ids"),
        ([1], 3000, "invalid_spec_ids"),
        (["FEAT::CHECKOUT"], 99, "invalid_budget"),
        (["FEAT::CHECKOUT"], 20001, "invalid_budget"),
        (["FEAT::CHECKOUT"], True, "invalid_budget"),
    ],
)
async def test_expand_checks_its_arguments_before_any_read(spec_ids: Any, budget: Any, code: str) -> None:
    executor = shop_executor()

    with pytest.raises(ConfigurationError) as error:
        await executor.reader().expand(PROJECT, spec_ids, budget_tokens=budget)

    assert error.value.code == code and executor.calls == []


@pytest.mark.parametrize("exclude", ["FEAT::CHECKOUT", [1], ["X"] * 501, None])
async def test_expand_checks_exclude_before_any_read(exclude: Any) -> None:
    executor = shop_executor()

    with pytest.raises(ConfigurationError) as error:
        await executor.reader().expand(PROJECT, SEEDS, exclude=exclude)

    assert error.value.code == "invalid_exclude" and executor.calls == []


async def test_expand_compares_one_centroid_and_keeps_ten_vector_candidates_past_the_left_out_nearest_ones() -> None:
    graph = TraceGraph()
    seeds = ["FEAT::S0", "FEAT::S1"]
    excluded = [f"FEAT::E{index:02d}" for index in range(10)]
    others = [f"FEAT::C{index:02d}" for index in range(60)]
    for spec_id in (*seeds, *excluded, *others):
        graph.node(spec_id, "Feature", name="n")
    executor = ReadsExecutor(graph)
    # the seeds' centroid is (1, 0); the excluded nodes and the seeds are nearer to it than any other node
    executor.vectors = {
        "FEAT::S0": (MODEL, (1.0, 0.01)),
        "FEAT::S1": (MODEL, (1.0, -0.01)),
        **{spec_id: (MODEL, (1.0, 0.001 * index)) for index, spec_id in enumerate(excluded)},
        **{spec_id: (MODEL, (1.0, 0.02 + 0.001 * index)) for index, spec_id in enumerate(others)},
    }

    found = await executor.reader(Gateway()).expand(PROJECT, seeds, exclude=excluded, budget_tokens=20000)

    assert [entry.spec_id for entry in found.items] == others[:SIMILAR_ONLY_MAX]
    assert {entry.via for entry in found.items} == {"similar"}
    names = [name for name, _ in executor.calls]
    assert names.count("search_nodes_vector_exact") == 1  # one nearest read for the centroid, not one per seed


def big_graph(size: int) -> TraceGraph:
    """``size`` features in a ring with two chords each, every 50th one also part of one of 20 modules."""

    graph = TraceGraph()
    for index in range(20):
        graph.node(f"MOD::M{index:02d}", "Module")
    for index in range(size):
        graph.node(f"FEAT::F{index:05d}", "Feature", name=f"Feature number {index}")
    for index in range(size):
        graph.edge("DEPENDS_ON", f"FEAT::F{index:05d}", f"FEAT::F{(index + 1) % size:05d}", basis="stated")
        graph.edge("DEPENDS_ON", f"FEAT::F{index:05d}", f"FEAT::F{(index * 7 + 3) % size:05d}", basis="implied")
        graph.edge("DEPENDS_ON", f"FEAT::F{index:05d}", f"FEAT::F{(index * 13 + 5) % size:05d}", basis="stated")
        if index % 50 == 0:
            graph.edge("PART_OF", f"FEAT::F{index:05d}", f"MOD::M{index % 20:02d}", basis="stated")
    return graph


async def test_expand_does_bounded_work_on_a_graph_of_four_thousand_nodes(record_property: Any) -> None:
    executor = ReadsExecutor(big_graph(4000))
    executor.vectors = {f"FEAT::F{index:05d}": (MODEL, (1.0, index / 4000)) for index in range(0, 4000, 3)}
    seeds = [f"FEAT::F{index:05d}" for index in range(0, 600, 10)]

    started = time.perf_counter()
    found = await executor.reader(Gateway()).expand(PROJECT, seeds, budget_tokens=8000)
    record_property("expand_seconds", time.perf_counter() - started)  # a measurement, not a gate

    assert found.items and found.tokens <= 8000
    near_seeds, _ = await near([(e["source"], e["target"]) for e in executor.graph.edges], seeds, set())
    assert len(near_seeds.nodes) == SUBGRAPH_MAX_NODES and ITERATIONS == 30
    names = [name for name, _ in executor.calls]
    # the walk's reads and one of the owners' members
    assert names.count("read_trace_hop") <= 5 and names.count("search_nodes_vector_exact") == 1
    assert "read_edges" not in names and "read_nodes" not in names  # nothing read project-wide
    assert len(names) <= 13
