"""Cost estimate of a build's model calls (build.md "Cost estimate"; tech-specs 5.4 "Estimate"; FR-BLD-16).

``estimate_steps`` predicts, from a plan and read-only lookups, the calls and input tokens a build
would make before inference and verification. It never calls a model and never writes: every prompt
is rendered by the code the build itself uses (``prompts``, ``extract.attribute_request``,
``graphiti.render_*_request``, ``resolve.hub_block_messages``, ``conform.dedup_messages``) and counted
with ``gateway.request_tokens`` (``ceil(len / 3.6)`` of the messages as sent); embedding requests are
counted as the gateway batches them. Steps:

- ``hub_catalog`` (``first`` and ``rebuild``): one call per outline batch, exact.
- ``node_extraction``: one call per window holding a dirty fragment, exact. Known terms are the
  persisted hub names (``update``, and ``rebuild`` as a stand-in for its hub catalog); a ``first``
  build gets its expected hubs as placeholder names. Truncation splits are not predicted.
- ``hub_consolidation``: the expected hubs in blocks of ``BLOCK_SIZE`` (overlap ``BLOCK_OVERLAP``);
  without a hub unseen by the decision memory nothing is sent, else one block per unseen hub at most
  (the nearest-neighbour order is unknown before the vectors exist; the close-pair condition of a
  skipped block is not checked).
- ``edge_extraction``: one call per unit with a dirty fragment, except an ``update`` unit whose
  content has a stored answer (its edges are likely reused); a clean unit's catalog-driven call is
  not predicted. The catalog is the unit's expected nodes plus hubs from elsewhere (``_catalog``); a
  unit without an expected node of its own, or with fewer than two in all, makes no call.
- ``attributes``: the expected nodes with a dirty fragment, less those whose attributes an
  ``update`` build finds stored, batched by ``extract.attribute_batches``; a hub's fragments are
  those naming it (``extract.attribute_scope``), for the stored lookup too.
- ``requirement_dedup``: ``DEDUP_PAIRS_PER_REQUIREMENT`` judged pairs per new expected id-less
  requirement (one without a persisted version). Pairs of two persisted requirements were judged,
  merged directly or refused by a guard in the build that made them, so they cost nothing.
- ``embeddings``: the texts each step embeds (fragment texts, known-term names, hub and node names,
  search texts) that the vector store does not hold, step by step as the build asks for them.

Expected nodes: a fragment with a persisted version (the fragment itself, the one it replaces, or one
of the same document with the same content; ``update`` and ``rebuild``) is assumed to give back the
persisted nodes grounded in it, with their names, statements and quotes; any other gives
``NODES_PER_KTOKEN`` per 1,000 estimated tokens of placeholders (``HUB_SHARE`` of them hubs, at least
one outside a ``first`` build). Output tokens are not estimated.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from graph.build.conform import dedup_messages
from graph.build.consolidate import BLOCK_OVERLAP, BLOCK_SIZE, HUBS_PASS, HUBS_PROMPT, DecisionMemory, blocks
from graph.build.diff import QUOTE_PROPERTY, STMT_PROPERTY, CurrentGraph, CurrentNode
from graph.build.extract import (
    StoredResults,
    attribute_batches,
    attribute_request,
    attribute_scope,
    stored_attribute_nodes,
    stored_edge_units,
)
from graph.build.graphiti import render_edges_request, render_nodes_request
from graph.build.models import BuildMode, CanonicalNode, EstimateStep, FragmentRec, PlanArtifact, StepEstimate, Unit
from graph.build.options import BuildOptions, StepServices
from graph.build.ports import CorpusRevision
from graph.build.prompts import edge_instructions, episode_text, hub_catalog_messages, node_instructions
from graph.build.resolve import embedding_text, hub_block_messages
from graph.build.terms import CATALOG_NEAREST, HubName, hub_catalog_batches
from graph.build.vectors import VectorCache
from graph.core.identity import bounded_key_value, identity_key, normalise_surface
from graph.core.llm import Gateway
from graph.core.llm.gateway import embed_batches, embed_inputs, estimate_tokens, request_tokens
from graph.core.ontology import Ontology

NODES_PER_KTOKEN: Final = 20
"""Expected canonical nodes per 1,000 estimated fragment tokens of a fragment without a persisted
version (measured with the real models: the shop corpus 19, input-v2 32)."""
HUB_SHARE: Final = 0.1
"""Share of hubs among the placeholder nodes (measured: about 13 of 124 on the shop corpus)."""
DEDUP_PAIRS_PER_REQUIREMENT: Final = 0.06
"""Judged dedup pairs per new id-less requirement (measured: about 7 per 110 on shop, 5 per 100 on input-v2)."""
MIN_CATALOG_NODES: Final = 2
"""Nodes an edge catalog needs to hold a pair."""
MIN_HUB_CONSOLIDATION: Final = 2
"""Hubs a consolidation block needs to hold a pair."""
PLACEHOLDER_NAME_CHARS: Final = 48
"""Length of a placeholder node name (about the average name length of the measured corpora)."""
PLACEHOLDER_HUB_CHARS: Final = 20
"""Length of a placeholder hub name (hub names are short: a module, feature or capability)."""
PLACEHOLDER_TEXT_CHARS: Final = 150
"""Length of a placeholder statement, quote or evidence line."""
EVIDENCE_LINES: Final = 2
"""Evidence lines shown per hub in a consolidation block (``resolve.EVIDENCE_LINES``)."""

StepCosts = dict[EstimateStep, StepEstimate]


@dataclass(frozen=True)
class Expected:
    """An expected node; equal by ``(label, name)``. Persisted ones carry their identity key and texts."""

    label: str
    name: str
    persisted: bool = field(default=False, compare=False)
    key: str = field(default="", compare=False)
    stmt: str = field(default="", compare=False)
    quote: str = field(default="", compare=False)
    evidence: tuple[str, ...] = field(default=(), compare=False)
    aliases: tuple[str, ...] = field(default=(), compare=False)
    """A persisted node's identity key and aliases (a hub is found by their ``surface`` values)."""


