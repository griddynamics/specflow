"""Known terms for node extraction: hub catalog, fragment vectors and per-unit ranking (build.md step 2).

Known terms are names of hub nodes (types whose ontology role is ``anchor`` or ``group``). In a
``first`` or ``rebuild`` build they come from the hub catalog, a small-model pass over an outline of
the corpus; in an ``update`` build from the persisted hub nodes (``persisted_hubs``). Each unit gets at most
``known_terms_limit`` of them: names that occur in its text first, then the closest by cosine between
the name's vector and the unit's fragment vectors; ties are broken by name (tech-specs 5.4).

After Resolve, ``EdgeCatalog`` picks the canonical nodes an edge-extraction call over a unit may link
(build.md step 4, FR-BLD-21).

A hub name occurs in a text when its words in Resolve's hub form (``resolve.hub_words`` of
``resolve.hub_surface``: no leading article, ``-``/``_`` as spaces, plurals folded) occur there
contiguously, the text read in the same form; so "Orders service" is found in "the order-service".
Every search goes through one ``NameIndex`` per build, which reads each text's token stream once and
takes the longest name at each position, so the cost grows with the text, not with texts x names.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any, Final, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, create_model

from graph.build.corpus import estimate_tokens
from graph.build.models import CanonicalNode, FragmentRec, Unit
from graph.build.options import BuildOptions
from graph.build.ports import CorpusRevision
from graph.build.prompts import HUB_CATALOG_PROMPT, DocumentOutline, hub_catalog_messages
from graph.build.queries import READ_PERSISTED_HUBS
from graph.build.resolve import hub_surface, hub_value_words, hub_words, surface_values
from graph.build.vectors import Vector, VectorCache
from graph.core.db import Executor
from graph.core.errors import LLMError, LLMFatalError
from graph.core.identity import normalise_surface
from graph.core.llm import Gateway
from graph.core.ontology import Ontology
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.terms")

CATALOG_NEAREST: Final = 20
"""Closest foreign hubs per fragment considered for an edge catalog (build.md step 4)."""
CATALOG_MIN_COSINE: Final = 0.40
"""A foreign hub enters an edge catalog by nearness only at this cosine to a fragment or above (unrelated
texts often reach 0.2-0.35 with ``text-embedding-3-large``) ..."""
CATALOG_RELATIVE_COSINE: Final = 0.8
"""... and at this share of the fragment's best foreign-hub cosine or above, so unrelated hubs stay
out and an unrelated new hub elsewhere leaves a unit's catalog unchanged (FR-BLD-22)."""
HUB_BATCH_TOKENS: Final = 12_000
"""Estimated input tokens of document outlines per hub catalog call (fits the small model)."""


