"""The hub consolidation pass of Resolve (step 3, ``resolve`` step 4)."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

import numpy as np

from graph.build.models import Confidence, ConsolidationCounters
from graph.build.ports import ExtractionKind, ExtractionStore, JsonObject
from graph.build.vectors import get_many, put_many
from graph.core.errors import LLMError, LLMFatalError
from graph.core.logfields import count_fields
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.consolidate")

HUBS_PROMPT: Final = "consolidate_hubs.4"
"""Prompt version of the hub judge; part of the decision fingerprint, so a bump drops stored decisions."""
_HUBS_LABEL: Final = "hubs"
HUBS_PASS: Final = _HUBS_LABEL
BLOCK_SIZE: Final = 120
BLOCK_OVERLAP: Final = 10
DECISION_COSINE: Final = 0.85
"""Pairs of a judged block at this ``name_embedding`` cosine or more get a stored decision."""
LINE_MAX_CHARS: Final = 160
"""Longest evidence line shown per item (``prompt_name``)."""
CHAIN_MATRIX_ROWS: Final = 4096
"""Runs up to this many rows order their chain from one cosine matrix; longer ones row by row."""
MIN_CHAIN_ROWS: Final = 2
"""Rows up to which the chain is the identity order."""
MIN_BLOCK_ITEMS: Final = 2
"""Items a block or a proposed group needs to hold a pair."""
DECISION_KIND: Final[ExtractionKind] = "decisions"
QUEUED_SUFFIX: Final = ".queued"
"""Pass-name suffix of the pairs a build left undecided (``DecisionMemory.queue``)."""
SHORT_ID: Final = "n"
COSINE_CHUNK: Final = 256
"""Rows per block of the chunked cosine products (memory: ``COSINE_CHUNK`` x rows floats)."""
COSINE_SLACK: Final = 1e-3
"""Float32 nearest-neighbour scores within this of the threshold are kept; the exact cosine decides."""
UNSURE: Final[Confidence] = "low"
"""The confidence of an unsure answer: no merge, nothing stored, judged again in the next build."""
StoredConfidence = Literal["high", "medium", "implicit"]
"""Confidence of a stored decision: the judge's for a ``same`` pair, ``implicit`` for a judged pair it
did not group (``different``)."""
IMPLICIT: Final[StoredConfidence] = "implicit"
_STORED_CONFIDENCES: Final[Mapping[str, StoredConfidence]] = {
    "high": "high",
    "medium": "medium",
    "implicit": "implicit",
}
_JUDGE_CONFIDENCES: Final[Mapping[str, Confidence]] = {"high": "high", "medium": "medium", "low": "low"}
_EVIDENCE: Final = "evidence"

Pair = tuple[int, int]
"""Two item indexes, lower first."""


def pair(first: int, second: int) -> Pair:
    return (first, second) if first < second else (second, first)


def short_id(position: int) -> str:
    """The id of the item at ``position`` (0-based) of a block: ``n1``, ``n2``, ..."""

    return f"{SHORT_ID}{position + 1}"


def evidence_hash(parts: object) -> str:
    """SHA-256 of ``parts`` as canonical JSON (sorted keys, no spaces): the evidence a stored decision
    was made on (module docstring, step 5)."""

    text = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pair_hash(first: str, first_hash: str, second: str, second_hash: str) -> str:
    """The evidence hash of a pair decision: both keys with their items' evidence hashes, sorted."""

    return evidence_hash(sorted((f"{first}:{first_hash}", f"{second}:{second_hash}")))


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