@dataclass(frozen=True)
class PersistedNodes:
    """The persisted nodes as the expected nodes of a plan's fragments (``persisted_nodes``)."""

    by_fragment: Mapping[str, Sequence[Expected]] = field(default_factory=dict)
    """The nodes grounded in each plan fragment's persisted version, by plan fragment id; a fragment
    without a persisted version is left out, one whose version holds no node maps to an empty list."""
    attribute_inputs: Mapping[Expected, tuple[str, str, tuple[str, ...]]] = field(default_factory=dict)
    """Identity key (as Resolve gives it, ``_resolve_key``), label and the content hashes of its plan
    fragments, of each node grounded in a plan fragment: the inputs of its attribute reuse key."""


@dataclass(frozen=True)
class Lookups:
    """The read-only stores the estimate consults; ``results`` only in ``update`` builds (the only
    mode that reuses stored extraction results)."""

    vectors: VectorCache
    decisions: DecisionMemory
    results: StoredResults | None = None
    edge_fingerprint: str = ""
    attribute_fingerprint: str = ""


def persisted_nodes(
    fragments: Sequence[FragmentRec], current: CurrentGraph, hub_labels: Collection[str] = ()
) -> PersistedNodes:
    """The persisted nodes of ``current`` on the plan's ``fragments``. A plan fragment's persisted
    version is the fragment itself (unchanged), the one it replaces (``previous_id``), else a
    persisted fragment of the same document with the same content (a heading renamed). A node of
    ``hub_labels`` is keyed, as the attributes pass keys it (``extract.attribute_scope``), by the plan
    fragments that name it, or by all of them when none does."""

    grounded: dict[str, list[Expected]] = {fragment.fragment_id: [] for fragment in current.fragments}
    keys: dict[Expected, str] = {}
    for node in current.nodes:
        stmt, quote = node.properties.get(STMT_PROPERTY), node.properties.get(QUOTE_PROPERTY)
        expected = Expected(
            node.label,
            node.name,
            persisted=True,
            key=node.identity_key,
            stmt=stmt if isinstance(stmt, str) else "",
            quote=quote if isinstance(quote, str) else "",
            evidence=tuple(line for evidence in node.evidence for line in evidence.lines if line.strip())[
                :EVIDENCE_LINES
            ],
            aliases=(node.identity_key, *node.identity_aliases),
        )
        keys.setdefault(expected, _resolve_key(node))
        for fid in dict.fromkeys(evidence.fragment_id for evidence in node.evidence):
            grounded.setdefault(fid, []).append(expected)
    by_content = {
        (fragment.document_id, fragment.content_hash): fragment.fragment_id
        for fragment in current.fragments
        if fragment.content_hash
    }
    by_fragment: dict[str, list[Expected]] = {}
    held: dict[Expected, list[str]] = {}
    for fragment in fragments:
        versions = (
            fragment.fragment_id,
            fragment.previous_id,
            by_content.get((fragment.document_id, fragment.content_hash)),
        )
        found = next((fid for fid in versions if fid is not None and fid in grounded), None)
        if found is None:
            continue
        by_fragment[fragment.fragment_id] = grounded[found]
        for expected_node in grounded[found]:
            held.setdefault(expected_node, []).append(fragment.fragment_id)
    content = {fragment.fragment_id: fragment.content_hash for fragment in fragments}
    scoped = {
        int(node.cid): node.fragment_ids
        for node in attribute_scope(
            [
                CanonicalNode(
                    cid=str(index),
                    identity_key=node.key,
                    identity_aliases=node.aliases,
                    name=node.name,
                    label=node.label,
                    member_ids=(),
                    fragment_ids=tuple(fids),
                )
                for index, (node, fids) in enumerate(held.items())
            ],
            fragments,
            hub_labels,
        )
    }
    return PersistedNodes(
        by_fragment=by_fragment,
        attribute_inputs={
            node: (keys[node], node.label, tuple(content[fid] for fid in scoped[index]))
            for index, node in enumerate(held)
        },
    )