class HubName(BaseModel):
    """A hub name and its label, from the hub catalog or a persisted hub node; ``aliases``: the values
    of a persisted hub's ``surface`` identity aliases (names folded in by consolidation or a rename),
    which find the hub by mention too."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    label: str
    aliases: tuple[str, ...] = ()


async def _gather[T](awaitables: Sequence[Awaitable[T]]) -> list[T]:
    """All results in order; on the first failure the others are cancelled and the failure raised."""

    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


# Fragment vectors


async def fragment_vectors(fragments: Sequence[FragmentRec], vectors: VectorCache) -> dict[str, np.ndarray]:
    """Embedding of each fragment's stripped text by fragment id, for ranking only, from the vector
    store (``graph.build.vectors``; reused by content across builds); each distinct text is embedded
    at most once. A fragment without text (all lines masked) gets no vector."""

    texts = {fragment.fragment_id: fragment.text.strip() for fragment in fragments if fragment.text.strip()}
    if not texts:
        return {}
    return dict(zip(texts, await vectors.get_or_embed(list(texts.values())), strict=True))


async def hub_vectors(hubs: Sequence[HubName], vectors: VectorCache) -> dict[str, np.ndarray]:
    """Embedding of each distinct hub name, by name (vector store first)."""

    names = list(dict.fromkeys(hub.name for hub in hubs if hub.name.strip()))
    if not names:
        return {}
    return dict(zip(names, await vectors.get_or_embed(names), strict=True))


# Persisted hubs (update builds)


async def persisted_hubs(executor: Executor, project_id: str, ontology: Ontology) -> list[HubName]:
    """Known-term hubs of an ``update`` build (FR-BLD-23): the name and label of every persisted spec
    node of a hub label, sorted by name and label; names equal after whitespace folding count once."""

    labels = sorted(ontology.hub_labels)
    if not labels:
        return []
    rows = await executor.read(READ_PERSISTED_HUBS, project_id, labels=labels)
    hubs: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        name = " ".join(str(row.get("name") or "").split())
        label = str(row.get("label") or "")
        if name and label in ontology.hub_labels:
            aliases = hubs.setdefault((name, label), [])
            keys = [str(key) for key in row.get("aliases") or () if key]
            aliases.extend(value for value in surface_values(keys) if value not in aliases)
    return [
        HubName(name=name, label=label, aliases=tuple(sorted(aliases)))
        for (name, label), aliases in sorted(hubs.items())
    ]


# Hub catalog


@cache
def _answer_model(labels: tuple[str, ...]) -> type[BaseModel]:
    label_type: Any = Literal[labels]
    item = create_model("HubCatalogItem", __config__=ConfigDict(frozen=True), name=(str, ...), label=(label_type, ...))
    return create_model("HubCatalog", __config__=ConfigDict(frozen=True), hubs=(list[item], ...))


def outlines(revisions: Sequence[CorpusRevision], fragments: Sequence[FragmentRec]) -> list[DocumentOutline]:
    """One outline per revision, in revision order: title, distinct heading paths, first fragment text."""

    by_document: dict[str, list[FragmentRec]] = {}
    for fragment in fragments:
        by_document.setdefault(fragment.document_id, []).append(fragment)
    result = []
    for revision in revisions:
        own = sorted(by_document.get(revision.lineage_id, []), key=lambda fragment: fragment.ordinal)
        paths = tuple(dict.fromkeys(fragment.heading_path for fragment in own if fragment.heading_path))
        result.append(
            DocumentOutline(title=revision.title, heading_paths=paths, first_fragment=own[0].text if own else "")
        )
    return result


def _batches(items: Sequence[DocumentOutline], budget: int) -> list[list[DocumentOutline]]:
    batches: list[list[DocumentOutline]] = []
    used = 0
    for outline in items:
        tokens = estimate_tokens(outline.text())
        if batches and used + tokens <= budget:
            batches[-1].append(outline)
            used += tokens
        else:
            batches.append([outline])
            used = tokens
    return batches


def hub_catalog_batches(
    revisions: Sequence[CorpusRevision],
    fragments: Sequence[FragmentRec],
    ontology: Ontology,
    options: BuildOptions,
) -> list[list[DocumentOutline]]:
    """The document outlines of each hub catalog call (``HUB_BATCH_TOKENS`` each, revision order);
    none when the pass is off (``hub_catalog`` option, no hub types or no revision)."""

    if not options.hub_catalog or not ontology.hub_labels or not revisions:
        return []
    return _batches(outlines(revisions, fragments), HUB_BATCH_TOKENS)


async def hub_catalog(
    revisions: Sequence[CorpusRevision],
    fragments: Sequence[FragmentRec],
    gateway: Gateway,
    ontology: Ontology,
    options: BuildOptions,
) -> list[HubName]:
    """Hub names of the corpus from a small-model pass over document outlines, sorted by name and label.

    Off (``hub_catalog`` option, or no hub types) → ``[]`` without a call. A batch that fails with a
    non-fatal ``LLMError`` adds no names; the other batches still count, and the failures are logged
    once. All batches failed → ``[]``: the build continues without known terms (FR-BLD-23).
    ``LLMFatalError`` and ``InfrastructureUnavailableError`` propagate.
    """

    labels = tuple(sorted(ontology.hub_labels))
    batches = hub_catalog_batches(revisions, fragments, ontology, options)
    if not batches:
        return []
    output_type = _answer_model(labels)

    async def one(batch: list[DocumentOutline]) -> BaseModel | LLMError:
        try:
            return await gateway.structured(
                "small", None, hub_catalog_messages(ontology, gateway, batch), output_type, prompt=HUB_CATALOG_PROMPT
            )
        except LLMFatalError:
            raise
        except LLMError as error:
            return error

    answers = await _gather([one(batch) for batch in batches])
    failures = [answer for answer in answers if isinstance(answer, LLMError)]
    if failures:
        log_event(
            logger,
            WARNING,
            "graph.hub_catalog.batches_failed",
            failed=len(failures),
            batches=len(batches),
            error_codes=sorted({failure.code for failure in failures}),
        )
    hubs: dict[tuple[str, str], HubName] = {}
    for answer in answers:
        if isinstance(answer, LLMError):
            continue
        for item in getattr(answer, "hubs", ()):
            name = " ".join(item.name.split())
            if not name or item.label not in labels:
                continue
            hubs.setdefault((normalise_surface(name), item.label), HubName(name=name, label=item.label))
    result = sorted(hubs.values(), key=lambda hub: (hub.name, hub.label))
    log_event(
        logger, INFO, "graph.hub_catalog.done", batches=len(batches), failed_batches=len(failures), hubs=len(result)
    )
    return result


# Name occurrences


class NameIndex:
    """Whole-word occurrences of names in token streams, for many names at once.

    Built from ``(name, key)`` pairs of hub names; a name is its words in hub form
    (``hub_words(hub_surface(name))``), and names with equal words share their position. ``values``:
    ``(value, key)`` pairs of hub identity values already in that form (``surface`` aliases,
    ``resolve.hub_value_words``). Token
    streams must be in the same form (``fragment_words``, ``resolve.hub_words``). ``count`` walks a token stream
    once: at each position the longest
    name starting there wins and the walk continues after it; a position no name starts at is passed.
    """

    def __init__(self, entries: Iterable[tuple[str, str]], values: Iterable[tuple[str, str]] = ()) -> None:
        self._keys: dict[tuple[str, ...], list[str]] = {}
        needles = [(tuple(hub_words(hub_surface(name))), key) for name, key in entries]
        needles += [(tuple(hub_value_words(value)), key) for value, key in values]
        for needle, key in needles:
            if not needle:
                continue
            keys = self._keys.setdefault(needle, [])
            if key not in keys:
                keys.append(key)
        lengths: dict[str, set[int]] = {}
        for needle in self._keys:
            lengths.setdefault(needle[0], set()).add(len(needle))
        self._lengths = {first: sorted(sizes, reverse=True) for first, sizes in lengths.items()}

    def __bool__(self) -> bool:
        return bool(self._keys)

    def count(self, words: Sequence[str]) -> Counter[str]:
        """Occurrences per key in ``words`` (longest name first, occurrences never overlap)."""

        counts: Counter[str] = Counter()
        size = len(words)
        index = 0
        while index < size:
            step = 1
            for length in self._lengths.get(words[index], ()):
                if index + length > size:
                    continue
                keys = self._keys.get(tuple(words[index : index + length]))
                if keys:
                    for key in keys:
                        counts[key] += 1
                    step = length
                    break
            index += step
        return counts


def hub_name_index(hubs: Iterable[tuple[str, str, Sequence[str]]]) -> NameIndex:
    """The ``NameIndex`` of hub mentions (``MENTIONED_IN``, hub statements and the attributes pass):
    ``(key, name, identity keys)`` per hub, found by its name and by the value of each ``surface``
    key among its identity keys (its own aliases and those of the persisted node it continues)."""

    listed = [(key, name, list(keys)) for key, name, keys in hubs]
    return NameIndex(
        ((name, key) for key, name, _ in listed),
        ((value, key) for key, _, keys in listed for value in surface_values(keys)),
    )


def named_in(index: NameIndex, fragments: Iterable[FragmentRec]) -> dict[str, dict[str, int]]:
    """Per key of ``index``: fragment id to the whole-word occurrences of its names there, in fragment
    order (``NameIndex.count``: longest name first). Each fragment's tokens are read once."""

    result: dict[str, dict[str, int]] = {}
    if not index:
        return result
    for fragment in fragments:
        counts = index.count(fragment_words(fragment))
        for key in sorted(counts):
            result.setdefault(key, {})[fragment.fragment_id] = counts[key]
    return result


