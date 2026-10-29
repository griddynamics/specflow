"""The hub consolidation pass of Resolve (docs/graph/build.md step 3, ``resolve`` step 4).

The pass shows the model whole blocks of hub nodes instead of single candidate pairs and asks which
of them are one node (``consolidate_hubs.1``). This module holds the block machinery and the decision
memory; ``graph.build.resolve`` builds the items' lines and applies proposals under its vetoes.

1. Order: the caller sorts its items (by decision key) and orders them by one greedy
   nearest-neighbour chain over their ``name_embedding`` vectors (``nn_chain``), so near-duplicates
   sit next to each other.
2. Blocks: the ordered sequence is cut into blocks of at most ``BLOCK_SIZE`` items that overlap by
   ``BLOCK_OVERLAP`` items (``blocks``), so neighbours across a cut are seen together. Each block is
   one ``judge`` call; its lines carry short ids ``n1``, ``n2``, ... in block order.
3. Answers: ``groups`` of ids. An id that names no line of the block, or one already grouped earlier
   in the same answer, is dropped (``unknown_ids``); a group left with fewer than two ids is ignored.
   A failed call proposes nothing (counted; no decision is stored for it).
4. Proposals are applied in block order, then answer order; the caller's ``apply`` enforces the
   vetoes and returns the subsets it really joined.
5. Decision memory (``DecisionMemory``, ``ExtractionStore`` kind ``decisions``): after a judged block,
   every pair inside a joined subset is stored as ``same`` and every other pair of the block with
   ``name_embedding`` cosine at least ``DECISION_COSINE`` as ``different`` (a pair judged in two
   overlapping blocks is ``same`` when either joined it). Key ``<project>|<pass>|<key a>|<key b>``
   (identity keys sorted), fingerprint ``<prompt version>|<judge model>``. Each judged item also gets
   a node entry (``<project>|<pass>.node|<key>``: the keys it was stored ``same`` with), which marks
   it as seen and finds its ``same`` pairs below the cosine floor. In a later build a stored decision
   overrides the model: a stored ``same`` pair is proposed even when the answer did not group it, a
   stored ``different`` pair is a veto inside a proposed group (``decision_overrides``). A block whose
   items were all seen before and whose pairs at cosine at least ``DECISION_COSINE`` all have a
   stored decision is not sent at all (``blocks_skipped``): its stored ``same`` pairs are applied.

Prompts hold the ontology's wording only; document-derived names and evidence lines reach them
inside ``Gateway.untrusted`` blocks, each made safe with ``prompt_name``. Logs carry counts only.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import numpy as np

from graph.build.models import ConsolidationCounters
from graph.build.ports import ExtractionKind, ExtractionStore, JsonObject
from graph.build.vectors import get_many, put_many
from graph.core.errors import LLMError, LLMFatalError
from graph.core.logfields import count_fields
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.consolidate")

HUBS_PROMPT: Final = "consolidate_hubs.1"
_HUBS_LABEL: Final = "hubs"
HUBS_PASS: Final = _HUBS_LABEL
BLOCK_SIZE: Final = 400
BLOCK_OVERLAP: Final = 20
DECISION_COSINE: Final = 0.85
"""Pairs of a judged block at this ``name_embedding`` cosine or more get a stored decision."""
LINE_MAX_CHARS: Final = 160
"""Longest evidence line shown per item (``prompt_name``)."""
CHAIN_MATRIX_ROWS: Final = 4096
MIN_CHAIN_ROWS: Final = 2
"""Rows up to which the chain is the identity order."""
MIN_BLOCK_ITEMS: Final = 2
"""Items a block or a proposed group needs to hold a pair."""
"""Runs up to this many rows order their chain from one cosine matrix; longer ones row by row."""
DECISION_KIND: Final[ExtractionKind] = "decisions"
SHORT_ID: Final = "n"

Pair = tuple[int, int]
"""Two item indexes, lower first."""


def pair(first: int, second: int) -> Pair:
    return (first, second) if first < second else (second, first)


def short_id(position: int) -> str:
    """The id of the item at ``position`` (0-based) of a block: ``n1``, ``n2``, ..."""

    return f"{SHORT_ID}{position + 1}"


def nn_chain(unit: np.ndarray) -> list[int]:
    """Rows of ``unit`` (unit vectors) as a greedy nearest-neighbour chain: row 0 first, then each
    time the unvisited row with the highest cosine to the last one (ties: the lower row)."""

    count = unit.shape[0]
    if count <= MIN_CHAIN_ROWS:
        return list(range(count))
    matrix = unit @ unit.T if count <= CHAIN_MATRIX_ROWS else None
    visited = np.zeros(count, dtype=bool)
    visited[0] = True
    order = [0]
    current = 0
    for _ in range(count - 1):
        scores = matrix[current] if matrix is not None else unit @ unit[current]
        scores = np.where(visited, -np.inf, scores)
        current = int(np.argmax(scores))
        visited[current] = True
        order.append(current)
    return order


def blocks(sequence: Sequence[int], size: int = BLOCK_SIZE, overlap: int = BLOCK_OVERLAP) -> list[list[int]]:
    """``sequence`` cut into blocks of at most ``size`` that overlap by ``overlap``; none for fewer
    than two items."""

    if len(sequence) < MIN_BLOCK_ITEMS:
        return []
    step = size - overlap
    result: list[list[int]] = []
    start = 0
    while True:
        result.append(list(sequence[start : start + size]))
        if start + size >= len(sequence):
            return result
        start += step


def close_pairs(unit: np.ndarray, block: Sequence[int], threshold: float = DECISION_COSINE) -> list[Pair]:
    """Item pairs of ``block`` whose cosine (float64) is at least ``threshold``, sorted."""

    rows = unit[list(block)].astype(np.float64)
    scores = rows @ rows.T
    first, second = np.nonzero(np.triu(scores >= threshold, k=1))
    return sorted(pair(block[a], block[b]) for a, b in zip(first.tolist(), second.tolist(), strict=True))


# Decision memory


@dataclass(frozen=True)
class Decision:
    same: bool
    label: str | None = None
    """The label a hub group was given (hubs pass, ``same`` only)."""


class DecisionMemory:
    """Stored consolidation decisions of one project (see the module docstring, step 5)."""

    def __init__(self, store: ExtractionStore, project_id: str, model: str) -> None:
        self._store = store
        self._project_id = project_id
        self._model = model

    def fingerprint(self, prompt: str) -> str:
        return f"{prompt}|{self._model}"

    def pair_key(self, pass_name: str, first: str, second: str) -> str:
        low, high = sorted((first, second))
        return f"{self._project_id}|{pass_name}|{low}|{high}"

    def node_key(self, pass_name: str, key: str) -> str:
        return f"{self._project_id}|{pass_name}.node|{key}"

    async def nodes(self, pass_name: str, prompt: str, keys: Iterable[str]) -> dict[str, frozenset[str]]:
        """Seen identity keys with the keys they were stored ``same`` with."""

        wanted = {self.node_key(pass_name, key): key for key in sorted(set(keys))}
        found = await get_many(self._store, DECISION_KIND, list(wanted), self.fingerprint(prompt))
        result: dict[str, frozenset[str]] = {}
        for store_key, payload in found.items():
            partners = payload.get("same")
            if isinstance(partners, list):
                result[wanted[store_key]] = frozenset(item for item in partners if isinstance(item, str))
        return result

    async def pairs(
        self, pass_name: str, prompt: str, pairs: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], Decision]:
        """Stored decisions by sorted key pair."""

        wanted = {self.pair_key(pass_name, a, b): tuple(sorted((a, b))) for a, b in pairs}
        found = await get_many(self._store, DECISION_KIND, sorted(wanted), self.fingerprint(prompt))
        result: dict[tuple[str, str], Decision] = {}
        for store_key, payload in found.items():
            same = payload.get("same")
            label = payload.get("label")
            if isinstance(same, bool):
                low, high = wanted[store_key]
                result[(low, high)] = Decision(same, label if isinstance(label, str) else None)
        return result

    async def write(
        self,
        pass_name: str,
        prompt: str,
        pairs: Mapping[tuple[str, str], Decision],
        nodes: Mapping[str, frozenset[str]],
    ) -> int:
        """Store pair decisions and node entries in one batch; returns the pair decisions written."""

        entries: dict[str, JsonObject] = {}
        for (first, second), decision in sorted(pairs.items()):
            payload: JsonObject = {"same": decision.same}
            if decision.label is not None:
                payload["label"] = decision.label
            entries[self.pair_key(pass_name, first, second)] = payload
        for key in sorted(nodes):
            entries[self.node_key(pass_name, key)] = {"same": sorted(nodes[key])}
        await put_many(self._store, DECISION_KIND, self.fingerprint(prompt), entries)
        return len(pairs)


# Blocks


@dataclass(frozen=True)
class Proposal:
    """Items one answer (or one stored ``same`` pair) puts together, with the label it gave them."""

    members: tuple[int, ...]
    label: str | None = None
    stored: bool = False


@dataclass(frozen=True)
class PassSpec:
    """One consolidation pass: its ``name`` (decision keys, logs) and ``prompt`` version (decision fingerprint)."""

    name: str
    prompt: str


RawGroup = tuple[Sequence[object], str | None]
Ask = Callable[[Sequence[int]], Awaitable[list[RawGroup]]]
"""Sends one block (item indexes in block order, shown as ``short_id`` of their position); returns
the answer's groups as ``(short ids, label)``; raises ``LLMError`` on a failed call."""
Apply = Callable[[Proposal, frozenset[Pair]], list[tuple[int, ...]]]
"""Applies one proposal under the caller's vetoes and the stored ``different`` pairs given; returns
the subsets of it that are now one node (each of at least two items)."""


