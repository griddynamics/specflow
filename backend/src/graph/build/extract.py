"""Node extraction over units and windows in two passes."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, create_model

from graph.build.answers import HUBS_LIST, STATEMENTS_LIST, NodeList, node_lists
from graph.build.calls import extract_nodes_call, warm_up_nodes
from graph.build.changes import DEFAULT_EDGE_WINDOWS, EdgeWindowParams
from graph.build.conform import SNAP_OVERLAP, span_overlap
from graph.build.models import (
    AttributeValue,
    Attribution,
    CanonicalNode,
    FragmentRec,
    HubName,
    KnownTermsShown,
    KnownTermsSource,
    NodeCounters,
    NodesArtifact,
    PlanArtifact,
    RawNode,
    ResolveArtifact,
    Unit,
    Window,
)
from graph.build.options import BuildOptions
from graph.build.ports import ExtractionKind, ExtractionStore, JsonObject
from graph.build.prompts import (
    ATTRIBUTES_PROMPT,
    EDGE_WINDOW_PROMPT,
    NODE_PROMPTS,
    KnownTerm,
    attribute_messages,
    attribute_system_message,
    breadcrumb,
    edge_window_system_message,
    fragment_text,
    node_fixed_text,
    out_of_scope_marker,
)
from graph.build.resolve import tokens
from graph.build.terms import (
    CatalogMerge,
    catalog_from_hub_pass,
    hub_name_index,
    named_in,
    shown_terms,
    window_key,
    with_catalog,
)
from graph.build.vectors import get_many, put_many
from graph.core.errors import LLMError, LLMFatalError
from graph.core.identity import STOP_WORDS
from graph.core.llm import Gateway
from graph.core.ontology import Ontology
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.extract")

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
NODE_KIND: Final[ExtractionKind] = "nodes"
EDGE_KIND: Final[ExtractionKind] = "edges"
ATTRIBUTE_KIND: Final[ExtractionKind] = "attributes"
REUSED_CALL: Final = "reused"
"""``call_id`` suffix of a raw node reused from the store (``<unit_id>@<i>-<i+1>:<pass>:reused``)."""
NODE_OPTIONS: Final = frozenset(
    {"fragment_tokens", "hub_catalog", "known_terms_limit", "unit_tokens", "max_node_windows"}
)
EDGE_OPTIONS: Final = frozenset({"fragment_tokens", "catalog_limit", "unit_tokens"})


# Stored results and extractor fingerprints


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def node_passes(ontology: Ontology) -> tuple[NodeList, ...]:
    """The node passes of a build under ``ontology``, in order: ``hubs`` then ``statements``, each only
    when the ontology extracts a label of it."""

    return tuple(node_lists(ontology))


def node_fingerprint(ontology: Ontology, gateway: Gateway, options: BuildOptions) -> str:
    """Node extractor fingerprint (both passes): the ontology's ``nodes`` prompt part and out-of-scope marker, each
    pass's prompt name and the hash of its fixed text (the shared system message and its pass line,
    ``prompts.node_fixed_text``), the extraction model and the options that shape the windows and known terms.
    """

    passes = node_passes(ontology)
    return _digest(
        {
            "ontology": ontology.fingerprint("nodes"),
            "scope": out_of_scope_marker(ontology),
            "prompt": [NODE_PROMPTS[name] for name in passes],
            "system": [_text_hash(node_fixed_text(ontology, node_list=name)) for name in passes],
            "model": gateway.config.model_for("extraction"),
            "options": options.model_dump(mode="json", include=set(NODE_OPTIONS)),
        }
    )


def edge_fingerprint(
    ontology: Ontology, gateway: Gateway, options: BuildOptions, params: EdgeWindowParams = DEFAULT_EDGE_WINDOWS
) -> str:
    """Edge extractor fingerprint: the ontology's ``edges`` prompt part, the prompt name, the hash of the rendered
    edge-window system message, the extraction model, the options that shape a unit and its catalog and the internal
    edge window parameters (``changes.EdgeWindowParams``: target size, most windows, nearness, section hints,
    references).
    """

    return _digest(
        {
            "ontology": ontology.fingerprint("edges"),
            "prompt": EDGE_WINDOW_PROMPT,
            "system": _text_hash(edge_window_system_message(ontology)),
            "model": gateway.config.model_for("extraction"),
            "options": options.model_dump(mode="json", include=set(EDGE_OPTIONS)),
            "windows": dataclasses.asdict(params),
        }
    )


def attribute_fingerprint(ontology: Ontology, gateway: Gateway) -> str:
    """Attributes fingerprint: the ontology's ``attributes`` prompt part and out-of-scope marker, the
    prompt name, the hash of the attributes system message rendered for every extracted label and the
    small model."""

    return _digest(
        {
            "ontology": ontology.fingerprint("attributes"),
            "scope": out_of_scope_marker(ontology),
            "prompt": ATTRIBUTES_PROMPT,
            "system": _text_hash(attribute_system_message(ontology, ontology.extracted_labels)),
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

    def node_key(self, fragment: FragmentRec, content_hash: str, node_list: NodeList) -> str:
        """Nodes of one fragment version from one node pass."""

        path = hashlib.sha256("\n".join(fragment.heading_path).encode("utf-8")).hexdigest()[:16]
        return f"{self.project_id}|{NODE_KIND}|{node_list}|{fragment.document_id}|{content_hash}|{path}"

    def edge_key(self, window_hash: str, catalog_keys: Sequence[str]) -> str:
        """The edges of an edge window's content (``edges.window_hash``) under its window catalog
        (the identity keys ``terms.EdgeCatalog.window_catalog`` gives): a window whose content and
        window catalog are unchanged reuses them."""

        catalog = hashlib.sha256("\n".join(sorted(catalog_keys)).encode("utf-8")).hexdigest()
        return f"{self.project_id}|{EDGE_KIND}|{window_hash}|{catalog}"

    def last_edge_key(self, window_hash: str) -> str:
        """The last answered edges of an edge window's content whatever its window catalog (the
        fallback of a failed call), with its unit catalog's nearness hubs (a clean unit's catalog in
        the next ``update``) and the edge key it was stored under."""

        return f"{self.project_id}|{EDGE_KIND}_last|{window_hash}"

    def window_edge_key(self, document_id: str, first_fragment_id: str) -> str:
        """The last answered edges of an edge window by identity (its document and first fragment),
        whatever its content: the fallback of a failed call when no edges are stored for its content."""

        return f"{self.project_id}|{EDGE_KIND}_window|{document_id}|{first_fragment_id}"

    def attribute_key(self, identity_key: str, label: str, content_hashes: Sequence[str]) -> str:
        evidence = hashlib.sha256("\n".join(sorted(content_hashes)).encode("utf-8")).hexdigest()
        return f"{self.project_id}|{ATTRIBUTE_KIND}|{identity_key}|{label}|{evidence}"


async def stored_node_fragments(
    results: StoredResults, fingerprint: str, fragments: Sequence[FragmentRec], passes: Sequence[NodeList]
) -> set[str]:
    """Ids of ``fragments`` whose nodes of every pass in ``passes`` are stored for their current
    content under ``fingerprint``."""

    keys = {
        results.node_key(fragment, fragment.content_hash, name): fragment.fragment_id
        for fragment in fragments
        for name in passes
    }
    found = Counter(keys[key] for key in await get_many(results.store, NODE_KIND, sorted(keys), fingerprint))
    return {fid for fid, count in found.items() if count == len(set(passes))}


async def stored_attribute_nodes[T](
    results: StoredResults, fingerprint: str, inputs: Mapping[T, tuple[str, str, Sequence[str]]]
) -> set[T]:
    """The nodes of ``inputs`` (identity key, label and fragment content hashes each) whose attribute
    values are stored under ``fingerprint`` (an ``update`` build reuses them; ``estimate_build``)."""

    keys = [(results.attribute_key(*value), node) for node, value in inputs.items()]
    found = await get_many(results.store, ATTRIBUTE_KIND, sorted({key for key, _ in keys}), fingerprint)
    return {node for key, node in keys if key in found}


_StoredNode = tuple[str, str, Attribution, tuple[str, ...], str | None, tuple[str, ...]]
"""``(label, name, attribution, fragments, quote, aliases)`` of a stored raw node (``fragments``: all
its fragment ids)."""


def _stored_attribution(value: Any) -> Attribution:
    """A stored attribution; ``graphiti`` (stored before the build's own node call) and anything
    unknown read as ``model``."""

    return value if value in ("quote", "window_fallback") else "model"


def _stored_nodes(payload: JsonObject) -> list[tuple[_StoredNode, int]]:
    """The stored nodes of an entry with their ``count`` (``fragments`` empty and ``count`` 1 when the
    item has none, ``quote`` None and ``aliases`` empty when it has none); malformed items are left out."""

    items: list[tuple[_StoredNode, int]] = []
    for item in payload.get("nodes") or ():
        if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("label"), str):
            fragments = tuple(fid for fid in item.get("fragments") or () if isinstance(fid, str))
            count = item.get("count")
            quote = item.get("quote")
            node: _StoredNode = (
                item["label"],
                item["name"],
                _stored_attribution(item.get("attribution")),
                fragments,
                quote if isinstance(quote, str) and quote else None,
                tuple(alias for alias in item.get("aliases") or () if isinstance(alias, str) and alias),
            )
            items.append((node, count if isinstance(count, int) and not isinstance(count, bool) and count > 1 else 1))
    return items


async def gather_all[T](awaitables: Sequence[Awaitable[T]]) -> list[T]:
    """All results in order; on the first failure the others are cancelled and the failure raised."""

    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def gather_with[A, B](many: Sequence[Awaitable[A]], last: Awaitable[B]) -> tuple[list[A], B]:
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
    quote_attributions: int = 0
    quote_outside_window: int = 0
    label_collapses: int = 0
    stray_pass_items: int = 0


@dataclass(frozen=True)
class _WindowNode:
    """One node a window call gave, with how it got its fragments."""

    window: Window
    label: str
    name: str
    quote: str | None
    fragment_ids: tuple[str, ...]
    attribution: Attribution
    aliases: tuple[str, ...] = ()


@dataclass
class _WindowResult:
    nodes: list[_WindowNode] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    covered: list[Window] = field(default_factory=list)
    """Windows whose call answered (their fragments' results are this build's)."""


def halve(unit: Unit, window: Window, fragments_by_id: Mapping[str, FragmentRec]) -> tuple[Window, Window]:
    """Split a window of at least two fragments where the two halves' token counts differ least
    (the earlier split point on a tie)."""

    start, end = window
    tokens_ = [fragments_by_id[fragment_id].tokens for fragment_id in unit.fragment_ids[start:end]]
    total = sum(tokens_)
    best, best_gap, left = start + 1, None, 0
    for offset in range(1, end - start):
        left += tokens_[offset - 1]
        gap = abs(total - 2 * left)
        if best_gap is None or gap < best_gap:
            best, best_gap = start + offset, gap
    return (start, best), (best, end)


QUOTE_FUZZY_TOKENS: Final = 3
"""Tokens a quote needs before Conform may attribute it by snap score."""
QUOTE_FUZZY_CONTENT_TOKENS: Final = 2
"""Non-stop-word tokens a short quote still needs for that attribution."""


def _holds(words: Sequence[str], needle: Sequence[str]) -> bool:
    """Whether ``needle`` occurs in ``words`` as a contiguous run (whole tokens only)."""

    size = len(needle)
    return any(list(words[index : index + size]) == list(needle) for index in range(len(words) - size + 1))


def quote_fragments(quote: str | None, texts: Sequence[str], window: Window) -> tuple[int, ...]:
    """The indices of ``window`` whose fragment text holds ``quote``: every fragment holding the quote's tokens (case
    folded, punctuation dropped) as a contiguous run, whole tokens only.
    """

    needle = tokens(quote or "")
    if not needle:
        return ()
    start, end = window
    words = {index: tokens(texts[index]) for index in range(start, end)}
    found = tuple(index for index in range(start, end) if _holds(words[index], needle))
    content = [token for token in needle if token not in STOP_WORDS]
    if found or (len(needle) < QUOTE_FUZZY_TOKENS and len(content) < QUOTE_FUZZY_CONTENT_TOKENS):
        return found
    scores = {index: span_overlap(quote or "", texts[index]) for index in range(start, end)}
    best = max(scores.values(), default=0.0)
    winners = [index for index, score in scores.items() if score == best]
    return (winners[0],) if best >= SNAP_OVERLAP and len(winners) == 1 else ()


@dataclass(frozen=True)
class CallEnv:
    """What the extraction calls of every unit share: the plan's dirty fragments and the gateway and
    ontology they run under."""

    dirty: set[str]
    fragments_by_id: Mapping[str, FragmentRec]
    gateway: Gateway
    ontology: Ontology


class WarmUp:
    """The prompt-cache warm-up of one unit's calls (``send``), sent at most once, as a task of its own: ``send`` starts
    it in the background, a caller (``__call__``) starts it when needed and waits for it; a waiting caller that is
    cancelled never cancels it (``stop`` does).
    """

    def __init__(self, send: Callable[[], Awaitable[None]], *, ordinal: int, calls: str) -> None:
        self._send = send
        self._ordinal = ordinal
        self._calls = calls
        self._task: asyncio.Future[None] | None = None

    @property
    def started(self) -> bool:
        """Whether the warm-up was started (sent, or on its way)."""

        return self._task is not None

    def send(self) -> None:
        """Start the warm-up now (in the background) unless it already was."""

        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def __call__(self) -> None:
        self.send()
        if self._task is not None:
            await asyncio.shield(self._task)

    async def stop(self) -> None:
        """Cancel the warm-up if it still runs and collect its outcome (an error nobody waited for)."""

        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        try:
            await self._send()
        except LLMFatalError:
            raise
        except LLMError as error:
            log_event(
                logger,
                WARNING,
                "graph.extract.warm_up_failed",
                unit=self._ordinal,
                calls=self._calls,
                code=error.code,
            )


class _UnitRun:
    """The windows of one unit in one node pass: shared fragment texts, known terms and the call's
    collaborators."""

    def __init__(
        self,
        unit: Unit,
        *,
        ordinal: int,
        node_list: NodeList,
        texts: list[str],
        terms: Mapping[Window, Sequence[KnownTerm]],
        env: CallEnv,
        tally: _Tally,
        warm_up: WarmUp,
    ) -> None:
        self.unit = unit
        self.ordinal = ordinal
        self.node_list = node_list
        self.texts = texts
        self.bodies = [env.fragments_by_id[fragment_id].text for fragment_id in unit.fragment_ids]
        self.terms = terms
        self.dirty = env.dirty
        self.fragments_by_id = env.fragments_by_id
        self.gateway = env.gateway
        self.ontology = env.ontology
        self.tally = tally
        self.labels = frozenset(node_lists(env.ontology, [node_list]).get(node_list, ()))
        self.warm_up = warm_up

    async def run_all(self, windows: Sequence[Window]) -> list[_WindowResult]:
        """The unit's windows concurrently, after the prompt-cache warm-up when there are two or more
        (``_WarmUps``: it may have started before this pass)."""

        if len(windows) > 1:
            await self.warm_up()
        return await gather_all([self.run(window) for window in windows])

    async def run(self, window: Window, depth: int = 0, terms: Sequence[KnownTerm] | None = None) -> _WindowResult:
        """One window (``terms``: those of the window it was halved from, else its own)."""

        start, end = window
        shown = self.terms.get(window, ()) if terms is None else terms
        self.tally.calls += 1
        try:
            call = await extract_nodes_call(
                self.gateway, self.ontology, self.texts, shown, window, node_list=self.node_list
            )
        except LLMFatalError:
            raise
        except LLMError as error:
            if error.reason == "truncated" and end - start > 1 and depth < MAX_SPLIT_DEPTH:
                self.tally.truncation_splits += 1
                await self.warm_up()
                halves = halve(self.unit, window, self.fragments_by_id)
                parts = await gather_all([self.run(half, depth + 1, shown) for half in halves])
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
                node_pass=self.node_list,
                window_start=start,
                window_end=end,
                code=error.code,
                fragments=len(skipped),
                exc_info=True,
            )
            return _WindowResult(skipped=skipped)
        self.tally.label_collapses += call.label_collapses
        self.tally.stray_pass_items += call.stray_items
        result = _WindowResult(covered=[window])
        for node in call.nodes:
            if node.label not in self.labels:
                continue
            indices = node.indices
            attribution: Attribution = "model"
            if not indices:
                indices = quote_fragments(node.quote, self.bodies, window)
                if not indices:  # a quote from another fragment of the unit (the window line was missed)
                    indices = quote_fragments(node.quote, self.bodies, (0, len(self.bodies)))
                    self.tally.quote_outside_window += int(bool(indices))
                if indices:
                    self.tally.quote_attributions += 1
                    attribution = "quote"
                else:
                    self.tally.window_fallbacks += 1
                    indices = tuple(range(start, end))
                    attribution = "window_fallback"
            fragment_ids = tuple(self.unit.fragment_ids[index] for index in indices)
            result.nodes.append(
                _WindowNode(window, node.label, node.name, node.quote, fragment_ids, attribution, node.aliases)
            )
        return result


class TermSource(Protocol):
    """The known terms of the given windows for a hub list, by ``terms.window_key``
    (``terms.TermRanker``), and the windows whose mentioned terms hit the safety cap so far."""

    capped_units: int

    async def __call__(
        self, hubs: Sequence[HubName], windows: Sequence[tuple[Unit, Window]]
    ) -> Mapping[str, Sequence[KnownTerm]]: ...


def dirty_windows(units: Sequence[Unit], dirty: Collection[str]) -> list[tuple[Unit, Window]]:
    """The windows holding a dirty fragment, in unit and window order (the node calls of each pass)."""

    return [
        (unit, window)
        for unit in units
        for window in unit.windows
        if any(fid in dirty for fid in unit.fragment_ids[window[0] : window[1]])
    ]


_RawKey = tuple[int, int, int, int, bool, str, str]
"""Order of a raw node: unit, window start and end, pass, reused, label, name."""


@dataclass
class _NodePass:
    """The raw nodes of one node pass (unnumbered, with their order keys) and its counts."""

    node_list: NodeList
    collected: list[tuple[_RawKey, RawNode]] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    tally: _Tally = field(default_factory=_Tally)
    reused: set[str] = field(default_factory=set)
    supported: set[str] = field(default_factory=set)
    units: set[int] = field(default_factory=set)
    covered: set[str] = field(default_factory=set)
    """Fragments inside a window of this pass that answered."""


@dataclass(frozen=True)
class _PassInputs:
    """What every node pass of a build reads."""

    plan: PlanArtifact
    units: Sequence[Unit]
    env: CallEnv
    titles: Mapping[str, str]
    results: StoredResults | None
    fingerprint: str


def _unit_texts(inputs: _PassInputs, unit: Unit) -> list[str]:
    """The unit's fragments as every node call over it shows them (``prompts.fragment_text``)."""

    title = inputs.titles.get(unit.document_id, unit.document_id)
    fragments = [inputs.env.fragments_by_id[fragment_id] for fragment_id in unit.fragment_ids]
    return [fragment_text(title, fragment.heading_path, fragment.text) for fragment in fragments]