def fragment_words(fragment: FragmentRec) -> list[str]:
    """The token stream of a fragment's text in hub form (``resolve.hub_words``), for ``NameIndex``."""

    return hub_words(fragment.text)


def _unit_rows(unit: Unit, fragment_vectors: Mapping[str, Vector]) -> np.ndarray | None:
    rows = [fragment_vectors[fragment_id] for fragment_id in unit.fragment_ids if fragment_id in fragment_vectors]
    return _normalised_rows(rows) if rows else None


def _normalised_rows(rows: Sequence[Vector]) -> np.ndarray:
    matrix = np.asarray(rows, dtype=float)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalised: np.ndarray = matrix / norms
    return normalised


# Known terms


class KnownTerms:
    """Known-term ranking over one build's hubs: the name index and the hub vectors, built once.

    Names equal after hub normalisation (``resolve.hub_surface``) count once (the first by name); a
    hub without a (non-zero) vector is only found by mention.
    """

    def __init__(self, hubs: Sequence[HubName], hub_vectors: Mapping[str, Vector]) -> None:
        names: dict[str, str] = {}
        for name in sorted(hub.name for hub in hubs):
            surface = hub_surface(name)
            if surface:
                names.setdefault(surface, name)
        # A hub is also found by its aliases; the term shown is its name.
        aliases = sorted(
            {(value, names[hub_surface(hub.name)]) for hub in hubs if hub_surface(hub.name) for value in hub.aliases}
        )
        self._index = NameIndex(((name, name) for name in names.values()), aliases)
        self._ranked = [
            name
            for name in names.values()
            if (vector := hub_vectors.get(name)) is not None and float(np.linalg.norm(vector)) > 0.0
        ]
        self._matrix = _normalised_rows([hub_vectors[name] for name in self._ranked]) if self._ranked else None

    def for_unit(
        self,
        unit: Unit,
        fragments_by_id: Mapping[str, FragmentRec],
        fragment_vectors: Mapping[str, Vector],
        limit: int,
    ) -> list[str]:
        """At most ``limit`` names for ``unit``: those occurring in its fragments (by name), then the rest
        by the best cosine to any of its fragment vectors (descending, ties by name)."""

        if limit <= 0:
            return []
        counts: Counter[str] = Counter()
        for fragment_id in unit.fragment_ids:
            if fragment_id in fragments_by_id:
                counts.update(self._index.count(fragment_words(fragments_by_id[fragment_id])))
        mentioned = sorted(counts)
        if len(mentioned) >= limit:
            return mentioned[:limit]
        rows = _unit_rows(unit, fragment_vectors)
        if rows is None or self._matrix is None:
            return mentioned
        scores = np.round(np.max(self._matrix @ rows.T, axis=1), 9)
        chosen = set(mentioned)
        ranked = [
            self._ranked[index]
            for index in np.argsort(-scores, kind="stable").tolist()
            if self._ranked[index] not in chosen
        ]
        return mentioned + ranked[: limit - len(mentioned)]