@dataclass
class _Block:
    items: list[int]
    close: list[Pair]
    stored: dict[Pair, Decision] = field(default_factory=dict)
    skip: bool = False
    answer: list[RawGroup] | None = None
    error: str | None = None


async def _read_decisions(
    work: Sequence[_Block],
    keys: Sequence[str],
    memory: DecisionMemory,
    *,
    pass_name: str,
    prompt: str,
    counts: dict[str, int],
) -> dict[str, frozenset[str]]:
    """Load the stored decisions into the blocks (``stored``, ``skip``); returns the stored same-partners."""

    index_of = {key: index for index, key in enumerate(keys)}
    seen = await memory.nodes(pass_name, prompt, (keys[i] for block in work for i in block.items))
    wanted: set[Pair] = set()
    for block in work:
        wanted.update(block.close)
        members = set(block.items)
        for item in block.items:
            for partner in seen.get(keys[item], ()):
                other = index_of.get(partner)
                if other is not None and other in members and other != item:
                    wanted.add(pair(item, other))
    stored = await memory.pairs(pass_name, prompt, ((keys[a], keys[b]) for a, b in sorted(wanted)))
    counts["decisions_read"] = len(stored)
    for block in work:
        members = set(block.items)
        for first, second in sorted(wanted):
            if first in members and second in members:
                decision = stored.get(_key_pair(keys, first, second))
                if decision is not None:
                    block.stored[(first, second)] = decision
        block.skip = all(keys[item] in seen for item in block.items) and all(
            close in block.stored for close in block.close
        )
    return seen