class _WarmUps:
    """The prompt-cache warm-ups of the node passes: one per pass and unit (``WarmUp``), all sharing the unit's prefix
    (``prompts.node_warm_up_messages``).
    """

    def __init__(self, inputs: _PassInputs) -> None:
        self._inputs = inputs
        self._made: dict[tuple[NodeList, int], WarmUp] = {}

    def of(self, node_list: NodeList, unit_index: int) -> WarmUp:
        key = (node_list, unit_index)
        if key not in self._made:
            env = self._inputs.env
            texts = _unit_texts(self._inputs, self._inputs.units[unit_index])
            self._made[key] = WarmUp(
                lambda: warm_up_nodes(env.gateway, env.ontology, texts), ordinal=unit_index, calls=node_list
            )
        return self._made[key]

    def start(self, node_list: NodeList, unit_indexes: Iterable[int]) -> None:
        """Send the warm-ups of these units for ``node_list`` now, those of units with one window to
        extract excepted (they send none unless a truncated answer splits it)."""

        for unit_index in unit_indexes:
            unit = self._inputs.units[unit_index]
            if len(dirty_windows([unit], self._inputs.env.dirty)) > 1:
                self.of(node_list, unit_index).send()

    async def stop(self) -> None:
        """Cancel the warm-ups still running (a pass failed) and collect their outcomes."""

        for warm_up in self._made.values():
            await warm_up.stop()