def nearest_pairs(unit: np.ndarray, k: int, threshold: float) -> set[tuple[int, int]]:
    """Position pairs ``(i, j)``, ``i < j``, of each row with its ``k`` nearest other rows whose float32 cosine is at
    least ``threshold - COSINE_SLACK`` (the exact cosine decides later; ``unit``: float32 unit rows; products in blocks
    of ``COSINE_CHUNK`` rows, never n x n at once).
    """

    floor = threshold - COSINE_SLACK
    count = unit.shape[0]
    k = min(k, count - 1)
    close: set[tuple[int, int]] = set()
    if k <= 0:
        return close
    for start in range(0, count, COSINE_CHUNK):
        block = unit[start : start + COSINE_CHUNK] @ unit.T
        rows = np.arange(block.shape[0])
        block[rows, start + rows] = -np.inf
        hits = block >= floor
        many = hits.sum(axis=1) > k
        found: list[tuple[np.ndarray, np.ndarray]] = []
        few_rows, few_cols = np.nonzero(hits & ~many[:, None])
        found.append((few_rows, few_cols))
        crowded = np.nonzero(many)[0]
        if crowded.size:
            top = np.argpartition(-block[crowded], k - 1, axis=1)[:, :k]
            found.append((np.repeat(crowded, k), top.ravel()))
        for found_rows, found_cols in found:
            for row, column in zip((found_rows + start).tolist(), found_cols.tolist(), strict=True):
                close.add((row, column) if row < column else (column, row))
    return close


def pair_cosines(unit: np.ndarray, pairs: Sequence[tuple[int, int]], chunk: int = 1024) -> list[float]:
    """Cosine of each pair, summed in float64 (one row product per pair, never a full matrix)."""

    result: list[float] = []
    if not pairs:
        return result
    for start in range(0, len(pairs), chunk):
        part = np.asarray(pairs[start : start + chunk], dtype=np.int64)
        first = unit[part[:, 0]].astype(np.float64)
        second = unit[part[:, 1]].astype(np.float64)
        result.extend(np.einsum("ij,ij->i", first, second).tolist())
    return result


# Decision memory


@dataclass(frozen=True)
class Decision:
    same: bool
    label: str | None = None
    """The label a hub group was given (hubs pass, ``same`` only)."""
    confidence: StoredConfidence | None = None
    """The judge's confidence (``same``), ``implicit`` for a judged pair it did not group."""


@dataclass(frozen=True)
class StoredNode:
    """A valid node entry: the keys the item was stored ``same`` with; ``unsure`` when it was in an
    unsure group (not seen: its block is sent again)."""

    partners: frozenset[str] = frozenset()
    unsure: bool = False


@dataclass(frozen=True)
class Recalled[K, V]:
    """Stored entries whose evidence hash matches (``found``) and the number that expired."""

    found: dict[K, V]
    expired: int = 0