def _resolve_key(node: CurrentNode) -> str:
    """The identity key Resolve gives the node's name before Conform: a requirement keyed by its span
    has a provisional key from its normalised name then (``resolve``), any other node its own key."""

    scope, kind, _ = (*node.identity_key.split("|", 2), "", "")[:3]
    if kind == "span":
        return identity_key(scope, "span", bounded_key_value(normalise_surface(node.name)))
    return node.identity_key


async def estimate_steps(
    plan: PlanArtifact,
    *,
    mode: BuildMode,
    services: StepServices,
    revisions: Sequence[CorpusRevision],
    hubs: Sequence[HubName],
    persisted: PersistedNodes,
    lookups: Lookups,
) -> StepCosts:
    """Calls and input tokens per step of a ``mode`` build of ``plan`` (module docstring).

    ``revisions``: the active documents whose outlines the hub catalog reads (empty when the build
    runs none); ``hubs``: the persisted hub names (empty in a ``first`` build); ``persisted``: the
    persisted nodes of the plan's fragments (``persisted_nodes``); ``services``: the gateway, ontology
    and options.
    """

    gateway, ontology, options = services.gateway, services.ontology, services.options
    fragments_by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    titles = {document.document_id: document.title for document in plan.documents}
    dirty = set(plan.dirty_ids)
    expected = _Expected(ontology, persisted.by_fragment, new_hub=mode != "first")
    every = list(dict.fromkeys(node for fragment in plan.fragments for node in expected.of(fragment)))
    hub_nodes = [node for node in every if node.label in ontology.hub_labels]
    if mode == "first":
        names = [node.name for node in hub_nodes] if revisions else []
    else:
        names = [hub.name for hub in sorted(hubs, key=lambda hub: (hub.name, hub.label))]
    dirty_units = [unit for unit in plan.units if any(fid in dirty for fid in unit.fragment_ids)]
    edges_stored: set[str] = set()
    attributes_stored: set[Expected] = set()
    if lookups.results is not None:
        edges_stored = await stored_edge_units(lookups.results, lookups.edge_fingerprint, dirty_units)
        attributes_stored = await stored_attribute_nodes(
            lookups.results, lookups.attribute_fingerprint, persisted.attribute_inputs
        )
    asked = _attribute_targets(plan, dirty, expected, attributes_stored, ontology.hub_labels)
    nodes, edges = await _extraction(
        dirty_units,
        _ExtractionInputs(
            fragments_by_id=fragments_by_id,
            titles=titles,
            dirty=dirty,
            expected=expected,
            terms=names[: options.known_terms_limit],
            hubs=hubs if mode != "first" else [],
            first_hubs=len(hub_nodes) if mode == "first" else 0,
            edges_stored=edges_stored,
            gateway=gateway,
            ontology=ontology,
            options=options,
        ),
    )
    return {
        "hub_catalog": _hub_catalog(plan, revisions, gateway, ontology, options),
        "node_extraction": nodes,
        "hub_consolidation": await _consolidation(hub_nodes, lookups.decisions, gateway, ontology),
        "edge_extraction": edges,
        "attributes": _attributes(
            plan, fragments_by_id, titles, dirty=dirty, targets=asked, gateway=gateway, ontology=ontology
        ),
        "requirement_dedup": _dedup(every, gateway, ontology),
        "embeddings": await _embeddings(
            plan, every, hub_nodes, names, extracting=bool(dirty_units), vectors=lookups.vectors
        ),
    }