def _collect_same_pairs(
    work: Sequence[_Block], apply: Apply, counts: dict[str, int]
) -> tuple[dict[Pair, Decision], set[Pair], set[int]]:
    """Proposals of every block applied: ``(same pairs, judged pairs, judged items)``."""

    same_pairs: dict[Pair, Decision] = {}
    judged: set[Pair] = set()
    judged_items: set[int] = set()
    for block in work:
        proposals: list[Proposal] = []
        cannot = frozenset(found for found, decision in block.stored.items() if not decision.same)
        stored_same = [(found, decision) for found, decision in sorted(block.stored.items()) if decision.same]
        if block.answer is not None:
            proposals, unknown = _proposals(block.answer, block.items)
            counts["unknown_ids"] += unknown
            grouped = {pair(a, b) for proposal in proposals for a, b in itertools.combinations(proposal.members, 2)}
            counts["decision_overrides"] += len(cannot & grouped)
            extra = [(found, decision) for found, decision in stored_same if found not in grouped]
            counts["decision_overrides"] += len(extra)
            stored_same = extra
        proposals += [Proposal(found, decision.label, stored=True) for found, decision in stored_same]
        counts["groups_proposed"] += len(proposals)
        joined: list[tuple[tuple[int, ...], str | None]] = []
        for proposal in proposals:
            joined.extend((subset, proposal.label) for subset in apply(proposal, cannot))
        if block.answer is None:
            continue
        judged.update(block.close)
        judged_items.update(block.items)
        for subset, label in joined:
            for a, b in itertools.combinations(sorted(subset), 2):
                same_pairs[(a, b)] = Decision(same=True, label=label)
    return same_pairs, judged, judged_items


