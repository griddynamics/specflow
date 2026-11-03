"""Cost estimate of a build's model calls."""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Final

from graph.build import infer_bound, infer_xdoc
from graph.build.answered import Passage, Question, answered_payload
from graph.build.answers import HUBS_LIST, STATEMENTS_LIST, NodeList
from graph.build.changes import EdgeWindowParams, edge_windows
from graph.build.conform import MIN_DEDUP_GROUP, STATEMENT_MAX_CHARS, dedup_candidate_text, dedup_messages
from graph.build.consolidate import (
    BLOCK_OVERLAP,
    BLOCK_SIZE,
    HUBS_PASS,
    HUBS_PROMPT,
    LINE_MAX_CHARS,
    DecisionMemory,
    blocks,
)
from graph.build.diff import QUOTE_PROPERTY, STMT_PROPERTY, CurrentGraph, CurrentNode
from graph.build.edges import DEFAULT_EDGE_WINDOWS, unique_names
from graph.build.extract import (
    StoredResults,
    attribute_batches,
    attribute_request,
    attribute_scope,
    dirty_windows,
    node_passes,
    stored_attribute_nodes,
)
from graph.build.hubjudge import (
    EVIDENCE_LINES,
    HEADING_MAX_CHARS,
    PAIR_BATCH_ITEMS,
    PAIR_CALL_CAP,
    QUOTE_MAX_CHARS,
    REQUIREMENT_MAX_CHARS,
    REQUIREMENTS_SHOWN,
    JudgeItem,
    candidate_text,
    hub_block_messages,
    label_part,
)
from graph.build.infer import (
    ATTACH_ORPHAN,
    ENDPOINTS,
    MAX_FRAGMENTS,
    OTHER_FRAGMENTS,
    REALIZATION,
    STATEMENTS_SHOWN,
)
from graph.build.models import (
    BuildMode,
    CanonicalNode,
    EstimateStep,
    FragmentRec,
    HubName,
    InferenceReport,
    PlanArtifact,
    StepEstimate,
    Unit,
    Window,
)
from graph.build.normalise import (
    MAX_JUDGE_FRAGMENTS,
    MAX_JUDGE_STATEMENTS,
    JudgeGroup,
    NormNode,
    owner_conflict_payload,
)
from graph.build.options import BuildOptions, StepServices
from graph.build.ports import BuildStore
from graph.build.prompts import (
    KnownTerm,
    ReferencedEntity,
    edge_window_messages,
    edge_window_warm_up_messages,
    fragment_text,
    node_messages,
    node_warm_up_messages,
)
from graph.build.references import ReferenceFinder, requirement_labels
from graph.build.resolve import embedding_text, majority_label
from graph.build.terms import CATALOG_NEAREST, KnownTerms, window_key
from graph.build.vectors import VectorCache
from graph.core.identity import bounded_key_value, identity_key, normalise_surface
from graph.core.llm import Gateway
from graph.core.llm.gateway import embed_batches, embed_inputs, estimate_tokens, prompt_name, request_tokens
from graph.core.llm.judge import FRAGMENTS_KEY, judge_messages
from graph.core.ontology import InferenceRule, Ontology
from graph.core.ontology.model import CROSS_DOCUMENT

NODES_PER_KTOKEN: Final = 20
"""Expected canonical nodes per 1,000 estimated fragment tokens of a fragment without a persisted version."""
HUB_SHARE: Final = 0.1
"""Share of hubs among the placeholder nodes."""
DEDUP_CALLS_PER_REQUIREMENT: Final = 0.04
"""Judged dedup pairs (small-model calls: name and rich-only candidates that pass the guards and the direct-merge rule)
per new expected id-less requirement in a pool of ``DEDUP_REFERENCE_REQUIREMENTS`` (``_dedup``: scaled by the square
root of the pool's size).
"""
DEDUP_REFERENCE_REQUIREMENTS: Final = 1000
"""Pool size at which ``DEDUP_CALLS_PER_REQUIREMENT`` holds as it is."""
PAIR_CALLS_PER_HUB: Final = 0.15
"""Hub pair-pass and cross-label twin calls per unseen expected hub of a label part cut into more than one block (a part
of one block holds every candidate pair in its block: no pair call).
"""
PAIR_BATCH_PRICED_ITEMS: Final = min(8, PAIR_BATCH_ITEMS)
"""Items of a priced pair batch: the mean a real batch shows (at most ``PAIR_BATCH_ITEMS``)."""
CONFORM_JUDGE_GROUPS_PER_REQUIREMENT: Final = 0.01
"""The larger of the two, 4 / 400, is 0.01."""
CONFORM_JUDGE_FRAGMENTS: Final = min(MAX_JUDGE_FRAGMENTS, 3)
"""Fragments a priced conform judge call shows: the winner's, the subject's and the loser's evidence
(often fewer, when they share a fragment; never more than a group shows)."""
CONFORM_JUDGE_STATEMENT_CHARS: Final = STATEMENT_MAX_CHARS
"""Length of a placeholder statement in a priced conform judge call (a snapped span, cut as Conform
cuts statements)."""
ANSWERED_CALLS_PER_QUESTION: Final = 0.25
"""Answered-elsewhere calls (``answered_check``) per new expected question."""
MIN_CATALOG_NODES: Final = 2
"""Nodes an edge catalog needs to hold a pair."""
MIN_HUB_CONSOLIDATION: Final = 2
"""Hubs a consolidation block needs to hold a pair."""
PLACEHOLDER_NAME_CHARS: Final = 48
"""Length of a placeholder node name."""
PLACEHOLDER_HUB_CHARS: Final = 20
"""Length of a placeholder hub name (hub names are short: a module, feature or capability)."""
PLACEHOLDER_TEXT_CHARS: Final = 150
"""Length of a placeholder statement, quote or evidence line."""
PLACEHOLDER_HEADING_CHARS: Final = 40
"""Length of a placeholder heading path of a hub judge item."""
PLACEHOLDER_SECTION_CHARS: Final = 100
"""Length of a placeholder section of an ``attach_orphan`` node (document title, chapter and section)."""
PLACEHOLDER_QUOTE_CHARS: Final = 100
"""Length of a placeholder hub quote (a hub's quote is one short sentence; shop candidate texts of hubs
average about 160 characters with the name and heading)."""
CATALOG_ALIAS_EVERY: Final = 3
"""One catalog hub in this many has another name shown with it."""

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
    mentioned: Mapping[str, Sequence[Expected]] = field(default_factory=dict)
    """The nodes mentioned (``MENTIONED_IN``) in each plan fragment's persisted version and not grounded
    in it, by plan fragment id (the update scope of a ``cross_document`` rule)."""


@dataclass(frozen=True)
class Lookups:
    """The read-only stores the estimate consults; ``results`` only in ``update`` builds (the only
    mode that reuses stored extraction results)."""

    vectors: VectorCache
    decisions: DecisionMemory
    results: StoredResults | None = None
    attribute_fingerprint: str = ""