async def _node_pass(
    inputs: _PassInputs,
    node_list: NodeList,
    position: int,
    known_terms: Mapping[str, Sequence[KnownTerm]],
    *,
    warm_ups: _WarmUps,
    then: Callable[[int], None] | None = None,
) -> _NodePass:
    """One node pass: a call per window with dirty fragments and (``update`` builds) the stored results of the fragments
    outside every answered window reused.
    """

    env, units = inputs.env, inputs.units
    found = _NodePass(node_list)
    jobs: list[tuple[int, Awaitable[list[_WindowResult]]]] = []
    for unit_index, unit in enumerate(units):
        windows = [window for _, window in dirty_windows([unit], env.dirty)]
        if not windows:
            continue
        run = _UnitRun(
            unit,
            ordinal=unit_index,
            node_list=node_list,
            texts=_unit_texts(inputs, unit),
            terms={window: list(known_terms.get(window_key(unit.unit_id, window), ())) for window in windows},
            env=env,
            tally=found.tally,
            warm_up=warm_ups.of(node_list, unit_index),
        )
        found.tally.windows += len(windows)
        jobs.append((unit_index, _then(run.run_all(windows), unit_index, then)))
    # every warm-up of the pass first, then the units concurrently, each unit's windows after its own
    # warm-up (``_UnitRun.run_all``)
    warm_ups.start(node_list, [unit_index for unit_index, _ in jobs])
    answers = await gather_all([job for _, job in jobs])
    covered = found.covered
    results = [
        (unit_index, result)
        for (unit_index, _), unit_results in zip(jobs, answers, strict=True)
        for result in unit_results
    ]
    for unit_index, result in results:
        unit = units[unit_index]
        found.units.add(unit_index)
        found.skipped.update(result.skipped)
        for start, end in result.covered:
            covered.update(unit.fragment_ids[start:end])
        for item in result.nodes:
            window = item.window
            node = RawNode(
                raw_id="",
                name=item.name,
                label=item.label,
                unit_id=unit.unit_id,
                call_id=f"{unit.unit_id}@{window[0]}-{window[1]}:{node_list}",
                window=window,
                fragment_ids=item.fragment_ids,
                attribution=item.attribution,
                quote=item.quote,
                aliases=item.aliases,
            )
            found.collected.append(((unit_index, window[0], window[1], position, False, item.label, item.name), node))
    if inputs.results is not None and inputs.results.read:
        await _reuse_nodes(inputs, found, position, covered=covered)
    return found