def _plus(step: StepEstimate, tokens: int, calls: int = 1) -> StepEstimate:
    return StepEstimate(calls=step.calls + calls, input_tokens=step.input_tokens + tokens)


def _hub_catalog(
    plan: PlanArtifact,
    revisions: Sequence[CorpusRevision],
    gateway: Gateway,
    ontology: Ontology,
    options: BuildOptions,
) -> StepEstimate:
    batches = hub_catalog_batches(revisions, plan.fragments, ontology, options)
    return StepEstimate(
        calls=len(batches),
        input_tokens=sum(request_tokens(hub_catalog_messages(ontology, gateway, batch)) for batch in batches),
    )


class _Expected:
    """The expected nodes of each fragment: persisted ones, else placeholders by token density."""

    def __init__(
        self, ontology: Ontology, persisted: Mapping[str, Sequence[Expected]], *, new_hub: bool = False
    ) -> None:
        # ``new_hub``: a fragment without a persisted version in a project that has one gets at least
        # one placeholder hub (new text usually names a part the project did not have).
        self._new_hub = new_hub
        extracted = tuple(ontology.extracted_labels)
        self._hub_labels = tuple(label for label in extracted if label in ontology.hub_labels)
        self._other_labels = tuple(label for label in extracted if label not in ontology.hub_labels)
        self._persisted = {fid: [node for node in nodes if node.label in extracted] for fid, nodes in persisted.items()}

    def of(self, fragment: FragmentRec) -> list[Expected]:
        found = self._persisted.get(fragment.fragment_id)
        if found is not None:
            return found
        if not fragment.text.strip():
            return []
        count = math.ceil(fragment.tokens * NODES_PER_KTOKEN / 1000)
        hubs = max(int(self._new_hub), round(count * HUB_SHARE)) if self._hub_labels else 0
        tag = fragment.content_hash[:8]
        result = [
            Expected(
                self._hub_labels[index % len(self._hub_labels)], _placeholder(f"{tag}h{index}", PLACEHOLDER_HUB_CHARS)
            )
            for index in range(hubs)
        ]
        if self._other_labels:
            result += [
                Expected(self._other_labels[index % len(self._other_labels)], _placeholder(f"{tag}n{index}"))
                for index in range(count - hubs)
            ]
        return result


def _placeholder(tag: str, length: int = PLACEHOLDER_NAME_CHARS) -> str:
    """A placeholder text of ``length`` characters (at least the tag's), unique per ``tag``."""

    return tag.rjust(length, "n")


@dataclass(frozen=True)
class _ExtractionInputs:
    """What the node and edge extraction estimates read besides the units."""

    fragments_by_id: Mapping[str, FragmentRec]
    titles: Mapping[str, str]
    dirty: set[str]
    expected: _Expected
    terms: Sequence[str]
    hubs: Sequence[HubName]
    first_hubs: int
    edges_stored: Collection[str]
    gateway: Gateway
    ontology: Ontology
    options: BuildOptions