def persisted_nodes(
    fragments: Sequence[FragmentRec], current: CurrentGraph, hub_labels: Collection[str] = ()
) -> PersistedNodes:
    """The persisted nodes of ``current`` on the plan's ``fragments``."""

    grounded: dict[str, list[Expected]] = {fragment.fragment_id: [] for fragment in current.fragments}
    mentions: dict[str, list[Expected]] = {}
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
        own = dict.fromkeys(evidence.fragment_id for evidence in node.evidence)
        for fid in own:
            grounded.setdefault(fid, []).append(expected)
        for fid in node.mentions:
            if fid not in own:
                mentions.setdefault(fid, []).append(expected)
    by_content = {
        (fragment.document_id, fragment.content_hash): fragment.fragment_id
        for fragment in current.fragments
        if fragment.content_hash
    }
    by_fragment: dict[str, list[Expected]] = {}
    mentioned: dict[str, list[Expected]] = {}
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
        if mentions.get(found):
            mentioned[fragment.fragment_id] = mentions[found]
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
        mentioned=mentioned,
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
    hubs: Sequence[HubName],
    persisted: PersistedNodes,
    lookups: Lookups,
    edge_params: EdgeWindowParams = DEFAULT_EDGE_WINDOWS,
    inference: InferenceState | None = None,
) -> Estimated:
    """Calls and input tokens per step of a ``mode`` build of ``plan`` and the subjects its inference limits would leave
    queued (module docstring).
    """

    gateway, ontology, options = services.gateway, services.ontology, services.options
    fragments_by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    titles = {document.document_id: document.title for document in plan.documents}
    dirty = set(plan.dirty_ids)
    expected = _Expected(ontology, persisted.by_fragment, new_hub=mode != "first")
    every = list(dict.fromkeys(node for fragment in plan.fragments for node in expected.of(fragment)))
    hub_nodes = [node for node in every if node.label in ontology.hub_labels]
    if mode == "first":
        names = [node.name for node in hub_nodes] if options.hub_catalog else []
    else:
        names = [hub.name for hub in sorted(hubs, key=lambda hub: (hub.name, hub.label))]
        if options.hub_catalog:  # new hubs of the hub pass catalog are embedded for the statement pass
            names += [node.name for node in hub_nodes if not node.persisted]
    dirty_units = [unit for unit in plan.units if any(fid in dirty for fid in unit.fragment_ids)]
    terms = await _node_terms(
        plan, dirty_units, mode=mode, hubs=hubs, expected=expected, services=services, lookups=lookups
    )
    attributes_stored: set[Expected] = set()
    if lookups.results is not None:
        attributes_stored = await stored_attribute_nodes(
            lookups.results, lookups.attribute_fingerprint, persisted.attribute_inputs
        )
    asked = _attribute_targets(plan, dirty, expected, attributes_stored, ontology.hub_labels)
    settled = _settled(plan, expected)
    nodes, edges = _extraction(
        dirty_units,
        _ExtractionInputs(
            fragments_by_id=fragments_by_id,
            titles=titles,
            dirty=dirty,
            expected=expected,
            terms=terms,
            hubs=hubs if mode != "first" else [],
            first_hubs=len(hub_nodes) if mode == "first" else 0,
            gateway=gateway,
            ontology=ontology,
            options=options,
            every_window=mode != "update",
            edge_params=edge_params,
            referenced=_referenced(plan, dirty_units, expected, ontology, edge_params),
        ),
    )
    dirty_fragments = [fragment for fragment in plan.fragments if fragment.fragment_id in dirty]
    changed = list(dict.fromkeys(node for fragment in dirty_fragments for node in expected.of(fragment)))
    mentioned = (node for fragment in dirty_fragments for node in persisted.mentioned.get(fragment.fragment_id, ()))
    judged = _inference(
        _InferenceInputs(
            mode=mode,
            every=every,
            changed=changed,
            scope=list(dict.fromkeys([*changed, *mentioned])),
            length=_mean_length(plan.fragments),
            state=inference or InferenceState(),
        ),
        ontology,
    )
    steps: StepCosts = {
        "hub_catalog": StepEstimate(),
        "node_extraction": nodes,
        "hub_consolidation": await _consolidation(hub_nodes, _Judging(lookups.decisions, settled, gateway, ontology)),
        "edge_extraction": edges,
        "attributes": _attributes(
            plan, fragments_by_id, titles, dirty=dirty, targets=asked, gateway=gateway, ontology=ontology
        ),
        "requirement_dedup": _dedup(every, gateway, ontology),
        "conform_judge": _conform_judge(every, dirty_fragments, ontology),
        "answered_check": _answered_check(every, dirty_fragments, ontology),
        "inference": judged.inference,
        "cross_document_inference": judged.cross_document,
        "embeddings": await _embeddings(
            plan,
            _EmbeddingInputs(
                every=every,
                hub_nodes=hub_nodes,
                hub_names=names,
                settled=settled,
                extracting=bool(dirty_units),
                vectors=lookups.vectors,
            ),
            ontology,
        ),
    }
    return Estimated(steps, judged.backlog)


def _settled(plan: PlanArtifact, expected: _Expected) -> set[Expected]:
    """The persisted nodes grounded only in unchanged fragments: their candidate texts are stored."""

    changed = {node for fragment in plan.fragments if fragment.change != "unchanged" for node in expected.of(fragment)}
    return {
        node for fragment in plan.fragments for node in expected.of(fragment) if node.persisted and node not in changed
    }


def _plus(step: StepEstimate, tokens: int, calls: int = 1) -> StepEstimate:
    return StepEstimate(calls=step.calls + calls, input_tokens=step.input_tokens + tokens)


async def _node_terms(
    plan: PlanArtifact,
    units: Sequence[Unit],
    *,
    mode: BuildMode,
    hubs: Sequence[HubName],
    expected: _Expected,
    services: StepServices,
    lookups: Lookups,
) -> dict[NodeList, dict[str, list[KnownTerm]]]:
    """The known terms of each extracted window (``terms.window_key``) per node pass (module docstring): the persisted
    hubs selected as the build selects them, with the vectors the store holds (a fragment or name without one is found
    by mention only), or a ``first`` build's expected hubs of the window's fragments.
    """

    options = services.options
    fragments_by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    windows = dirty_windows(units, set(plan.dirty_ids))
    persisted: dict[str, list[KnownTerm]] = {}
    if mode != "first" and hubs and windows:
        texts = {fragment.fragment_id: fragment.text.strip() for fragment in plan.fragments if fragment.text.strip()}
        names = [hub.name for hub in hubs]
        stored = await lookups.vectors.stored_vectors([*names, *texts.values()])
        ranking = KnownTerms(hubs, {name: stored[name] for name in names if name in stored})
        vectors = {fid: stored[text] for fid, text in texts.items() if text in stored}
        persisted = {
            window_key(unit.unit_id, window): ranking.for_window(
                unit, window, fragments_by_id, vectors, options.known_terms_limit
            )
            for unit, window in windows
        }
    hub_terms = persisted if mode == "update" else {}
    if not options.hub_catalog:
        statement_terms = hub_terms
    elif mode == "first":
        statement_terms = _first_terms(plan, windows, expected, services)
    else:
        statement_terms = persisted
    return {HUBS_LIST: hub_terms, STATEMENTS_LIST: statement_terms}


