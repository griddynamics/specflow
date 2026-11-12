"""Shortest directed chains between two spec nodes (api.md "Read and search", ``GraphReader.paths``).

The walk is the trace walk's steps (trace.md "Walk", rules 4 to 7, trusted edges only) from one node, hop by hop,
keeping every edge that first reaches a node at its lowest hop. A spine root may start or end a chain but is never
walked through, so a chain shows a real dependency, not a shared module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict

from graph.core.trace import Direction, TraceNode, TraceWalk, Visit

MAX_CHAINS: Final = 3

# Per hop (index 0 is hop 1): each node first reached at that hop, by uuid, with the edge from each parent (by
# parent uuid) that reached it; of several edges between one pair the lowest (relation, step) is kept.
Levels = list[dict[str, dict[str, TraceNode]]]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class PathStep(_Frozen):
    """One step of a chain: the node it reaches and the edge it takes."""

    spec_id: str
    label: str
    name: str
    relation: str
    step: str
    role: str
    basis: str


class PathChain(_Frozen):
    hops: int
    steps: tuple[PathStep, ...] = ()


class PathResult(_Frozen):
    """``direction`` ``based_on`` (the first node depends on the second), ``affects`` (the reverse) or ``None`` (no
    chain, or both ends are one node); at most 3 shortest ``chains``, ordered by the ``spec_id``s along them."""

    direction: Direction | None = None
    chains: tuple[PathChain, ...] = ()


@dataclass(frozen=True, slots=True)
class PathWalk(TraceWalk):
    """The trace walk's steps in one direction, searched for the chains from one node to another."""

    async def chains(self, start: Visit, end: str) -> tuple[PathChain, ...]:
        """The shortest chains from ``start`` to the node of uuid ``end`` within ``depth`` hops, at most 3."""

        roots = self.ontology.spine.roots
        levels: Levels = []
        visited = {start.uuid}
        frontier = [start]
        for hop in range(1, self.depth + 1):
            level = await self._level(frontier, hop, visited)
            if not level:
                break
            levels.append(level)
            if end in level:
                return chains_to(start.uuid, end, levels)
            visited |= set(level)
            frontier = [
                Visit(uuid, node.spec_id, node.label, via_owner=False)
                for uuid, edges in level.items()
                for node in (next(iter(edges.values())),)
                if node.label not in roots  # a root is never inside a chain
            ]
            if not frontier:
                break
        return ()

    async def _level(self, frontier: Sequence[Visit], hop: int, visited: set[str]) -> dict[str, dict[str, TraceNode]]:
        by_uuid = {visit.uuid: visit for visit in frontier}
        level: dict[str, dict[str, TraceNode]] = {}
        for row in await self._frontier_rows(frontier):
            parent = by_uuid[row["frontier"]]
            candidate = self._candidate(parent, row, hop, visited)
            if candidate is None:
                continue
            edges = level.setdefault(candidate.uuid, {})
            current = edges.get(parent.uuid)
            node = candidate.node
            if current is None or (node.relation, node.step) < (current.relation, current.step):
                edges[parent.uuid] = node
        return level


def chains_to(start: str, end: str, levels: Levels) -> tuple[PathChain, ...]:
    """The chains from ``start`` to ``end`` (reached at the last level), first by the ``spec_id``s along them."""

    keep: list[set[str]] = [set() for _ in levels]
    keep[-1] = {end}
    for index in range(len(levels) - 1, 0, -1):
        keep[index - 1] = {parent for uuid in keep[index] for parent in levels[index][uuid]}
    found: list[PathChain] = []
    _extend(levels, keep, 0, parent=start, steps=(), found=found)
    return tuple(found)


def _extend(
    levels: Levels,
    keep: Sequence[set[str]],
    index: int,
    *,
    parent: str,
    steps: tuple[TraceNode, ...],
    found: list[PathChain],
) -> None:
    if len(found) >= MAX_CHAINS:
        return
    if index == len(levels):
        found.append(PathChain(hops=len(steps), steps=tuple(_step(node) for node in steps)))
        return
    children = sorted(
        (edges[parent].spec_id, uuid, edges[parent]) for uuid in keep[index] if parent in (edges := levels[index][uuid])
    )
    for _, uuid, node in children:
        _extend(levels, keep, index + 1, parent=uuid, steps=(*steps, node), found=found)


def _step(node: TraceNode) -> PathStep:
    return PathStep(
        spec_id=node.spec_id,
        label=node.label,
        name=node.name,
        relation=node.relation,
        step=node.step,
        role=node.role,
        basis=node.basis,
    )