def evidence_matches(payload: Mapping[str, object], evidence: str) -> bool | None:
    """Whether a stored payload was made on ``evidence``: None when it carries no evidence hash."""

    stored = payload.get(_EVIDENCE)
    return stored == evidence if isinstance(stored, str) else None


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

    async def nodes(self, pass_name: str, prompt: str, hashes: Mapping[str, str]) -> Recalled[str, StoredNode]:
        """Valid node entries of the identity keys in ``hashes`` (each with its item's current hash)."""

        wanted = {self.node_key(pass_name, key): key for key in sorted(hashes)}
        found = await get_many(self._store, DECISION_KIND, list(wanted), self.fingerprint(prompt))
        result: dict[str, StoredNode] = {}
        expired = 0
        for store_key, payload in found.items():
            key = wanted[store_key]
            partners = payload.get("same")
            match = evidence_matches(payload, hashes[key])
            if match is False:
                expired += 1
            elif match and isinstance(partners, list):
                result[key] = StoredNode(
                    frozenset(item for item in partners if isinstance(item, str)), payload.get("unsure") is True
                )
        return Recalled(result, expired)

    async def known(self, pass_name: str, prompt: str, keys: Iterable[str]) -> frozenset[str]:
        """Identity keys with a node entry that carries an evidence hash, whatever the hash (the
        estimate's guess at the items a build has seen; it knows no hashes)."""

        wanted = {self.node_key(pass_name, key): key for key in sorted(set(keys))}
        found = await get_many(self._store, DECISION_KIND, list(wanted), self.fingerprint(prompt))
        return frozenset(wanted[store_key] for store_key, payload in found.items() if _EVIDENCE in payload)

    async def pairs(
        self, pass_name: str, prompt: str, pairs: Iterable[tuple[str, str]], hashes: Mapping[str, str]
    ) -> Recalled[tuple[str, str], Decision]:
        """Valid stored decisions by sorted key pair (``hashes``: each key's item hash)."""

        wanted: dict[str, tuple[str, str]] = {}
        for first, second in pairs:
            low, high = sorted((first, second))
            wanted[self.pair_key(pass_name, low, high)] = (low, high)
        found = await get_many(self._store, DECISION_KIND, sorted(wanted), self.fingerprint(prompt))
        result: dict[tuple[str, str], Decision] = {}
        expired = 0
        for store_key, payload in found.items():
            low, high = wanted[store_key]
            same, label, confidence = payload.get("same"), payload.get("label"), payload.get("confidence")
            match = evidence_matches(payload, pair_hash(low, hashes[low], high, hashes[high]))
            if match is False:
                expired += 1
            elif match and isinstance(same, bool):
                result[(low, high)] = Decision(
                    same,
                    label if isinstance(label, str) else None,
                    _stored_confidence(confidence),
                )
        return Recalled(result, expired)

    def queue_key(self, pass_name: str, first: str, second: str) -> str:
        return self.pair_key(f"{pass_name}{QUEUED_SUFFIX}", first, second)

    async def queue(
        self, pass_name: str, prompt: str, pairs: Iterable[tuple[str, str]], hashes: Mapping[str, str]
    ) -> None:
        """Record ``pairs`` as left undecided (over a call cap, unsure, a failed call), each with its
        pair evidence hash: a later build may still judge them although both items are settled."""

        entries: dict[str, JsonObject] = {}
        for first, second in pairs:
            low, high = sorted((first, second))
            entries[self.queue_key(pass_name, low, high)] = {
                "queued": True,
                _EVIDENCE: pair_hash(low, hashes[low], high, hashes[high]),
            }
        if entries:
            await put_many(self._store, DECISION_KIND, self.fingerprint(prompt), entries)

    async def queued(
        self, pass_name: str, prompt: str, pairs: Iterable[tuple[str, str]], hashes: Mapping[str, str]
    ) -> frozenset[tuple[str, str]]:
        """The sorted key pairs of ``pairs`` an earlier build queued (``queue``) on today's evidence."""

        wanted: dict[str, tuple[str, str]] = {}
        for first, second in pairs:
            low, high = sorted((first, second))
            wanted[self.queue_key(pass_name, low, high)] = (low, high)
        found = await get_many(self._store, DECISION_KIND, sorted(wanted), self.fingerprint(prompt))
        result: set[tuple[str, str]] = set()
        for store_key, payload in found.items():
            low, high = wanted[store_key]
            if evidence_matches(payload, pair_hash(low, hashes[low], high, hashes[high])):
                result.add((low, high))
        return frozenset(result)

    async def write(
        self,
        pass_name: str,
        prompt: str,
        decisions: Mapping[tuple[str, str], Decision],
        nodes: Mapping[str, StoredNode],
        hashes: Mapping[str, str],
    ) -> int:
        """Store pair decisions and node entries in one batch, each with its evidence hash (``hashes``:
        each key's item hash); returns the pair decisions written."""

        entries: dict[str, JsonObject] = {}
        for (first, second), decision in sorted(decisions.items()):
            payload: JsonObject = {"same": decision.same}
            if decision.label is not None:
                payload["label"] = decision.label
            if decision.confidence is not None:
                payload["confidence"] = decision.confidence
            payload[_EVIDENCE] = pair_hash(first, hashes[first], second, hashes[second])
            entries[self.pair_key(pass_name, first, second)] = payload
        for key in sorted(nodes):
            node: JsonObject = {"same": sorted(nodes[key].partners), _EVIDENCE: hashes[key]}
            if nodes[key].unsure:
                node["unsure"] = True
            entries[self.node_key(pass_name, key)] = node
        await put_many(self._store, DECISION_KIND, self.fingerprint(prompt), entries)
        return len(decisions)