def _first_terms(
    plan: PlanArtifact, windows: Sequence[tuple[Unit, Window]], expected: _Expected, services: StepServices
) -> dict[str, list[KnownTerm]]:
    """A ``first`` build's statement-pass terms per window: the expected hubs of its fragments as
    mentioned terms, then up to ``known_terms_limit`` other expected hubs as nearby stand-ins; every
    ``CATALOG_ALIAS_EVERY``-th term shows one placeholder alias."""

    fragments_by_id = {fragment.fragment_id: fragment for fragment in plan.fragments}
    hub_labels = services.ontology.hub_labels

    def hubs_of(fragment_ids: Iterable[str]) -> list[Expected]:
        return list(
            dict.fromkeys(
                node for fid in fragment_ids for node in expected.of(fragments_by_id[fid]) if node.label in hub_labels
            )
        )

    every = hubs_of(fragment.fragment_id for fragment in plan.fragments)
    alias = _placeholder("alias", PLACEHOLDER_HUB_CHARS)
    found: dict[str, list[KnownTerm]] = {}
    for unit, (start, end) in windows:
        own = hubs_of(unit.fragment_ids[start:end])
        mine = set(own)
        near = [node for node in every if node not in mine][: services.options.known_terms_limit]
        terms = [KnownTerm(node.name, node.label, mentioned=True) for node in own]
        terms += [KnownTerm(node.name, node.label) for node in near]
        found[window_key(unit.unit_id, (start, end))] = [
            KnownTerm(
                term.name, term.label, (alias,) if index % CATALOG_ALIAS_EVERY == 0 else (), mentioned=term.mentioned
            )
            for index, term in enumerate(terms, start=1)
        ]
    return found


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
    terms: Mapping[NodeList, Mapping[str, Sequence[KnownTerm]]]
    hubs: Sequence[HubName]
    first_hubs: int
    gateway: Gateway
    ontology: Ontology
    options: BuildOptions
    every_window: bool = True
    """Every edge window of a unit is called (``first``, ``rebuild``), else those with a dirty fragment."""
    edge_params: EdgeWindowParams = DEFAULT_EDGE_WINDOWS
    referenced: Mapping[str, Sequence[tuple[str, str, str]]] = field(default_factory=dict)
    """The referenced catalog part of each unit, by unit id (``_referenced``)."""


def _extraction(units: Sequence[Unit], inputs: _ExtractionInputs) -> tuple[StepEstimate, StepEstimate]:
    """Node extraction (both passes, with the warm-ups) and edge extraction over the units with a dirty
    fragment: the messages ``prompts.node_messages``, ``prompts.node_warm_up_messages`` and, per unit,
    ``_edge_windows`` render for the build's calls."""

    ontology, fragments_by_id = inputs.ontology, inputs.fragments_by_id
    hub_label = min(ontology.hub_labels, default="")
    passes = node_passes(ontology)
    nodes = edges = StepEstimate()
    for unit in units:
        texts = [
            fragment_text(
                inputs.titles.get(unit.document_id, unit.document_id),
                fragments_by_id[fid].heading_path,
                fragments_by_id[fid].text,
            )
            for fid in unit.fragment_ids
        ]
        windows = [window for _, window in dirty_windows([unit], inputs.dirty)]
        if len(windows) > 1:  # one warm-up per pass (extract._UnitRun.run_all)
            nodes = _plus(nodes, len(passes) * request_tokens(node_warm_up_messages(ontology, texts)), len(passes))
        for window in windows:
            for node_list in passes:
                terms = inputs.terms.get(node_list, {}).get(window_key(unit.unit_id, window), ())
                nodes = _plus(nodes, request_tokens(node_messages(ontology, texts, terms, window, node_list=node_list)))
        cost = _edge_windows(unit, texts, inputs, hub_label)
        edges = _plus(edges, cost.input_tokens, cost.calls)
    return nodes, edges


def edge_requests(
    ontology: Ontology,
    texts: Sequence[str],
    catalog: Sequence[tuple[str, str]],
    windows: Sequence[Window],
    *,
    referenced: Mapping[str, ReferencedEntity] | None = None,
) -> list[list[dict[str, Any]]]:
    """The requests a unit's edge windows send (``edges._EdgeUnitRun.run_all``): one warm-up
    (``prompts.edge_window_warm_up_messages``) when two or more windows are called, then one
    ``prompts.edge_window_messages`` per window, each with the unit's whole ``catalog`` (``referenced``: the referenced
    part's catalog-line extra by name).
    """

    warm_up = (
        [edge_window_warm_up_messages(ontology, texts, catalog, referenced=referenced)] if len(windows) > 1 else []
    )
    return [
        *warm_up,
        *(edge_window_messages(ontology, texts, catalog, window, referenced=referenced) for window in windows),
    ]


def _edge_windows(unit: Unit, texts: Sequence[str], inputs: _ExtractionInputs, hub_label: str) -> StepEstimate:
    """The edge-window calls of one unit with a dirty fragment (module docstring): its windows as the build cuts them,
    every one or those holding a dirty fragment (``every_window``), over its expected catalog (local, foreign,
    referenced) under unique names (``edges.unique_names``).
    """

    local, foreign = _catalog(unit, inputs, hub_label)
    named = {name for name, _ in (*local, *foreign)}
    found = [entry for entry in inputs.referenced.get(unit.unit_id, ()) if entry[0] not in named]
    referenced = [(name, label) for name, label, _ in found]
    if not local or len(local) + len(foreign) + len(referenced) < MIN_CATALOG_NODES:
        return StepEstimate()
    windows = [
        window
        for window in edge_windows(unit, inputs.fragments_by_id, inputs.edge_params)
        if inputs.every_window or any(fid in inputs.dirty for fid in unit.fragment_ids[window[0] : window[1]])
    ]
    entries = [*local, *foreign, *referenced]
    names = unique_names([(f"{index:06d}", name, label) for index, (name, label) in enumerate(entries)])
    catalog = [(names[f"{index:06d}"], label) for index, (_, label) in enumerate(entries)]
    first = len(local) + len(foreign)
    # the expected name stands in for the statement (the build shows the node's first quote)
    extra = {
        names[f"{first + index:06d}"]: ReferencedEntity(statement=name, referenced_in=sentence)
        for index, (name, _, sentence) in enumerate(found)
    }
    requests = edge_requests(inputs.ontology, texts, catalog, windows, referenced=extra)
    return StepEstimate(calls=len(requests), input_tokens=sum(request_tokens(request) for request in requests))


