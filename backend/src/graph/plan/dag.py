"""Graph utilities over waits: topological order, cycles, strongly connected groups, Mermaid text.

An edge ``(waiter, prerequisite)`` means the waiter needs the prerequisite first. Every function is
iterative and deterministic: among the nodes that may come next, the lowest id comes first.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

_MERMAID_ID: Final = re.compile(r"[^A-Za-z0-9_]")


def topological_order(ids: Iterable[str], edges: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """Prerequisites first, lowest id first among the ready ones; nodes on a cycle are left out."""

    nodes = sorted(set(ids))
    known = frozenset(nodes)
    waiting: dict[str, int] = dict.fromkeys(nodes, 0)
    dependants: dict[str, list[str]] = {node: [] for node in nodes}
    for waiter, prerequisite in set(edges):
        if waiter in known and prerequisite in known and waiter != prerequisite:
            waiting[waiter] += 1
            dependants[prerequisite].append(waiter)
    ready = [node for node in nodes if waiting[node] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for dependant in dependants[node]:
            waiting[dependant] -= 1
            if waiting[dependant] == 0:
                heapq.heappush(ready, dependant)
    return tuple(order)


def find_cycle(ids: Iterable[str], edges: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """One cycle as ``(a, b, ..., a)`` following waits, the lowest start first; ``()`` when acyclic."""

    nodes = sorted(set(ids))
    known = frozenset(nodes)
    successors: dict[str, list[str]] = {node: [] for node in nodes}
    for waiter, prerequisite in sorted(set(edges)):
        if waiter in known and prerequisite in known:
            successors[waiter].append(prerequisite)
    state: dict[str, int] = dict.fromkeys(nodes, 0)
    for start in nodes:
        if state[start]:
            continue
        path: list[str] = [start]
        stack: list[Iterable[str]] = [iter(successors[start])]
        state[start] = 1
        while stack:
            following = next(iter(stack[-1]), None)
            if following is None:
                state[path.pop()] = 2
                stack.pop()
                continue
            if state[following] == 1:
                return (*path[path.index(following) :], following)
            if state[following] == 0:
                state[following] = 1
                path.append(following)
                stack.append(iter(successors[following]))
    return ()


def strongly_connected(ids: Iterable[str], edges: Iterable[tuple[str, str]]) -> tuple[tuple[str, ...], ...]:
    """Strongly connected groups (Tarjan, iterative), each sorted, ordered by their lowest id."""

    nodes = sorted(set(ids))
    known = frozenset(nodes)
    successors: dict[str, list[str]] = {node: [] for node in nodes}
    for waiter, prerequisite in sorted(set(edges)):
        if waiter in known and prerequisite in known:
            successors[waiter].append(prerequisite)
    search = _Tarjan(successors)
    for root in nodes:
        if root not in search.index:
            search.visit(root)
    return tuple(sorted(search.groups, key=lambda group: group[0]))


class _Tarjan:
    """Iterative Tarjan search state over a successor map."""

    def __init__(self, successors: Mapping[str, Sequence[str]]) -> None:
        self.successors = successors
        self.index: dict[str, int] = {}
        self.low: dict[str, int] = {}
        self.on_stack: set[str] = set()
        self.stack: list[str] = []
        self.groups: list[tuple[str, ...]] = []

    def visit(self, root: str) -> None:
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, position = work.pop()
            if position == 0:
                self._enter(node)
            following = self.successors[node]
            if position < len(following):
                work.append((node, position + 1))
                child = following[position]
                if child not in self.index:
                    work.append((child, 0))
                elif child in self.on_stack:
                    self.low[node] = min(self.low[node], self.index[child])
                continue
            self._leave(node)

    def _enter(self, node: str) -> None:
        self.index[node] = self.low[node] = len(self.index)
        self.stack.append(node)
        self.on_stack.add(node)

    def _leave(self, node: str) -> None:
        for child in self.successors[node]:
            if child in self.on_stack:
                self.low[node] = min(self.low[node], self.low[child])
        if self.low[node] != self.index[node]:
            return
        group: list[str] = []
        while True:
            member = self.stack.pop()
            self.on_stack.discard(member)
            group.append(member)
            if member == node:
                break
        self.groups.append(tuple(sorted(group)))


def transitive_prerequisites(start: Iterable[str], edges: Iterable[tuple[str, str]]) -> frozenset[str]:
    """Every node reachable from ``start`` by following waits (a start node only when it is on a cycle)."""

    successors: dict[str, set[str]] = {}
    for waiter, prerequisite in edges:
        successors.setdefault(waiter, set()).add(prerequisite)
    seen: set[str] = set()
    frontier = list(start)
    while frontier:
        node = frontier.pop()
        for prerequisite in successors.get(node, ()):
            if prerequisite not in seen:
                seen.add(prerequisite)
                frontier.append(prerequisite)
    return frozenset(seen)


def to_mermaid(groups: Mapping[str, Sequence[str]], edges: Iterable[tuple[str, str]]) -> str:
    """Mermaid flowchart text: one subgraph per group (a story), an arrow from each prerequisite to its
    waiter. Ids are escaped; labels are the ids themselves, never document text."""

    lines = ["flowchart TD"]
    for group, members in sorted(groups.items()):
        lines.append(f'  subgraph {_mermaid_id(group)}["{group}"]')
        lines.extend(f'    {_mermaid_id(member)}["{member}"]' for member in sorted(members))
        lines.append("  end")
    lines.extend(
        f"  {_mermaid_id(prerequisite)} --> {_mermaid_id(waiter)}" for waiter, prerequisite in sorted(set(edges))
    )
    return "\n".join(lines)


def _mermaid_id(value: str) -> str:
    return "n_" + _MERMAID_ID.sub("_", value)