def _stored_confidence(value: object) -> StoredConfidence | None:
    return _STORED_CONFIDENCES.get(value) if isinstance(value, str) else None


# Blocks


@dataclass(frozen=True)
class Proposal:
    """Items one answer (or one stored ``same`` pair) puts together, with the label it gave them and
    its confidence (a stored pair's: the confidence it was stored with)."""

    members: tuple[int, ...]
    label: str | None = None
    stored: bool = False
    confidence: Confidence | None = None


@dataclass(frozen=True)
class PassSpec:
    """One consolidation pass: its ``name`` (decision keys, logs) and ``prompt`` version (decision fingerprint);
    ``node_entries`` False: a second pass over items a first pass already marked (the hub pair pass): it writes no new
    node entry, it only adds new ``same`` partners to the valid entries there are (keeping their ``unsure`` flag).
    """

    name: str
    prompt: str
    node_entries: bool = True


@dataclass(frozen=True)
class PassItems:
    """The items of one pass by index: identity ``keys`` (one per item), evidence ``hashes`` (``evidence_hash`` of what
    the judge is shown of each item), ``unit`` name vectors (rows), and the caller's candidate ``pairs``, judged (and
    stored) like close pairs in any block holding both.
    """

    keys: Sequence[str]
    hashes: Sequence[str]
    unit: np.ndarray
    pairs: Collection[Pair] = ()


RawGroup = tuple[Sequence[object], str | None, str | None]
Ask = Callable[[Sequence[int]], Awaitable[list[RawGroup]]]
"""Sends one block (item indexes in block order, shown as ``short_id`` of their position); returns
the answer's groups as ``(short ids, label, confidence)``; raises ``LLMError`` on a failed call."""
Apply = Callable[[Proposal, frozenset[Pair]], list[tuple[int, ...]]]
"""Applies one proposal under the caller's vetoes and the stored ``different`` pairs given; returns
the subsets of it that are now one node (each of at least two items)."""


@dataclass(frozen=True)
class PassHooks:
    """The caller's side of one pass: ``ask`` sends a block, ``apply`` applies a proposal.
    ``undecidable``: item pairs the caller never joins whatever the answer (a fixed veto); they get no
    stored decision and do not keep a block from being skipped."""

    ask: Ask
    apply: Apply
    undecidable: Callable[[int, int], bool] | None = None


@dataclass
class _Block:
    items: list[int]
    judged: list[Pair]
    stored: dict[Pair, Decision] = field(default_factory=dict)
    skip: bool = False
    answer: list[RawGroup] | None = None
    error: str | None = None


@dataclass
class _Answer:
    """One answer read against its block: valid proposals, dropped ids, unsure groups."""

    proposals: list[Proposal] = field(default_factory=list)
    unknown: int = 0
    unsure: list[tuple[int, ...]] = field(default_factory=list)


@dataclass
class _Collected:
    """What the judged blocks decided: ``same`` pairs, judged pairs and items, unsure pairs and items."""

    same: dict[Pair, Decision] = field(default_factory=dict)
    judged: set[Pair] = field(default_factory=set)
    items: set[int] = field(default_factory=set)
    unsure_pairs: set[Pair] = field(default_factory=set)
    unsure_items: set[int] = field(default_factory=set)