def _referenced(
    plan: PlanArtifact, units: Sequence[Unit], expected: _Expected, ontology: Ontology, params: EdgeWindowParams
) -> dict[str, list[tuple[str, str, str]]]:
    """The referenced catalog part of each of ``units`` as ``(name, label, referring sentence)``: the build's
    ``references.ReferenceFinder`` over the plan with the expected nodes as its nodes (each grounded in the fragments
    that expect it; no explicit ids), mapped back to their names; empty with ``references`` off.
    """

    if not params.references or not units:
        return {}
    grounded: dict[Expected, list[str]] = {}
    for fragment in plan.fragments:
        for node in expected.of(fragment):
            grounded.setdefault(node, []).append(fragment.fragment_id)
    nodes = list(grounded)
    finder = ReferenceFinder(
        units=plan.units,
        fragments_by_id={fragment.fragment_id: fragment for fragment in plan.fragments},
        titles={document.document_id: document.title for document in plan.documents},
        canonical=[
            CanonicalNode(
                cid=f"{index:06d}",
                identity_key=f"{index:06d}",
                name=node.name,
                label=node.label,
                member_ids=(),
                fragment_ids=tuple(grounded[node]),
            )
            for index, node in enumerate(nodes)
        ],
        labels=requirement_labels(ontology),
    )
    found: dict[str, list[tuple[str, str, str]]] = {}
    for unit in units:
        references = finder.for_unit(unit)
        if references.cids:
            found[unit.unit_id] = [
                (nodes[int(cid)].name, nodes[int(cid)].label, references.sentences.get(cid, ""))
                for cid in references.cids
            ]
    return found


