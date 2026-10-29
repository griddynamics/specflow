"""store_work.yml's `async def` handle scope beside a TypeVar generic: the generic
spelling leaves semgrep's `async def` patterns matching."""

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def head(values: Sequence[T]) -> T:
    return values[0]


async def nearest(graph: GraphCatalogRepository, text: str) -> object:
    hits = await graph.search(text)
    # ruleid: kit.store_work.caller-shapes-repository-read
    return min(hits, key=lambda hit: hit.distance)


async def nearest_in_store(graph: GraphCatalogRepository, text: str) -> object:
    # ok: kit.store_work.caller-shapes-repository-read
    return await graph.nearest(text)