async def _then[T](job: Awaitable[T], unit_index: int, then: Callable[[int], None] | None) -> T:
    result = await job
    if then is not None:
        then(unit_index)
    return result


async def _store_passes(inputs: _PassInputs, done: Sequence[_NodePass]) -> None:
    """Store each pass's answered nodes for the fragments every pass answered, so a fragment one pass
    skipped stores nothing new: its stored versions stay those of one extraction for every pass (a
    later build never mixes a pass's new nodes with another pass's old ones)."""

    if inputs.results is None or not done:
        return
    both = set.intersection(*(found.covered for found in done))
    for found in done:
        await _store_nodes(
            inputs.results,
            inputs.fingerprint,
            node_list=found.node_list,
            collected=[item for item in found.collected if not item[1].reused],
            covered=both,
            fragments_by_id=inputs.env.fragments_by_id,
        )


def _source_and_hubs(
    persisted: Sequence[HubName], catalog: CatalogMerge, *, use_catalog: bool
) -> tuple[KnownTermsSource, list[HubName], int]:
    """The statement pass's known-term hubs, where they came from and the ambiguous catalog hubs."""

    if use_catalog and catalog.hubs:
        if persisted:
            hubs, ambiguous = with_catalog(persisted, catalog.hubs)
            return "persisted_and_catalog", hubs, ambiguous
        return "hub_pass", list(catalog.hubs), 0
    return ("persisted" if persisted else "none"), list(persisted), 0


