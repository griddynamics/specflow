"""The read menu of one investigation: ``GraphSiteReads`` (docs/graph/gap/investigation.md "Read menu"; FR-GINV-03).

Each read maps to one core ``GraphReader`` call, bound to the project the run belongs to: no read takes a project id
from its caller, and the model never sees one. A read naming an id that does not resolve, or a read the store
refuses, returns an empty result with the reason (never an error to the model). Each result is cut to
``READ_TOKENS`` tokens. Reads never write.

The reads remember every spec id and fragment id they showed (``shown_spec_ids``, ``shown_fragment_ids``): the
check of the site drops any id a report names that neither the context pack nor a read showed ("No new ids").
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel

from graph.core.errors import GraphError
from graph.core.llm.gateway import estimate_tokens
from graph.gap.models import ReadResult
from logger.logger import WARNING, get_logger, log_event

logger = get_logger(__name__)

READ_TOKENS: Final = 2000
TRACE_MAX_DEPTH: Final = 3
SIMILAR_MAX: Final = 10
PATH_MAX_HOPS: Final = 4
SEARCH_LIMIT: Final = 10
UNKNOWN_ID: Final = "unknown_id"
EMPTY_ARGUMENT: Final = "empty_argument"
READ_NAMES: Final = (
    "node",
    "evidence",
    "neighbours",
    "trace",
    "paths",
    "similar",
    "find_nodes",
    "find_fragments",
)
_ID_KEYS: Final = frozenset({"spec_id", "source", "target", "parent", "like"})


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    return value


def cut_items(
    items: Iterable[Mapping[str, Any]], budget: int = READ_TOKENS
) -> tuple[tuple[Mapping[str, Any], ...], bool]:
    """The items that fit in ``budget`` tokens (as compact JSON), in order, and whether any was left out."""

    kept: list[Mapping[str, Any]] = []
    used = 0
    for item in items:
        cost = estimate_tokens(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str))
        if used + cost > budget:
            return tuple(kept), True
        kept.append(item)
        used += cost
    return tuple(kept), False


def _collect(value: Any, spec_ids: set[str], fragment_ids: set[str]) -> None:
    """Every spec id and fragment id a read result shows (nested dicts and lists)."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _ID_KEYS and isinstance(item, str):
                spec_ids.add(item)
            elif key == "fragment_id" and isinstance(item, str):
                fragment_ids.add(item)
            elif key == "source_fragment_ids" and isinstance(item, list | tuple):
                fragment_ids.update(str(entry) for entry in item)
            else:
                _collect(item, spec_ids, fragment_ids)
    elif isinstance(value, list | tuple):
        for item in value:
            _collect(item, spec_ids, fragment_ids)


class GraphSiteReads:
    """``SiteReads`` over a core ``GraphReader`` for one project (module docstring)."""

    def __init__(self, reader: Any, project_id: str, *, run_id: str = "") -> None:
        self._reader = reader
        self._project_id = project_id
        self._run_id = run_id
        self.shown_spec_ids: set[str] = set()
        self.shown_fragment_ids: set[str] = set()

    def show(self, *, spec_ids: Iterable[str] = (), fragment_ids: Iterable[str] = ()) -> None:
        """Mark ids as shown to the investigator (the context pack calls this)."""

        self.shown_spec_ids.update(spec_ids)
        self.shown_fragment_ids.update(fragment_ids)

    async def _read(self, read: str, call: Callable[[], Any]) -> ReadResult:
        try:
            found = await call()
        except GraphError as error:
            log_event(logger, WARNING, "graph.gap.read.failed", run_id=self._run_id, read=read, code=error.code)
            return ReadResult(read=read, reason=error.code or "read_failed")
        if found is None:
            return ReadResult(read=read, reason=UNKNOWN_ID)
        raw = found if isinstance(found, list | tuple) else [found]
        items, truncated = cut_items([_plain(item) for item in raw])
        _collect(items, self.shown_spec_ids, self.shown_fragment_ids)
        return ReadResult(read=read, items=items, truncated=truncated)

    async def node(self, spec_id: str) -> ReadResult:
        return await self._read("node", lambda: self._reader.node(self._project_id, spec_id))

    async def evidence(self, spec_id: str) -> ReadResult:
        return await self._read("evidence", lambda: self._reader.evidence(self._project_id, spec_id, context_lines=2))

    async def neighbours(self, spec_id: str, relations: Sequence[str] = ()) -> ReadResult:
        async def call() -> Any:
            if await self._reader.node(self._project_id, spec_id) is None:
                return None
            return await self._reader.neighbours(self._project_id, spec_id, list(relations) or None)

        return await self._read("neighbours", call)

    async def trace(self, spec_id: str, direction: Literal["based_on", "affects"], depth: int) -> ReadResult:
        async def call() -> Any:
            result = await self._reader.trace(
                self._project_id, [spec_id], direction=direction, depth=max(1, min(depth, TRACE_MAX_DEPTH))
            )
            return None if spec_id in result.unknown else list(result.nodes)

        return await self._read("trace", call)

    async def paths(self, source: str, target: str) -> ReadResult:
        return await self._read(
            "paths", lambda: self._reader.paths(self._project_id, source, target, max_hops=PATH_MAX_HOPS)
        )

    async def similar(self, query: str, limit: int) -> ReadResult:
        """Near statements of a spec id (stored vectors); a text that names no node is searched as words."""

        wanted = max(1, min(limit, SIMILAR_MAX))
        if not query.strip():
            return ReadResult(read="similar", reason=EMPTY_ARGUMENT)

        async def call() -> Any:
            if await self._reader.node(self._project_id, query) is not None:
                return list((await self._reader.similar(self._project_id, [query], limit=wanted)).items)
            return await self._reader.find_nodes(self._project_id, query, limit=wanted)

        return await self._read("similar", call)

    async def find_nodes(self, words: str, labels: Sequence[str] = ()) -> ReadResult:
        if not words.strip():
            return ReadResult(read="find_nodes", reason=EMPTY_ARGUMENT)
        return await self._read(
            "find_nodes",
            lambda: self._reader.find_nodes(self._project_id, words, list(labels) or None, limit=SEARCH_LIMIT),
        )

    async def find_fragments(self, words: str) -> ReadResult:
        if not words.strip():
            return ReadResult(read="find_fragments", reason=EMPTY_ARGUMENT)
        return await self._read(
            "find_fragments", lambda: self._reader.find_fragments(self._project_id, words, limit=SEARCH_LIMIT)
        )
