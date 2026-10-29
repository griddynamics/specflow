"""Node extraction over units and windows with Graphiti (docs/graph/build.md step 2; tech-specs 5.4, 5.5).

Every unit with dirty fragments gets one Graphiti ``extract_nodes`` call per window: all of the unit's
fragments are the call's episodes (the whole unit stays visible as context), the instructions name the
window's episodes as the only ones to extract from. Graphiti's per-node episode indices, restricted to
the window, tie each node to its fragments; a node without them, or attributed only outside its
window, is tied to every fragment of its window (``window_fallback``). When a multi-fragment window
answers ``[0]`` for every node, that is Graphiti's default rather than an attribution: the whole call
falls back to the window and counts as a default attribution.

A truncated answer halves its window (by tokens) and retries both halves, up to ``MAX_SPLIT_DEPTH``
times. Any other non-fatal ``LLMError`` marks the window's dirty fragments ``skipped`` with the error
code; ``LLMFatalError`` and ``InfrastructureUnavailableError`` propagate. Windows of all units run
concurrently; the gateway's limiter bounds the requests in flight.

Edge extraction (build.md step 4) calls Graphiti's ``extract_edges`` once per unit with dirty
fragments, all its fragments as episodes and its catalog of canonical nodes (``terms.EdgeCatalog``)
under unique names; truncation and failures are handled like node windows. The attributes pass fills
the attributes of new and changed canonical nodes in batches of the small model; a failed batch
leaves its nodes without attributes. ``extract_edges`` runs both concurrently.

Reuse (build.md "Reuse in update builds"; FR-BLD-03, FR-BLD-12, FR-BLD-22): every build writes its
results to the ``ExtractionStore`` (``StoredResults``), keyed so that they never depend on the
build-local cids: the raw nodes of each fragment (kind ``nodes``, by lineage and content hash), the raw
edges of each unit (kind ``edges``, by unit content hash and the sorted identity keys of its catalog,
endpoints by identity key, fragments by position in the unit and by id; the last answer also by
content alone and by unit identity, with its catalog's nearness hubs) and the attribute values of
each node (kind ``attributes``, by identity key, label and the content hashes of its fragments; a
hub's are those naming it, ``attribute_scope``). Each
entry carries the step's extractor fingerprint (``node_fingerprint``, ``edge_fingerprint``,
``attribute_fingerprint``; the first two include the installed ``graphiti-core`` version), so a
changed prompt, model, Graphiti release, ontology part or option is a miss. Only ``update`` builds read them
(``StoredResults.read``); reused results are marked ``reused``.

Log records name a unit by its ordinal in the plan, never by its id (ids embed headings).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, create_model

from graph.build.graphiti import (
    EDGES_PROMPT,
    NODES_PROMPT,
    GraphitiClients,
    extract_edges_call,
    extract_nodes_call,
    graphiti_clients,
)
from graph.build.models import (
    AttributeValue,
    Attribution,
    CanonicalNode,
    EdgeCounters,
    EdgesArtifact,
    FragmentRec,
    NodeCounters,
    NodesArtifact,
    PlanArtifact,
    RawEdge,
    RawNode,
    ResolveArtifact,
    Unit,
    Window,
)
from graph.build.options import BuildOptions, StepServices
from graph.build.ports import ExtractionKind, ExtractionStore, JsonObject
from graph.build.prompts import (
    ATTRIBUTES_PROMPT,
    EDGES_PROMPT_VERSION,
    NODES_PROMPT_VERSION,
    attribute_messages,
    breadcrumb,
    edge_instructions,
    episode_text,
    node_instructions,
)
from graph.build.terms import EdgeCatalog, UnitCatalog, hub_name_index, named_in
from graph.build.vectors import Vector, get_many, put_many
from graph.core.errors import LLMError, LLMFatalError
from graph.core.llm import Gateway
from graph.core.llm.gateway import prompt_name
from graph.core.ontology import Ontology
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.extract")

MIN_EDGE_CATALOG: Final = 2
"""Nodes an edge catalog needs to hold an edge."""
MAX_SPLIT_DEPTH: Final = 4
"""Halvings of one window after truncated answers before its fragments are skipped."""
ATTRIBUTE_BATCH_NODES: Final = 20
"""Nodes per attributes call at most."""
ATTRIBUTE_BATCH_TOKENS: Final = 12_000
"""Estimated tokens of distinct fragment text per attributes call (a single node may exceed it)."""
ATTRIBUTE_NODE_FRAGMENTS: Final = 3
"""Fragments shown per node in the attributes pass: its dirty fragments first, then its others, each
in document order (shown in document order)."""
BATCH_MODELS: Final = 256
"""Attribute batch output models kept (one per ontology and label sequence)."""
KnownTermsSource = Literal["hub_catalog", "persisted", "none"]
NODE_KIND: Final[ExtractionKind] = "nodes"
EDGE_KIND: Final[ExtractionKind] = "edges"
ATTRIBUTE_KIND: Final[ExtractionKind] = "attributes"
REUSED_CALL: Final = "reused"
"""``call_id`` suffix of a raw node reused from the store (``<unit_id>@<i>-<i+1>:reused``)."""
NODE_OPTIONS: Final = frozenset(
    {"fragment_tokens", "hub_catalog", "known_terms_limit", "unit_tokens", "max_node_windows"}
)
EDGE_OPTIONS: Final = frozenset({"fragment_tokens", "catalog_limit", "unit_tokens"})


# Stored results and extractor fingerprints


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def graphiti_version() -> str:
    """The installed ``graphiti-core`` version (its prompts and parsing shape the node and edge calls),
    read from the package metadata without importing it; ``unknown`` when it is not installed."""

    try:
        return version("graphiti-core")
    except PackageNotFoundError:
        return "unknown"


def node_fingerprint(ontology: Ontology, gateway: Gateway, options: BuildOptions) -> str:
    """Node extractor fingerprint: the ontology's ``nodes`` prompt part, the prompt versions, the
    installed ``graphiti-core`` version, the extraction model and the options that shape the windows
    and known terms."""

    return _digest(
        {
            "ontology": ontology.fingerprint("nodes"),
            "prompt": [NODES_PROMPT_VERSION, NODES_PROMPT],
            "graphiti": graphiti_version(),
            "model": gateway.config.model_for("extraction"),
            "options": options.model_dump(mode="json", include=set(NODE_OPTIONS)),
        }
    )


def edge_fingerprint(ontology: Ontology, gateway: Gateway, options: BuildOptions) -> str:
    """Edge extractor fingerprint: the ontology's ``edges`` prompt part, the prompt versions, the
    installed ``graphiti-core`` version, the extraction model and the options that shape a unit and
    its catalog."""

    return _digest(
        {
            "ontology": ontology.fingerprint("edges"),
            "prompt": [EDGES_PROMPT_VERSION, EDGES_PROMPT],
            "graphiti": graphiti_version(),
            "model": gateway.config.model_for("extraction"),
            "options": options.model_dump(mode="json", include=set(EDGE_OPTIONS)),
        }
    )


def attribute_fingerprint(ontology: Ontology, gateway: Gateway) -> str:
    """Attributes fingerprint: the ontology's ``attributes`` prompt part, the prompt version and the small model."""

    return _digest(
        {
            "ontology": ontology.fingerprint("attributes"),
            "prompt": ATTRIBUTES_PROMPT,
            "model": gateway.config.model_for("small"),
        }
    )


