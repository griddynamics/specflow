"""Live graph-store tests of the agent reads of ``GraphReader``: ``hops`` (and the search near a node it ranks),
``similar``, ``evidence``, ``paths`` and ``expand`` on the store the switch selects (Neo4j or PostgreSQL), through the
real templates of that store (``read_trace_hop``, ``read_edges``, ``read_node_vectors``, the vector search,
``read_fragments``, ``read_nodes_for_lines``).

Each answer is compared with the same read over the same graph offline (``graph_reads_support.ReadsExecutor``), so the
store and the in-memory executor are held to one contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import pytest

from graph.core import GraphReader
from graph_probes import requires_live_graph
from graph_probes.base import EdgeSpec, NodeSpec, select
from graph_reads_support import PROJECT, ReadsExecutor
from graph_support import GraphProject, live_embedding_dim, live_llm_config, shipped_ontology
from graph_trace_support import TraceGraph, shop_graph, write_trace_graph

# The graph fixtures (graph_project) live in graph_support.
pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

DIM: Final = live_embedding_dim()
MODEL: Final = live_llm_config().embedding_model
VECTORS: Final = {
    "FEAT::CHECKOUT": (1.0, 0.0, 0.0),
    "REQ::FR::CHECKOUT": (0.9, 0.1, 0.0),
    "FEAT::CHECKOUT_REVIEW": (0.95, 0.3, 0.0),
    "FEAT::CARD_PAYMENT": (0.8, 0.6, 0.0),
    "REQ::FR::CARD_PAYMENT": (0.6, 0.8, 0.0),
    "MOD::WEB_SHOP": (0.0, 0.0, 1.0),
}
STMTS: Final = {"FEAT::CARD_PAYMENT": "Customers pay by card when they check out."}
LINES: Final = ("# Shop", "Intro line", "Checkout is fast.", "It runs in the EU.", "Cards are accepted.", "Last line")


class Gateway:
    """Names the live embedding model; the reads under test never embed."""

    config = live_llm_config()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError(f"no embedding call ({len(texts)} texts)")


def padded(vector: Sequence[float]) -> list[float]:
    return [*map(float, vector), *([0.0] * (DIM - len(vector)))]


def offline() -> ReadsExecutor:
    """The same graph as ``live_reader`` writes, for the in-memory executor."""

    executor = ReadsExecutor(shop_graph())
    executor.vectors = {spec_id: (MODEL, tuple(padded(vector))) for spec_id, vector in VECTORS.items()}
    executor.attributes = {spec_id: {"stmt": stmt} for spec_id, stmt in STMTS.items()}
    executor.fragments = [
        {"fragment_id": "f1", "document_id": "d", "text": "\n".join(LINES[:2]), "line_start": 1, "line_end": 2},
        {"fragment_id": "f2", "document_id": "d", "text": "\n".join(LINES[2:5]), "line_start": 3, "line_end": 5},
        {"fragment_id": "f3", "document_id": "d", "text": LINES[5], "line_start": 6, "line_end": 6},
    ]
    for item in executor.fragments:
        item["status"] = "active"
    executor.provenance = {
        "CON::EU_REGION": [
            {"document_id": "d", "path": "docs/shop.md", "fragment_id": "f2", "line_start": 4, "line_end": 4}
        ],
        "REQ::Q::CHECKOUT_SPEED": [
            {"document_id": "d", "path": "docs/shop.md", "fragment_id": "f2", "line_start": 3, "line_end": 4}
        ],
    }
    return executor


async def live_reader(project: GraphProject) -> GraphReader:
    """The shop graph with vectors, statements, one document of three fragments and two nodes' evidence."""

    ontology = shipped_ontology()
    graph: TraceGraph = shop_graph()
    pid = project.project_id
    await write_trace_graph(project.writer(ontology=ontology), pid, graph)
    for spec_id, vector in VECTORS.items():
        await project.update_nodes(
            select("SpecNode", spec_id=spec_id), {"embedding": padded(vector), "embedding_model": MODEL}
        )
    for spec_id, stmt in STMTS.items():
        await project.update_nodes(select("SpecNode", spec_id=spec_id), {"stmt": stmt})
    fragments = offline().fragments
    await project.create_nodes(
        [
            NodeSpec(("Document",), {"document_id": "d", "spec_id": "DOC::d", "path": "docs/shop.md"}),
            *(NodeSpec(("Fragment",), {**item, "spec_id": f"FRG::{item['fragment_id']}"}) for item in fragments),
        ]
    )
    document = select("Document", document_id="d")
    await project.create_edges(
        [
            *(
                EdgeSpec("CONTAINS", document, select("Fragment", fragment_id=item["fragment_id"]))
                for item in fragments
            ),
            EdgeSpec(
                "REFERENCED_BY",
                select("SpecNode", spec_id="CON::EU_REGION"),
                select("Fragment", fragment_id="f2"),
                {"line_start": 4, "line_end": 4},
            ),
            EdgeSpec(
                "REFERENCED_BY",
                select("SpecNode", spec_id="REQ::Q::CHECKOUT_SPEED"),
                select("Fragment", fragment_id="f2"),
                {"line_start": 3, "line_end": 4},
            ),
        ]
    )
    await project.probe.await_indexes(60)
    return project.reader(ontology=ontology, gateway=Gateway())