async def _extraction(units: Sequence[Unit], inputs: _ExtractionInputs) -> tuple[StepEstimate, StepEstimate]:
    """Node extraction and edge extraction over the units with a dirty fragment."""

    gateway, ontology, fragments_by_id = inputs.gateway, inputs.ontology, inputs.fragments_by_id
    entity_types = ontology.entity_models()
    edge_types, edge_type_map = ontology.edge_models(), ontology.edge_type_map()
    hub_label = min(ontology.hub_labels, default="")
    nodes = edges = StepEstimate()
    for unit in units:
        episodes = [
            episode_text(
                gateway,
                inputs.titles.get(unit.document_id, unit.document_id),
                fragments_by_id[fid].heading_path,
                fragments_by_id[fid].text,
            )
            for fid in unit.fragment_ids
        ]
        for window in unit.windows:
            if not any(fid in inputs.dirty for fid in unit.fragment_ids[window[0] : window[1]]):
                continue
            instructions = node_instructions(
                ontology, gateway, known_terms=inputs.terms, window=window, episodes=len(episodes)
            )
            nodes = _plus(nodes, request_tokens(await render_nodes_request(episodes, entity_types, instructions)))
        if unit.unit_id in inputs.edges_stored:
            continue
        local, foreign = _catalog(unit, inputs, hub_label)
        if not local or len(local) + len(foreign) < MIN_CATALOG_NODES:
            continue
        instructions = edge_instructions(ontology, window=(0, len(episodes)), episodes=len(episodes))
        messages = await render_edges_request(episodes, [*local, *foreign], edge_types, edge_type_map, instructions)
        edges = _plus(edges, request_tokens(messages))
    return nodes, edges


