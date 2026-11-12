"""Fewest-edge distances between spec nodes (api.md "Read and search", ``GraphReader.hops``).

The walk is a breadth-first search over the trusted domain edges, either direction, read frontier by frontier (one
read per hop). A spine root is a hub that nearly every node reaches, so it may end a chain (as a source or a target)
but is never walked through.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Final

MAX_HOP_IDS: Final = 500
MIN_HOPS: Final = 1
MAX_HOPS: Final = 4


@dataclass(frozen=True, slots=True)
class HopNode:
    """A spec node on a walk; ``root`` when it is a spine root (reached, not walked through unless a start)."""

    uuid: str
    spec_id: str
    root: bool = False


HopRead = Callable[[list[str]], Awaitable[list[tuple[str, HopNode]]]]
"""The other ends of the trusted domain edges at the nodes of ``uuids``, either way: ``(frontier uuid, other end)``."""


async def hop_distances(
    read: HopRead, sources: Sequence[HopNode], targets: Collection[str], max_hops: int
) -> dict[str, int]:
    """The fewest edges from any of ``sources`` to each of ``targets`` (``spec_id``s) reached within ``max_hops``; a
    source is 0. Stops reading once every target is found."""

    wanted = set(targets)
    seen = {node.uuid for node in sources}
    found = {node.spec_id: 0 for node in sources if node.spec_id in wanted}
    frontier = list(sources)
    for hops in range(1, max_hops + 1):
        walked = [node.uuid for node in frontier if hops == 1 or not node.root]
        if not walked or wanted <= found.keys():
            break
        reached: dict[str, HopNode] = {}
        for _, node in await read(walked):
            if node.uuid not in seen:
                reached[node.uuid] = node
        seen |= reached.keys()
        found |= {node.spec_id: hops for node in reached.values() if node.spec_id in wanted}
        frontier = list(reached.values())
    return found
