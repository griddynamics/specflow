"""What to read next around seed nodes (api.md "Read and search", ``GraphReader.expand``), and the stored-vector
neighbours of ``GraphReader.similar``.

No model call: the graph part is a personalised PageRank over the trusted domain edges near the seeds, the vector
part compares stored vectors, and tokens are estimated as characters / 4. The constants are the fixed defaults of
api.md (A40); the same graph gives the same answer (ties by ``spec_id``).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from graph.core.hops import HopNode, HopRead

RESTART: Final = 0.15
ITERATIONS: Final = 30
SUBGRAPH_HOPS: Final = 3
SUBGRAPH_MAX_NODES: Final = 2000
HUB_PASS: Final = 0.2
"""The share of its rank a spine root passes on to its neighbours; the rest returns to the seeds."""
GRAPH_WEIGHT: Final = 0.7
VECTOR_WEIGHT: Final = 0.3
VECTOR_CANDIDATES: Final = 50
MAX_RANKED: Final = 200
MIN_BUDGET: Final = 100
MAX_BUDGET: Final = 20000
CHARS_PER_TOKEN: Final = 4
MAX_SEED_IDS: Final = 500
MAX_SIMILAR_SOURCES: Final = 50

Via = Literal["graph", "similar", "both"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class SimilarItem(_Frozen):
    """A node whose stored vector reads like the source ``like`` (its best score); ``aliases`` for plan joins."""

    spec_id: str
    label: str
    name: str
    score: float
    like: str
    aliases: tuple[str, ...] = ()


class SimilarResult(_Frozen):
    sources: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    items: tuple[SimilarItem, ...] = ()


class ExpansionItem(_Frozen):
    spec_id: str
    label: str
    name: str
    score: float
    via: Via
    text: str
    tokens: int


class Expansion(_Frozen):
    """The ranked nodes taken within the budget, their ``tokens`` and how many ranked nodes were ``left_out``."""

    items: tuple[ExpansionItem, ...] = ()
    tokens: int = 0
    left_out: int = 0


def estimate_tokens(*texts: str) -> int:
    """Characters of ``texts`` divided by 4, rounded up."""

    return math.ceil(sum(len(text) for text in texts) / CHARS_PER_TOKEN)


def adjacency(edges: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    """Each node's neighbours over ``edges`` taken either way (a self loop is no neighbour)."""

    found: dict[str, set[str]] = defaultdict(set)
    for first, second in edges:
        if first != second:
            found[first].add(second)
            found[second].add(first)
    return found


@dataclass(frozen=True, slots=True)
class Subgraph:
    """The nodes near the seeds (nearest first), their neighbours among them and the spine roots among them."""

    nodes: tuple[str, ...]
    neighbours: Mapping[str, Collection[str]]
    roots: frozenset[str]


async def subgraph(read: HopRead, seeds: Sequence[HopNode]) -> Subgraph:
    """The seeds and the nodes within 3 hops of them, nearest first then by ``spec_id``, at most 2000, read frontier
    by frontier; the walk never leaves a spine root that is no seed (a hub is reached, not walked through). At most
    4 reads: the last reads the farthest nodes and the reached roots, for the edges among the kept nodes."""

    starts = {node.spec_id for node in seeds}
    level = sorted({node.uuid: node for node in seeds}.values(), key=lambda node: node.spec_id)[:SUBGRAPH_MAX_NODES]
    order = [node.spec_id for node in level]
    seen = {node.uuid for node in level}
    roots: dict[str, HopNode] = {}
    edges: list[tuple[str, str]] = []
    for hop in range(SUBGRAPH_HOPS + 1):
        roots |= {node.uuid: node for node in level if node.root and node.spec_id not in starts}
        room = SUBGRAPH_MAX_NODES - len(order)
        last = hop == SUBGRAPH_HOPS or room <= 0 or not level
        walked = {node.uuid: node.spec_id for node in level if node.spec_id in starts or not node.root}
        if last:
            walked |= {uuid: node.spec_id for uuid, node in roots.items()}
        reached: dict[str, HopNode] = {}
        for frontier, node in await read(list(walked)) if walked else []:
            edges.append((walked[frontier], node.spec_id))
            if node.uuid not in seen:
                reached[node.uuid] = node
        if last:
            break
        level = sorted(reached.values(), key=lambda node: node.spec_id)[:room]
        order.extend(node.spec_id for node in level)
        seen |= {node.uuid for node in level}
    kept = set(order)
    neighbours = adjacency((first, second) for first, second in edges if first in kept and second in kept)
    return Subgraph(nodes=tuple(order), neighbours=neighbours, roots=frozenset(node.spec_id for node in roots.values()))