async def extract_nodes(
    plan: PlanArtifact,
    *,
    gateway: Gateway,
    ontology: Ontology,
    options: BuildOptions,
    persisted_hubs: Sequence[HubName] = (),
    ranker: TermSource | None = None,
    results: StoredResults | None = None,
) -> NodesArtifact:
    """Raw nodes of both node passes over every window with dirty fragments, the reused nodes of the other fragments,
    the skipped fragments by error code, the hub catalog, the known terms shown to each unit's statement pass and
    (``update`` builds) hub pass, and counters.
    """

    dirty = set(plan.dirty_ids)
    fragments_by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    inputs = _PassInputs(
        plan=plan,
        units=list(plan.units),
        env=CallEnv(dirty, fragments_by_id, gateway, ontology),
        titles={document.document_id: document.title for document in plan.documents},
        results=results,
        fingerprint=node_fingerprint(ontology, gateway, options) if results is not None else "",
    )
    extracted = dirty_windows(inputs.units, dirty)
    passes = node_passes(ontology)
    done: list[_NodePass] = []
    shown: dict[str, list[KnownTerm]] = {}
    catalog = CatalogMerge()
    source: KnownTermsSource = "none"
    ambiguous = 0
    hub_shown: dict[str, KnownTermsShown] | None = None
    stated: dict[str, KnownTermsShown] | None = None
    warm_ups = _WarmUps(inputs)
    every_unit = range(len(inputs.units))
    try:
        if HUBS_LIST in passes:
            warm_ups.start(HUBS_LIST, every_unit)  # while the known terms are ranked
            terms = (
                await ranker(persisted_hubs, extracted) if ranker is not None and persisted_hubs and extracted else {}
            )
            if ranker is not None and persisted_hubs and extracted:  # what the hub pass was shown
                source = "persisted"
                hub_shown = _shown_by_unit(extracted, terms)
            # a unit's statement-pass warm-up starts once its hub windows answered, during the hub pass's
            # tail: the statement windows of a warm unit start as soon as the pass does
            ahead = (
                (lambda unit_index: warm_ups.start(STATEMENTS_LIST, [unit_index]))
                if STATEMENTS_LIST in passes
                else None
            )
            hub_pass = await _node_pass(
                inputs, HUBS_LIST, passes.index(HUBS_LIST), terms, warm_ups=warm_ups, then=ahead
            )
            done.append(hub_pass)
            _remember_terms(shown, terms)
            document_of = {unit.unit_id: unit.document_id for unit in inputs.units}
            catalog = catalog_from_hub_pass((node for _, node in hub_pass.collected), document_of, ontology.hub_labels)
        if STATEMENTS_LIST in passes:
            warm_ups.start(STATEMENTS_LIST, every_unit)  # while the known terms are ranked
            source, hubs, ambiguous = _source_and_hubs(persisted_hubs, catalog, use_catalog=options.hub_catalog)
            terms = await ranker(hubs, extracted) if ranker is not None and hubs and extracted else {}
            done.append(
                await _node_pass(inputs, STATEMENTS_LIST, passes.index(STATEMENTS_LIST), terms, warm_ups=warm_ups)
            )
            _remember_terms(shown, terms)
            if ranker is not None and hubs and extracted:  # known terms computed
                stated = _shown_by_unit(extracted, terms)
    finally:
        await warm_ups.stop()
    await _store_passes(inputs, done)
    seen = _TermsSeen(
        source=source,
        shown=shown,
        stated=stated,
        hub_shown=hub_shown,
        persisted=persisted_hubs,
        capped=ranker.capped_units if ranker is not None else 0,
        ambiguous=ambiguous,
    )
    return _nodes_artifact(plan, done, catalog=catalog, seen=seen)