@dataclass(frozen=True, slots=True)
class StoredResults:
    """The project's extraction results in the ``ExtractionStore``: every build writes them, only an
    ``update`` build reads them (``read``)."""

    store: ExtractionStore
    project_id: str
    read: bool = False

    def node_key(self, fragment: FragmentRec, content_hash: str) -> str:
        """Nodes of one fragment version. The heading path (which carries the document's title heading)
        is part of the key because the node prompt shows it as the fragment's breadcrumb: a renamed
        heading or title must not reuse nodes extracted under the old one."""

        path = hashlib.sha256("\n".join(fragment.heading_path).encode("utf-8")).hexdigest()[:16]
        return f"{self.project_id}|{NODE_KIND}|{fragment.document_id}|{content_hash}|{path}"

    def edge_key(self, unit_hash: str, catalog_keys: Sequence[str]) -> str:
        catalog = hashlib.sha256("\n".join(sorted(catalog_keys)).encode("utf-8")).hexdigest()
        return f"{self.project_id}|{EDGE_KIND}|{unit_hash}|{catalog}"

    def last_edge_key(self, unit_hash: str) -> str:
        """The last answered edges of a unit content whatever its catalog (the fallback of a failed
        call), with that catalog's nearness hubs (a clean unit's catalog in the next ``update``)."""

        return f"{self.project_id}|{EDGE_KIND}_last|{unit_hash}"

    def unit_edge_key(self, document_id: str, first_fragment_id: str) -> str:
        """The last answered edges of a unit by identity (its document and first fragment), whatever
        its content: the fallback of a failed call when no edges are stored for its content."""

        return f"{self.project_id}|{EDGE_KIND}_unit|{document_id}|{first_fragment_id}"

    def attribute_key(self, identity_key: str, label: str, content_hashes: Sequence[str]) -> str:
        evidence = hashlib.sha256("\n".join(sorted(content_hashes)).encode("utf-8")).hexdigest()
        return f"{self.project_id}|{ATTRIBUTE_KIND}|{identity_key}|{label}|{evidence}"


async def stored_node_fragments(results: StoredResults, fingerprint: str, fragments: Sequence[FragmentRec]) -> set[str]:
    """Ids of ``fragments`` whose nodes are stored for their current content under ``fingerprint``."""

    keys = {results.node_key(fragment, fragment.content_hash): fragment.fragment_id for fragment in fragments}
    found = await get_many(results.store, NODE_KIND, sorted(keys), fingerprint)
    return {keys[key] for key in found}


async def stored_edge_units(results: StoredResults, fingerprint: str, units: Sequence[Unit]) -> set[str]:
    """Ids of ``units`` whose content has a stored last edge answer under ``fingerprint`` (the edges an
    ``update`` build reuses when the unit's catalog is unchanged; ``estimate_build``)."""

    keys = {results.last_edge_key(unit.content_hash): unit.unit_id for unit in units}
    found = await get_many(results.store, EDGE_KIND, sorted(keys), fingerprint)
    return {keys[key] for key in found}


async def stored_attribute_nodes[T](
    results: StoredResults, fingerprint: str, inputs: Mapping[T, tuple[str, str, Sequence[str]]]
) -> set[T]:
    """The nodes of ``inputs`` (identity key, label and fragment content hashes each) whose attribute
    values are stored under ``fingerprint`` (an ``update`` build reuses them; ``estimate_build``)."""

    keys = [(results.attribute_key(*value), node) for node, value in inputs.items()]
    found = await get_many(results.store, ATTRIBUTE_KIND, sorted({key for key, _ in keys}), fingerprint)
    return {node for key, node in keys if key in found}


def _stored_nodes(payload: JsonObject) -> list[tuple[str, str, Attribution, tuple[str, ...], int]]:
    """``(label, name, attribution, fragments, count)`` of a stored node entry (``fragments`` empty and
    ``count`` 1 when the item has none); malformed items are left out."""

    items: list[tuple[str, str, Attribution, tuple[str, ...], int]] = []
    for item in payload.get("nodes") or ():
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("label"), str):
            attribution = item.get("attribution")
            fragments = tuple(fid for fid in item.get("fragments") or () if isinstance(fid, str))
            count = item.get("count")
            items.append(
                (
                    item["label"],
                    item["name"],
                    "window_fallback" if attribution == "window_fallback" else "graphiti",
                    fragments,
                    count if isinstance(count, int) and not isinstance(count, bool) and count > 1 else 1,
                )
            )
    return items


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


async def _gather_with[A, B](many: Sequence[Awaitable[A]], last: Awaitable[B]) -> tuple[list[A], B]:
    """``many`` and ``last`` run together: the results of ``many`` in order and the result of ``last``; on the
    first failure the others are cancelled and the failure raised."""

    tasks = [asyncio.ensure_future(awaitable) for awaitable in many]
    final = asyncio.ensure_future(last)
    everything: list[asyncio.Future[A] | asyncio.Future[B]] = [*tasks, final]
    try:
        await asyncio.gather(*everything)
    except BaseException:
        for task in everything:
            task.cancel()
        await asyncio.gather(*everything, return_exceptions=True)
        raise
    return [task.result() for task in tasks], final.result()


@dataclass
class _Tally:
    windows: int = 0
    calls: int = 0
    failed_calls: int = 0
    truncation_splits: int = 0
    window_fallbacks: int = 0
    default_attributions: int = 0
    label_collapses: int = 0


@dataclass
class _WindowResult:
    nodes: list[tuple[Window, str, str, tuple[str, ...], bool]] = field(default_factory=list)
    """``(window, label, name, fragment ids, fallback)`` per node."""
    skipped: dict[str, str] = field(default_factory=dict)
    covered: list[Window] = field(default_factory=list)
    """Windows whose call answered (their fragments' results are this build's)."""


def halve(unit: Unit, window: Window, fragments_by_id: Mapping[str, FragmentRec]) -> tuple[Window, Window]:
    """Split a window of at least two fragments where the two halves' token counts differ least
    (the earlier split point on a tie)."""

    start, end = window
    tokens = [fragments_by_id[fragment_id].tokens for fragment_id in unit.fragment_ids[start:end]]
    total = sum(tokens)
    best, best_gap, left = start + 1, None, 0
    for offset in range(1, end - start):
        left += tokens[offset - 1]
        gap = abs(total - 2 * left)
        if best_gap is None or gap < best_gap:
            best, best_gap = start + offset, gap
    return (start, best), (best, end)


@dataclass(frozen=True)
class _CallEnv:
    """What the extraction calls of every unit share: the plan's dirty fragments, the Graphiti clients and
    the gateway and ontology they run under."""

    dirty: set[str]
    fragments_by_id: Mapping[str, FragmentRec]
    clients: GraphitiClients
    gateway: Gateway
    ontology: Ontology


class _UnitRun:
    """The windows of one unit: shared episodes, known terms and the call's collaborators."""

    def __init__(
        self,
        unit: Unit,
        *,
        ordinal: int,
        episodes: list[str],
        terms: Sequence[str],
        env: _CallEnv,
        tally: _Tally,
    ) -> None:
        self.unit = unit
        self.ordinal = ordinal
        self.episodes = episodes
        self.terms = terms
        self.dirty = env.dirty
        self.fragments_by_id = env.fragments_by_id
        self.clients = env.clients
        self.gateway = env.gateway
        ontology = env.ontology
        self.ontology = ontology
        self.tally = tally
        self.labels = frozenset(ontology.extracted_labels)
        self.entity_types = ontology.entity_models()

    async def run(self, window: Window, depth: int = 0) -> _WindowResult:
        start, end = window
        instructions = node_instructions(
            self.ontology,
            self.gateway,
            known_terms=self.terms,
            window=window,
            episodes=len(self.episodes),
        )
        self.tally.calls += 1
        try:
            call = await extract_nodes_call(self.clients, self.episodes, self.entity_types, instructions)
        except LLMFatalError:
            raise
        except LLMError as error:
            if error.reason == "truncated" and end - start > 1 and depth < MAX_SPLIT_DEPTH:
                self.tally.truncation_splits += 1
                halves = halve(self.unit, window, self.fragments_by_id)
                parts = await _gather([self.run(half, depth + 1) for half in halves])
                return _WindowResult(
                    nodes=[node for part in parts for node in part.nodes],
                    skipped={key: value for part in parts for key, value in part.skipped.items()},
                    covered=[window for part in parts for window in part.covered],
                )
            self.tally.failed_calls += 1
            skipped = {
                fragment_id: error.code
                for fragment_id in self.unit.fragment_ids[start:end]
                if fragment_id in self.dirty
            }
            log_event(
                logger,
                WARNING,
                "graph.extract.window_skipped",
                unit=self.ordinal,
                window_start=start,
                window_end=end,
                code=error.code,
                fragments=len(skipped),
                exc_info=True,
            )
            return _WindowResult(skipped=skipped)
        self.tally.label_collapses += call.label_collapses
        index_map = call.index_map
        typed = [node for node in call.nodes if node.label in self.labels]
        default = (
            end - start > 1
            and len(self.episodes) > 1
            and len(typed) > 1
            and all(index_map.get(node.uuid) == [0] for node in typed)
        )
        if default:
            self.tally.default_attributions += 1
        window_ids = self.unit.fragment_ids[start:end]
        result = _WindowResult(covered=[window])
        for node in typed:
            indices = [] if default else [index for index in index_map.get(node.uuid, ()) if start <= index < end]
            if indices:
                result.nodes.append(
                    (window, node.label, node.name, tuple(self.unit.fragment_ids[index] for index in indices), False)
                )
            else:
                self.tally.window_fallbacks += 1
                result.nodes.append((window, node.label, node.name, window_ids, True))
        return result