def personalised_pagerank(
    nodes: Sequence[str], neighbours: Mapping[str, Collection[str]], seeds: Collection[str], roots: Collection[str]
) -> dict[str, float]:
    """PageRank restarting at the seeds (probability 0.15, 30 iterations) over ``nodes`` and the edges among them.

    A spine root passes on 0.2 of its rank and returns the rest to the seeds; a node without a neighbour in
    ``nodes`` returns all of it.
    """

    index = {spec_id: position for position, spec_id in enumerate(nodes)}
    restart = [0.0] * len(nodes)
    starts = [index[spec_id] for spec_id in seeds if spec_id in index]
    if not starts:
        return {}
    for position in starts:
        restart[position] = 1.0 / len(starts)
    links = [sorted(index[other] for other in neighbours.get(spec_id, ()) if other in index) for spec_id in nodes]
    passes = [HUB_PASS if spec_id in roots and spec_id not in seeds else 1.0 for spec_id in nodes]
    rank = list(restart)
    for _ in range(ITERATIONS):
        returned = 0.0
        following = [RESTART * value for value in restart]
        for position, value in enumerate(rank):
            moving = (1.0 - RESTART) * value
            targets = links[position]
            if not targets:
                returned += moving
                continue
            share = moving * passes[position] / len(targets)
            returned += moving * (1.0 - passes[position])
            for target in targets:
                following[target] += share
        for position in starts:
            following[position] += returned * restart[position]
        rank = following
    return {spec_id: rank[position] for spec_id, position in index.items()}


def mean_vector(vectors: Sequence[Sequence[float]]) -> list[float] | None:
    """The mean of ``vectors``: the centroid ``expand`` compares with (cosine ignores its length, so it is not scaled);
    None without a vector, when the lengths differ or when the mean is zero (no direction)."""

    if not vectors or len({len(vector) for vector in vectors}) != 1:
        return None
    mean = [sum(values) / len(vectors) for values in zip(*vectors, strict=True)]
    return mean if any(mean) else None


def best_scores(hits: Iterable[tuple[str, str, float]], excluded: Collection[str]) -> dict[str, tuple[float, str]]:
    """Per hit node, its best score and the source that gave it (ties: the lowest source ``spec_id``), from
    ``(source, spec_id, score)`` hits; ``excluded`` nodes are left out."""

    best: dict[str, tuple[float, str]] = {}
    for source, spec_id, score in hits:
        if spec_id in excluded:
            continue
        current = best.get(spec_id)
        if current is None or score > current[0] or (score == current[0] and source < current[1]):
            best[spec_id] = (score, source)
    return best


def top_scores(scores: Mapping[str, float], count: int) -> dict[str, float]:
    """The ``count`` best of ``scores``, by score descending then ``spec_id``."""

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:count]
    return dict(ranked)


def ranked_scores(graph: Mapping[str, float], vector: Mapping[str, float]) -> list[tuple[str, float, Via]]:
    """0.7 x PageRank / the highest PageRank + 0.3 x vector score per node, best first then by ``spec_id``; nodes
    scoring 0 are left out, at most 200."""

    highest = max(graph.values(), default=0.0)
    ranked: list[tuple[str, float, Via]] = []
    for spec_id in set(graph) | set(vector):
        rank = graph.get(spec_id, 0.0) / highest if highest > 0 else 0.0
        similarity = vector.get(spec_id, 0.0)
        score = GRAPH_WEIGHT * rank + VECTOR_WEIGHT * similarity
        if score <= 0:
            continue
        via: Via = "both" if rank > 0 and similarity > 0 else "graph" if rank > 0 else "similar"
        ranked.append((spec_id, score, via))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[:MAX_RANKED]


def within_budget(items: Sequence[ExpansionItem], budget_tokens: int) -> Expansion:
    """``items`` in rank order until the next would pass ``budget_tokens``."""

    taken: list[ExpansionItem] = []
    total = 0
    for item in items:
        if total + item.tokens > budget_tokens:
            break
        taken.append(item)
        total += item.tokens
    return Expansion(items=tuple(taken), tokens=total, left_out=len(items) - len(taken))
