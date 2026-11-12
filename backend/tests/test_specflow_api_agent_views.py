"""The pure parts of the spec-graph routes: which edges ``get_node`` shows and how it counts the rest
(``api.agent_views.shown_edges``, ``node_edges``), and how ``search_spec near`` re-ranks hits by hops
(``api.routers.agent_spec._near_first``)."""

from __future__ import annotations

import pytest

from api.agent_views import node_edges, shown_edges
from api.routers.agent_spec import _near_first
from graph.core.reader import GraphEdge, GraphNode, NodeHit

NODE = "FEAT::N"


def edge(relation: str, source: str, target: str, basis: str | None = "stated") -> GraphEdge:
    return GraphEdge(relation=relation, source=source, target=target, basis=basis)


EDGES = [  # stored in no useful order
    edge("DEPENDS_ON", NODE, "FEAT::C", "inferred"),
    edge("DEPENDS_ON", NODE, "FEAT::B"),
    edge("PART_OF", NODE, "MOD::M", "implied"),
    edge("DEPENDS_ON", NODE, "FEAT::A"),
    edge("CONSTRAINED_BY", "REQ::X", NODE, None),  # no basis reads as stated
    edge("DEPENDS_ON", "FEAT::A", NODE),
]


def shown(edges: list[GraphEdge], limit: int = 50) -> list[tuple[str, str, str, str | None]]:
    out = []
    for item in shown_edges(edges, NODE, limit):
        direction, other = ("out", item.target) if item.source == NODE else ("in", item.source)
        out.append((item.relation, other, direction, item.basis))
    return out


@pytest.mark.parametrize("edges", [EDGES, list(reversed(EDGES))])
def test_shown_edges_are_stated_first_then_by_relation_other_end_and_direction(edges: list[GraphEdge]) -> None:
    assert shown(edges) == [
        ("CONSTRAINED_BY", "REQ::X", "in", None),
        ("DEPENDS_ON", "FEAT::A", "in", "stated"),  # one other end both ways: "in" before "out"
        ("DEPENDS_ON", "FEAT::A", "out", "stated"),
        ("DEPENDS_ON", "FEAT::B", "out", "stated"),
        ("PART_OF", "MOD::M", "out", "implied"),
        ("DEPENDS_ON", "FEAT::C", "out", "inferred"),
    ]
    assert shown(edges, 2) == shown(edges)[:2]


def test_node_edges_list_the_shown_ones_with_their_ends_and_count_every_edge_per_relation_and_direction() -> None:
    ends = {"FEAT::A": GraphNode(spec_id="FEAT::A", uuid="a", label="Feature", name="A")}

    found = node_edges(EDGES, NODE, 3, ends)

    assert [(item.spec_id, item.direction, item.label, item.name) for item in found.items] == [
        ("REQ::X", "in", None, None),  # no end read: no label or name
        ("FEAT::A", "in", "Feature", "A"),
        ("FEAT::A", "out", "Feature", "A"),
    ]
    assert [(count.relation, count.direction, count.count) for count in found.by_relation] == [
        ("CONSTRAINED_BY", "in", 1),
        ("DEPENDS_ON", "in", 1),
        ("DEPENDS_ON", "out", 3),
        ("PART_OF", "out", 1),
    ]
    assert found.total == 6


def test_a_hub_lists_edge_limit_edges_and_counts_all_of_them() -> None:
    hub = [edge("PART_OF", f"FEAT::F{index:02d}", NODE) for index in range(40)]
    hub += [edge("PROVIDED_BY", f"IFC::I{index:02d}", NODE) for index in range(20)]

    found = node_edges(hub, NODE, 25, {})

    assert len(found.items) == 25 and found.total == 60
    assert sum(count.count for count in found.by_relation) == 60
    assert [(count.relation, count.count) for count in found.by_relation] == [("PART_OF", 40), ("PROVIDED_BY", 20)]


def hit(spec_id: str, score: float) -> NodeHit:
    return NodeHit(
        node=GraphNode(spec_id=spec_id, uuid=spec_id, label="Feature", name=spec_id), score=score, matched="fulltext"
    )


def test_hits_within_reach_come_first_by_hops_then_score_and_the_rest_keep_their_order() -> None:
    hits = [hit("H5", 0.99), hit("FAR", 0.98), hit("H3", 0.9), hit("H1", 0.5)]

    ranked = _near_first(hits, {"H1": 1, "H3": 3})

    assert [item.node.spec_id for item in ranked] == ["H1", "H3", "H5", "FAR"]


def test_hits_at_the_same_hop_rank_by_score() -> None:
    ranked = _near_first([hit("LOW", 0.4), hit("HIGH", 0.8), hit("ZERO", 0.9)], {"LOW": 1, "HIGH": 1, "ZERO": 0})

    assert [item.node.spec_id for item in ranked] == ["ZERO", "HIGH", "LOW"]
