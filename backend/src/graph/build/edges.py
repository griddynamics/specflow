"""Edge extraction over edge windows."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from graph.build import terms
from graph.build.answers import NodeList
from graph.build.calls import extract_edge_window_call, warm_up_edge_windows
from graph.build.changes import DEFAULT_EDGE_WINDOWS, EdgeWindowParams, edge_windows
from graph.build.extract import (
    EDGE_KIND,
    MAX_SPLIT_DEPTH,
    AttributesOutcome,
    CallEnv,
    StoredResults,
    WarmUp,
    edge_fingerprint,
    fill_attributes,
    gather_all,
    gather_with,
    halve,
    kept_versions,
    node_fingerprint,
    node_passes,
    stored_node_fragments,
)
from graph.build.hubjudge import HEADING_MAX_CHARS, HEADING_SEPARATOR, QUOTE_MAX_CHARS, JudgeItem, candidate_text
from graph.build.models import (
    CanonicalNode,
    EdgeCounters,
    EdgesArtifact,
    EdgeWindowCall,
    EdgeWindowCause,
    FragmentRec,
    PlanArtifact,
    RawEdge,
    ResolveArtifact,
    Unit,
    Window,
)
from graph.build.options import StepServices
from graph.build.ports import JsonObject
from graph.build.prompts import ReferencedEntity, fragment_text, ownership_targets
from graph.build.references import ReferenceFinder, UnitReferences, requirement_labels, section_hub_labels
from graph.build.terms import EdgeCatalog, UnitCatalog
from graph.build.vectors import Vector, VectorCache, get_many, put_many
from graph.core.errors import LLMError, LLMFatalError
from graph.core.llm.gateway import prompt_name
from graph.core.ontology import Ontology
from logger.logger import INFO, WARNING, get_logger, log_event

__all__ = [
    "DEFAULT_EDGE_WINDOWS",
    "MIN_EDGE_CATALOG",
    "CatalogVectors",
    "attributes_from",
    "catalog_names",
    "catalog_vectors",
    "extract_edges",
    "referenced_entities",
    "replay_edges",
    "rich_text",
    "unique_names",
    "window_hash",
]

logger = get_logger("graph.build.edges")

MIN_EDGE_CATALOG: Final = 2
"""Nodes an edge catalog needs to hold an edge."""
WINDOW_FRAGMENTS: Final = "window_fragments"
"""The key of a stored window answer's fragment ids (what an identity fallback covers; ``_uncovered``)."""


# Catalog names and window keys


def catalog_names(nodes: Sequence[CanonicalNode]) -> dict[str, str]:
    """A unique catalog name per cid, by ``unique_names`` over ``(cid, name, label)``."""

    return unique_names([(node.cid, node.name, node.label) for node in nodes])


def unique_names(items: Sequence[tuple[str, str, str]]) -> dict[str, str]:
    """A unique prompt name per key of ``(key, name, label)`` items (the edge catalog; the estimate renders its expected
    catalog with it too).
    """

    shown = {key: prompt_name(name) for key, name, _ in items}
    counts = Counter(shown[key] for key, _, _ in items)
    names: dict[str, str] = {}
    taken: set[str] = set()
    for key, _, label in sorted(items):
        name = shown[key]
        base = name if counts[name] == 1 else f"{name} ({label})"
        name, number = base, 1
        while name in taken:
            number += 1
            name = f"{base} #{number}"
        taken.add(name)
        names[key] = name
    return names


def referenced_entities(
    cids: Sequence[str], references: UnitReferences, nodes: Mapping[str, CanonicalNode]
) -> dict[str, ReferencedEntity]:
    """The catalog lines' extra of a unit's referenced part (``cids``, the catalog's ``referenced``), by cid: the node's
    first quote as its statement (none: empty) and the unit's first sentence that refers to it
    (``UnitReferences.sentences``).
    """

    found: dict[str, ReferencedEntity] = {}
    for cid in cids:
        node = nodes.get(cid)
        sentence = references.sentences.get(cid, "")
        if node is None or not sentence.strip():
            continue
        statement = node.quotes[0].quote if node.quotes else ""
        found[cid] = ReferencedEntity(statement=statement, referenced_in=sentence)
    return found


def attributes_from(edges: EdgesArtifact) -> AttributesOutcome:
    """The attributes pass outcome a stored extract-edges artifact records (values, failed cids and
    batch counters), for ``replay_edges`` (attributes are not asked again)."""

    counters = edges.counters
    return AttributesOutcome(
        values={cid: dict(found) for cid, found in edges.attributes.items()},
        batches=counters.attribute_batches,
        failed_batches=counters.attribute_batches_failed,
        reused=counters.attribute_nodes_reused,
        failed=tuple(edges.attributes_failed),
    )