_KNOWN_TERMS_CACHE: list[tuple[Sequence[HubName], Mapping[str, Vector], KnownTerms]] = []


def known_terms(
    unit: Unit,
    fragments_by_id: Mapping[str, FragmentRec],
    hubs: Sequence[HubName],
    hub_vectors: Mapping[str, Vector],
    fragment_vectors: Mapping[str, Vector],
    *,
    limit: int,
) -> list[str]:
    """``KnownTerms(hubs, hub_vectors).for_unit(...)``; the ``KnownTerms`` of the last ``hubs`` and
    ``hub_vectors`` objects (by identity) is kept, so a loop over a build's units builds it once.
    Callers must not mutate ``hubs`` or ``hub_vectors`` between calls."""

    cached = _KNOWN_TERMS_CACHE[0] if _KNOWN_TERMS_CACHE else None
    if cached is not None and cached[0] is hubs and cached[1] is hub_vectors:
        index = cached[2]
    else:
        index = KnownTerms(hubs, hub_vectors)
        _KNOWN_TERMS_CACHE[:] = [(hubs, hub_vectors, index)]
    return index.for_unit(unit, fragments_by_id, fragment_vectors, limit)


# Edge catalog


class EdgeCatalog:
    """The cids an edge call over a unit may link (build.md step 4, FR-BLD-21), indexes built once per build.

    ``for_unit``: local nodes, then at most ``limit`` foreign hubs. Local: every canonical node
    grounded in one of the unit's fragments, in cid order (a node grounded in the unit is always in
    its catalog). Foreign: hub nodes (``hub_cids``) not grounded in the unit; those whose name occurs
    in the unit's fragments first (cid order; a hub is also found by the values of its ``surface``
    identity aliases and of ``aliases[cid]``, the keys of the persisted node it continues), then, from the union of
    each fragment's
    ``CATALOG_NEAREST`` closest by cosine between ``name_vectors[cid]`` (the ``name_embedding``) and
    the fragment vector that reach the fragment's floor (``max(CATALOG_MIN_COSINE,
    CATALOG_RELATIVE_COSINE x`` its best cosine to any foreign hub``)``), by best cosine descending;
    ties by cid (cids follow identity-key order). Local and mentioned nodes need no floor. Only
    hubs count as mentioned: a non-hub node from elsewhere is never pulled in by its name. Same inputs,
    same catalog.

    ``select`` with ``kept`` (a clean unit of an ``update`` build whose last catalog is stored): the
    nearness part is not ranked again; it is the hubs of ``kept`` (the cids its last catalog took by
    nearness that still exist, in that order) that are neither local nor mentioned now, so the
    catalog changes only when a hub is newly mentioned or a hub it used is gone (FR-BLD-22).
    """

    def __init__(
        self,
        *,
        fragments_by_id: Mapping[str, FragmentRec],
        canonical: Sequence[CanonicalNode],
        hub_cids: frozenset[str] | set[str],
        name_vectors: Mapping[str, Vector],
        fragment_vectors: Mapping[str, Vector],
        limit: int,
        aliases: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._fragments = fragments_by_id
        self._fragment_vectors = fragment_vectors
        self._limit = max(limit, 0)
        ordered = sorted(canonical, key=lambda node: node.cid)
        self._grounded: dict[str, list[str]] = {}
        for node in ordered:
            for fragment_id in dict.fromkeys(node.fragment_ids):
                self._grounded.setdefault(fragment_id, []).append(node.cid)
        hubs = [node for node in ordered if node.cid in hub_cids]
        extra = aliases or {}
        self._mentions = NameIndex(
            ((node.name, node.cid) for node in hubs),
            (
                (value, node.cid)
                for node in hubs
                for value in surface_values([*node.identity_aliases, *extra.get(node.cid, ())])
            ),
        )
        self._ranked = [node.cid for node in hubs if node.cid in name_vectors]
        self._matrix = _normalised_rows([name_vectors[cid] for cid in self._ranked]) if self._ranked else None
        self._position = {cid: index for index, cid in enumerate(self._ranked)}

    def for_unit(self, unit: Unit) -> list[str]:
        """The unit's catalog by the full rule: local, mentioned, then nearest hubs."""

        return self.select(unit).cids

    def select(self, unit: Unit, kept: Sequence[str] | None = None) -> UnitCatalog:
        """The unit's catalog by part; ``kept``: the nearness hubs to keep instead of ranking (class docstring)."""

        local = sorted({cid for fid in unit.fragment_ids for cid in self._grounded.get(fid, ())})
        excluded = set(local)
        counts: Counter[str] = Counter()
        for fragment_id in unit.fragment_ids:
            if fragment_id in self._fragments:
                counts.update(self._mentions.count(fragment_words(self._fragments[fragment_id])))
        foreign = sorted(cid for cid in counts if cid not in excluded)[: self._limit]
        excluded.update(foreign)
        room = self._limit - len(foreign)
        if kept is not None:
            near = [cid for cid in dict.fromkeys(kept) if cid in self._position and cid not in excluded]
            return UnitCatalog(local=local, mentioned=foreign, near=near[: max(room, 0)])
        return UnitCatalog(local=local, mentioned=foreign, near=self._nearest(unit, local, excluded, room))

    def _nearest(self, unit: Unit, local: Sequence[str], excluded: set[str], room: int) -> list[str]:
        rows = _unit_rows(unit, self._fragment_vectors)
        if room <= 0 or rows is None or self._matrix is None:
            return []
        scores = np.round(rows @ self._matrix.T, 9)
        grounded = [self._position[cid] for cid in local if cid in self._position]
        if grounded:
            scores[:, grounded] = -np.inf
        # Each fragment's floor, from its best cosine to any foreign hub (mentioned ones included).
        floors = np.maximum(CATALOG_MIN_COSINE, CATALOG_RELATIVE_COSINE * np.max(scores, axis=1))
        blocked = [self._position[cid] for cid in excluded if cid in self._position]
        if blocked:
            scores[:, blocked] = -np.inf
        best: dict[int, float] = {}
        for row, floor in zip(scores, floors.tolist(), strict=True):
            for index in np.argsort(-row, kind="stable")[:CATALOG_NEAREST].tolist():
                score = float(row[index])
                if score != -np.inf and score >= floor:
                    best[index] = max(best.get(index, -2.0), score)
        ranked = sorted(best, key=lambda index: (-best[index], self._ranked[index]))
        return [self._ranked[index] for index in ranked[:room]]


@dataclass(frozen=True, slots=True)
class UnitCatalog:
    """An edge catalog by part (``EdgeCatalog.select``): local nodes, mentioned hubs, nearness hubs."""

    local: list[str]
    mentioned: list[str]
    near: list[str]

    @property
    def cids(self) -> list[str]:
        return [*self.local, *self.mentioned, *self.near]