def _judged_pairs(items: PassItems, block: Sequence[int], undecidable: Callable[[int, int], bool] | None) -> list[Pair]:
    """The block's close pairs and the caller's candidate pairs inside it, less the undecidable ones."""

    members = set(block)
    found = set(close_pairs(items.unit, block))
    found.update(pair(a, b) for a, b in items.pairs if a in members and b in members and a != b)
    return sorted(judged for judged in found if undecidable is None or not undecidable(*judged))


async def _read_decisions(
    work: Sequence[_Block], items: PassItems, memory: DecisionMemory, *, spec: PassSpec, counts: dict[str, int]
) -> dict[str, StoredNode]:
    """Load the valid stored decisions into the blocks (``stored``, ``skip``); returns the valid node
    entries by key."""

    keys = items.keys
    hashes = dict(zip(keys, items.hashes, strict=True))
    index_of = {key: index for index, key in enumerate(keys)}
    nodes = await memory.nodes(
        spec.name, spec.prompt, {keys[i]: hashes[keys[i]] for block in work for i in block.items}
    )
    seen = nodes.found
    wanted: set[Pair] = set()
    for block in work:
        wanted.update(block.judged)
        members = set(block.items)
        for item in block.items:
            for partner in seen[keys[item]].partners if keys[item] in seen else ():
                other = index_of.get(partner)
                if other is not None and other in members and other != item:
                    wanted.add(pair(item, other))
    stored = await memory.pairs(spec.name, spec.prompt, ((keys[a], keys[b]) for a, b in sorted(wanted)), hashes)
    counts["decisions_read"] = len(stored.found)
    counts["decisions_expired"] = nodes.expired + stored.expired
    for block in work:
        members = set(block.items)
        for first, second in sorted(wanted):
            if first in members and second in members:
                decision = stored.found.get(_key_pair(keys, first, second))
                if decision is not None:
                    block.stored[(first, second)] = decision
        block.skip = all(keys[item] in seen and not seen[keys[item]].unsure for item in block.items) and all(
            judged in block.stored for judged in block.judged
        )
    return seen


def _kept_apart(block: _Block, proposals: Sequence[Proposal], settled: Collection[int]) -> frozenset[Pair]:
    """The pairs of an answer's proposals that the block keeps apart (module docstring, step 6): both
    items settled, the pair neither judged in the block nor stored."""

    judged = set(block.judged)
    return frozenset(
        found
        for proposal in proposals
        for found in (pair(a, b) for a, b in itertools.combinations(proposal.members, 2))
        if found[0] in settled and found[1] in settled and found not in judged and found not in block.stored
    )


def _split_apart(proposal: Proposal, apart: frozenset[Pair], cannot: frozenset[Pair] = frozenset()) -> list[Proposal]:
    """``proposal`` cut into the parts its members form when the ``apart`` pairs are taken out (members joined by any
    other pair stay one part, through a changed item too); parts of one member go.
    """

    members = list(proposal.members)
    if not any(pair(a, b) in apart for a, b in itertools.combinations(members, 2)):
        return [proposal]
    cut = apart | cannot
    part = {item: item for item in members}

    def root(item: int) -> int:
        while part[item] != item:
            part[item] = part[part[item]]
            item = part[item]
        return item

    for a, b in itertools.combinations(members, 2):
        if pair(a, b) not in cut:
            part[root(a)] = root(b)
    parts: dict[int, list[int]] = {}
    for item in members:
        parts.setdefault(root(item), []).append(item)
    return [
        Proposal(tuple(found), proposal.label, proposal.stored, proposal.confidence)
        for found in parts.values()
        if len(found) >= MIN_BLOCK_ITEMS
    ]


def _stored_proposal(found: Pair, decision: Decision) -> Proposal:
    return Proposal(found, decision.label, stored=True, confidence=_JUDGE_CONFIDENCES.get(decision.confidence or ""))