def _catalog(
    unit: Unit, inputs: _ExtractionInputs, hub_label: str
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The expected edge catalog of ``unit`` as ``(name, label)``: its expected nodes (one per name)
    and the hubs from elsewhere (at most ``catalog_limit``, and ``CATALOG_NEAREST`` per fragment): the
    persisted hubs not among its nodes, or, in a ``first`` build, ``first_hubs`` placeholders."""

    local: dict[str, str] = {}
    for fid in unit.fragment_ids:
        for node in inputs.expected.of(inputs.fragments_by_id[fid]):
            local.setdefault(node.name, node.label)
    room = min(inputs.options.catalog_limit, CATALOG_NEAREST * len(unit.fragment_ids))
    if inputs.hubs:
        foreign = [(hub.name, hub.label) for hub in inputs.hubs if hub.name not in local][:room]
    else:
        foreign = [
            (_placeholder(f"hub{index}", PLACEHOLDER_HUB_CHARS), hub_label)
            for index in range(min(room, inputs.first_hubs))
        ]
    return list(local.items()), foreign


async def _consolidation(
    hub_nodes: Sequence[Expected], memory: DecisionMemory, gateway: Gateway, ontology: Ontology
) -> StepEstimate:
    """Hub consolidation blocks over the expected hubs (sorted by key, as the build sorts them before
    its chain); only blocks with a hub the decision memory has not seen are sent."""

    items = sorted(hub_nodes, key=lambda node: (node.key or "~", node.name))
    block_list = blocks(list(range(len(items))), BLOCK_SIZE, BLOCK_OVERLAP)
    if not block_list:
        return StepEstimate()
    seen = await memory.nodes(HUBS_PASS, HUBS_PROMPT, (node.key for node in items if node.key))
    unseen = sum(1 for node in items if node.key not in seen)
    step = StepEstimate()
    for block in block_list[: min(len(block_list), unseen)]:
        shown = [
            (
                items[index].label,
                items[index].name,
                items[index].evidence or (_placeholder("e", PLACEHOLDER_TEXT_CHARS),) * EVIDENCE_LINES,
            )
            for index in block
        ]
        step = _plus(step, request_tokens(hub_block_messages(ontology, gateway, shown)))
    return step


def _attribute_targets(
    plan: PlanArtifact, dirty: set[str], expected: _Expected, stored: Collection[Expected], hub_labels: Collection[str]
) -> list[CanonicalNode]:
    """The nodes the attributes pass asks: one per distinct expected node with a dirty fragment and
    no stored values, grounded in every fragment that holds it (a hub in those naming it, as
    ``extract.attribute_scope`` reads it), ordered by its first fragment."""

    grounded: dict[Expected, list[str]] = {}
    for fragment in plan.fragments:
        for node in expected.of(fragment):
            if node not in stored:
                grounded.setdefault(node, []).append(fragment.fragment_id)
    nodes = attribute_scope(
        [
            CanonicalNode(
                cid=f"e{index}",
                identity_key=f"e{index}",
                identity_aliases=node.aliases,
                name=node.name,
                label=node.label,
                member_ids=(),
                fragment_ids=tuple(fids),
            )
            for index, (node, fids) in enumerate(grounded.items())
        ],
        plan.fragments,
        hub_labels,
    )
    return [node for node in nodes if any(fid in dirty for fid in node.fragment_ids)]


def _attributes(
    plan: PlanArtifact,
    fragments_by_id: Mapping[str, FragmentRec],
    titles: Mapping[str, str],
    *,
    dirty: set[str],
    targets: Sequence[CanonicalNode],
    gateway: Gateway,
    ontology: Ontology,
) -> StepEstimate:
    rank = {fragment.fragment_id: index for index, fragment in enumerate(plan.fragments)}
    batches = attribute_batches(targets, fragments_by_id, rank, dirty)
    return StepEstimate(
        calls=len(batches),
        input_tokens=sum(
            request_tokens(attribute_request(batch, fragments_by_id, titles, ontology, gateway)) for batch in batches
        ),
    )


def _dedup(every: Sequence[Expected], gateway: Gateway, ontology: Ontology) -> StepEstimate:
    """Judged dedup pairs: ``DEDUP_PAIRS_PER_REQUIREMENT`` per new id-less requirement, each priced
    as a call over two placeholder statements of the most frequent such label."""

    new: dict[str, int] = {}
    for node in every:
        rule = ontology.identity_rule(node.label)
        if not node.persisted and rule.key == "id" and rule.fallback == "span":
            new[node.label] = new.get(node.label, 0) + 1
    calls = round(DEDUP_PAIRS_PER_REQUIREMENT * sum(new.values()))
    if not calls:
        return StepEstimate()
    label = max(sorted(new), key=lambda name: new[name])
    statement = (_placeholder("s"), _placeholder("q", PLACEHOLDER_TEXT_CHARS))
    return StepEstimate(
        calls=calls,
        input_tokens=calls * request_tokens(dedup_messages(ontology, gateway, label, [statement, statement])),
    )


async def _embeddings(
    plan: PlanArtifact,
    every: Sequence[Expected],
    hub_nodes: Sequence[Expected],
    hub_names: Sequence[str],
    *,
    extracting: bool,
    vectors: VectorCache,
) -> StepEstimate:
    """The embedding requests of the build, in the order its steps ask for vectors (each ask embeds
    the texts neither the store nor an earlier ask holds, in the gateway's batches)."""

    fragment_texts = [fragment.text.strip() for fragment in plan.fragments if fragment.text.strip()]
    names = [embedding_text(node.label, node.name) for node in every]
    search = [
        f"{embedding_text(node.label, node.name)}. {node.stmt or _placeholder('t', PLACEHOLDER_TEXT_CHARS)}"
        if node.stmt or not node.persisted
        else embedding_text(node.label, node.name)
        for node in every
    ]
    asks: list[Sequence[str]] = []
    if hub_names and extracting:
        asks += [fragment_texts, hub_names]  # known terms: fragment and hub-name vectors
    if len(hub_nodes) >= MIN_HUB_CONSOLIDATION:
        asks.append([embedding_text(node.label, node.name) for node in hub_nodes])  # hub consolidation
    asks += [names, fragment_texts, [*search, *names]]  # Resolve, edge catalogs, Conform
    stored = await vectors.stored({text for ask in asks for text in ask})
    known = set(stored)
    step = StepEstimate()
    for ask in asks:
        missing = list(dict.fromkeys(text for text in ask if text not in known))
        if not missing:
            continue
        known.update(missing)
        step = _embed(step, missing)
    return step


def _embed(step: StepEstimate, texts: Iterable[str]) -> StepEstimate:
    items, _ = embed_inputs(list(texts))
    batches = embed_batches(items)
    return _plus(step, sum(estimate_tokens(text) for text in items), calls=len(batches))