async def _write_decisions(
    memory: DecisionMemory,
    *,
    pass_name: str,
    prompt: str,
    keys: Sequence[str],
    seen: Mapping[str, frozenset[str]],
    same_pairs: Mapping[Pair, Decision],
    judged: set[Pair],
    judged_items: set[int],
) -> int:
    """Store the judged pairs as decisions (same, and different for the rest) and the partners per node."""

    decisions: dict[tuple[str, str], Decision] = {}
    for (a, b), decision in sorted(same_pairs.items()):
        decisions[_key_pair(keys, a, b)] = decision
    for a, b in sorted(judged - set(same_pairs)):
        decisions.setdefault(_key_pair(keys, a, b), Decision(same=False))
    partners: dict[str, set[str]] = {keys[item]: set(seen.get(keys[item], ())) for item in judged_items}
    for a, b in same_pairs:
        for one, other in ((a, b), (b, a)):
            if keys[one] in partners:
                partners[keys[one]].add(keys[other])
    return await memory.write(pass_name, prompt, decisions, {key: frozenset(value) for key, value in partners.items()})


async def consolidate(
    *,
    spec: PassSpec,
    keys: Sequence[str],
    unit: np.ndarray,
    block_list: Sequence[Sequence[int]],
    ask: Ask,
    apply: Apply,
    memory: DecisionMemory | None,
    tally: ConsolidationCounters,
) -> ConsolidationCounters:
    """Run one pass over ``block_list`` (item indexes; ``keys`` and ``unit`` rows by item) and return
    ``tally`` with the pass's counts added (the caller adds ``vetoes``, ``merges`` and
    ``cross_label_merges`` from its ``apply``); ``spec`` names the pass and its prompt version."""

    pass_name, prompt = spec.name, spec.prompt
    counts: dict[str, int] = dict.fromkeys(
        ("blocks", "blocks_skipped", "calls", "failed_calls", "groups_proposed", "unknown_ids"), 0
    )
    counts.update(decisions_read=0, decision_overrides=0, decisions_written=0)
    work = [_Block(list(items), close_pairs(unit, items)) for items in block_list]
    counts["blocks"] = len(work)

    seen: Mapping[str, frozenset[str]] = {}
    if memory is not None and work:
        seen = await _read_decisions(work, keys, memory, pass_name=pass_name, prompt=prompt, counts=counts)

    asked = [block for block in work if not block.skip]
    counts["blocks_skipped"] = len(work) - len(asked)
    counts["calls"] = len(asked)
    await _ask_all(asked, ask)
    failures = [block.error for block in asked if block.error is not None]
    counts["failed_calls"] = len(failures)
    if failures:
        log_event(
            logger,
            WARNING,
            "graph.consolidate.calls_failed",
            pass_name=pass_name,
            failed=len(failures),
            calls=len(asked),
            error_codes=sorted(set(failures)),
        )

    same_pairs, judged, judged_items = _collect_same_pairs(work, apply, counts)
    if memory is not None and judged_items:
        counts["decisions_written"] = await _write_decisions(
            memory,
            pass_name=pass_name,
            prompt=prompt,
            keys=keys,
            seen=seen,
            same_pairs=same_pairs,
            judged=judged,
            judged_items=judged_items,
        )

    result = tally.model_copy(update={name: getattr(tally, name) + value for name, value in counts.items()})
    log_event(logger, INFO, "graph.consolidate.done", pass_name=pass_name, items=tally.items, **count_fields(counts))
    return result


def _key_pair(keys: Sequence[str], first: int, second: int) -> tuple[str, str]:
    low, high = sorted((keys[first], keys[second]))
    return low, high


def _proposals(answer: Sequence[RawGroup], items: Sequence[int]) -> tuple[list[Proposal], int]:
    """Valid proposals of one answer and the number of ids dropped (unknown or already grouped)."""

    by_id = {short_id(position): item for position, item in enumerate(items)}
    used: set[int] = set()
    unknown = 0
    proposals: list[Proposal] = []
    for ids, label in answer:
        members: list[int] = []
        for raw in ids:
            item = by_id.get(raw.strip().casefold()) if isinstance(raw, str) else None
            if item is None or item in used:
                unknown += 1
                continue
            used.add(item)
            members.append(item)
        if len(members) >= MIN_BLOCK_ITEMS:
            proposals.append(Proposal(tuple(members), label))
    return proposals, unknown


async def _ask_all(asked: Sequence[_Block], ask: Ask) -> None:
    """Send every block concurrently; a failed call leaves its block without an answer (and its
    error code); ``LLMFatalError`` cancels the others and propagates."""

    async def one(block: _Block) -> None:
        try:
            block.answer = await ask(block.items)
        except LLMFatalError:
            raise
        except LLMError as error:
            block.error = error.code

    tasks = [asyncio.ensure_future(one(block)) for block in asked]
    if not tasks:
        return
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