async def extract_nodes(
    plan: PlanArtifact,
    *,
    gateway: Gateway,
    ontology: Ontology,
    options: BuildOptions,
    known_terms: Mapping[str, Sequence[str]],
    known_terms_source: KnownTermsSource,
    results: StoredResults | None = None,
) -> NodesArtifact:
    """Raw nodes of every window with dirty fragments, the reused nodes of the other fragments, the
    skipped fragments by error code, and counters.

    ``known_terms`` maps a unit id to the terms shown to its calls. Windows come from the plan. The
    nodes of every fragment inside a window that answered are stored (``results``, an empty list for
    a fragment without nodes). With ``results.read`` (``update`` builds), a fragment outside every
    answered window reuses its stored nodes: an unchanged fragment those of its content (or, when it
    is ``skipped``, of its ``extracted_hash``); a dirty fragment whose window failed those of its
    ``extracted_hash``, the last extracted version, so the nodes stay grounded while the fragment is
    ``skipped`` (FR-BLD-12). A reused node is tied to the fragment alone and marked ``reused``.

    Raw nodes are ordered by unit, window, reuse, label and name; ``raw_id`` is
    ``<call_id>#<n>`` with ``call_id`` ``<unit_id>@<start>-<end>`` (``:reused`` appended for reused nodes).
    """

    dirty = set(plan.dirty_ids)
    fragments_by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    titles = {document.document_id: document.title for document in plan.documents}
    env = _CallEnv(dirty, fragments_by_id, graphiti_clients(gateway), gateway, ontology)
    tally = _Tally()
    used_terms: set[str] = set()
    jobs: list[tuple[int, Awaitable[_WindowResult]]] = []
    units = list(plan.units)
    for unit_index, unit in enumerate(units):
        windows = [
            window for window in unit.windows if any(fid in dirty for fid in unit.fragment_ids[window[0] : window[1]])
        ]
        if not windows:
            continue
        fragments = [fragments_by_id[fragment_id] for fragment_id in unit.fragment_ids]
        title = titles.get(unit.document_id, unit.document_id)
        terms = list(known_terms.get(unit.unit_id, ()))
        used_terms.update(terms)
        run = _UnitRun(
            unit,
            ordinal=unit_index,
            episodes=[episode_text(gateway, title, fragment.heading_path, fragment.text) for fragment in fragments],
            terms=terms,
            env=env,
            tally=tally,
        )
        tally.windows += len(windows)
        jobs.extend((unit_index, run.run(window)) for window in windows)
    answers = await _gather([job for _, job in jobs])

    collected: list[tuple[tuple[int, int, int, bool, str, str], RawNode]] = []
    skipped: dict[str, str] = {}
    covered: set[str] = set()
    for (unit_index, _), result in zip(jobs, answers, strict=True):
        unit = units[unit_index]
        skipped.update(result.skipped)
        for start, end in result.covered:
            covered.update(unit.fragment_ids[start:end])
        for window, label, name, fragment_ids, fallback in result.nodes:
            node = RawNode(
                raw_id="",
                name=name,
                label=label,
                unit_id=unit.unit_id,
                call_id=f"{unit.unit_id}@{window[0]}-{window[1]}",
                window=window,
                fragment_ids=fragment_ids,
                attribution="window_fallback" if fallback else "graphiti",
            )
            collected.append(((unit_index, window[0], window[1], False, label, name), node))
    reused, supported = 0, 0
    if results is not None:
        fingerprint = node_fingerprint(ontology, gateway, options)
        await _store_nodes(results, fingerprint, collected, covered, fragments_by_id)
        if results.read:
            reused, supported = await _reuse_nodes(
                results,
                fingerprint,
                units,
                fragments_by_id,
                dirty=dirty,
                covered=covered,
                skipped=skipped,
                collected=collected,
            )
    collected.sort(key=lambda item: item[0])
    raw_nodes = []
    numbers: dict[str, int] = {}
    for _, node in collected:
        number = numbers.get(node.call_id, 0)
        numbers[node.call_id] = number + 1
        raw_nodes.append(node.model_copy(update={"raw_id": f"{node.call_id}#{number}"}))
    order = {fragment.fragment_id: index for index, fragment in enumerate(plan.fragments)}
    skipped = dict(sorted(skipped.items(), key=lambda item: order.get(item[0], len(order))))
    counters = NodeCounters(
        windows=tally.windows,
        calls=tally.calls,
        failed_calls=tally.failed_calls,
        truncation_splits=tally.truncation_splits,
        window_fallbacks=tally.window_fallbacks,
        default_attributions=tally.default_attributions,
        fragments_reused=reused,
        fragments_supported=supported,
        known_terms=len(used_terms),
        label_collapses=tally.label_collapses,
    )
    log_event(
        logger,
        INFO,
        "graph.extract.nodes_done",
        units=len({unit_index for unit_index, _ in jobs}),
        windows=tally.windows,
        calls=tally.calls,
        failed_calls=tally.failed_calls,
        raw_nodes=len(raw_nodes),
        reused_nodes=sum(1 for node in raw_nodes if node.reused),
        fragments_reused=reused,
        fragments_supported=supported,
        skipped_fragments=len(skipped),
        window_fallbacks=tally.window_fallbacks,
        default_attributions=tally.default_attributions,
        truncation_splits=tally.truncation_splits,
        label_collapses=tally.label_collapses,
    )
    return NodesArtifact(
        raw_nodes=tuple(raw_nodes), skipped=skipped, known_terms_source=known_terms_source, counters=counters
    )


async def _store_nodes(
    results: StoredResults,
    fingerprint: str,
    collected: Sequence[tuple[Any, RawNode]],
    covered: set[str],
    fragments_by_id: Mapping[str, FragmentRec],
) -> None:
    """Store the nodes of every covered fragment (by Graphiti's attribution), sorted by label, name and
    fragments. Each item carries the raw node's ``fragments`` (all its fragment ids) and, when one call
    gave it more than once, its ``count``, so a reuse gives back each raw node once (``_reuse_nodes``)."""

    by_fragment: dict[str, Counter[tuple[str, str, str, tuple[str, ...]]]] = {fid: Counter() for fid in covered}
    for _, node in collected:
        origin = tuple(sorted(set(node.fragment_ids)))
        for fid in origin:
            if fid in by_fragment:
                by_fragment[fid][(node.label, node.name, node.attribution, origin)] += 1
    entries: dict[str, JsonObject] = {}
    for fid in sorted(by_fragment):
        fragment = fragments_by_id[fid]
        entries[results.node_key(fragment, fragment.content_hash)] = {
            "nodes": [
                {
                    "label": label,
                    "name": name,
                    "attribution": attribution,
                    "fragments": list(origin),
                    **({"count": count} if count > 1 else {}),
                }
                for (label, name, attribution, origin), count in sorted(by_fragment[fid].items())
            ]
        }
    await put_many(results.store, NODE_KIND, fingerprint, entries)


async def _wanted_node_keys(
    results: StoredResults,
    fragments_by_id: Mapping[str, FragmentRec],
    *,
    dirty: set[str],
    covered: set[str],
    skipped: Mapping[str, str],
) -> dict[str, list[str]]:
    """The stored-answer keys to read per fragment outside every answered window: its content hash,
    and for a skipped fragment also the hash it was last extracted at."""

    wanted: dict[str, list[str]] = {}
    for fid, fragment in fragments_by_id.items():
        if fid in covered:
            continue
        hashes: list[str] = []
        if fid not in dirty:
            hashes.append(fragment.content_hash)
            if fragment.status == "skipped" and fragment.extracted_hash:
                hashes.append(fragment.extracted_hash)
        elif fid in skipped and fragment.extracted_hash:
            hashes.append(fragment.extracted_hash)
        keys = [results.node_key(fragment, value) for value in dict.fromkeys(hashes)]
        if keys:
            wanted[fid] = keys
    return wanted