def _collect(work: Sequence[_Block], apply: Apply, counts: dict[str, int], settled: Collection[int]) -> _Collected:
    """Proposals of every block applied (answers, then valid stored ``same`` pairs); what the judged
    blocks decided. ``settled``: the items kept apart (``_kept_apart``)."""

    out = _Collected()
    for block in work:
        proposals: list[Proposal] = []
        regrouped: list[tuple[Pair, Decision]] = []
        cannot = frozenset(found for found, decision in block.stored.items() if not decision.same)
        stored_same = [(found, decision) for found, decision in sorted(block.stored.items()) if decision.same]
        if block.answer is not None:
            read = _proposals(block.answer, block.items)
            counts["unknown_ids"] += read.unknown
            counts["unsure_groups"] += len(read.unsure)
            for group in read.unsure:
                out.unsure_items.update(group)
                out.unsure_pairs.update(pair(a, b) for a, b in itertools.combinations(group, 2))
            proposals = read.proposals
            grouped = {pair(a, b) for proposal in proposals for a, b in itertools.combinations(proposal.members, 2)}
            counts["decision_overrides"] += len(cannot & grouped)
            apart = _kept_apart(block, proposals, settled)
            counts["kept_apart"] += len(apart)
            proposals = [part for proposal in proposals for part in _split_apart(proposal, apart, cannot)]
            regrouped = [(found, decision) for found, decision in stored_same if found in grouped]
            stored_same = [(found, decision) for found, decision in stored_same if found not in grouped]
            counts["decision_overrides"] += len(stored_same)
        proposals += [_stored_proposal(found, decision) for found, decision in stored_same]
        counts["groups_proposed"] += len(proposals)
        # a stored ``same`` pair the answer grouped holds even when a veto kept it out of that group
        proposals += [_stored_proposal(found, decision) for found, decision in regrouped]
        joined: list[tuple[tuple[int, ...], Proposal]] = []
        for proposal in proposals:
            joined.extend((subset, proposal) for subset in apply(proposal, cannot))
        if block.answer is None:
            continue
        out.judged.update(block.judged)
        out.items.update(block.items)
        for subset, proposal in joined:
            confidence = _STORED_CONFIDENCES.get(proposal.confidence or "")
            for a, b in itertools.combinations(sorted(subset), 2):
                out.same[(a, b)] = Decision(same=True, label=proposal.label, confidence=confidence)
    return out


async def _write_decisions(
    memory: DecisionMemory,
    *,
    spec: PassSpec,
    items: PassItems,
    seen: Mapping[str, StoredNode],
    collected: _Collected,
) -> int:
    """Store the judged pairs as decisions (``same``; ``different``, ``implicit``, for the rest but the
    unsure ones) and a node entry per judged item (its ``same`` partners; ``unsure`` when it was in
    an unsure group)."""

    keys = items.keys
    decisions: dict[tuple[str, str], Decision] = {}
    for (a, b), decision in sorted(collected.same.items()):
        decisions[_key_pair(keys, a, b)] = decision
    for a, b in sorted(collected.judged - set(collected.same) - collected.unsure_pairs):
        decisions.setdefault(_key_pair(keys, a, b), Decision(same=False, confidence=IMPLICIT))
    partners: dict[str, set[str]] = {
        keys[item]: set(seen[keys[item]].partners if keys[item] in seen else ()) for item in collected.items
    }
    for a, b in collected.same:
        for one, other in ((a, b), (b, a)):
            if keys[one] in partners:
                partners[keys[one]].add(keys[other])
    unsure = {keys[item] for item in collected.unsure_items}
    if spec.node_entries:
        nodes = {key: StoredNode(frozenset(value), key in unsure) for key, value in partners.items()}
    else:  # only new ``same`` partners join the entries a first pass wrote (their unsure flag kept)
        nodes = {
            key: StoredNode(frozenset(value), seen[key].unsure)
            for key, value in partners.items()
            if key in seen and value != seen[key].partners
        }
    return await memory.write(spec.name, spec.prompt, decisions, nodes, dict(zip(keys, items.hashes, strict=True)))