async def both(project: GraphProject, read: str, *args: Any, **options: Any) -> tuple[Any, Any]:
    live = await live_reader(project)
    found = await getattr(live, read)(project.project_id, *args, **options)
    expected = await getattr(offline().reader(Gateway()), read)(PROJECT, *args, **options)
    return found, expected


async def test_hops_and_the_search_near_a_node_on_the_live_store(graph_project: GraphProject) -> None:
    await live_reader(graph_project)
    live = graph_project.reader(ontology=shipped_ontology())  # text-only search: no gateway
    pid = graph_project.project_id
    hits = await live.find_nodes(pid, "checkout card payment", limit=10)
    ids = [hit.node.spec_id for hit in hits]

    found = await live.hops(pid, ["FEAT::CARD_PAYMENT"], ids, max_hops=3)
    expected = await offline().reader().hops(PROJECT, ["FEAT::CARD_PAYMENT"], ids, max_hops=3)

    assert {"FEAT::CHECKOUT", "FEAT::CARD_PAYMENT"} <= set(ids)
    assert found == expected and found["FEAT::CARD_PAYMENT"] == 0 and found["FEAT::CHECKOUT"] == 1
    assert "FEAT::GIFT_CARDS" not in await live.hops(pid, ["FEAT::CHECKOUT"], ["FEAT::GIFT_CARDS"], max_hops=4)


async def test_similar_on_the_live_store(graph_project: GraphProject) -> None:
    found, expected = await both(graph_project, "similar", ["FEAT::CHECKOUT", "REQ::FR::CHECKOUT", "FEAT::NOPE"])

    assert [(item.spec_id, item.like) for item in found.items] == [
        ("FEAT::CHECKOUT_REVIEW", "REQ::FR::CHECKOUT"),
        ("REQ::FR::CARD_PAYMENT", "REQ::FR::CHECKOUT"),
    ]
    assert (found.sources, found.unknown) == (expected.sources, expected.unknown)
    assert [item.spec_id for item in found.items] == [item.spec_id for item in expected.items]
    for live, offline_item in zip(found.items, expected.items, strict=True):
        assert live.score == pytest.approx(offline_item.score, abs=1e-5)


async def test_evidence_on_the_live_store(graph_project: GraphProject) -> None:
    found, expected = await both(graph_project, "evidence", "CON::EU_REGION", context_lines=2)

    assert found == expected
    assert found is not None and found[0].quote == "It runs in the EU."
    assert found[0].before == ("Intro line", "Checkout is fast.") and found[0].after == (
        "Cards are accepted.",
        "Last line",
    )
    assert [other.spec_id for other in found[0].others] == ["REQ::Q::CHECKOUT_SPEED"]


@pytest.mark.parametrize(
    ("from_id", "to_id"),
    [
        ("MOD::PAYMENTS_SERVICE", "MOD::STRIPE"),
        ("FEAT::CARD_PAYMENT", "FEAT::CHECKOUT"),
        ("MOD::PAYMENTS_SERVICE", "CON::STRIPE"),
    ],
)
async def test_paths_on_the_live_store(graph_project: GraphProject, from_id: str, to_id: str) -> None:
    found, expected = await both(graph_project, "paths", from_id, to_id)

    assert found == expected


async def test_expand_on_the_live_store(graph_project: GraphProject) -> None:
    found, expected = await both(graph_project, "expand", ["FEAT::CHECKOUT", "REQ::FR::CHECKOUT"], budget_tokens=3000)

    assert [(item.spec_id, item.via, item.text, item.tokens) for item in found.items] == [
        (item.spec_id, item.via, item.text, item.tokens) for item in expected.items
    ]
    for live, offline_item in zip(found.items, expected.items, strict=True):
        assert live.score == pytest.approx(offline_item.score, abs=1e-5)
    assert found.items[0].spec_id == "FEAT::CARD_PAYMENT" and {"graph", "similar"} <= {i.via for i in found.items}