def _reused_unit_nodes(
    unit: Unit,
    unit_index: int,
    found: Mapping[str, JsonObject],
    wanted: Mapping[str, list[str]],
    *,
    fragments_by_id: Mapping[str, FragmentRec],
    skipped: Mapping[str, str],
) -> tuple[int, int, list[tuple[tuple[int, int, int, bool, str, str], RawNode]]]:
    """The reused nodes of one unit: ``(fragments reused, skipped ones among them, nodes)``."""

    reused = supported = 0
    collected: list[tuple[tuple[int, int, int, bool, str, str], RawNode]] = []
    groups: dict[tuple[str, str, Attribution, tuple[str, ...], int], list[int]] = {}
    for position, fid in enumerate(unit.fragment_ids):
        payload = next((found[key] for key in wanted.get(fid, ()) if key in found), None)
        if payload is None:
            continue
        reused += 1
        fragment = fragments_by_id[fid]
        if fid in skipped or fragment.status == "skipped":
            supported += 1
        for label, name, attribution, origin, count in _stored_nodes(payload):
            positions = groups.setdefault((label, name, attribution, origin or (fid,), count), [])
            positions.append(position)
    for (label, name, attribution, _, count), positions in groups.items():
        window = (min(positions), max(positions) + 1)
        for _ in range(count):
            node = RawNode(
                raw_id="",
                name=name,
                label=label,
                unit_id=unit.unit_id,
                call_id=f"{unit.unit_id}@{window[0]}-{window[1]}:{REUSED_CALL}",
                window=window,
                fragment_ids=tuple(unit.fragment_ids[position] for position in sorted(set(positions))),
                attribution=attribution,
                reused=True,
            )
            collected.append(((unit_index, window[0], window[1], True, label, name), node))
    return reused, supported, collected


async def _reuse_nodes(
    results: StoredResults,
    fingerprint: str,
    units: Sequence[Unit],
    fragments_by_id: Mapping[str, FragmentRec],
    *,
    dirty: set[str],
    covered: set[str],
    skipped: Mapping[str, str],
    collected: list[tuple[tuple[int, int, int, bool, str, str], RawNode]],
) -> tuple[int, int]:
    """Append the reused nodes of the fragments outside every answered window to ``collected``;
    returns the fragments reused and, among them, the skipped ones kept by their last extracted version.

    A stored node is one raw node of the call that gave it, on all its ``fragments``; the reused
    fragments of a unit that hold it give it back once, on those fragments (window: from the first to
    the last of them), as many times as that call gave it. An item without ``fragments`` (stored by
    an earlier release) stands for a node of its fragment alone."""

    wanted = await _wanted_node_keys(results, fragments_by_id, dirty=dirty, covered=covered, skipped=skipped)
    found = await get_many(
        results.store, NODE_KIND, sorted({key for keys in wanted.values() for key in keys}), fingerprint
    )
    reused = supported = 0
    for unit_index, unit in enumerate(units):
        unit_reused, unit_supported, nodes = _reused_unit_nodes(
            unit, unit_index, found, wanted, fragments_by_id=fragments_by_id, skipped=skipped
        )
        reused += unit_reused
        supported += unit_supported
        collected.extend(nodes)
    return reused, supported


# Edges


def catalog_names(nodes: Sequence[CanonicalNode]) -> dict[str, str]:
    """A unique catalog name per cid (Graphiti keys nodes by exact name; tech-specs 5.5).

    Names are document-derived, so each is first made safe for a prompt (``prompt_name``: marker runs
    defused, at most 200 characters). A name used once stays as it is; a name shared by several nodes
    becomes ``<name> (<Label>)``; a name still taken gets `` #2``, `` #3`` in cid order.
    """

    shown = {node.cid: prompt_name(node.name) for node in nodes}
    counts: dict[str, int] = {}
    for node in nodes:
        counts[shown[node.cid]] = counts.get(shown[node.cid], 0) + 1
    names: dict[str, str] = {}
    taken: set[str] = set()
    for node in sorted(nodes, key=lambda node: node.cid):
        name = shown[node.cid]
        base = name if counts[name] == 1 else f"{name} ({node.label})"
        name, number = base, 1
        while name in taken:
            number += 1
            name = f"{base} #{number}"
        taken.add(name)
        names[node.cid] = name
    return names


@dataclass
class _EdgeTally:
    calls: int = 0
    failed_calls: int = 0
    truncation_splits: int = 0
    attribution_fallbacks: int = 0
    unknown_name_edges: int = 0
    unknown_relation_edges: int = 0


@dataclass
class _EdgeResult:
    edges: list[RawEdge] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    complete: bool = True
    """Every call of the unit answered (only then is the result stored)."""


class _EdgeUnitRun:
    """The edge call of one unit over a window of its episodes (the whole unit unless truncated)."""

    def __init__(
        self,
        unit: Unit,
        *,
        ordinal: int,
        episodes: list[str],
        catalog: Sequence[CanonicalNode],
        env: _CallEnv,
        tally: _EdgeTally,
    ) -> None:
        self.unit = unit
        self.ordinal = ordinal
        self.episodes = episodes
        names = catalog_names(catalog)
        self.catalog = [(names[node.cid], node.label) for node in catalog]
        self.cid_by_name = {name: cid for cid, name in names.items()}
        self.dirty = env.dirty
        self.fragments_by_id = env.fragments_by_id
        self.clients = env.clients
        ontology = env.ontology
        self.ontology = ontology
        self.tally = tally
        self.edge_types = ontology.edge_models()
        self.edge_type_map = ontology.edge_type_map()

    async def run(self, window: Window, depth: int = 0) -> _EdgeResult:
        start, end = window
        window_ids = self.unit.fragment_ids[start:end]
        instructions = edge_instructions(self.ontology, window=window, episodes=len(self.episodes))
        self.tally.calls += 1
        try:
            call = await extract_edges_call(
                self.clients,
                self.episodes,
                self.catalog,
                self.edge_types,
                self.edge_type_map,
                instructions=instructions,
            )
        except LLMFatalError:
            raise
        except LLMError as error:
            if error.reason == "truncated" and end - start > 1 and depth < MAX_SPLIT_DEPTH:
                self.tally.truncation_splits += 1
                parts = await _gather(
                    [self.run(half, depth + 1) for half in halve(self.unit, window, self.fragments_by_id)]
                )
                return _EdgeResult(
                    edges=[edge for part in parts for edge in part.edges],
                    skipped={key: value for part in parts for key, value in part.skipped.items()},
                    complete=all(part.complete for part in parts),
                )
            self.tally.failed_calls += 1
            skipped = {fragment_id: error.code for fragment_id in window_ids if fragment_id in self.dirty}
            log_event(
                logger,
                WARNING,
                "graph.extract.edges_skipped",
                unit=self.ordinal,
                window_start=start,
                window_end=end,
                code=error.code,
                fragments=len(skipped),
                exc_info=True,
            )
            return _EdgeResult(skipped=skipped, complete=False)
        self.tally.unknown_name_edges += call.unknown_names
        self.tally.unknown_relation_edges += call.unknown_relations
        fallback = tuple(fragment_id for fragment_id in window_ids if fragment_id in self.dirty) or window_ids
        result = _EdgeResult()
        for edge in call.edges:
            source = self.cid_by_name.get(edge.source_name)
            target = self.cid_by_name.get(edge.target_name)
            if source is None or target is None or source == target:
                continue
            indices = [index for index in edge.episode_indices if start <= index < end]
            if not indices:
                self.tally.attribution_fallbacks += 1
            result.edges.append(
                RawEdge(
                    relation=edge.relation,
                    source_cid=source,
                    target_cid=target,
                    fact=edge.fact.strip() or None,
                    fragment_ids=tuple(self.unit.fragment_ids[index] for index in indices) or fallback,
                    unit_id=self.unit.unit_id,
                )
            )
        return result


@dataclass(frozen=True)
class _EdgeContext:
    """The read-only inputs of one ``extract_edges`` run."""

    plan: PlanArtifact
    units: list[Unit]
    dirty: set[str]
    fragments_by_id: Mapping[str, FragmentRec]
    by_cid: Mapping[str, CanonicalNode]
    cid_of: Mapping[str, str]
    results: StoredResults | None
    reading: bool
    fingerprint: str


