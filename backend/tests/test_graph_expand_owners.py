"""``GraphReader.expand`` past the walk: the owners near the seeds lend members that read like the story (QA F22), and
a ``query`` aims the vector part (QA F23). api.md "Read and search"."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from graph.core import ConfigurationError
from graph.core.errors import LLMError
from graph.core.expand import (
    MEMBER_BAND,
    MEMBERS_MAX,
    OWNERS_BY_VECTOR_MAX,
    member_scores,
    ranked_scores,
    vector_owners,
)
from graph_reads_support import MODEL, PROJECT, Gateway, ReadsExecutor
from graph_trace_support import shop_graph

# --- pure parts -----------------------------------------------------------------------------------------------


def test_owners_by_meaning_are_the_candidates_near_the_best_one_outside_the_left_out_nodes() -> None:
    hits = [("u-seed", "SEED", 0.99), ("u-a", "A", 0.9), ("u-b", "B", 0.9 - MEMBER_BAND), ("u-c", "C", 0.7)]

    assert vector_owners(hits, {"SEED"}) == ["u-a", "u-b"]  # C is past the band under the best candidate
    many = [(f"u{index:02d}", f"N{index:02d}", 0.9) for index in range(OWNERS_BY_VECTOR_MAX + 5)]
    assert len(vector_owners(many, set())) == OWNERS_BY_VECTOR_MAX
    assert vector_owners([], set()) == []


def test_members_are_ranked_by_meaning_within_the_band_and_capped() -> None:
    scores = {
        "BEST": 0.9,
        "M1": 0.88,
        "M2": 0.86,
        "M3": 0.8,
        "M4": 0.79,
        "LOW": 0.9 - MEMBER_BAND - 0.01,
        "WALKED": 0.9,
    }
    members = ["M1", "M2", "M3", "M4", "LOW", "WALKED", "NO_VECTOR"]

    found = member_scores(members, scores, {"WALKED"})

    # the walked node keeps its graph rank; one without a vector score is not ranked; at most three
    assert list(found) == ["M1", "M2", "M3"] and len(found) == MEMBERS_MAX
    assert member_scores(members, {}, set()) == {}


def test_a_member_is_found_by_its_owner_and_ranked_by_its_vector() -> None:
    ranked = ranked_scores({"G": 1.0}, {"G": 0.8, "V": 0.85}, {"M": 0.82})

    assert ranked == [
        ("G", pytest.approx(0.7 + 0.3 * 0.8), "both"),
        ("V", pytest.approx(0.3 * 0.85), "similar"),
        ("M", pytest.approx(0.3 * 0.82), "both"),
    ]


def test_a_query_weighs_meaning_by_its_place_in_the_band_under_the_best_score() -> None:
    graph = {"HUB": 1.0, "REQ": 0.1}
    vector = {"HUB": 0.75, "REQ": 0.9}

    plain = ranked_scores(graph, vector)
    aimed = ranked_scores(graph, vector, aimed=True)

    assert [spec_id for spec_id, _, _ in plain] == ["HUB", "REQ"]
    # aimed: 0.4 x rank + 0.6 x (score - (best - 0.15)) / 0.15; HUB sits at the bottom of the band, REQ at the top
    assert aimed == [("REQ", pytest.approx(0.4 * 0.1 + 0.6), "both"), ("HUB", pytest.approx(0.4), "both")]


# --- the read -------------------------------------------------------------------------------------------------


class Embedder(Gateway):
    """A gateway that embeds every text as ``vector``, or fails with ``error``; records the texts."""

    def __init__(self, vector: Sequence[float] = (0.0, 1.0), error: Exception | None = None) -> None:
        super().__init__()
        self.vector = list(vector)
        self.error = error
        self.texts: list[str] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        if self.error is not None:
            raise self.error
        return [self.vector for _ in texts]


def orders_executor() -> ReadsExecutor:
    """The shop graph; Place order is the seed, the Orders service module its owner (a spine root)."""

    executor = ReadsExecutor(shop_graph())
    executor.vectors = {
        "FEAT::PLACE_ORDER": (MODEL, (1.0, 0.0)),
        "REQ::BR::ORDER_LIMIT": (MODEL, (1.0, 0.3)),  # a member of the module, near in meaning
        "FEAT::ORDER_HISTORY_EXPORT": (MODEL, (1.0, 0.4)),  # a member of the module, near in meaning
        "BR::RELEASE": (MODEL, (0.2, 1.0)),  # a member, far in meaning
        "IFC::ORDERS_API": (MODEL, (1.0, 0.35)),  # near, but provided by the module: no member
        "REQ::FR::GIFT_CARDS": (MODEL, (0.0, 1.0)),
    }
    return executor


async def test_the_module_the_walk_reached_lends_its_members_that_read_like_the_seeds() -> None:
    executor = orders_executor()

    found = await executor.reader(Gateway()).expand(PROJECT, ["FEAT::PLACE_ORDER"], budget_tokens=20000)

    vias = {entry.spec_id: entry.via for entry in found.items}
    assert vias["REQ::BR::ORDER_LIMIT"] == "both" and vias["FEAT::ORDER_HISTORY_EXPORT"] == "both"
    assert "BR::RELEASE" not in vias  # past the band under the best candidate
    assert vias.get("IFC::ORDERS_API") != "both"  # near in meaning only: no owner pair to the module
    hops = [params["frontier"] for name, params in executor.calls if name == "read_trace_hop"]
    orders = executor.graph.nodes["MOD::ORDERS_SERVICE"]["uuid"]
    assert orders in hops[-1]  # one read of the owners' members, after the walk


async def test_an_owner_found_by_meaning_lends_its_members_when_no_edge_leads_there() -> None:
    """QA E03: the story's entity has no edge to the feature its rules hang on; the feature reads like it."""

    executor = ReadsExecutor(shop_graph())
    executor.vectors = {
        "REQ::FR::ORPHAN": (MODEL, (1.0, 0.0)),
        "FEAT::GIFT_CARDS": (MODEL, (1.0, 0.05)),
        "REQ::FR::GIFT_CARDS": (MODEL, (1.0, 0.5)),  # only reachable through its feature
    }

    found = await executor.reader(Gateway()).expand(PROJECT, ["REQ::FR::ORPHAN"], budget_tokens=20000)

    vias = {entry.spec_id: entry.via for entry in found.items}
    assert vias == {"FEAT::GIFT_CARDS": "similar", "REQ::FR::GIFT_CARDS": "both"}


