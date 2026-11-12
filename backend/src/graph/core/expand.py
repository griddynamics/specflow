"""What to read next around seed nodes (api.md "Read and search", ``GraphReader.expand``), and the stored-vector
neighbours of ``GraphReader.similar``.

The graph part is a personalised PageRank over the trusted domain edges near the seeds, the vector part compares
stored vectors (no model call; a ``query`` adds one embedding call that aims it), and the owners near the seeds lend
their members that read like the story; tokens are estimated as characters / 4. The constants are the fixed defaults
of api.md (A40); the same graph gives the same answer (ties by ``spec_id``).
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
VECTOR_WEIGHT: Final = 0.3
VECTOR_CANDIDATES: Final = 50
SIMILAR_ONLY_BAND: Final = 0.05
"""A node found by its vector alone is kept only within this vector score of the best candidate's: Neo4j's
``(1 + cos) / 2``, so 0.1 of raw cosine. Unrelated text of one project sits within a narrow band of the seeds
(about 0.62 to 0.80 on the QA shop), so an absolute floor cannot tell the tail from a near match."""
SIMILAR_ONLY_MAX: Final = 10
"""At most this many nodes found by their vector alone."""
MEMBER_BAND: Final = 0.15
"""An owner's member, and an owner found by its vector, is kept only within this vector score of the best
candidate's: wider than ``SIMILAR_ONLY_BAND``, since membership is a graph tie the vector only ranks."""
MEMBERS_MAX: Final = 3
"""At most this many members of the owners near the seeds, best vector score first."""
QUERY_VECTOR_WEIGHT: Final = 0.6
"""With a ``query``, the vector part's weight (the graph part's is the rest): the question ranks over nearness."""
QUERY_SHARE: Final = 2
"""With a ``query``, the vector part compares with the mean of the seeds' centroid and this many copies of the
query's vector: the question weighs twice the story."""
OWNERS_BY_VECTOR_MAX: Final = 20
"""At most this many vector candidates are read for members (owners found by meaning, not by an edge)."""
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
    """A ranked node with its text and tokens; ``document`` is the path of its first source document (None without
    one), so two items from different documents or revisions can be told apart."""

    spec_id: str
    label: str
    name: str
    score: float
    via: Via
    text: str
    tokens: int
    document: str | None = None


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
    """The nodes near the seeds (nearest first), their neighbours among them and the spine roots among them
    (``root_uuids``: the same roots by uuid, for the read of their members)."""

    nodes: tuple[str, ...]
    neighbours: Mapping[str, Collection[str]]
    roots: frozenset[str]
    root_uuids: tuple[str, ...] = ()


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
    return Subgraph(
        nodes=tuple(order),
        neighbours=neighbours,
        roots=frozenset(node.spec_id for node in roots.values()),
        root_uuids=tuple(sorted(roots)),
    )


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


def ranked_scores(
    graph: Mapping[str, float],
    vector: Mapping[str, float],
    members: Mapping[str, float] | None = None,
    *,
    aimed: bool = False,
) -> list[tuple[str, float, Via]]:
    """0.7 x PageRank / the highest PageRank + 0.3 x vector score per node, best first then by ``spec_id``; nodes
    scoring 0 are left out, at most 200. A node with a vector score alone is kept only within 0.05 of the best
    vector score, at most 10 of them. ``members`` (an owner's member and its vector score, see ``member_scores``) are
    found by a graph tie and ranked by meaning: ``both``. ``aimed`` (a query) weighs 0.4 x PageRank and 0.6 x the
    vector score's place in the 0.15 below the best one (0 at the bottom, 1 at the best), so the question decides
    within the narrow band where the project's texts sit."""

    members = members or {}
    weight = QUERY_VECTOR_WEIGHT if aimed else VECTOR_WEIGHT
    best = max((*vector.values(), *members.values()), default=0.0)
    highest = max(graph.values(), default=0.0)
    ranked: list[tuple[str, float, Via]] = []
    ranked_ids = {spec_id for spec_id, rank in graph.items() if rank > 0}
    for spec_id in ranked_ids | _similar_only(ranked_ids | set(members), vector) | set(members):
        rank = graph.get(spec_id, 0.0) / highest if highest > 0 else 0.0
        similarity = members.get(spec_id) or vector.get(spec_id, 0.0)
        meaning = max(0.0, similarity - best + MEMBER_BAND) / MEMBER_BAND if aimed else similarity
        score = (1.0 - weight) * rank + weight * meaning
        if score <= 0:
            continue
        via: Via = "both" if (rank > 0 and similarity > 0) or spec_id in members else "graph" if rank > 0 else "similar"
        ranked.append((spec_id, score, via))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[:MAX_RANKED]


def _similar_only(ranked: Collection[str], vector: Mapping[str, float]) -> set[str]:
    """The nodes of ``vector`` outside ``ranked`` within 0.05 of its best score, at most 10, best first."""

    floor = max(vector.values(), default=0.0) - SIMILAR_ONLY_BAND
    alone = {spec_id: score for spec_id, score in vector.items() if spec_id not in ranked and score >= floor}
    return set(top_scores(alone, SIMILAR_ONLY_MAX))


def vector_owners(hits: Sequence[tuple[str, str, float]], skip: Collection[str]) -> list[str]:
    """The uuids of the vector candidates ``(uuid, spec_id, score)`` outside ``skip`` (the left-out nodes) within
    0.15 of the best such score, at most 20, best first: the nodes read for members because they read like the
    seeds, though no trusted edge may lead to them (or the walk stopped at them)."""

    outside = [(uuid, score) for uuid, spec_id, score in hits if spec_id not in skip]
    floor = max((score for _, score in outside), default=0.0) - MEMBER_BAND
    return [uuid for uuid, score in sorted(outside, key=lambda hit: -hit[1]) if score >= floor][:OWNERS_BY_VECTOR_MAX]


def member_scores(members: Iterable[str], scores: Mapping[str, float], skip: Collection[str]) -> dict[str, float]:
    """The vector scores of the ``members`` outside ``skip`` (the subgraph and the left-out nodes) within 0.15 of the
    best of ``scores`` (the candidates, left-out nodes already gone), at most 3, best first then by ``spec_id``; a
    member without a vector score is not ranked."""

    floor = max(scores.values(), default=0.0) - MEMBER_BAND
    found = {spec_id: scores[spec_id] for spec_id in set(members) - set(skip) if spec_id in scores}
    return top_scores({spec_id: score for spec_id, score in found.items() if score >= floor and score > 0}, MEMBERS_MAX)


def within_budget(items: Sequence[ExpansionItem], budget_tokens: int) -> Expansion:
    """``items`` in rank order, each taken while it fits in what is left of ``budget_tokens`` (a long item is
    skipped, the shorter ones after it still fit)."""

    taken: list[ExpansionItem] = []
    total = 0
    for item in items:
        if total + item.tokens > budget_tokens:
            continue
        taken.append(item)
        total += item.tokens
    return Expansion(items=tuple(taken), tokens=total, left_out=len(items) - len(taken))