@dataclass
class _EdgeSelection:
    """The catalog of each unit that makes a call: its cids, selection and stored-answer key."""

    catalogs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    selections: dict[int, UnitCatalog] = field(default_factory=dict)
    keys: dict[int, str] = field(default_factory=dict)
    kept_catalogs: int = 0


@dataclass
class _EdgeBook:
    """What the units of one ``extract_edges`` run give: edges, entries to store and outcomes."""

    collected: list[tuple[tuple[int, str, str, str, str], RawEdge]] = field(default_factory=list)
    entries: dict[str, JsonObject] = field(default_factory=dict)
    answered: set[int] = field(default_factory=set)
    """Units whose edges are this build's or reused for their content and catalog (or need no call)."""
    reused_units: int = 0
    jobs: list[tuple[int, Awaitable[_EdgeResult]]] = field(default_factory=list)
    fallbacks: dict[int, list[RawEdge]] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    edge_failed: list[str] = field(default_factory=list)


async def _select_catalogs(ctx: _EdgeContext, selector: EdgeCatalog) -> _EdgeSelection:
    """The catalog of every unit to consider: all of them when reading stored results, else the dirty ones."""

    selection = _EdgeSelection()
    clean = {
        unit_index
        for unit_index, unit in enumerate(ctx.units)
        if not any(fragment_id in ctx.dirty for fragment_id in unit.fragment_ids)
    }
    results = ctx.results
    last_answers: dict[str, JsonObject] = {}
    if results is not None and ctx.reading:
        last_answers = await get_many(
            results.store,
            EDGE_KIND,
            sorted({results.last_edge_key(ctx.units[unit_index].content_hash) for unit_index in clean}),
            ctx.fingerprint,
        )
    for unit_index, unit in enumerate(ctx.units):
        if not ctx.reading and unit_index in clean:
            continue
        kept = None
        if results is not None and unit_index in clean:
            kept = _kept_near(last_answers.get(results.last_edge_key(unit.content_hash)), ctx.cid_of)
            selection.kept_catalogs += kept is not None
        chosen = selection.selections[unit_index] = selector.select(unit, kept)
        cids = chosen.cids
        selection.catalogs[unit.unit_id] = tuple(cids)
        if len(cids) >= MIN_EDGE_CATALOG and results is not None:
            selection.keys[unit_index] = results.edge_key(
                unit.content_hash, [ctx.by_cid[cid].identity_key for cid in cids]
            )
    return selection


def _queue_edge_units(
    ctx: _EdgeContext,
    selection: _EdgeSelection,
    found: Mapping[str, JsonObject],
    book: _EdgeBook,
    make_run: Callable[[Unit, int, Sequence[str]], _EdgeUnitRun],
) -> None:
    """Reuse the stored edges of each unit whose content and catalog have them; queue a call for the rest."""

    results = ctx.results
    for unit_index, unit in enumerate(ctx.units):
        cids = selection.catalogs.get(unit.unit_id)
        if cids is None:
            continue
        if len(cids) < MIN_EDGE_CATALOG:
            book.answered.add(unit_index)
            continue
        payload = found.get(selection.keys.get(unit_index, ""))
        if payload is not None:
            book.reused_units += 1
            book.answered.add(unit_index)
            book.collected.extend(
                (_edge_order(unit_index, edge), edge) for edge in _stored_edges(payload, unit, ctx.cid_of)
            )
            if results is not None:
                book.entries[results.last_edge_key(unit.content_hash)] = payload
                book.entries[results.unit_edge_key(unit.document_id, unit.fragment_ids[0])] = payload
            continue
        run = make_run(unit, unit_index, cids)
        book.jobs.append((unit_index, run.run((0, len(unit.fragment_ids)))))


def _settle_edge_answers(
    ctx: _EdgeContext, selection: _EdgeSelection, answers: Sequence[_EdgeResult], book: _EdgeBook
) -> None:
    """Take the edges (or the fallback edges) of every called unit and the entries to store."""

    results = ctx.results
    for (unit_index, _), result in zip(book.jobs, answers, strict=True):
        unit = ctx.units[unit_index]
        if not result.complete and unit_index not in book.fallbacks:
            book.edge_failed.extend(unit.fragment_ids)
        if unit_index in book.fallbacks:
            book.collected.extend((_edge_order(unit_index, edge), edge) for edge in book.fallbacks[unit_index])
            continue
        book.skipped.update(result.skipped)
        book.collected.extend((_edge_order(unit_index, edge), edge) for edge in result.edges)
        if result.complete:
            book.answered.add(unit_index)
        if result.complete and unit_index in selection.keys and results is not None:
            near = [ctx.by_cid[cid].identity_key for cid in selection.selections[unit_index].near]
            payload = _edge_payload(result.edges, unit, ctx.by_cid, near)
            book.entries[selection.keys[unit_index]] = payload
            book.entries[results.last_edge_key(unit.content_hash)] = payload
            book.entries[results.unit_edge_key(unit.document_id, unit.fragment_ids[0])] = payload


async def extract_edges(
    plan: PlanArtifact,
    resolved: ResolveArtifact,
    *,
    services: StepServices,
    fragment_vectors: Mapping[str, Vector],
    name_vectors: Mapping[str, Vector],
    results: StoredResults | None = None,
    node_skipped: Collection[str] = (),
    hub_aliases: Mapping[str, Sequence[str]] | None = None,
) -> EdgesArtifact:
    """Raw edges of the units, the attributes pass, catalogs and skipped fragments.

    Per unit: the catalog (``terms.EdgeCatalog`` with ``options.catalog_limit``) under unique names,
    one Graphiti ``extract_edges`` call with all unit fragments as episodes, names mapped back to cids.
    The relation is kept as returned (Conform legalises it), the fact as given (Conform snaps it), the
    edge's fragments from Graphiti's attribution (none → the window's dirty fragments). A unit whose
    catalog has fewer than two nodes makes no call. The attributes pass runs concurrently; its result
    is ``attributes`` (the nodes of failed batches: ``attributes_failed``; ``_fill_attributes``).
    ``node_skipped``: the fragments the node step skipped this build. ``hub_aliases``: more identity
    keys per hub cid whose ``surface`` values find it by mention (the persisted node it continues). Raw
    edges are ordered by
    unit, source, relation, target and fact. ``fragment_vectors`` (by fragment id) and
    ``name_vectors`` (the ``name_embedding`` by cid) rank the catalog's foreign hubs.

    Without ``results.read`` the units with dirty fragments are extracted. With it (``update``
    builds) every unit is considered: a clean unit (no dirty fragment) whose last answer is stored for
    its content keeps that answer's nearness hubs (``EdgeCatalog.select`` with ``kept``;
    ``catalogs_kept``), every other unit gets the full catalog rule; a unit whose content and catalog
    (identity keys) have stored edges reuses them (FR-BLD-22), the others are extracted
    (``called_units``). A unit whose calls all answered is stored, also under its content alone
    (``last_edge_key``, with its catalog's nearness hubs) and under its identity (``unit_edge_key``:
    document and first fragment). In an ``update`` build a unit whose call fails falls back to the
    last edges of the same content, else of the same unit identity (its first fragment's id or the id
    it replaces), mapped to this build's nodes by identity key or alias where both endpoints still
    exist (``units_fallback``; nothing is skipped); without them its dirty fragments are skipped and
    all its fragments are ``edge_failed_fragments`` (Diff keeps their current edges).

    ``recovered`` (``update`` builds): fragments persisted ``skipped`` and not dirty whose nodes are
    stored for their current content and whose unit's edges answered or were reused (or whose unit
    needs no call), so a fragment skipped only for a failed edge call becomes ``active`` again.
    ``services`` gives the gateway, ontology and options.
    """

    gateway, ontology, options = services.gateway, services.ontology, services.options
    dirty = set(plan.dirty_ids)
    fragments_by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    titles = {document.document_id: document.title for document in plan.documents}
    canonical = list(resolved.canonical)
    hub_cids = frozenset(node.cid for node in canonical if node.label in ontology.hub_labels)
    env = _CallEnv(dirty, fragments_by_id, graphiti_clients(gateway), gateway, ontology)
    tally = _EdgeTally()
    selector = EdgeCatalog(
        fragments_by_id=fragments_by_id,
        canonical=canonical,
        hub_cids=hub_cids,
        name_vectors=name_vectors,
        fragment_vectors=fragment_vectors,
        limit=options.catalog_limit,
        aliases=hub_aliases,
    )
    ctx = _EdgeContext(
        plan=plan,
        units=list(plan.units),
        dirty=dirty,
        fragments_by_id=fragments_by_id,
        by_cid={node.cid: node for node in canonical},
        cid_of=_cids_by_key(canonical),
        results=results,
        reading=results is not None and results.read,
        fingerprint=edge_fingerprint(ontology, gateway, options),
    )
    selection = await _select_catalogs(ctx, selector)
    found = (
        await get_many(results.store, EDGE_KIND, sorted(set(selection.keys.values())), ctx.fingerprint)
        if results is not None and ctx.reading
        else {}
    )
    book = _EdgeBook()

    def unit_run(unit: Unit, unit_index: int, cids: Sequence[str]) -> _EdgeUnitRun:
        title = titles.get(unit.document_id, unit.document_id)
        return _EdgeUnitRun(
            unit,
            ordinal=unit_index,
            episodes=[
                episode_text(gateway, title, fragments_by_id[fid].heading_path, fragments_by_id[fid].text)
                for fid in unit.fragment_ids
            ],
            catalog=[ctx.by_cid[cid] for cid in cids],
            env=env,
            tally=tally,
        )

    _queue_edge_units(ctx, selection, found, book, unit_run)
    node_fingerprint_ = node_fingerprint(ontology, gateway, options)
    kept = await _kept_versions(plan, dirty, set(node_skipped), results if ctx.reading else None, node_fingerprint_)
    attribute_job = _fill_attributes(
        resolved, plan, gateway=gateway, ontology=ontology, results=results, kept=kept, hub_aliases=hub_aliases
    )
    answers, attributes = await _gather_with([job for _, job in book.jobs], attribute_job)

    if results is not None and ctx.reading:
        failed_units = [
            ctx.units[unit_index]
            for (unit_index, _), result in zip(book.jobs, answers, strict=True)
            if not result.complete
        ]
        fallbacks = await _fallback_edges(results, ctx.fingerprint, failed_units, fragments_by_id, ctx.cid_of)
        book.fallbacks = {
            unit_index: fallbacks[ctx.units[unit_index].unit_id]
            for unit_index, _ in book.jobs
            if ctx.units[unit_index].unit_id in fallbacks
        }
    _settle_edge_answers(ctx, selection, answers, book)
    if results is not None:
        await put_many(results.store, EDGE_KIND, ctx.fingerprint, dict(sorted(book.entries.items())))
    recovered: tuple[str, ...] = ()
    if results is not None and ctx.reading:
        recovered = await _recovered(
            results, node_fingerprint_, ctx.units, book.answered, plan=plan, dirty=dirty, skipped=book.skipped
        )
    return _edges_artifact(ctx, selection, book, tally, attributes, recovered=recovered)