def _shown_by_unit(
    windows: Sequence[tuple[Unit, Window]], terms: Mapping[str, Sequence[KnownTerm]]
) -> dict[str, KnownTermsShown]:
    """The known terms each unit's windows were shown, joined per unit (``terms.shown_terms``: a
    name mentioned in one window counts as mentioned)."""

    joined: dict[str, list[KnownTerm]] = {}
    for unit, window in windows:
        joined.setdefault(unit.unit_id, []).extend(terms.get(window_key(unit.unit_id, window), ()))
    return {unit_id: shown_terms(unit_terms) for unit_id, unit_terms in joined.items()}


def _remember_terms(shown: dict[str, list[KnownTerm]], terms: Mapping[str, Sequence[KnownTerm]]) -> None:
    for unit_id, unit_terms in terms.items():
        shown.setdefault(unit_id, []).extend(unit_terms)


@dataclass(frozen=True)
class _TermsSeen:
    """What a build's node passes were shown: the source, every term per unit, the terms each pass's
    units were shown (None when not computed), the persisted hubs, capped units and ambiguous hubs."""

    source: KnownTermsSource
    shown: Mapping[str, Sequence[KnownTerm]]
    stated: Mapping[str, KnownTermsShown] | None
    hub_shown: Mapping[str, KnownTermsShown] | None
    persisted: Sequence[HubName]
    capped: int
    ambiguous: int