async def test_without_vectors_nothing_is_lent_and_no_member_read_is_made() -> None:
    executor = ReadsExecutor(shop_graph())

    found = await executor.reader(Gateway()).expand(PROJECT, ["FEAT::PLACE_ORDER"], budget_tokens=20000)

    assert [entry.spec_id for entry in found.items] == ["MOD::ORDERS_SERVICE", "REQ::FR::PLACE_ORDER"]
    assert [name for name, _ in executor.calls].count("read_trace_hop") == 3  # the walk only


async def test_a_query_is_embedded_once_and_aims_the_ranking() -> None:
    plain_executor, aimed_executor = orders_executor(), orders_executor()
    gateway = Embedder(vector=(0.2, 1.0))  # reads like the far member

    plain = await plain_executor.reader(Gateway()).expand(PROJECT, ["FEAT::PLACE_ORDER"], budget_tokens=20000)
    aimed = await aimed_executor.reader(gateway).expand(
        PROJECT, ["FEAT::PLACE_ORDER"], budget_tokens=20000, query="When is a release made?"
    )

    assert gateway.texts == ["When is a release made"]  # cut like a search text
    assert "BR::RELEASE" not in {entry.spec_id for entry in plain.items}
    assert aimed.items[0].spec_id == "BR::RELEASE"


@pytest.mark.parametrize("query", ["", "   ", None])
async def test_a_blank_query_is_no_query(query: Any) -> None:
    gateway = Embedder()

    found = await orders_executor().reader(gateway).expand(PROJECT, ["FEAT::PLACE_ORDER"], query=query)
    plain = await orders_executor().reader(Gateway()).expand(PROJECT, ["FEAT::PLACE_ORDER"])

    assert gateway.texts == [] and found == plain


async def test_a_failed_embedding_falls_back_to_the_story_alone() -> None:
    gateway = Embedder(error=LLMError("exhausted"))

    found = await orders_executor().reader(gateway).expand(PROJECT, ["FEAT::PLACE_ORDER"], query="release")
    plain = await orders_executor().reader(Gateway()).expand(PROJECT, ["FEAT::PLACE_ORDER"])

    assert gateway.texts == ["release"] and [e.spec_id for e in found.items] == [e.spec_id for e in plain.items]


async def test_a_query_that_is_no_text_is_refused_before_any_read() -> None:
    executor = orders_executor()
    query: Any = ["release"]

    with pytest.raises(ConfigurationError) as error:
        await executor.reader(Embedder()).expand(PROJECT, ["FEAT::PLACE_ORDER"], query=query)

    assert error.value.code == "invalid_query" and executor.calls == []