def _edges_artifact(
    ctx: _EdgeContext,
    selection: _EdgeSelection,
    book: _EdgeBook,
    tally: _EdgeTally,
    attributes: _Attributes,
    *,
    recovered: tuple[str, ...],
) -> EdgesArtifact:
    """The artifact of the run (raw edges in order, skipped fragments by plan order), counted and logged."""

    book.collected.sort(key=lambda item: item[0])
    order = {fragment.fragment_id: index for index, fragment in enumerate(ctx.plan.fragments)}
    skipped = dict(sorted(book.skipped.items(), key=lambda item: order.get(item[0], len(order))))
    jobs = book.jobs
    counters = EdgeCounters(
        units_extracted=len(jobs),
        units_reused=book.reused_units,
        units_fallback=len(book.fallbacks),
        catalogs_kept=selection.kept_catalogs,
        calls=tally.calls,
        failed_calls=tally.failed_calls,
        attribute_batches=attributes.batches,
        attribute_batches_failed=attributes.failed_batches,
        attribute_nodes_reused=attributes.reused,
        truncation_splits=tally.truncation_splits,
        attribution_fallbacks=tally.attribution_fallbacks,
        unknown_name_edges=tally.unknown_name_edges,
        unknown_relation_edges=tally.unknown_relation_edges,
    )
    log_event(
        logger,
        INFO,
        "graph.extract.edges_done",
        units=len(jobs),
        units_reused=book.reused_units,
        units_fallback=len(book.fallbacks),
        catalogs_kept=selection.kept_catalogs,
        calls=tally.calls,
        failed_calls=tally.failed_calls,
        raw_edges=len(book.collected),
        reused_edges=sum(1 for _, edge in book.collected if edge.reused),
        skipped_fragments=len(skipped),
        recovered_fragments=len(recovered),
        truncation_splits=tally.truncation_splits,
        attribution_fallbacks=tally.attribution_fallbacks,
        attribute_batches=attributes.batches,
        attribute_batches_failed=attributes.failed_batches,
        attributed_nodes=len(attributes.values),
        attribute_nodes_reused=attributes.reused,
        unknown_name_edges=tally.unknown_name_edges,
        unknown_relation_edges=tally.unknown_relation_edges,
    )
    return EdgesArtifact(
        raw_edges=tuple(edge for _, edge in book.collected),
        attributes=attributes.values,
        attributes_failed=attributes.failed,
        catalogs=selection.catalogs,
        skipped=skipped,
        called_units=tuple(
            ctx.units[unit_index].unit_id for unit_index in sorted(unit_index for unit_index, _ in jobs)
        ),
        recovered=recovered,
        edge_failed_fragments=tuple(sorted(set(book.edge_failed), key=lambda fid: order.get(fid, len(order)))),
        counters=counters,
    )


def _edge_order(unit_index: int, edge: RawEdge) -> tuple[int, str, str, str, str]:
    """Sort key of a raw edge: unit, source, relation, target, fact."""

    return (unit_index, edge.source_cid, edge.relation, edge.target_cid, edge.fact or "")


def _cids_by_key(canonical: Sequence[CanonicalNode]) -> dict[str, str]:
    """Cid by identity key, then by identity alias (a key wins over an alias; the first node wins a tie)."""

    cid_of = {node.identity_key: node.cid for node in canonical}
    for node in canonical:
        for alias in node.identity_aliases:
            cid_of.setdefault(alias, node.cid)
    return cid_of


def _kept_near(payload: JsonObject | None, cid_of: Mapping[str, str]) -> list[str] | None:
    """The cids of the nearness hubs of a stored last answer that still exist; None when the answer
    or its ``near`` list is missing (entries written before catalogs were stored)."""

    near = payload.get("near") if payload is not None else None
    if not isinstance(near, list):
        return None
    return [cid_of[key] for key in near if isinstance(key, str) and key in cid_of]


async def _fallback_edges(
    results: StoredResults,
    fingerprint: str,
    failed: Sequence[Unit],
    fragments_by_id: Mapping[str, FragmentRec],
    cid_of: Mapping[str, str],
) -> dict[str, list[RawEdge]]:
    """The last stored edges of each failed unit, by unit id: those of its content first, else those
    of its identity (document and first fragment, or the id that fragment replaces)."""

    wanted: dict[str, list[tuple[str, bool]]] = {}
    for unit in failed:
        first = fragments_by_id.get(unit.fragment_ids[0])
        candidates = [(results.last_edge_key(unit.content_hash), True)]
        candidates.append((results.unit_edge_key(unit.document_id, unit.fragment_ids[0]), False))
        if first is not None and first.previous_id:
            candidates.append((results.unit_edge_key(unit.document_id, first.previous_id), False))
        wanted[unit.unit_id] = candidates
    found = await get_many(
        results.store, EDGE_KIND, sorted({key for keys in wanted.values() for key, _ in keys}), fingerprint
    )
    edges: dict[str, list[RawEdge]] = {}
    for unit in failed:
        for key, same_content in wanted[unit.unit_id]:
            if key in found:
                edges[unit.unit_id] = _stored_edges(
                    found[key], unit, cid_of, fragments_by_id=None if same_content else fragments_by_id
                )
                break
    return edges