def _nodes_artifact(
    plan: PlanArtifact, done: Sequence[_NodePass], *, catalog: CatalogMerge, seen: _TermsSeen
) -> NodesArtifact:
    """The extract-nodes artifact of the passes: raw nodes numbered per call, skipped fragments in plan order (the first
    pass's code wins), counters (the catalog ones only when the hub pass ran), the hub catalog, the known terms shown to
    the statement pass and to the hub pass (each None when none were computed) and the persisted hubs among the known
    terms (``update`` builds).
    """

    collected = sorted((item for found in done for item in found.collected), key=lambda item: item[0])
    raw_nodes = []
    numbers: dict[str, int] = {}
    for _, node in collected:
        number = numbers.get(node.call_id, 0)
        numbers[node.call_id] = number + 1
        raw_nodes.append(node.model_copy(update={"raw_id": f"{node.call_id}#{number}"}))
    skipped: dict[str, str] = {}
    for found in done:
        for fid, code in found.skipped.items():
            skipped.setdefault(fid, code)
    order = {fragment.fragment_id: index for index, fragment in enumerate(plan.fragments)}
    skipped = dict(sorted(skipped.items(), key=lambda item: order.get(item[0], len(order))))
    tallies = [found.tally for found in done]
    hub_calls = sum(found.tally.calls for found in done if found.node_list == HUBS_LIST)
    counters = NodeCounters(
        windows=max((tally.windows for tally in tallies), default=0),
        calls=sum(tally.calls for tally in tallies),
        failed_calls=sum(tally.failed_calls for tally in tallies),
        truncation_splits=sum(tally.truncation_splits for tally in tallies),
        window_fallbacks=sum(tally.window_fallbacks for tally in tallies),
        quote_attributions=sum(tally.quote_attributions for tally in tallies),
        quote_outside_window=sum(tally.quote_outside_window for tally in tallies) if tallies else None,
        hub_calls=hub_calls,
        known_terms_capped=seen.capped,
        fragments_reused=len(set().union(*(found.reused for found in done))),
        fragments_supported=len(set().union(*(found.supported for found in done))),
        known_terms=len({term.name for terms in seen.shown.values() for term in terms}),
        label_collapses=sum(tally.label_collapses for tally in tallies),
        stray_pass_items=sum(tally.stray_pass_items for tally in tallies) if tallies else None,
        **(
            {
                "catalog_hubs": len(catalog.hubs),
                "catalog_aliases": catalog.aliases,
                "catalog_links_refused": catalog.links_refused,
                "catalog_aliases_dropped": catalog.aliases_dropped,
                "catalog_ambiguous": seen.ambiguous,
            }
            if any(found.node_list == HUBS_LIST for found in done)
            else {}
        ),
    )
    log_event(
        logger,
        INFO,
        "graph.extract.nodes_done",
        units=len(set().union(*(found.units for found in done))),
        windows=counters.windows,
        calls=counters.calls,
        hub_calls=hub_calls,
        failed_calls=counters.failed_calls,
        raw_nodes=len(raw_nodes),
        reused_nodes=sum(1 for node in raw_nodes if node.reused),
        fragments_reused=counters.fragments_reused,
        fragments_supported=counters.fragments_supported,
        skipped_fragments=len(skipped),
        window_fallbacks=counters.window_fallbacks,
        quote_attributions=counters.quote_attributions,
        truncation_splits=counters.truncation_splits,
        label_collapses=counters.label_collapses,
        catalog_hubs=counters.catalog_hubs,
        catalog_aliases=counters.catalog_aliases,
    )
    return NodesArtifact(
        raw_nodes=tuple(raw_nodes),
        skipped=skipped,
        known_terms_source=seen.source,
        counters=counters,
        hub_catalog=catalog.hubs,
        known_terms=dict(seen.stated) if seen.stated is not None else None,
        hub_known_terms=dict(seen.hub_shown) if seen.hub_shown is not None else None,
        persisted_hubs=tuple(seen.persisted) if seen.persisted else None,
    )


async def _store_nodes(
    results: StoredResults,
    fingerprint: str,
    *,
    node_list: NodeList,
    collected: Sequence[tuple[Any, RawNode]],
    covered: set[str],
    fragments_by_id: Mapping[str, FragmentRec],
) -> None:
    """Store the nodes of one pass for every covered fragment (by the attribution they got), sorted by label, name,
    attribution, fragments, quote and aliases.
    """

    by_fragment: dict[str, Counter[tuple[str, str, str, tuple[str, ...], str, tuple[str, ...]]]] = {
        fid: Counter() for fid in covered
    }
    for _, node in collected:
        origin = tuple(sorted(set(node.fragment_ids)))
        for fid in origin:
            if fid in by_fragment:
                key = (node.label, node.name, node.attribution, origin, node.quote or "", node.aliases)
                by_fragment[fid][key] += 1
    entries: dict[str, JsonObject] = {}
    for fid in sorted(by_fragment):
        fragment = fragments_by_id[fid]
        entries[results.node_key(fragment, fragment.content_hash, node_list)] = {
            "nodes": [
                {
                    "label": label,
                    "name": name,
                    "attribution": attribution,
                    "fragments": list(origin),
                    **({"quote": quote} if quote else {}),
                    **({"aliases": list(aliases)} if aliases else {}),
                    **({"count": count} if count > 1 else {}),
                }
                for (label, name, attribution, origin, quote, aliases), count in sorted(by_fragment[fid].items())
            ]
        }
    await put_many(results.store, NODE_KIND, fingerprint, entries)


def _wanted_hashes(fragment: FragmentRec, *, dirty: set[str], skipped: Mapping[str, str]) -> list[str]:
    """The content hashes whose stored nodes a fragment outside every answered window of a pass reads:
    its content hash, and for a skipped fragment also the hash it was last extracted at."""

    hashes: list[str] = []
    if fragment.fragment_id not in dirty:
        hashes.append(fragment.content_hash)
        if fragment.status == "skipped" and fragment.extracted_hash:
            hashes.append(fragment.extracted_hash)
    elif fragment.fragment_id in skipped and fragment.extracted_hash:
        hashes.append(fragment.extracted_hash)
    return list(dict.fromkeys(hashes))


def _reused_unit_nodes(
    unit: Unit,
    unit_index: int,
    position: int,
    node_list: NodeList,
    payloads: Mapping[str, JsonObject],
) -> list[tuple[_RawKey, RawNode]]:
    """The reused nodes of one unit in one pass from the payload found per fragment id."""

    collected: list[tuple[_RawKey, RawNode]] = []
    groups: dict[tuple[_StoredNode, int], list[int]] = {}
    for unit_position, fid in enumerate(unit.fragment_ids):
        payload = payloads.get(fid)
        if payload is None:
            continue
        for (label, name, attribution, origin, quote, aliases), count in _stored_nodes(payload):
            positions = groups.setdefault(((label, name, attribution, origin or (fid,), quote, aliases), count), [])
            positions.append(unit_position)
    for ((label, name, attribution, _, quote, aliases), count), positions in groups.items():
        window = (min(positions), max(positions) + 1)
        for _ in range(count):
            node = RawNode(
                raw_id="",
                name=name,
                label=label,
                unit_id=unit.unit_id,
                call_id=f"{unit.unit_id}@{window[0]}-{window[1]}:{node_list}:{REUSED_CALL}",
                window=window,
                fragment_ids=tuple(unit.fragment_ids[index] for index in sorted(set(positions))),
                attribution=attribution,
                reused=True,
                quote=quote,
                aliases=aliases,
            )
            collected.append(((unit_index, window[0], window[1], position, True, label, name), node))
    return collected