async def _given(outcome: AttributesOutcome) -> AttributesOutcome:
    return outcome


def window_hash(unit: Unit, window: Window, fragments_by_id: Mapping[str, FragmentRec]) -> str:
    """The hash of an edge window's content: sha256 of its fragments' content hashes, in order."""

    start, end = window
    hashes = "\n".join(fragments_by_id[fid].content_hash for fid in unit.fragment_ids[start:end])
    return hashlib.sha256(hashes.encode("utf-8")).hexdigest()


# Window calls


@dataclass
class _EdgeTally:
    calls: int = 0
    failed_calls: int = 0
    truncation_splits: int = 0
    attribution_fallbacks: int = 0
    outside_window_edges: int = 0
    unknown_name_edges: int = 0
    unknown_relation_edges: int = 0
    self_loops: int = 0


@dataclass
class _EdgeResult:
    edges: list[RawEdge] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    complete: bool = True
    """Every call of the window answered (only then is the result stored)."""


class _EdgeUnitRun:
    """The edge-window calls of one unit: its fragment texts, catalog (names and section hints), the
    unit's warm-up and the call's collaborators."""

    def __init__(
        self,
        unit: Unit,
        *,
        ordinal: int,
        texts: list[str],
        catalog: Sequence[CanonicalNode],
        hints: Mapping[str, str],
        env: CallEnv,
        tally: _EdgeTally,
        referenced: Mapping[str, ReferencedEntity] | None = None,
    ) -> None:
        self.unit = unit
        self.ordinal = ordinal
        self.texts = texts
        names = catalog_names(catalog)
        self.catalog = [(names[node.cid], node.label) for node in catalog]
        self.sections = {names[cid]: hint for cid, hint in hints.items() if cid in names} or None
        self.referenced = {names[cid]: entry for cid, entry in (referenced or {}).items() if cid in names} or None
        self.cid_by_name = {name: cid for cid, name in names.items()}
        self.dirty = env.dirty
        self.fragments_by_id = env.fragments_by_id
        self.gateway = env.gateway
        self.ontology = env.ontology
        self.tally = tally
        self.warm_up = WarmUp(
            lambda: warm_up_edge_windows(
                self.gateway,
                self.ontology,
                self.texts,
                self.catalog,
                sections=self.sections,
                referenced=self.referenced,
            ),
            ordinal=ordinal,
            calls=EDGE_KIND,
        )

    async def run_all(self, windows: Sequence[Window]) -> list[_EdgeResult]:
        """The unit's windows concurrently, after its warm-up when there are two or more (sent ahead
        by ``extract_edges``, before any window call of the run)."""

        if len(windows) > 1:
            await self.warm_up()
        return await gather_all([self.run(window) for window in windows])

    async def run(self, window: Window, depth: int = 0) -> _EdgeResult:
        start, end = window
        window_ids = self.unit.fragment_ids[start:end]
        self.tally.calls += 1
        try:
            call = await extract_edge_window_call(
                self.gateway,
                self.ontology,
                self.texts,
                self.catalog,
                window,
                sections=self.sections,
                referenced=self.referenced,
            )
        except LLMFatalError:
            raise
        except LLMError as error:
            if error.reason == "truncated" and end - start > 1 and depth < MAX_SPLIT_DEPTH:
                self.tally.truncation_splits += 1
                await self.warm_up()
                parts = await gather_all(
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
        self.tally.self_loops += call.self_loops
        fallback = tuple(fragment_id for fragment_id in window_ids if fragment_id in self.dirty) or window_ids
        result = _EdgeResult()
        for edge in call.edges:
            if not edge.indices:
                self.tally.attribution_fallbacks += 1
                self.tally.outside_window_edges += edge.outside
            result.edges.append(
                RawEdge(
                    relation=edge.relation,
                    source_cid=self.cid_by_name[edge.source_name],
                    target_cid=self.cid_by_name[edge.target_name],
                    fact=edge.fact or None,
                    fragment_ids=tuple(self.unit.fragment_ids[index] for index in edge.indices) or fallback,
                    unit_id=self.unit.unit_id,
                )
            )
        return result


# The run


@dataclass(frozen=True)
class _EdgeContext:
    """The read-only inputs of one ``extract_edges`` run."""

    plan: PlanArtifact
    units: list[Unit]
    env: CallEnv
    titles: Mapping[str, str]
    by_cid: Mapping[str, CanonicalNode]
    cid_of: Mapping[str, str]
    results: StoredResults | None
    reading: bool
    fingerprint: str
    params: EdgeWindowParams

    @property
    def dirty(self) -> set[str]:
        return self.env.dirty

    @property
    def fragments_by_id(self) -> Mapping[str, FragmentRec]:
        return self.env.fragments_by_id


@dataclass(frozen=True)
class CatalogVectors:
    """The vectors that rank a catalog's foreign hubs by nearness: ``fragments`` by fragment id, and
    ``hubs`` by cid: each hub's ``name_embedding`` (nearness ``name``), or its Resolve candidate vector
    (``rich``: ``hubjudge.candidate_text`` embedded; the caller embeds them)."""

    fragments: Mapping[str, Vector] = field(default_factory=dict)
    hubs: Mapping[str, Vector] = field(default_factory=dict)


def rich_text(node: CanonicalNode, fragments_by_id: Mapping[str, FragmentRec]) -> str:
    """A hub's Resolve candidate text (``hubjudge.candidate_text``): its name, the heading path of its
    first fragment and its first source quote, each made safe and capped as the hub judge shows them."""

    first = next((fragments_by_id[fid] for fid in node.fragment_ids if fid in fragments_by_id), None)
    heading = HEADING_SEPARATOR.join(first.heading_path) if first is not None else ""
    quote = node.quotes[0].quote if node.quotes else ""
    item = JudgeItem(
        label=node.label,
        name=prompt_name(node.name),
        quote=prompt_name(quote, QUOTE_MAX_CHARS),
        heading=prompt_name(heading, HEADING_MAX_CHARS),
    )
    return candidate_text(item)


async def catalog_vectors(
    plan: PlanArtifact,
    resolved: ResolveArtifact,
    ontology: Ontology,
    cache: VectorCache,
    params: EdgeWindowParams = DEFAULT_EDGE_WINDOWS,
) -> CatalogVectors:
    """The vectors ``extract_edges`` ranks nearness hubs by (``CatalogVectors``), from ``cache``: every fragment's, and
    per nearness mode each hub's ``name_embedding`` text (``name``; every canonical node with one, as before nearness
    modes), its candidate text (``rich``, ``rich_text``) or none (``off``, and no fragment vectors either).
    """

    if params.nearness == "off":
        return CatalogVectors()
    fragments = await terms.fragment_vectors(plan.fragments, cache)
    if params.nearness == "rich":
        by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
        texts = {node.cid: rich_text(node, by_id) for node in resolved.canonical if node.label in ontology.hub_labels}
    else:
        texts = {
            node.cid: node.name_embedding_text for node in resolved.canonical if node.name_embedding_text is not None
        }
    embedded = await cache.get_or_embed(list(texts.values()))
    return CatalogVectors(fragments=fragments, hubs=dict(zip(texts, embedded, strict=True)))


@dataclass
class _UnitEdges:
    """One unit of the run: its edge windows with their hashes, its references and catalog, per
    window its edge key (none without a store) and whether its content has a stored last answer,
    and what became of its windows."""

    index: int
    unit: Unit
    windows: tuple[Window, ...]
    hashes: tuple[str, ...]
    references: UnitReferences
    catalog: UnitCatalog = field(default_factory=lambda: UnitCatalog(local=[], mentioned=[], near=[]))
    keys: tuple[str, ...] = ()
    stored: tuple[bool, ...] = ()
    calls: list[tuple[int, EdgeWindowCause]] = field(default_factory=list)
    """Positions in ``windows`` to call, with the cause, in window order."""
    reused: int = 0
    fallback: int = 0
    failed: bool = False
    """A window of the unit failed (fallen back or not): the unit's edges are not all this build's."""


@dataclass
class _EdgeBook:
    """What the units of one ``extract_edges`` run give: edges, entries to store and outcomes."""

    collected: list[tuple[tuple[int, str, str, str, str], RawEdge]] = field(default_factory=list)
    entries: dict[str, JsonObject] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    edge_failed: list[str] = field(default_factory=list)
    kept_catalogs: int = 0
    warm_ups: int = 0


@dataclass(frozen=True)
class _EdgeInputs:
    """What one edge extraction reads (``extract_edges``'s arguments)."""

    plan: PlanArtifact
    resolved: ResolveArtifact
    services: StepServices
    vectors: CatalogVectors
    results: StoredResults | None = None
    node_skipped: tuple[str, ...] = ()
    hub_aliases: Mapping[str, Sequence[str]] | None = None
    params: EdgeWindowParams = DEFAULT_EDGE_WINDOWS


async def extract_edges(
    plan: PlanArtifact,
    resolved: ResolveArtifact,
    *,
    services: StepServices,
    vectors: CatalogVectors,
    results: StoredResults | None = None,
    node_skipped: Collection[str] = (),
    hub_aliases: Mapping[str, Sequence[str]] | None = None,
    params: EdgeWindowParams = DEFAULT_EDGE_WINDOWS,
) -> EdgesArtifact:
    """Raw edges of the units' edge windows, the attributes pass, catalogs and skipped fragments (module docstring)."""

    inputs = _EdgeInputs(plan, resolved, services, vectors, results, tuple(node_skipped), hub_aliases, params)
    return await _extract(inputs, None)


async def replay_edges(
    plan: PlanArtifact,
    resolved: ResolveArtifact,
    *,
    services: StepServices,
    vectors: CatalogVectors,
    attributes: AttributesOutcome,
    params: EdgeWindowParams = DEFAULT_EDGE_WINDOWS,
) -> EdgesArtifact:
    """``extract_edges`` without a store and without persisted hub aliases (every window of the units with a dirty
    fragment is called; nothing is stored), the attributes pass replaced by ``attributes`` (``attributes_from`` a stored
    artifact: they are not asked again).
    """

    return await _extract(_EdgeInputs(plan, resolved, services, vectors, params=params), attributes)


async def _extract(inputs: _EdgeInputs, attributes: AttributesOutcome | None) -> EdgesArtifact:
    """``extract_edges`` of ``inputs``; with ``attributes`` no attributes pass runs."""

    plan, resolved, vectors, results = inputs.plan, inputs.resolved, inputs.vectors, inputs.results
    node_skipped, hub_aliases, params = inputs.node_skipped, inputs.hub_aliases, inputs.params
    services = inputs.services
    gateway, ontology, options = services.gateway, services.ontology, services.options
    fragments_by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    canonical = list(resolved.canonical)
    ctx = _EdgeContext(
        plan=plan,
        units=list(plan.units),
        env=CallEnv(set(plan.dirty_ids), fragments_by_id, gateway, ontology),
        titles={document.document_id: document.title for document in plan.documents},
        by_cid={node.cid: node for node in canonical},
        cid_of=_cids_by_key(canonical),
        results=results,
        reading=results is not None and results.read,
        fingerprint=edge_fingerprint(ontology, gateway, options, params),
        params=params,
    )
    selector = EdgeCatalog(
        fragments_by_id=fragments_by_id,
        canonical=canonical,
        hub_cids=frozenset(node.cid for node in canonical if node.label in ontology.hub_labels),
        name_vectors=vectors.hubs,
        fragment_vectors=vectors.fragments,
        limit=options.catalog_limit,
        aliases=hub_aliases,
        params=params,
    )
    book = _EdgeBook()
    units = _considered_units(ctx, _reference_finder(ctx, canonical))
    await _select_catalogs(ctx, selector, units, book)
    await _reuse_windows(ctx, units, book)
    tally = _EdgeTally()
    runs = _unit_runs(ctx, selector, units, tally)
    node_fingerprint_ = node_fingerprint(ontology, gateway, options)
    passes = node_passes(ontology)
    if attributes is None:
        kept = await kept_versions(
            plan, ctx.dirty, set(node_skipped), results if ctx.reading else None, node_fingerprint_, passes=passes
        )
        attribute_job = fill_attributes(
            resolved, plan, gateway=gateway, ontology=ontology, results=results, kept=kept, hub_aliases=hub_aliases
        )
    else:
        attribute_job = _given(attributes)
    jobs = [run.run_all([unit_edges.windows[position] for position, _ in unit_edges.calls]) for unit_edges, run in runs]
    try:
        # every warm-up first, then the units, each unit's windows after its own warm-up (``_EdgeUnitRun.run_all``)
        for unit_edges, run in runs:
            if len(unit_edges.calls) > 1:
                run.warm_up.send()
        answers, outcome = await gather_with(jobs, attribute_job)
    finally:
        for _, run in runs:
            await run.warm_up.stop()
    book.warm_ups = sum(run.warm_up.started for _, run in runs)
    await _settle_windows(ctx, [unit_edges for unit_edges, _ in runs], answers, book)
    if results is not None:
        await put_many(results.store, EDGE_KIND, ctx.fingerprint, dict(sorted(book.entries.items())))
    recovered: tuple[str, ...] = ()
    if results is not None and ctx.reading:
        answered = {unit_edges.index for unit_edges in units if not unit_edges.failed}
        recovered = await _recovered(results, (node_fingerprint_, passes), ctx, answered, skipped=book.skipped)
    return _edges_artifact(ctx, units, book, tally, outcome, recovered=recovered)


def _clean(ctx: _EdgeContext, fragment_ids: Sequence[str]) -> bool:
    return not any(fragment_id in ctx.dirty for fragment_id in fragment_ids)


def _reference_finder(ctx: _EdgeContext, canonical: Sequence[CanonicalNode]) -> ReferenceFinder | None:
    """The references of the build's units (``params.references``), else None."""

    if not ctx.params.references:
        return None
    ontology = ctx.env.ontology
    return ReferenceFinder(
        units=ctx.units,
        fragments_by_id=ctx.fragments_by_id,
        titles=ctx.titles,
        canonical=canonical,
        labels=requirement_labels(ontology),
        id_families=sorted({family for node_type in ontology.node_types.values() for family in node_type.id_families}),
        hub_labels=section_hub_labels(ontology),
    )


def _considered_units(ctx: _EdgeContext, finder: ReferenceFinder | None) -> list[_UnitEdges]:
    """Every unit when reading stored results, else those with a dirty fragment, in plan order, with
    their edge windows, window hashes and references."""

    found = []
    for index, unit in enumerate(ctx.units):
        if not ctx.reading and _clean(ctx, unit.fragment_ids):
            continue
        windows = edge_windows(unit, ctx.fragments_by_id, ctx.params)
        found.append(
            _UnitEdges(
                index=index,
                unit=unit,
                windows=windows,
                hashes=tuple(window_hash(unit, window, ctx.fragments_by_id) for window in windows),
                references=finder.for_unit(unit) if finder is not None else UnitReferences(),
            )
        )
    return found


async def _select_catalogs(
    ctx: _EdgeContext, selector: EdgeCatalog, units: Sequence[_UnitEdges], book: _EdgeBook
) -> None:
    """Each unit's catalog (a clean unit of an ``update`` build keeps the nearness hubs of its
    windows' last answer) and its windows' edge keys (with a store)."""

    results = ctx.results
    last: Mapping[str, JsonObject] = {}
    if results is not None and ctx.reading:
        wanted = sorted({results.last_edge_key(digest) for unit_edges in units for digest in unit_edges.hashes})
        last = await get_many(results.store, EDGE_KIND, wanted, ctx.fingerprint)
    for unit_edges in units:
        unit = unit_edges.unit
        kept = None
        if results is not None:
            payloads = [last.get(results.last_edge_key(digest)) for digest in unit_edges.hashes]
            unit_edges.stored = tuple(payload is not None for payload in payloads)
            if ctx.reading and _clean(ctx, unit.fragment_ids):
                kept = _kept_near(payloads, ctx.cid_of)
                book.kept_catalogs += kept is not None
        catalog = unit_edges.catalog = selector.select(unit, kept, referenced=unit_edges.references.cids)
        if results is not None and len(catalog.cids) >= MIN_EDGE_CATALOG:
            unit_edges.keys = tuple(
                results.edge_key(
                    digest, selector.window_catalog(unit, window, catalog, unit_edges.references.by_fragment)
                )
                for window, digest in zip(unit_edges.windows, unit_edges.hashes, strict=True)
            )


def _cause(ctx: _EdgeContext, unit_edges: _UnitEdges, position: int) -> EdgeWindowCause:
    """Why a window without reusable edges is called (``models.EdgeWindowCause``)."""

    if not ctx.reading:
        return "first"
    start, end = unit_edges.windows[position]
    if not _clean(ctx, unit_edges.unit.fragment_ids[start:end]):
        return "dirty"
    return "catalog_changed" if unit_edges.stored[position] else "not_stored"


async def _reuse_windows(ctx: _EdgeContext, units: Sequence[_UnitEdges], book: _EdgeBook) -> None:
    """Reuse the stored edges of each window whose content and window catalog have them; list the
    other windows to call, with their cause (a unit whose catalog is too small calls none)."""

    results = ctx.results
    found: Mapping[str, JsonObject] = {}
    if results is not None and ctx.reading:
        wanted = sorted({key for unit_edges in units for key in unit_edges.keys})
        found = await get_many(results.store, EDGE_KIND, wanted, ctx.fingerprint)
    for unit_edges in units:
        if len(unit_edges.catalog.cids) < MIN_EDGE_CATALOG:
            continue
        for position, window in enumerate(unit_edges.windows):
            payload = found.get(unit_edges.keys[position]) if unit_edges.keys else None
            if payload is None:
                unit_edges.calls.append((position, _cause(ctx, unit_edges, position)))
                continue
            unit_edges.reused += 1
            stored = _stored_edges(payload, unit_edges.unit, window, ctx.cid_of)
            book.collected.extend((_edge_order(unit_edges.index, edge), edge) for edge in stored)
            near = [ctx.by_cid[cid].identity_key for cid in unit_edges.catalog.near]
            _remember(ctx, unit_edges, position, {**payload, "near": near}, book)


def _remember(ctx: _EdgeContext, unit_edges: _UnitEdges, position: int, payload: JsonObject, book: _EdgeBook) -> None:
    """Store a window's answer under its content (``last_edge_key``) and identity (``window_edge_key``),
    with the window's fragment ids (``WINDOW_FRAGMENTS``: what an identity fallback covers)."""

    if ctx.results is None:
        return
    start, end = unit_edges.windows[position]
    unit = unit_edges.unit
    payload = {**payload, WINDOW_FRAGMENTS: list(unit.fragment_ids[start:end])}
    book.entries[ctx.results.last_edge_key(unit_edges.hashes[position])] = payload
    book.entries[ctx.results.window_edge_key(unit.document_id, unit.fragment_ids[start])] = payload


def _unit_runs(
    ctx: _EdgeContext, selector: EdgeCatalog, units: Sequence[_UnitEdges], tally: _EdgeTally
) -> list[tuple[_UnitEdges, _EdgeUnitRun]]:
    """The calls of each unit with a window to call, in plan order (section hints with
    ``params.section_hints``: the owner targets of the ownership line, ``prompts.ownership_targets``)."""

    owners = {label for labels in ownership_targets(ctx.env.ontology).values() for label in labels}
    runs = []
    for unit_edges in units:
        if not unit_edges.calls:
            continue
        unit = unit_edges.unit
        title = ctx.titles.get(unit.document_id, unit.document_id)
        fragments = [ctx.fragments_by_id[fid] for fid in unit.fragment_ids]
        hints = selector.section_hints(unit, unit_edges.catalog, owners) if ctx.params.section_hints else {}
        run = _EdgeUnitRun(
            unit,
            ordinal=unit_edges.index,
            texts=[fragment_text(title, fragment.heading_path, fragment.text) for fragment in fragments],
            catalog=[ctx.by_cid[cid] for cid in unit_edges.catalog.cids],
            hints=hints,
            env=ctx.env,
            tally=tally,
            referenced=referenced_entities(unit_edges.catalog.referenced, unit_edges.references, ctx.by_cid),
        )
        runs.append((unit_edges, run))
    return runs


async def _settle_windows(
    ctx: _EdgeContext, called: Sequence[_UnitEdges], answers: Sequence[Sequence[_EdgeResult]], book: _EdgeBook
) -> None:
    """Take the edges of every answered window and the entries to store; a failed window takes its
    fallback edges (``update`` builds), else its answered parts, and skips its dirty fragments."""

    failed: list[tuple[_UnitEdges, int, _EdgeResult]] = []
    for unit_edges, results_ in zip(called, answers, strict=True):
        for (position, _), result in zip(unit_edges.calls, results_, strict=True):
            if not result.complete:
                failed.append((unit_edges, position, result))
                continue
            book.collected.extend((_edge_order(unit_edges.index, edge), edge) for edge in result.edges)
            if ctx.results is not None:
                key = unit_edges.keys[position]
                near = [ctx.by_cid[cid].identity_key for cid in unit_edges.catalog.near]
                window = unit_edges.windows[position]
                payload = _edge_payload(result.edges, unit_edges.unit, window, ctx.by_cid, near=near, key=key)
                book.entries[key] = payload
                _remember(ctx, unit_edges, position, payload, book)
    fallbacks: dict[tuple[str, Window], _Fallback] = {}
    if ctx.results is not None and ctx.reading and failed:
        fallbacks = await _fallback_edges(
            ctx.results,
            ctx.fingerprint,
            [(unit_edges.unit, unit_edges.windows[position]) for unit_edges, position, _ in failed],
            ctx.fragments_by_id,
            ctx.cid_of,
        )
    for unit_edges, position, result in failed:
        unit_edges.failed = True
        start, end = window = unit_edges.windows[position]
        fallback = fallbacks.get((unit_edges.unit.unit_id, window))
        if fallback is not None:
            unit_edges.fallback += 1
            book.collected.extend((_edge_order(unit_edges.index, edge), edge) for edge in fallback.edges)
            book.edge_failed.extend(fallback.uncovered)
            continue
        book.skipped.update(result.skipped)
        book.collected.extend((_edge_order(unit_edges.index, edge), edge) for edge in result.edges)
        book.edge_failed.extend(unit_edges.unit.fragment_ids[start:end])


def _edges_artifact(
    ctx: _EdgeContext,
    units: Sequence[_UnitEdges],
    book: _EdgeBook,
    tally: _EdgeTally,
    attributes: AttributesOutcome,
    *,
    recovered: tuple[str, ...],
) -> EdgesArtifact:
    """The artifact of the run (raw edges in order, skipped fragments by plan order), counted and logged."""

    book.collected.sort(key=lambda item: item[0])
    order = {fragment.fragment_id: index for index, fragment in enumerate(ctx.plan.fragments)}
    skipped = dict(sorted(book.skipped.items(), key=lambda item: order.get(item[0], len(order))))
    called = [unit_edges for unit_edges in units if unit_edges.calls]
    counters = EdgeCounters(
        units_extracted=len(called),
        units_reused=sum(1 for unit_edges in units if unit_edges.reused and not unit_edges.calls),
        units_fallback=sum(1 for unit_edges in units if unit_edges.fallback),
        catalogs_kept=book.kept_catalogs,
        calls=tally.calls,
        failed_calls=tally.failed_calls,
        attribute_batches=attributes.batches,
        attribute_batches_failed=attributes.failed_batches,
        attribute_nodes_reused=attributes.reused,
        truncation_splits=tally.truncation_splits,
        attribution_fallbacks=tally.attribution_fallbacks,
        outside_window_edges=tally.outside_window_edges,
        unknown_name_edges=tally.unknown_name_edges,
        unknown_relation_edges=tally.unknown_relation_edges,
        self_loops=tally.self_loops,
        windows_called=sum(len(unit_edges.calls) for unit_edges in units),
        windows_reused=sum(unit_edges.reused for unit_edges in units),
        windows_fallback=sum(unit_edges.fallback for unit_edges in units),
        warm_ups=book.warm_ups,
    )
    log_event(
        logger,
        INFO,
        "graph.extract.edges_done",
        **counters.model_dump(exclude={"attribute_batches", "attribute_batches_failed", "attribute_nodes_reused"}),
        raw_edges=len(book.collected),
        reused_edges=sum(1 for _, edge in book.collected if edge.reused),
        skipped_fragments=len(skipped),
        recovered_fragments=len(recovered),
        attribute_batches=attributes.batches,
        attribute_batches_failed=attributes.failed_batches,
        attributed_nodes=len(attributes.values),
        attribute_nodes_reused=attributes.reused,
    )
    return EdgesArtifact(
        raw_edges=tuple(edge for _, edge in book.collected),
        attributes={cid: dict(found) for cid, found in sorted(attributes.values.items())},
        attributes_failed=attributes.failed,
        catalogs={unit_edges.unit.unit_id: tuple(unit_edges.catalog.cids) for unit_edges in units},
        skipped=skipped,
        called_units=tuple(unit_edges.unit.unit_id for unit_edges in called),
        recovered=recovered,
        edge_failed_fragments=tuple(sorted(set(book.edge_failed), key=lambda fid: order.get(fid, len(order)))),
        counters=counters,
        called_windows=tuple(
            EdgeWindowCall(unit_id=unit_edges.unit.unit_id, window=unit_edges.windows[position], cause=cause)
            for unit_edges in called
            for position, cause in unit_edges.calls
        ),
        catalog_parts={unit_edges.unit.unit_id: unit_edges.catalog.parts() for unit_edges in units},
    )


# Stored edges


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


def _kept_near(payloads: Sequence[JsonObject | None], cid_of: Mapping[str, str]) -> list[str] | None:
    """The cids of the nearness hubs of the first of a unit's stored last window answers that has
    them, those that still exist; None when no answer has a ``near`` list."""

    for payload in payloads:
        near = payload.get("near") if payload is not None else None
        if isinstance(near, list):
            return [cid_of[key] for key in near if isinstance(key, str) and key in cid_of]
    return None


@dataclass(frozen=True)
class _Fallback:
    """A failed window's last stored edges and the window's fragments they do not cover (none for the
    same content; for the same identity, those the stored window did not hold)."""

    edges: list[RawEdge]
    uncovered: tuple[str, ...] = ()


async def _fallback_edges(
    results: StoredResults,
    fingerprint: str,
    failed: Sequence[tuple[Unit, Window]],
    fragments_by_id: Mapping[str, FragmentRec],
    cid_of: Mapping[str, str],
) -> dict[tuple[str, Window], _Fallback]:
    """The last stored edges of each failed window, by unit id and window: those of its content first (complete), else
    those of its identity (document and first fragment, or the id that fragment replaces), clipped to the window, with
    the window's fragments the stored window did not hold.
    """

    wanted: dict[tuple[str, Window], list[tuple[str, bool]]] = {}
    for unit, window in failed:
        first_id = unit.fragment_ids[window[0]]
        first = fragments_by_id.get(first_id)
        candidates = [(results.last_edge_key(window_hash(unit, window, fragments_by_id)), True)]
        candidates.append((results.window_edge_key(unit.document_id, first_id), False))
        if first is not None and first.previous_id:
            candidates.append((results.window_edge_key(unit.document_id, first.previous_id), False))
        wanted[(unit.unit_id, window)] = candidates
    found = await get_many(
        results.store, EDGE_KIND, sorted({key for keys in wanted.values() for key, _ in keys}), fingerprint
    )
    fallbacks: dict[tuple[str, Window], _Fallback] = {}
    for unit, window in failed:
        for key, same_content in wanted[(unit.unit_id, window)]:
            if key not in found:
                continue
            if same_content:
                fallbacks[(unit.unit_id, window)] = _Fallback(_stored_edges(found[key], unit, window, cid_of))
            else:
                fallbacks[(unit.unit_id, window)] = _Fallback(
                    _stored_edges(found[key], unit, window, cid_of, fragments_by_id=fragments_by_id),
                    _uncovered(found[key], unit.fragment_ids[window[0] : window[1]], fragments_by_id),
                )
            break
    return fallbacks


def _uncovered(
    payload: JsonObject, members: Sequence[str], fragments_by_id: Mapping[str, FragmentRec]
) -> tuple[str, ...]:
    """The window ``members`` a stored window answer of other content does not cover: neither the
    fragment nor the id it replaces is among the stored window's fragments (an answer stored without
    them covers none)."""

    stored = payload.get(WINDOW_FRAGMENTS)
    held = {fid for fid in stored if isinstance(fid, str)} if isinstance(stored, list) else set()
    uncovered = []
    for fid in members:
        fragment = fragments_by_id.get(fid)
        previous = fragment.previous_id if fragment is not None else None
        if fid not in held and (not previous or previous not in held):
            uncovered.append(fid)
    return tuple(uncovered)


async def _recovered(
    results: StoredResults,
    stored_nodes: tuple[str, Sequence[NodeList]],
    ctx: _EdgeContext,
    answered: set[int],
    *,
    skipped: Mapping[str, str],
) -> tuple[str, ...]:
    """Fragments persisted ``skipped``, not dirty and not skipped now, whose unit's edges are this
    build's (``answered``) and whose nodes are stored for their current content (plan order);
    ``stored_nodes``: the node fingerprint and passes."""

    unit_of = {fid: unit_index for unit_index, unit in enumerate(ctx.units) for fid in unit.fragment_ids}
    candidates = [
        fragment
        for fragment in ctx.plan.fragments
        if fragment.status == "skipped"
        and fragment.fragment_id not in ctx.dirty
        and fragment.fragment_id not in skipped
        and unit_of.get(fragment.fragment_id) in answered
    ]
    if not candidates:
        return ()
    fingerprint, passes = stored_nodes
    stored = await stored_node_fragments(results, fingerprint, candidates, passes)
    return tuple(fragment.fragment_id for fragment in candidates if fragment.fragment_id in stored)


def _edge_payload(
    edges: Sequence[RawEdge],
    unit: Unit,
    window: Window,
    by_cid: Mapping[str, CanonicalNode],
    *,
    near: Sequence[str],
    key: str,
) -> JsonObject:
    """A window's raw edges as stored: endpoints by identity key, fragments by position in the window
    and by id; ``near``: the identity keys of the hubs its unit catalog took by nearness; ``key``: the
    edge key it is stored under."""

    start, end = window
    position = {fid: index for index, fid in enumerate(unit.fragment_ids[start:end])}
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
    return {"edges": items, "near": list(near), "edge_key": key}


def _stored_edges(
    payload: JsonObject,
    unit: Unit,
    window: Window,
    cid_of: Mapping[str, str],
    *,
    fragments_by_id: Mapping[str, FragmentRec] | None = None,
) -> list[RawEdge]:
    """The stored raw edges of a window on this build's cids, marked ``reused``; an edge whose endpoint has no canonical
    node now, or a malformed item, is left out.
    """

    start, end = window
    members = unit.fragment_ids[start:end]
    successor: dict[str, str] = {}
    if fragments_by_id is not None:
        for fid in members:
            successor[fid] = fid
            previous = fragments_by_id[fid].previous_id if fid in fragments_by_id else None
            if previous:
                successor.setdefault(previous, fid)
    edges = []
    for item in payload.get("edges") or ():
        if not isinstance(item, dict):
            continue
        source, target = (cid_of.get(str(item.get("source"))), cid_of.get(str(item.get("target"))))
        relation = item.get("relation")
        if source is None or target is None or source == target or not isinstance(relation, str):
            continue
        if fragments_by_id is None:
            positions = [
                index for index in item.get("positions") or () if isinstance(index, int) and 0 <= index < len(members)
            ]
            fragment_ids = tuple(members[index] for index in positions)
        else:
            stored = [fid for fid in item.get("fragments") or () if isinstance(fid, str)]
            fragment_ids = tuple(dict.fromkeys(successor[fid] for fid in stored if fid in successor))
            if not fragment_ids:
                continue
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