async def consolidate(
    *,
    spec: PassSpec,
    items: PassItems,
    block_list: Sequence[Sequence[int]],
    hooks: PassHooks,
    memory: DecisionMemory | None,
    tally: ConsolidationCounters,
    settled: Collection[int] | None = None,
) -> ConsolidationCounters:
    """Run one pass over ``block_list`` (item indexes into ``items``) and return ``tally`` with the pass's counts added
    (the caller adds ``vetoes``, ``merges`` and ``cross_label_merges`` from its ``hooks.apply``); ``spec`` names the
    pass and its prompt version.
    """

    counts: dict[str, int] = dict.fromkeys(
        ("blocks", "blocks_skipped", "calls", "failed_calls", "groups_proposed", "unknown_ids", "unsure_groups"), 0
    )
    counts.update(decisions_read=0, decisions_expired=0, decision_overrides=0, decisions_written=0, kept_apart=0)
    work = [_Block(list(block), _judged_pairs(items, block, hooks.undecidable)) for block in block_list]
    counts["blocks"] = len(work)

    seen: Mapping[str, StoredNode] = {}
    if memory is not None and work:
        seen = await _read_decisions(work, items, memory, spec=spec, counts=counts)

    asked = [block for block in work if not block.skip]
    counts["blocks_skipped"] = len(work) - len(asked)
    counts["calls"] = len(asked)
    await _ask_all(asked, hooks.ask)
    failures = [block.error for block in asked if block.error is not None]
    counts["failed_calls"] = len(failures)
    if failures:
        log_event(
            logger,
            WARNING,
            "graph.consolidate.calls_failed",
            pass_name=spec.name,
            failed=len(failures),
            calls=len(asked),
            error_codes=sorted(set(failures)),
        )

    # A pass over items an earlier pass of this build marked (``node_entries`` off) keeps only the
    # items its caller names apart: those settled before that earlier pass.
    keep = frozenset(settled_items(items.keys, seen) if spec.node_entries else settled or ())
    collected = _collect(work, hooks.apply, counts, keep)
    if memory is not None and collected.items:
        counts["decisions_written"] = await _write_decisions(
            memory, spec=spec, items=items, seen=seen, collected=collected
        )

    result = tally.model_copy(update={name: getattr(tally, name) + value for name, value in counts.items()})
    log_event(logger, INFO, "graph.consolidate.done", pass_name=spec.name, items=tally.items, **count_fields(counts))
    return result


def settled_items(keys: Sequence[str], seen: Mapping[str, StoredNode]) -> list[int]:
    """Indexes of the items settled by ``seen`` (valid node entries): an entry that is not unsure."""

    return [index for index, key in enumerate(keys) if key in seen and not seen[key].unsure]


def _key_pair(keys: Sequence[str], first: int, second: int) -> tuple[str, str]:
    low, high = sorted((keys[first], keys[second]))
    return low, high


def _proposals(answer: Sequence[RawGroup], items: Sequence[int]) -> _Answer:
    """Valid proposals of one answer, the number of ids dropped (unknown or already grouped) and its
    unsure (``low``) groups."""

    by_id = {short_id(position): item for position, item in enumerate(items)}
    used: set[int] = set()
    out = _Answer()
    for ids, label, confidence in answer:
        members: list[int] = []
        for raw in ids:
            item = by_id.get(raw.strip().casefold()) if isinstance(raw, str) else None
            if item is None or item in used:
                out.unknown += 1
                continue
            used.add(item)
            members.append(item)
        if len(members) < MIN_BLOCK_ITEMS:
            continue
        judged = _JUDGE_CONFIDENCES.get(confidence or "")
        if judged == UNSURE:
            out.unsure.append(tuple(members))
        else:
            out.proposals.append(Proposal(tuple(members), label, confidence=judged))
    return out


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