async def _reuse_nodes(inputs: _PassInputs, found: _NodePass, position: int, *, covered: set[str]) -> None:
    """Add the reused nodes of the pass's fragments outside every answered window to ``found``, with the fragments
    reused and, among them, the skipped ones kept by their last extracted version.
    """

    results = inputs.results
    if results is None:
        return
    fragments_by_id = inputs.env.fragments_by_id
    wanted: dict[str, list[str]] = {}
    for fid, fragment in fragments_by_id.items():
        if fid in covered:
            continue
        hashes = _wanted_hashes(fragment, dirty=inputs.env.dirty, skipped=found.skipped)
        if hashes:
            wanted[fid] = [results.node_key(fragment, value, found.node_list) for value in hashes]
    stored = await get_many(
        results.store, NODE_KIND, sorted({key for keys in wanted.values() for key in keys}), inputs.fingerprint
    )
    payloads: dict[str, JsonObject] = {}
    for fid, keys in wanted.items():
        payload = next((stored[key] for key in keys if key in stored), None)
        if payload is None:
            continue
        payloads[fid] = payload
        found.reused.add(fid)
        if fid in found.skipped or fragments_by_id[fid].status == "skipped":
            found.supported.add(fid)
    for unit_index, unit in enumerate(inputs.units):
        found.collected.extend(_reused_unit_nodes(unit, unit_index, position, found.node_list, payloads))


# Attributes


@dataclass(frozen=True, slots=True)
class AttributesOutcome:
    """Outcome of the attributes pass (``fill_attributes``)."""

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
    """``nodes`` as the attributes pass reads them: a hub keeps only its fragments that name it, by name or ``surface``
    alias (its own and ``aliases[cid]``, the keys of the persisted node it continues), matched as ``MENTIONED_IN``
    (``terms.hub_name_index``, ``terms.named_in``), or all of them when none does; any other node is unchanged.
    """

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
    """The messages of one attributes call over ``batch`` (``attribute_batches``): its distinct shown fragments numbered
    in batch order, each starting with its breadcrumb, and each node with the numbers of its fragments and the
    breadcrumbs of its fragments not shown (distinct, other than those of its shown ones; ``prompts.attribute_messages``
    lists the nearest ``OTHER_HEADINGS_SHOWN``).
    """

    def crumb(fid: str) -> str:
        fragment = fragments_by_id[fid]
        return breadcrumb(titles.get(fragment.document_id, ""), fragment.heading_path)

    fragment_ids = list(dict.fromkeys(fid for _, shown in batch for fid in shown))
    number = {fid: index + 1 for index, fid in enumerate(fragment_ids)}
    texts = [f"{crumb(fid)}\n\n{fragments_by_id[fid].text.strip(chr(10))}" for fid in fragment_ids]
    nodes: list[tuple[str, str, str, list[int]]] = []
    others: dict[str, list[str]] = {}
    for index, (node, shown) in enumerate(batch):
        field = f"node_{index + 1}"
        nodes.append((field, node.label, node.name, [number[fid] for fid in shown]))
        seen = {crumb(fid) for fid in shown}
        hidden = [
            path
            for path in dict.fromkeys(
                crumb(fid) for fid in node.fragment_ids if fid in fragments_by_id and fid not in shown
            )
            if path and path not in seen
        ]
        if hidden:
            others[field] = hidden
    return attribute_messages(ontology, gateway, nodes=nodes, fragments=texts, other_headings=others)


async def kept_versions(
    plan: PlanArtifact,
    dirty: set[str],
    node_skipped: set[str],
    results: StoredResults | None,
    node_fingerprint_: str,
    *,
    passes: Sequence[NodeList],
) -> dict[str, str]:
    """Fragments whose nodes are their last extracted version, with that version's content hash: a dirty fragment the
    node step skipped this build, and (``update`` builds) an unchanged ``skipped`` fragment whose nodes are not stored
    for its current content.
    """

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
        current = await stored_node_fragments(results, node_fingerprint_, candidates, passes)
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


async def fill_attributes(
    resolved: ResolveArtifact,
    plan: PlanArtifact,
    *,
    gateway: Gateway,
    ontology: Ontology,
    results: StoredResults | None = None,
    kept: Mapping[str, str] | None = None,
    hub_aliases: Mapping[str, Sequence[str]] | None = None,
) -> AttributesOutcome:
    """Attributes by cid (cid order), the number of batches, of failed batches and of reused nodes, and the cids of the
    failed batches' nodes (cid order).
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

    answers = await gather_all(
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
    return AttributesOutcome(
        values=dict(sorted(merged.items())),
        batches=len(batches),
        failed_batches=sum(1 for answer in answers if answer is None),
        reused=reused,
        failed=tuple(sorted(failed)),
    )