def _catalog(
    unit: Unit, inputs: _ExtractionInputs, hub_label: str
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The expected edge catalog of ``unit`` as ``(name, label)``: its expected nodes (one per name) and the hubs from
    elsewhere (at most ``catalog_limit``, and ``CATALOG_NEAREST`` per fragment): the persisted hubs not among its nodes,
    or, in a ``first`` build, ``first_hubs`` placeholders.
    """

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


def hub_judge_item(node: Expected) -> JudgeItem:
    """An expected hub as the hub judge would show it (module docstring): its persisted quote and
    evidence lines where it has them, else placeholders; a placeholder heading and requirements."""

    return JudgeItem(
        label=node.label,
        name=prompt_name(node.name),
        quote=prompt_name(node.quote or _placeholder("q", PLACEHOLDER_QUOTE_CHARS), QUOTE_MAX_CHARS),
        heading=prompt_name(_placeholder("heading", PLACEHOLDER_HEADING_CHARS), HEADING_MAX_CHARS),
        requirements=tuple(
            prompt_name(_placeholder(f"r{index}"), REQUIREMENT_MAX_CHARS) for index in range(REQUIREMENTS_SHOWN)
        ),
        evidence=tuple(prompt_name(line, LINE_MAX_CHARS) for line in node.evidence[:EVIDENCE_LINES])
        or (_placeholder("e", PLACEHOLDER_TEXT_CHARS),) * EVIDENCE_LINES,
    )


def pair_batch_items(label: str) -> list[JudgeItem]:
    """The items a priced pair-pass call shows: ``PAIR_BATCH_PRICED_ITEMS`` placeholder hubs of ``label``."""

    return [
        hub_judge_item(Expected(label, _placeholder(f"pair{index}", PLACEHOLDER_HUB_CHARS)))
        for index in range(PAIR_BATCH_PRICED_ITEMS)
    ]


@dataclass(frozen=True)
class _Judging:
    """What the hub consolidation estimate reads: the decision memory, the settled persisted nodes
    (``_settled``: their stored node entries still match their evidence), the gateway and ontology."""

    memory: DecisionMemory
    settled: Collection[Expected]
    gateway: Gateway
    ontology: Ontology


async def _consolidation(hub_nodes: Sequence[Expected], judging: _Judging) -> StepEstimate:
    """Hub consolidation blocks over the expected hubs of each label part (sorted by key, as the build sorts them before
    its chain): only blocks with a hub the decision memory has not seen are sent; then the pair pass (module docstring).
    """

    memory, gateway, ontology = judging.memory, judging.gateway, judging.ontology
    apart = frozenset(label for label in ontology.hub_labels if not ontology.merge_across_labels(label))
    parts: dict[str, list[Expected]] = {}
    for node in sorted(hub_nodes, key=lambda node: (node.key or "~", node.name)):
        parts.setdefault(label_part({node.label}, apart), []).append(node)
    known = await memory.known(HUBS_PASS, HUBS_PROMPT, (node.key for node in hub_nodes if node.key))
    seen = {node.key for node in hub_nodes if node.key in known and node in judging.settled}
    step = StepEstimate()
    unseen_hubs: list[Expected] = []
    for part in sorted(parts):
        items = parts[part]
        block_list = blocks(list(range(len(items))), BLOCK_SIZE, BLOCK_OVERLAP)
        unseen = [node for node in items if node.key not in seen]
        if not block_list:
            continue
        if len(block_list) > 1:  # in a part of one block every candidate pair shares it: no pair call
            unseen_hubs += unseen
        for block in block_list[: min(len(block_list), len(unseen))]:
            shown = [hub_judge_item(items[index]) for index in block]
            step = _plus(step, request_tokens(hub_block_messages(ontology, gateway, shown)))
    pair_calls = min(PAIR_CALL_CAP, round(PAIR_CALLS_PER_HUB * len(unseen_hubs)))
    if pair_calls:
        label = majority_label([node.label for node in unseen_hubs])
        tokens = request_tokens(hub_block_messages(ontology, gateway, pair_batch_items(label)))
        step = _plus(step, pair_calls * tokens, calls=pair_calls)
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
    """Judged dedup pairs: ``DEDUP_CALLS_PER_REQUIREMENT`` per new id-less requirement times the square
    root of the id-less requirements in all over ``DEDUP_REFERENCE_REQUIREMENTS``, each priced as a call
    over two placeholder statements of the most frequent new such label."""

    new: dict[str, int] = {}
    pool = 0
    for node in every:
        rule = ontology.identity_rule(node.label)
        if rule.key != "id" or rule.fallback != "span":
            continue
        if not node.persisted:
            new[node.label] = new.get(node.label, 0) + 1
            pool += 1
        elif node.key.split("|", 2)[1:2] == ["span"]:
            pool += 1
    scale = math.sqrt(pool / DEDUP_REFERENCE_REQUIREMENTS)
    calls = round(DEDUP_CALLS_PER_REQUIREMENT * sum(new.values()) * scale)
    if not calls:
        return StepEstimate()
    label = max(sorted(new), key=lambda name: new[name])
    statement = (_placeholder("s"), _placeholder("q", PLACEHOLDER_TEXT_CHARS))
    return StepEstimate(
        calls=calls,
        input_tokens=calls * request_tokens(dedup_messages(ontology, gateway, label, [statement, statement])),
    )


def _conform_judge(every: Sequence[Expected], dirty: Sequence[FragmentRec], ontology: Ontology) -> StepEstimate:
    """Owner-conflict judge calls (module docstring), each priced by the judge's own messages."""

    spec = ontology.conform_spec()
    sources = ontology.spine.sources
    new: dict[str, int] = {}
    for node in every:
        if not node.persisted and node.label in sources:
            new[node.label] = new.get(node.label, 0) + 1
    calls = min(spec.cap, round(CONFORM_JUDGE_GROUPS_PER_REQUIREMENT * sum(new.values()))) if spec else 0
    if not spec or not calls:
        return StepEstimate()
    label = max(sorted(new), key=lambda name: new[name])
    return StepEstimate(
        calls=calls, input_tokens=calls * request_tokens(conform_judge_messages(ontology, label, dirty))
    )


def conform_judge_messages(ontology: Ontology, label: str, fragments: Sequence[FragmentRec]) -> list[dict[str, str]]:
    """The messages of a representative conform judge call (``conform_judge_group``), as the judge
    runner sends them; none without a ``conform`` section."""

    spec = ontology.conform_spec()
    group = conform_judge_group(ontology, label, fragments)
    if spec is None or group is None:
        return []
    return judge_messages(ontology, spec.judge_prompt, {**group.payload, FRAGMENTS_KEY: dict(group.fragments)})


def conform_judge_group(ontology: Ontology, label: str, fragments: Sequence[FragmentRec]) -> JudgeGroup | None:
    """A representative conform judge group for a ``label`` subject: placeholder names and statements (module
    docstring), its first and last owner pair targets as owner and loser, and ``CONFORM_JUDGE_FRAGMENTS`` placeholder
    fragments of the mean length of ``fragments``; None without a ``conform`` section.
    """

    spec = ontology.conform_spec()
    if spec is None:
        return None
    pairs = ontology.owner_pairs(label)
    owner_label = pairs[0].target if pairs else label
    loser_label = pairs[-1].target if pairs else label
    statements = tuple(
        _placeholder(f"s{index}", CONFORM_JUDGE_STATEMENT_CHARS) for index in range(MAX_JUDGE_STATEMENTS)
    )
    length = _mean_length(fragments)
    payload = owner_conflict_payload(
        spec.fallback_relation,
        NormNode("subject", label, _placeholder("subject"), statements=statements[:1]),
        NormNode("owner", owner_label, _placeholder("owner", PLACEHOLDER_HUB_CHARS), statements=statements),
        [NormNode("loser", loser_label, _placeholder("loser", PLACEHOLDER_HUB_CHARS), statements=statements)],
    )
    shown = {
        _placeholder(f"fragment{index}", PLACEHOLDER_HUB_CHARS): _placeholder(f"f{index}", length)
        for index in range(CONFORM_JUDGE_FRAGMENTS)
    }
    return JudgeGroup(payload, shown)


def _answered_check(every: Sequence[Expected], dirty: Sequence[FragmentRec], ontology: Ontology) -> StepEstimate:
    """Answered-elsewhere calls (module docstring), each priced by the check's own messages."""

    spec = ontology.conform_spec()
    answered = spec.answered_elsewhere if spec is not None else None
    if answered is None:
        return StepEstimate()
    new = sum(1 for node in every if not node.persisted and node.label == answered.label)
    calls = min(answered.cap, round(ANSWERED_CALLS_PER_QUESTION * new))
    if not calls:
        return StepEstimate()
    return StepEstimate(calls=calls, input_tokens=calls * request_tokens(answered_check_messages(ontology, dirty)))


def answered_check_request(
    ontology: Ontology, fragments: Sequence[FragmentRec]
) -> tuple[Question, list[Passage]] | None:
    """A representative answered-elsewhere question and its candidates: a placeholder name and quote,
    and the flag's ``candidates`` placeholder fragments of the mean length of ``fragments``; None
    without the ``conform.answered_elsewhere`` flag."""

    spec = ontology.conform_spec()
    answered = spec.answered_elsewhere if spec is not None else None
    if answered is None:
        return None
    question = Question(
        key="question",
        name=_placeholder("question"),
        quote=_placeholder("quote", PLACEHOLDER_TEXT_CHARS),
        text="",
        fragments=frozenset(),
    )
    length = _mean_length(fragments)
    passages = [
        Passage(
            FragmentRec(
                fragment_id=_placeholder(f"fragment{index}", PLACEHOLDER_HUB_CHARS),
                document_id="",
                heading_path=(),
                ordinal=index,
                line_start=1,
                line_end=1,
                text=_placeholder(f"f{index}", length),
                content_hash="",
                tokens=0,
            ),
        )
        for index in range(answered.candidates)
    ]
    return question, passages


def answered_check_messages(ontology: Ontology, fragments: Sequence[FragmentRec]) -> list[dict[str, str]]:
    """The messages of a representative answered-elsewhere call (``answered_check_request``), as the
    check sends them (``judge_messages`` over ``answered.answered_payload``); none without the flag."""

    spec = ontology.conform_spec()
    request = answered_check_request(ontology, fragments)
    if spec is None or spec.answered_elsewhere is None or request is None:
        return []
    return judge_messages(ontology, spec.answered_elsewhere.prompt, answered_payload(*request))


# Inference


@dataclass(frozen=True)
class InferenceState:
    """What the inference estimate reads besides the plan: whether the build infers (``BuildSettings.inference``) and,
    from the project's latest finished build's inference report, the subjects queued per rule id
    (``InferenceReport.backlog``, exact) and those deferred outside the queue (``unqueued``: never-judged subjects an
    ``update`` left to a later build, and subjects judged before whose inputs changed outside an update scope) and those
    judged before (``judged``: its subjects less its deferred ones, so they hold a stored judgement).
    """

    enabled: bool = True
    queued: Mapping[str, int] = field(default_factory=dict)
    unqueued: Mapping[str, int] = field(default_factory=dict)
    judged: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def of(cls, report: InferenceReport | None, *, enabled: bool = True) -> InferenceState:
        """The state after ``report`` (None: no previous build, or one that did not infer). A rule's
        queue holds its failed subjects and those a limit left (deferred); the deferred subjects past
        those are ``unqueued``."""

        if report is None:
            return cls(enabled)
        queued = report.backlog_counts()
        unqueued = {
            rule.rule: rule.deferred - (queued.get(rule.rule, 0) - rule.failed)
            for rule in report.rules
            if rule.deferred > queued.get(rule.rule, 0) - rule.failed
        }
        judged = {rule.rule: max(0, rule.subjects - rule.deferred) for rule in report.rules}
        return cls(enabled, queued, unqueued, judged)


async def inference_state(builds: BuildStore, project_id: str, *, enabled: bool = True) -> InferenceState:
    """The ``InferenceState`` of ``project_id``: the infer artifact of its latest finished build, read
    as the infer step reads its queues (``infer.previous_backlog``)."""

    latest = await builds.latest_build(project_id, finished_only=True)
    payload = None if latest is None else await builds.get_artifact(project_id, latest.build_id, "infer")
    return InferenceState.of(None if payload is None else InferenceReport.model_validate(payload), enabled=enabled)


@dataclass(frozen=True)
class SubjectRate:
    """Fresh subjects of a rule per expected node of its subject labels in a ``first`` or ``rebuild``
    build; ``scaled``: times the square root of those nodes over ``SUBJECT_REFERENCE_NODES`` (the
    share grows with the pool)."""

    rate: float
    scaled: bool = False


@dataclass(frozen=True)
class TemplateRates:
    """A template's ``SubjectRate`` for the subject labels that are not hubs (``nodes``) and the hub ones."""

    nodes: SubjectRate
    hubs: SubjectRate


SUBJECT_REFERENCE_NODES: Final = 1000
"""Pool of expected subject nodes at which a scaled ``SubjectRate`` holds as it is."""
SUBJECT_RATES: Final[Mapping[str, TemplateRates]] = {
    ATTACH_ORPHAN: TemplateRates(nodes=SubjectRate(0.36, scaled=True), hubs=SubjectRate(2.0)),
    ENDPOINTS: TemplateRates(nodes=SubjectRate(0.04), hubs=SubjectRate(0.04)),
    REALIZATION: TemplateRates(nodes=SubjectRate(0.05), hubs=SubjectRate(0.05)),
    CROSS_DOCUMENT: TemplateRates(nodes=SubjectRate(1.65, scaled=True), hubs=SubjectRate(1.65, scaled=True)),
}
"""- ``attach_orphan`` on hubs (features): 105 and 25 subjects for 65.2 and 10.2 expected features (1.6 and 2.5); 2.0
puts them at 130 and 20.
"""
CHANGED_SUBJECTS_PER_NODE: Final = 1.5
"""Fresh subjects of a rule other than ``cross_document`` per expected subject node of a dirty fragment in an ``update``
whose previous report gives no count of subjects judged before (else ``CHANGED_INPUT_SHARE``): the changed subject
itself and the subjects elsewhere whose payload shows a changed node or fragment (a candidate grounded in it).
"""
CHANGED_INPUT_SHARE: Final = TemplateRates(nodes=SubjectRate(2.1), hubs=SubjectRate(7.5))
"""Share of a rule's subjects judged before (``InferenceState.judged``) that an ``update`` judges again, per share of
the rule's target nodes in its scope (grounded in or mentioned in a dirty fragment), by the kind of its subject labels
(``nodes``: not hubs; ``hubs``); at most all of them.
"""
CROSS_DOCUMENT_SCOPE_PER_HUB: Final = 0.7
"""Fresh ``cross_document`` subjects in an ``update`` (its ``UpdateScope``) per hub of its subject labels grounded in or
mentioned in a dirty fragment.
"""
CROSS_DOCUMENT_PAIRS_PER_SUBJECT: Final = 12.5
"""Member pairs a priced ``cross_document`` call shows per square root of its expected subject hubs over
``SUBJECT_REFERENCE_NODES`` (rounded, at least one, at most the rule's ``max_pairs``), one price for every hub.
"""
PRICED_SUBJECT_FRAGMENTS: Final = 1
"""Grounding fragments of the subject a priced call shows (the build shows up to ``SUBJECT_FRAGMENTS``): a requirement
is grounded in one, and a hub's further fragments mostly are those of its candidates.
"""
PAIR_TARGETS: Final = 2
"""Targets an ``endpoints`` subject needs for one candidate pair."""
BATCH_FILL: Final = 0.5
"""Subjects of a batched ``attach_orphan`` call over the rule's ``batch`` (a call holds the orphans of one section of
one document, at most ``batch``).
"""
BATCH_NEW_CANDIDATES: Final = 2
"""Candidates each further subject of a priced batch adds to those shown (the rest it shares with the subject before
it).
"""
REALIZATION_PRICED_NODES: Final = 1
"""Members and candidates each a priced ``realization`` call shows."""


@dataclass(frozen=True)
class Estimated:
    """``estimate_steps``: calls and input tokens per step, and the subjects per rule id the inference
    limits would leave queued (rules without any left out)."""

    steps: StepCosts
    inference_backlog: dict[str, int]


@dataclass(frozen=True)
class _InferenceInputs:
    """What the inference estimate reads: the expected nodes of the plan (``every``), of its dirty
    fragments (``changed``) and those plus the persisted nodes mentioned in them (``scope``), the
    priced fragment length and the previous build's queues."""

    mode: BuildMode
    every: Sequence[Expected]
    changed: Sequence[Expected]
    scope: Sequence[Expected]
    length: int
    state: InferenceState


@dataclass(frozen=True)
class _Judged:
    inference: StepEstimate = field(default_factory=StepEstimate)
    cross_document: StepEstimate = field(default_factory=StepEstimate)
    backlog: dict[str, int] = field(default_factory=dict)


def _inference(inputs: _InferenceInputs, ontology: Ontology) -> _Judged:
    """Both inference steps and the predicted queues (module docstring): each rule's fresh subjects in
    ontology order, bounded by ``infer_bound`` with the build's input counted across rules."""

    if not inputs.state.enabled:
        return _Judged()
    spec, first = ontology.inference, inputs.mode != "update"
    steps = {False: StepEstimate(), True: StepEstimate()}
    backlog: dict[str, int] = {}
    spent = 0
    for rule in spec.rules:
        fresh = _fresh_subjects(rule, inputs, ontology)
        if not fresh:
            continue
        size, pairs = batch_size(rule), _priced_pairs(rule, ontology, inputs.every)
        full = _price(ontology, rule, inputs.length, pairs=pairs, subjects=size)
        last = _price(ontology, rule, inputs.length, pairs=pairs, subjects=fresh % size) if fresh % size else 0
        # each call charged at its last subject, as the infer step admits a call (``infer_bound.stop``)
        charges = [full if (index + 1) % size == 0 else last if index + 1 == fresh else 0 for index in range(fresh)]
        bounded = infer_bound.bound(charges, infer_bound.limits_of(spec, rule, first=first), spent=spent)
        calls, rest = divmod(bounded.taken, size)
        tokens = calls * full + (_price(ontology, rule, inputs.length, pairs=pairs, subjects=rest) if rest else 0)
        cost = StepEstimate(calls=calls + (1 if rest else 0), input_tokens=tokens)
        spent += tokens
        cross = rule.template == CROSS_DOCUMENT
        steps[cross] = _plus(steps[cross], cost.input_tokens, cost.calls)
        if fresh > bounded.taken:
            backlog[rule.id] = fresh - bounded.taken
    return _Judged(steps[False], steps[True], backlog)


def _fresh_subjects(rule: InferenceRule, inputs: _InferenceInputs, ontology: Ontology) -> int:
    """The rule's fresh subjects (module docstring)."""

    state = inputs.state
    if inputs.mode != "update":
        return round(_subjects(rule, inputs.every, ontology))
    queued = state.queued.get(rule.id, 0)
    if not inputs.changed:  # no dirty fragment: the diff changes nothing, the queue and never-judged ones
        return queued + state.unqueued.get(rule.id, 0)
    if rule.template == CROSS_DOCUMENT:
        return queued + round(CROSS_DOCUMENT_SCOPE_PER_HUB * sum(_subject_nodes(rule, inputs.scope, ontology)))
    judged = state.judged.get(rule.id)
    if judged is None:
        return queued + round(CHANGED_SUBJECTS_PER_NODE * sum(_subject_nodes(rule, inputs.changed, ontology)))
    new = [node for node in inputs.changed if not node.persisted]
    return (
        queued
        + round(_subjects(rule, new, ontology, pool=inputs.every))
        + _changed_inputs(rule, judged, inputs, ontology)
    )


def _changed_inputs(rule: InferenceRule, judged: int, inputs: _InferenceInputs, ontology: Ontology) -> int:
    """The subjects judged before whose inputs an ``update`` changes (``CHANGED_INPUT_SHARE``)."""

    targets = sum(1 for node in inputs.every if node.label in rule.targets)
    if not targets or not judged:
        return 0
    in_scope = sum(1 for node in dict.fromkeys(inputs.scope) if node.label in rule.targets)
    hubs = bool(rule.subject) and rule.subject <= ontology.hub_labels
    rate = (CHANGED_INPUT_SHARE.hubs if hubs else CHANGED_INPUT_SHARE.nodes).rate
    return round(judged * min(1.0, rate * in_scope / targets))


def _subjects(
    rule: InferenceRule, nodes: Sequence[Expected], ontology: Ontology, *, pool: Sequence[Expected] | None = None
) -> float:
    """Expected subjects of ``rule`` among ``nodes`` (``SUBJECT_RATES``): in a ``first`` or ``rebuild``
    build ``nodes`` are the whole pool; in an ``update`` the new nodes, a scaled rate scaled by ``pool``."""

    rates = SUBJECT_RATES[rule.template]
    pools = _subject_nodes(rule, nodes if pool is None else pool, ontology)
    total = 0.0
    for count, size, rate in zip(_subject_nodes(rule, nodes, ontology), pools, (rates.nodes, rates.hubs), strict=True):
        scale = math.sqrt(size / SUBJECT_REFERENCE_NODES) if rate.scaled else 1.0
        total += rate.rate * count * scale
    return total


def _subject_nodes(rule: InferenceRule, nodes: Iterable[Expected], ontology: Ontology) -> tuple[float, float]:
    """Expected nodes of the rule's subject labels among ``nodes``: those not hubs, then the hubs."""

    hub_labels = ontology.hub_labels
    extracted = ontology.extracted_labels
    kinds = {hub: [label for label in extracted if (label in hub_labels) == hub] for hub in (False, True)}
    share = {
        hub: sum(1 for label in labels if label in rule.subject) / len(labels) if labels else 0.0
        for hub, labels in kinds.items()
    }
    counts = {False: 0.0, True: 0.0}
    for node in nodes:
        hub = node.label in hub_labels
        if node.persisted:
            counts[hub] += node.label in rule.subject
        else:
            counts[hub] += share[hub]
    return counts[False], counts[True]


def _priced_pairs(rule: InferenceRule, ontology: Ontology, every: Sequence[Expected]) -> int:
    """Member pairs a priced ``cross_document`` call shows (``CROSS_DOCUMENT_PAIRS_PER_SUBJECT``); 1 for
    other templates."""

    if rule.template != CROSS_DOCUMENT:
        return 1
    pool = sum(_subject_nodes(rule, every, ontology))
    pairs = round(CROSS_DOCUMENT_PAIRS_PER_SUBJECT * math.sqrt(pool / SUBJECT_REFERENCE_NODES))
    return max(1, min(rule.max_pairs or 1, pairs))


def batch_size(rule: InferenceRule) -> int:
    """Subjects per priced call: ``BATCH_FILL`` of the rule's ``batch`` (rounded, at least one) when it
    has a batch prompt (``InferenceRule.batch_prompt``), else one."""

    if not rule.batch or rule.batch_prompt is None:
        return 1
    return max(1, round(rule.batch * BATCH_FILL))


def _price(ontology: Ontology, rule: InferenceRule, length: int, *, pairs: int, subjects: int) -> int:
    return request_tokens(inference_messages(ontology, rule, length, pairs=pairs, subjects=subjects))


def inference_messages(
    ontology: Ontology, rule: InferenceRule, length: int, *, pairs: int = 1, subjects: int = 1
) -> list[dict[str, str]]:
    """The messages of a representative judge call of ``rule`` (``inference_request``), as the judge
    runner sends them: a batch of ``subjects`` (more than one) with the rule's batch prompt."""

    prompt = rule.batch_prompt if subjects > 1 and rule.batch_prompt is not None else rule.prompt
    return judge_messages(ontology, prompt, inference_request(ontology, rule, length, pairs=pairs, subjects=subjects))


def inference_request(
    ontology: Ontology, rule: InferenceRule, length: int, *, pairs: int = 1, subjects: int = 1
) -> dict[str, Any]:
    """The payload of a representative judge call of ``rule``, fragments included, shaped as the infer step shapes it
    (``graph.build.infer``): placeholder nodes with ``STATEMENTS_SHOWN`` statements each and fragments of ``length``
    characters.
    """

    if rule.template == CROSS_DOCUMENT:
        return _cross_document_request(ontology, rule, length, pairs)
    if subjects > 1 and rule.batch_prompt is not None:
        return _batch_request(ontology, rule, length, subjects)
    target = min(rule.targets)
    subject = _shown("subject", min(rule.subject), ontology)
    payload: dict[str, Any] = {
        "relation": rule.writes,
        "subject": _owner_shown(subject) if rule.template == ATTACH_ORPHAN else subject,
    }
    if rule.template == ENDPOINTS:
        ids = [f"T{index}" for index in range(1, max(rule.min_targets or 1, PAIR_TARGETS) + 1)]
        payload["targets"] = [_shown(f"target{local}", target, ontology, local) for local in ids]
        payload["candidate_pairs"] = sorted(sorted(pair) for pair in combinations(ids, 2))
        others = len(ids)
    elif rule.template == REALIZATION:
        member = min(label for label in ontology.extracted_labels if label not in ontology.hub_labels)
        members = [f"M{index}" for index in range(1, REALIZATION_PRICED_NODES + 1)]
        payload["members"] = [_shown(f"member{local}", member, ontology, local) for local in members]
        payload["candidates"] = [
            {**_shown(f"candidate{index}", target, ontology, f"C{index}"), "via": members}
            for index in range(1, REALIZATION_PRICED_NODES + 1)
        ]
        others = 2 * REALIZATION_PRICED_NODES
    else:
        payload["candidates"] = [
            _owner_shown(_shown(f"candidate{index}", target, ontology, f"C{index}"))
            for index in range(1, (rule.max_candidates or 1) + 1)
        ]
        others = rule.max_candidates or 1
    shown = min(MAX_FRAGMENTS, PRICED_SUBJECT_FRAGMENTS + OTHER_FRAGMENTS * others)
    payload[FRAGMENTS_KEY] = {_fragment_id(index): _placeholder(f"f{index}", length) for index in range(shown)}
    return payload


def _shown(tag: str, label: str, ontology: Ontology, local_id: str | None = None) -> dict[str, Any]:
    """A placeholder node as a judge payload shows it (``id`` when given, ``name``, ``label``,
    ``statements``); a hub's name is short."""

    shown: dict[str, Any] = {} if local_id is None else {"id": local_id}
    length = PLACEHOLDER_HUB_CHARS if label in ontology.hub_labels else PLACEHOLDER_NAME_CHARS
    shown["name"] = _placeholder(tag, length)
    shown["label"] = label
    shown["statements"] = [_placeholder(f"{tag}s{index}", PLACEHOLDER_TEXT_CHARS) for index in range(STATEMENTS_SHOWN)]
    return shown


def _owner_shown(shown: dict[str, Any]) -> dict[str, Any]:
    """A placeholder ``attach_orphan`` subject or candidate: ``shown`` with one section (``infer._owner_shown``)."""

    return {**shown, "sections": [_placeholder(f"{shown['name']}h", PLACEHOLDER_SECTION_CHARS)]}


def _fragment_id(index: int) -> str:
    return _placeholder(f"fragment{index}", PLACEHOLDER_HUB_CHARS)


def _cross_document_request(ontology: Ontology, rule: InferenceRule, length: int, pairs: int) -> dict[str, Any]:
    """A representative ``cross_document`` payload (``inference_request``)."""

    hub_label, member_label = min(rule.subject), min(rule.targets)

    def node(uuid: str, label: str, index: int) -> infer_xdoc.Shown:
        view = _shown(uuid, label, ontology)
        return infer_xdoc.Shown(uuid, view, ((_fragment_id(index), ""),))

    hub = node("hub", hub_label, 0)
    members = [node(f"member{index:02d}", member_label, index) for index in range(1, 2 * pairs + 1)]
    view = infer_xdoc.View(
        hub=hub,
        members=members,
        pairs=[(members[index].uuid, members[index + 1].uuid) for index in range(0, 2 * pairs, 2)],
        documents={
            member.uuid: _placeholder(f"document{index % 2}", PLACEHOLDER_HEADING_CHARS)
            for index, member in enumerate(members)
        },
        owners={member.uuid: hub.view["name"] for member in members[1:]},
        candidates={members[0].uuid: [hub]},
        labels={hub.uuid: hub_label, **{member.uuid: member_label for member in members}},
    )
    parts = infer_xdoc.judgement_parts(ontology, rule, view)
    if parts is None:  # every pair is shown: never None
        return {}
    texts = {fragment_id: _placeholder(f"f{index}", length) for index, fragment_id in enumerate(parts.fragments)}
    return {**parts.payload, FRAGMENTS_KEY: texts}


def _batch_request(ontology: Ontology, rule: InferenceRule, length: int, subjects: int) -> dict[str, Any]:
    """A batched ``attach_orphan`` payload as the infer step builds it (``infer._call``): ``subjects`` subjects
    (``S1``,...) written in one section, so grounded in one fragment, each with its own ``max_candidates`` candidates,
    of which each subject after the first adds ``BATCH_NEW_CANDIDATES`` (subject ``i`` lists ``C(2i-1)`` onwards), every
    candidate once with a fragment of its own.
    """

    own = rule.max_candidates or 1
    step = min(own, BATCH_NEW_CANDIDATES)
    shown = own + step * (subjects - 1)
    target = min(rule.targets)
    section = _fragment_id(0)
    fragments = {section: _placeholder("f0", length)} | {
        _fragment_id(index): _placeholder(f"f{index}", length) for index in range(1, shown + 1)
    }
    return {
        "relation": rule.writes,
        "subjects": [
            {
                **_owner_shown(_shown(f"subject{index}", min(rule.subject), ontology, f"S{index}")),
                "fragments": [section],
                "candidates": [f"C{first}" for first in range(step * (index - 1) + 1, step * (index - 1) + own + 1)],
            }
            for index in range(1, subjects + 1)
        ],
        "candidates": [
            {
                **_owner_shown(_shown(f"candidate{index}", target, ontology, f"C{index}")),
                "fragments": [_fragment_id(index)][:OTHER_FRAGMENTS],
            }
            for index in range(1, shown + 1)
        ],
        FRAGMENTS_KEY: fragments,
    }


def _mean_length(fragments: Sequence[FragmentRec]) -> int:
    """The mean text length of ``fragments`` with text, else ``PLACEHOLDER_TEXT_CHARS``."""

    texts = [fragment.text for fragment in fragments if fragment.text.strip()]
    return round(sum(len(text) for text in texts) / len(texts)) if texts else PLACEHOLDER_TEXT_CHARS


@dataclass(frozen=True)
class _EmbeddingInputs:
    """What the embeddings estimate reads besides the plan: the expected nodes, hubs and known-term
    names, the persisted nodes whose candidate texts are stored (``settled``), whether units are
    extracted, and the vector store."""

    every: Sequence[Expected]
    hub_nodes: Sequence[Expected]
    hub_names: Sequence[str]
    settled: Collection[Expected]
    extracting: bool
    vectors: VectorCache


def _dedup_texts(every: Sequence[Expected], settled: Collection[Expected], ontology: Ontology) -> list[str]:
    """The candidate texts Conform's dedup embeds that the store does not hold: the id-less requirements of each (scope,
    label) group of ``MIN_DEDUP_GROUP`` or more, less the settled ones; a placeholder's text is its name and a
    placeholder statement (its quote mostly restates it).
    """

    groups: dict[tuple[str, str], list[Expected]] = {}
    for node in every:
        rule = ontology.identity_rule(node.label)
        span = not node.persisted or node.key.split("|", 2)[1:2] == ["span"]
        if rule.key == "id" and rule.fallback == "span" and span:
            groups.setdefault((rule.scope, node.label), []).append(node)
    return [
        dedup_candidate_text(node.label, node.name, node.stmt or _placeholder("t", PLACEHOLDER_TEXT_CHARS), node.quote)
        for members in groups.values()
        if len(members) >= MIN_DEDUP_GROUP
        for node in members
        if node not in settled
    ]


async def _embeddings(plan: PlanArtifact, inputs: _EmbeddingInputs, ontology: Ontology) -> StepEstimate:
    """The embedding requests of the build, in the order its steps ask for vectors (each ask embeds
    the texts neither the store nor an earlier ask holds, in the gateway's batches)."""

    every, hub_nodes = inputs.every, inputs.hub_nodes
    fragment_texts = [fragment.text.strip() for fragment in plan.fragments if fragment.text.strip()]
    names = [embedding_text(node.label, node.name) for node in every]
    search = [
        f"{embedding_text(node.label, node.name)}. {node.stmt or _placeholder('t', PLACEHOLDER_TEXT_CHARS)}"
        if node.stmt or not node.persisted
        else embedding_text(node.label, node.name)
        for node in every
    ]
    asks: list[Sequence[str]] = []
    if inputs.hub_names and inputs.extracting:
        asks += [fragment_texts, inputs.hub_names]  # known terms: fragment and hub-name vectors
    if len(hub_nodes) >= MIN_HUB_CONSOLIDATION:
        asks.append([embedding_text(node.label, node.name) for node in hub_nodes])  # hub consolidation
        # the hubs' candidate texts (pair pass)
        asks.append([candidate_text(hub_judge_item(node)) for node in hub_nodes if node not in inputs.settled])
    asks += [names, fragment_texts]  # Resolve, edge catalogs
    asks += [_dedup_texts(every, inputs.settled, ontology), [*search, *names]]  # Conform dedup, Conform
    stored = await inputs.vectors.stored({text for ask in asks for text in ask})
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