async def _recovered(
    results: StoredResults,
    node_fingerprint_: str,
    units: Sequence[Unit],
    answered: set[int],
    *,
    plan: PlanArtifact,
    dirty: set[str],
    skipped: Mapping[str, str],
) -> tuple[str, ...]:
    """Fragments persisted ``skipped``, not dirty and not skipped now, whose unit's edges are this
    build's (``answered``) and whose nodes are stored for their current content (plan order)."""

    unit_of = {fid: unit_index for unit_index, unit in enumerate(units) for fid in unit.fragment_ids}
    candidates = [
        fragment
        for fragment in plan.fragments
        if fragment.status == "skipped"
        and fragment.fragment_id not in dirty
        and fragment.fragment_id not in skipped
        and unit_of.get(fragment.fragment_id) in answered
    ]
    if not candidates:
        return ()
    stored = await stored_node_fragments(results, node_fingerprint_, candidates)
    return tuple(fragment.fragment_id for fragment in candidates if fragment.fragment_id in stored)


def _edge_payload(
    edges: Sequence[RawEdge], unit: Unit, by_cid: Mapping[str, CanonicalNode], near: Sequence[str] = ()
) -> JsonObject:
    """A unit's raw edges as stored: endpoints by identity key, fragments by position in the unit and
    by id; ``near``: the identity keys of the hubs its catalog took by nearness."""

    position = {fid: index for index, fid in enumerate(unit.fragment_ids)}
    items = [
        {
            "relation": edge.relation,
            "source": by_cid[edge.source_cid].identity_key,
            "target": by_cid[edge.target_cid].identity_key,
            "fact": edge.fact,
            "positions": sorted({position[fid] for fid in edge.fragment_ids if fid in position}),
            "fragments": sorted({fid for fid in edge.fragment_ids if fid in position}),
        }
        for edge in edges
    ]
    items.sort(key=lambda item: (item["source"], item["relation"], item["target"], item["fact"] or ""))
    return {"edges": items, "near": list(near)}


def _stored_edges(
    payload: JsonObject,
    unit: Unit,
    cid_of: Mapping[str, str],
    *,
    fragments_by_id: Mapping[str, FragmentRec] | None = None,
) -> list[RawEdge]:
    """The stored raw edges of a unit on this build's cids, marked ``reused``; an edge whose endpoint
    has no canonical node now, or a malformed item, is left out.

    Fragments come by position (the same unit content), or, with ``fragments_by_id`` (an answer of
    the same unit identity but other content), by id: a stored id that is a fragment of the unit, or
    the ``previous_id`` of one; none left means none (Conform then uses the unit's fragments)."""

    successor: dict[str, str] = {}
    if fragments_by_id is not None:
        for fid in unit.fragment_ids:
            successor[fid] = fid
            previous = fragments_by_id[fid].previous_id if fid in fragments_by_id else None
            if previous:
                successor.setdefault(previous, fid)
    edges = []
    for item in payload.get("edges") or ():
        if not isinstance(item, dict):
            continue
        source, target = cid_of.get(str(item.get("source"))), cid_of.get(str(item.get("target")))
        relation = item.get("relation")
        if source is None or target is None or source == target or not isinstance(relation, str):
            continue
        if fragments_by_id is None:
            positions = [
                index
                for index in item.get("positions") or ()
                if isinstance(index, int) and 0 <= index < len(unit.fragment_ids)
            ]
            fragment_ids = tuple(unit.fragment_ids[index] for index in positions)
        else:
            stored = [fid for fid in item.get("fragments") or () if isinstance(fid, str)]
            fragment_ids = tuple(dict.fromkeys(successor[fid] for fid in stored if fid in successor))
        fact = item.get("fact")
        edges.append(
            RawEdge(
                relation=relation,
                source_cid=source,
                target_cid=target,
                fact=fact if isinstance(fact, str) else None,
                fragment_ids=fragment_ids,
                unit_id=unit.unit_id,
                reused=True,
            )
        )
    return edges


# Attributes


@dataclass(frozen=True, slots=True)
class _Attributes:
    """Outcome of the attributes pass (``_fill_attributes``)."""

    values: dict[str, dict[str, AttributeValue]]
    batches: int
    failed_batches: int
    reused: int
    failed: tuple[str, ...]


@lru_cache(maxsize=BATCH_MODELS)
def _batch_model(ontology: Ontology, labels: tuple[str, ...]) -> type[BaseModel]:
    fields: dict[str, Any] = {
        f"node_{index + 1}": (ontology.attribute_output_model(label), ...) for index, label in enumerate(labels)
    }
    model: type[BaseModel] = create_model("AttributeBatch", __config__=ConfigDict(frozen=True), **fields)
    return model


def attribute_scope(
    nodes: Sequence[CanonicalNode],
    fragments: Iterable[FragmentRec],
    hub_labels: Collection[str],
    aliases: Mapping[str, Sequence[str]] | None = None,
) -> list[CanonicalNode]:
    """``nodes`` as the attributes pass reads them: a hub keeps only its fragments that name it, by
    name or ``surface`` alias (its own and ``aliases[cid]``, the keys of the persisted node it
    continues), matched as ``MENTIONED_IN`` (``terms.hub_name_index``, ``terms.named_in``), or all of
    them when none does; any other node is unchanged. Its shown fragments and its reuse key both come
    from these, so a hub's ``stmt`` is written from text that names it."""

    hubs = [node for node in nodes if node.label in hub_labels]
    if not hubs:
        return list(nodes)
    extra = aliases or {}
    index = hub_name_index((node.cid, node.name, [*node.identity_aliases, *extra.get(node.cid, ())]) for node in hubs)
    wanted = {fid for node in hubs for fid in node.fragment_ids}
    named = named_in(index, (fragment for fragment in fragments if fragment.fragment_id in wanted))
    scoped: list[CanonicalNode] = []
    for node in nodes:
        shown = node
        if node.label in hub_labels:
            own = named.get(node.cid, {})
            kept = tuple(fid for fid in node.fragment_ids if fid in own)
            if kept and kept != node.fragment_ids:
                shown = node.model_copy(update={"fragment_ids": kept})
        scoped.append(shown)
    return scoped


def shown_fragments(
    node: CanonicalNode, fragments_by_id: Mapping[str, FragmentRec], rank: Mapping[str, int], dirty: set[str]
) -> tuple[str, ...]:
    """The node's fragments shown in the attributes pass: at most ``ATTRIBUTE_NODE_FRAGMENTS``, those
    of its attributed fragments that are dirty first, then its others (each in document order); the
    chosen ones in document order."""

    known = sorted((fid for fid in set(node.fragment_ids) if fid in fragments_by_id), key=lambda fid: rank[fid])
    preferred = [fid for fid in known if fid in dirty] + [fid for fid in known if fid not in dirty]
    return tuple(sorted(preferred[:ATTRIBUTE_NODE_FRAGMENTS], key=lambda fid: rank[fid]))


def attribute_batches(
    nodes: Sequence[CanonicalNode],
    fragments_by_id: Mapping[str, FragmentRec],
    rank: Mapping[str, int],
    dirty: set[str],
) -> list[list[tuple[CanonicalNode, tuple[str, ...]]]]:
    """Greedy batches in document order: at most ``ATTRIBUTE_BATCH_NODES`` nodes and
    ``ATTRIBUTE_BATCH_TOKENS`` tokens of distinct fragments each."""

    batches: list[list[tuple[CanonicalNode, tuple[str, ...]]]] = []
    seen: set[str] = set()
    used = 0
    for node in nodes:
        shown = shown_fragments(node, fragments_by_id, rank, dirty)
        extra = sum(fragments_by_id[fid].tokens for fid in shown if fid not in seen)
        if (
            not batches
            or len(batches[-1]) >= ATTRIBUTE_BATCH_NODES
            or (batches[-1] and used + extra > ATTRIBUTE_BATCH_TOKENS)
        ):
            batches.append([])
            seen, used = set(), 0
            extra = sum(fragments_by_id[fid].tokens for fid in shown)
        batches[-1].append((node, shown))
        seen.update(shown)
        used += extra
    return batches


def attribute_request(
    batch: Sequence[tuple[CanonicalNode, tuple[str, ...]]],
    fragments_by_id: Mapping[str, FragmentRec],
    titles: Mapping[str, str],
    ontology: Ontology,
    gateway: Gateway,
) -> list[dict[str, Any]]:
    """The messages of one attributes call over ``batch`` (``attribute_batches``): its distinct shown
    fragments numbered in batch order, each node with the numbers of its fragments."""

    fragment_ids = list(dict.fromkeys(fid for _, shown in batch for fid in shown))
    number = {fid: index + 1 for index, fid in enumerate(fragment_ids)}
    texts = [
        f"{breadcrumb(titles.get(fragments_by_id[fid].document_id, ''), fragments_by_id[fid].heading_path)}\n\n"
        f"{fragments_by_id[fid].text.strip(chr(10))}"
        for fid in fragment_ids
    ]
    nodes = [
        (f"node_{index + 1}", node.label, node.name, [number[fid] for fid in shown])
        for index, (node, shown) in enumerate(batch)
    ]
    return attribute_messages(ontology, gateway, nodes=nodes, fragments=texts)


