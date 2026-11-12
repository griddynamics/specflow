"""Shortest directed chains between two spec nodes (api.md "Read and search", ``GraphReader.paths``).

The walk is the trace walk's steps (trace.md "Walk", rules 4 to 7, trusted edges only) from one node, hop by hop,
keeping every edge that first reaches a node at its lowest hop. A spine root inside a chain is passed only over its
``uses`` and ``used_by`` edges (rule 6), so a chain through a module is a call between modules, never a shared owner.

Without a chain, ``link`` is the shortest connection over the trusted edges of any walked role, either way and
through modules: how the two nodes relate when neither depends on the other, and why the chain is empty.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from graph.core.ontology.compiled import NO_IMPACT
from graph.core.trace import STATED, Direction, TraceNode, TraceWalk, Visit, edge_key

MAX_CHAINS: Final = 3
MIN_PATH_HOPS: Final = 1
MAX_PATH_HOPS: Final = 6
LINK_HOPS: Final = 6
"""The most edges in a ``link``, whatever ``max_hops`` is."""
LINK_MAX_NODES: Final = 2000
"""The link search stops once it has reached more than this many nodes without the end (``search_limit``)."""

PathReason = Literal["same_node", "no_directed_chain", "not_connected", "search_limit"]

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
    chain, or both ends are one node); at most 3 shortest ``chains``, ordered by the ``spec_id``s along them.

    Without a chain, ``reason`` says why: ``same_node``, ``no_directed_chain`` (none within ``max_hops`` either way;
    ``link`` then shows how the nodes connect without a dependency), ``not_connected`` (no connection of at most
    6 edges) or ``search_limit`` (the search for a connection reached 2000 nodes without the end: no proof either
    way)."""

    direction: Direction | None = None
    chains: tuple[PathChain, ...] = ()
    reason: PathReason | None = None
    link: PathChain | None = None


@dataclass(frozen=True, slots=True)
class PathWalk(TraceWalk):
    """The trace walk's steps in one direction, searched for the chains from one node to another."""

    async def chains(self, start: Visit, end: str) -> tuple[PathChain, ...]:
        """The shortest chains from ``start`` to the node of uuid ``end`` within ``depth`` hops, at most 3. The walk
        goes on from a reached node as ``TraceWalk.run`` does: over the edge with the lowest (parent, relation,
        step) that reached it."""

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
            first = {uuid: min(edges.values(), key=edge_key) for uuid, edges in level.items()}
            frontier = [
                Visit(uuid, node.spec_id, node.label, node.via_owner, node.weak)
                for uuid, node in sorted(first.items(), key=lambda item: item[1].spec_id)
                if self.walks_on(node)
            ]
            if not frontier:
                break
        return ()

    async def link(self, start: Visit, end: str) -> tuple[PathChain | None, bool]:
        """The shortest connection from ``start`` to the node of uuid ``end`` over the trusted edges of any walked
        role (``none`` never), either way, every node passed, within 6 edges; ties by the lowest parent
        ``spec_id``, then relation. None when there is none; with ``True`` when the search stopped past 2000
        reached nodes without the end (the cut is made after a whole hop, so a hop never ends half read)."""

        parents: dict[str, tuple[str, PathStep]] = {}
        visited = {start.uuid}
        frontier = {start.uuid: (start.spec_id, start.label)}
        for _ in range(LINK_HOPS):
            if not frontier:
                return None, False
            if len(visited) > LINK_MAX_NODES:
                return None, True
            best: dict[str, tuple[tuple[str, str, str], str, PathStep]] = {}
            for row in await self._read_hop(list(frontier), self.relations):
                found = self._link_step(row, frontier, visited)
                if found is not None and (row["uuid"] not in best or found[0] < best[row["uuid"]][0]):
                    best[row["uuid"]] = found
            parents |= {uuid: (parent, step) for uuid, (_, parent, step) in best.items()}
            if end in best:
                return _chain_back(parents, start.uuid, end), False
            visited |= set(best)
            frontier = {uuid: (step.spec_id, step.label) for uuid, (_, _, step) in best.items()}
        return None, False

    def _link_step(
        self, row: Mapping[str, Any], frontier: Mapping[str, tuple[str, str]], visited: set[str]
    ) -> tuple[tuple[str, str, str], str, PathStep] | None:
        """The step over ``row``'s edge to an unvisited node, keyed for the tie rule, with its parent's uuid."""

        label = self._label(row["labels"])
        if label is None or row["uuid"] in visited or not self._trusted(row["basis"]):
            return None
        parent_id, parent_label = frontier[row["frontier"]]
        outgoing = bool(row["outgoing"])
        source, target = (parent_label, label) if outgoing else (label, parent_label)
        role = self.ontology.impact_role(row["relation"], source, target)
        if role == NO_IMPACT:
            return None
        step = PathStep(
            spec_id=row["spec_id"],
            label=label,
            name=row["name"] or "",
            relation=row["relation"],
            step="forward" if outgoing else "backward",
            role=role,
            basis=row["basis"] or STATED,
        )
        return (parent_id, row["relation"], step.step), row["frontier"], step

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


def _chain_back(parents: Mapping[str, tuple[str, PathStep]], start: str, end: str) -> PathChain:
    """The chain from ``start`` to ``end`` by each node's parent."""

    steps: list[PathStep] = []
    current = end
    while current != start:
        current, step = parents[current]
        steps.append(step)
    return PathChain(hops=len(steps), steps=tuple(reversed(steps)))


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