async def _kept_versions(
    plan: PlanArtifact,
    dirty: set[str],
    node_skipped: set[str],
    results: StoredResults | None,
    node_fingerprint_: str,
) -> dict[str, str]:
    """Fragments whose nodes are their last extracted version (FR-BLD-12), with that version's
    content hash: a dirty fragment the node step skipped this build, and (``update`` builds) an
    unchanged ``skipped`` fragment whose nodes are not stored for its current content."""

    kept = {
        fragment.fragment_id: fragment.extracted_hash
        for fragment in plan.fragments
        if fragment.fragment_id in node_skipped and fragment.fragment_id in dirty and fragment.extracted_hash
    }
    if results is None:
        return kept
    candidates = [
        fragment
        for fragment in plan.fragments
        if fragment.status == "skipped"
        and fragment.fragment_id not in dirty
        and fragment.extracted_hash
        and fragment.extracted_hash != fragment.content_hash
    ]
    if candidates:
        current = await stored_node_fragments(results, node_fingerprint_, candidates)
        kept.update(
            {
                fragment.fragment_id: fragment.extracted_hash
                for fragment in candidates
                if fragment.fragment_id not in current and fragment.extracted_hash
            }
        )
    return kept


def _attribute_key(
    results: StoredResults, versions: Mapping[str, str], fragments_by_id: Mapping[str, FragmentRec], node: CanonicalNode
) -> str:
    """The store key of a node's attribute values: its identity and the content hashes of its fragments
    (a kept fragment at the version its nodes were extracted from)."""

    hashes = [
        versions.get(fid, fragments_by_id[fid].content_hash)
        for fid in dict.fromkeys(node.fragment_ids)
        if fid in fragments_by_id
    ]
    return results.attribute_key(node.identity_key, node.label, hashes)


def _only_kept(versions: Mapping[str, str], fragments_by_id: Mapping[str, FragmentRec], node: CanonicalNode) -> bool:
    """Whether every known fragment of the node is a kept one."""

    known = [fid for fid in node.fragment_ids if fid in fragments_by_id]
    return bool(known) and all(fid in versions for fid in known)


async def _reused_attributes(
    results: StoredResults,
    fingerprint: str,
    candidates: Sequence[CanonicalNode],
    key_of: Callable[[CanonicalNode], str],
    merged: dict[str, dict[str, AttributeValue]],
) -> tuple[list[CanonicalNode], int]:
    """The candidates without stored values (to ask) and the number reused; stored values go to ``merged``."""

    found = await get_many(results.store, ATTRIBUTE_KIND, sorted({key_of(node) for node in candidates}), fingerprint)
    targets: list[CanonicalNode] = []
    reused = 0
    for node in candidates:
        payload = found.get(key_of(node))
        values = payload.get("values") if payload is not None else None
        if not isinstance(values, dict):
            targets.append(node)
            continue
        reused += 1
        if values:
            merged[node.cid] = values
    return targets, reused


async def _ask_attributes(
    batch: list[tuple[CanonicalNode, tuple[str, ...]]],
    *,
    gateway: Gateway,
    ontology: Ontology,
    fragments_by_id: Mapping[str, FragmentRec],
    titles: Mapping[str, str],
) -> dict[str, dict[str, AttributeValue]] | None:
    """The attribute values of one batch by cid; None when the call failed."""

    output_type = _batch_model(ontology, tuple(node.label for node, _ in batch))
    try:
        answer = await gateway.structured(
            "small",
            None,
            attribute_request(batch, fragments_by_id, titles, ontology, gateway),
            output_type,
            prompt=ATTRIBUTES_PROMPT,
        )
    except LLMFatalError:
        raise
    except LLMError as error:
        log_event(logger, WARNING, "graph.attributes.batch_failed", code=error.code, nodes=len(batch), exc_info=True)
        return None
    values: dict[str, dict[str, AttributeValue]] = {}
    for index, (node, _) in enumerate(batch):
        item = getattr(answer, f"node_{index + 1}")
        normalised: dict[str, AttributeValue] = {
            name: value
            for name in ontology.attribute_specs(node.label)
            if (value := ontology.normalise_attribute(node.label, name, getattr(item, name, None))) is not None
        }
        if normalised:
            values[node.cid] = normalised
    return values


async def _fill_attributes(
    resolved: ResolveArtifact,
    plan: PlanArtifact,
    *,
    gateway: Gateway,
    ontology: Ontology,
    results: StoredResults | None = None,
    kept: Mapping[str, str] | None = None,
    hub_aliases: Mapping[str, Sequence[str]] | None = None,
) -> _Attributes:
    """Attributes by cid (cid order), the number of batches, of failed batches and of reused nodes,
    and the cids of the failed batches' nodes (cid order).

    Without ``results.read`` the nodes with a dirty fragment are asked. With it (``update`` builds)
    every node of an extracted label whose identity key and fragment contents have stored values
    reuses them; the others are asked. The values of every node of an answered batch are stored.

    ``kept`` (``_kept_versions``): fragments whose nodes are their last extracted version, by that
    version's content hash. Their current text is not what the nodes were extracted from, so the
    reuse key takes the kept version's hash, the fragment is never shown, and a node grounded only in
    such fragments is never asked: without stored values it counts as failed (``attributes_missing``,
    so Diff keeps its persisted values).

    A hub is read with only the fragments that name it (``attribute_scope``; ``hub_aliases`` as in
    ``extract_edges``): those are the fragments it is shown and keyed by.
    """

    canonical = attribute_scope(resolved.canonical, plan.fragments, ontology.hub_labels, hub_aliases)
    dirty = set(plan.dirty_ids)
    fragments_by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    titles = {document.document_id: document.title for document in plan.documents}
    rank = {fragment.fragment_id: index for index, fragment in enumerate(plan.fragments)}
    extracted = set(ontology.extracted_labels)
    fingerprint = attribute_fingerprint(ontology, gateway)
    versions = dict(kept or {})

    merged: dict[str, dict[str, AttributeValue]] = {}
    reused = 0
    if results is not None and results.read:
        candidates = [node for node in canonical if node.label in extracted]
        targets, reused = await _reused_attributes(
            results,
            fingerprint,
            candidates,
            lambda node: _attribute_key(results, versions, fragments_by_id, node),
            merged,
        )
    else:
        targets = [
            node for node in canonical if node.label in extracted and any(fid in dirty for fid in node.fragment_ids)
        ]
    unasked = sorted(node.cid for node in targets if _only_kept(versions, fragments_by_id, node))
    targets = [node for node in targets if not _only_kept(versions, fragments_by_id, node)]
    targets.sort(
        key=lambda node: (min((rank.get(fid, len(rank)) for fid in node.fragment_ids), default=len(rank)), node.cid)
    )
    shown_from = {fid: fragment for fid, fragment in fragments_by_id.items() if fid not in versions}
    batches = attribute_batches(targets, shown_from, rank, dirty)

    answers = await _gather(
        [
            _ask_attributes(batch, gateway=gateway, ontology=ontology, fragments_by_id=fragments_by_id, titles=titles)
            for batch in batches
        ]
    )
    entries: dict[str, JsonObject] = {}
    failed: set[str] = set(unasked)
    for batch, answer in zip(batches, answers, strict=True):
        if answer is None:
            failed.update(node.cid for node, _ in batch)
            continue
        merged.update(answer)
        if results is not None:
            entries.update(
                {
                    _attribute_key(results, versions, fragments_by_id, node): {"values": answer.get(node.cid, {})}
                    for node, _ in batch
                }
            )
    if results is not None:
        await put_many(results.store, ATTRIBUTE_KIND, fingerprint, dict(sorted(entries.items())))
    return _Attributes(
        values=dict(sorted(merged.items())),
        batches=len(batches),
        failed_batches=sum(1 for answer in answers if answer is None),
        reused=reused,
        failed=tuple(sorted(failed)),
    )
