"""Reads of the project graph."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from graph.core.errors import (
    ConfigurationError,
    ConformanceError,
    InfrastructureUnavailableError,
    LLMError,
    LLMFatalError,
)
from graph.core.evidence import (
    MAX_CONTEXT_LINES,
    EvidenceItem,
    EvidenceNode,
    FragmentLines,
    document_lines,
    fragment_lines,
    quote_lines,
)
from graph.core.expand import (
    MAX_BUDGET,
    MAX_SEED_IDS,
    MAX_SIMILAR_SOURCES,
    MIN_BUDGET,
    VECTOR_CANDIDATES,
    Expansion,
    ExpansionItem,
    SimilarItem,
    SimilarResult,
    Via,
    best_scores,
    estimate_tokens,
    mean_vector,
    personalised_pagerank,
    ranked_scores,
    subgraph,
    top_scores,
    within_budget,
)
from graph.core.hops import MAX_HOP_IDS, MAX_HOPS, MIN_HOPS, HopNode, HopRead, hop_distances
from graph.core.identity import (
    explicit_id_pattern,
    identity_key,
    normalise_id,
    validate_project_id,
)
from graph.core.ontology import AttributeSpec, Ontology
from graph.core.paths import PathResult, PathWalk
from graph.core.queries import (
    COUNT_NODES,
    COUNT_SEARCH_VECTORS,
    READ_DOCUMENTS,
    READ_EDGES,
    READ_FRAGMENT_HITS,
    READ_FRAGMENTS,
    READ_IMPACT_HOP,
    READ_NEIGHBOURS,
    READ_NODE_BY_ALIAS,
    READ_NODE_BY_SPEC_ID,
    READ_NODE_UUIDS_BY_SPEC_ID,
    READ_NODE_VECTORS,
    READ_NODES,
    READ_NODES_BY_IDS,
    READ_NODES_BY_UUID,
    READ_NODES_FOR_LINES,
    READ_NODES_PAGE,
    READ_OWNER_VALUES,
    READ_TRACE_HOP,
    SEARCH_FRAGMENTS,
    SEARCH_NODES_FULLTEXT,
    SEARCH_NODES_VECTOR,
    SEARCH_NODES_VECTOR_EXACT,
    Query,
    scope_sequence,
    scope_via,
)
from graph.core.store import (
    FRAGMENT_TEXT_INDEX,
    QUERY_TOO_COMPLEX,
    SPEC_NODE_EMBEDDING_INDEX,
    SPEC_NODE_TEXT_INDEX,
    as_executor,
)
from graph.core.trace import (
    BASES,
    DIRECTIONS,
    MAX_SPEC_IDS,
    MAX_TRACE_LIMIT,
    TRUSTED,
    Basis,
    Direction,
    TraceResult,
    TraceWalk,
    Visit,
)
from logger.logger import DEBUG, WARNING, get_logger, log_event

logger = get_logger("graph.core.reader")

BUILD: Final = "build"
RETIRED: Final = "retired"
ACTIVE: Final = "active"
INFERRED: Final = "inferred"
AttributeValue = str | int | float | bool | None
MatchKind = Literal["vector", "fulltext", "both", "id"]
# The score of a hit named by an id in the query: above every fused score (at most 3 / 11).
ID_HIT_SCORE: Final = 1.0

# Weighted reciprocal rank fusion: a hit at 1-based rank r adds weight / (RRF_K + r), with weight
# VECTOR_WEIGHT in the vector list and 1 in the fulltext list (tuned in M6, m6-eval-report 9(b)).
RRF_K: Final = 10
VECTOR_WEIGHT: Final = 2.0
# Each search list holds up to limit x OVERFETCH project hits.
OVERFETCH: Final = 1
# A hit found only by the vector list is dropped below this vector score. Neo4j scores cosine as
# (1 + cos) / 2 on both the index and the exact path, so 0.60 is a raw cosine of 0.2 (tuned on
# text-embedding-3-large; ``GraphReader(vector_floor=...)`` for another model).
DEFAULT_VECTOR_FLOOR: Final = 0.60
MAX_SEARCH_LIMIT: Final = 500
# At most this many candidate vectors: exact cosine at once, no index call.
EXACT_VECTOR_MAX: Final = 2000
# The vector fetch doubles at most this many times while hits < limit, then exact cosine.
VECTOR_DOUBLINGS: Final = 3
# Fulltext query terms are cut so Lucene's clause limit (1024) is never reached: at most this many
# terms and characters (an analyzer token is at least one character).
MAX_QUERY_TERMS: Final = 256
MAX_QUERY_CHARS: Final = 1000
IMPACT_DEPTHS: Final = (1, 3)
IMPACT_MAX_NODES: Final = 500
# Terms as Lucene's standard analyzer splits them (on non-word characters; ``.`` and apostrophes
# inside a word kept). Terms hold no query syntax; operator words are lower-cased (the analyzer
# lower-cases terms anyway, so they still match as words).
_QUERY_TERM: Final = re.compile(r"\w+(?:[.'\u2019]\w+)*")
_LUCENE_OPERATORS: Final = frozenset({"AND", "OR", "NOT", "TO"})
# Lucene's English stop set (EnglishAnalyzer.ENGLISH_STOP_WORDS_SET, 33 words): left out of fulltext
# queries. Analyzer data, kept as one text so no word is a separate literal (rule I4).
_STOP_WORDS: Final = frozenset(
    re.findall(
        r"\w+",
        "a an and are as at be but by for if in into is it no not of on or such that the their then there "
        "these they this to was will with",
    )
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class Statement(_Frozen):
    """What one supporting fragment says about a node."""

    fragment_id: str
    document_id: str | None = None
    revision_id: str | None = None
    version: int | None = None
    text: str = ""
    at: str | None = None


class Provenance(_Frozen):
    """One evidence fragment of a node: its document, lines and the node's quote there."""

    document_id: str | None
    path: str | None
    fragment_id: str
    line_start: int | None
    line_end: int | None
    quote: str | None


class GraphNode(_Frozen):
    spec_id: str
    uuid: str
    label: str
    name: str
    last_build_id: str | None = None
    spec_id_aliases: tuple[str, ...] = ()
    attributes: dict[str, Any] = {}
    statements: tuple[Statement, ...] = ()
    provenance: tuple[Provenance, ...] = ()


class EvidenceQuote(_Frozen):
    fragment_id: str
    quote: str = ""


class GraphEdge(_Frozen):
    """A domain edge in its stored direction; ``inference_rule``, ``confidence``: inferred only."""

    relation: str
    source: str
    target: str
    edge_key: str | None = None
    basis: str | None = None
    fact: str | None = None
    source_fragment_ids: tuple[str, ...] = ()
    inference_rule: str | None = None
    confidence: float | None = None
    evidence: tuple[EvidenceQuote, ...] = ()


class GraphDocument(_Frozen):
    document_id: str
    spec_id: str | None = None
    revision_id: str | None = None
    version: int | None = None
    path: str | None = None
    title: str | None = None
    status: str | None = None
    last_build_id: str | None = None


class GraphFragment(_Frozen):
    fragment_id: str
    document_id: str
    spec_id: str | None = None
    text: str = ""
    heading_path: tuple[str, ...] | str | None = None
    ordinal: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    content_hash: str | None = None
    status: str | None = None
    extracted_hash: str | None = None
    error_code: str | None = None
    last_build_id: str | None = None


class NodeHit(_Frozen):
    """A search hit: the node, its fused rank score and which index lists found it."""

    node: GraphNode
    score: float
    matched: MatchKind


class NodePage(_Frozen):
    """One page of spec nodes in ``spec_id`` order and the ``spec_id`` to continue after (None: no more)."""

    items: list[GraphNode]
    next_after: str | None = None


class FragmentHit(_Frozen):
    """A fulltext hit on an ``active`` fragment, with its document and Lucene score."""

    fragment: GraphFragment
    document: GraphDocument | None
    score: float


class ImpactResult(_Frozen):
    """Nodes reachable from ``sources`` (resolved ``spec_id``s), nearest first then by ``spec_id``."""

    sources: tuple[str, ...] = ()
    nodes: tuple[GraphNode, ...] = ()
    distance: dict[str, int] = {}
    truncated: bool = False


class GraphReader:
    """Project-scoped reads and search; ``gateway`` embeds search text (None: fulltext only)."""

    def __init__(
        self,
        driver_or_executor: Any,
        ontology: Ontology,
        gateway: Any = None,
        *,
        database: str | None = None,
        vector_floor: float = DEFAULT_VECTOR_FLOOR,
    ) -> None:
        """``vector_floor``: the lowest vector score (Neo4j's ``(1 + cos) / 2``, 0..1) a hit found
        only by the vector list may have; it depends on the embedding model."""

        if not _is_fraction(vector_floor):
            raise ConfigurationError("invalid_vector_floor")
        self._vector_floor = float(vector_floor)
        self._executor = as_executor(driver_or_executor, database)
        self._ontology = ontology
        self._gateway = gateway
        self._spec_labels = tuple(label for label in ontology.labels if ontology.owner_of(label) == BUILD)
        self._domain_relations = tuple(sorted(name for name in ontology.relations if ontology.owner_of(name) == BUILD))
        self._symmetric = tuple(name for name in self._domain_relations if ontology.is_symmetric(name))
        self._spine_roots = tuple(sorted(ontology.spine.roots))
        # Spec label to its inheriting attributes (attribute name to the ``inherit.via`` relation).
        self._inherit = {label: dict(rules) for label in self._spec_labels if (rules := ontology.inherit_rules(label))}
        self._id_pattern = explicit_id_pattern(
            [family for label in self._spec_labels for family in ontology.node_type(label).id_families]
        )
        prefixes = sorted(
            {prefix for label in self._spec_labels if (prefix := ontology.node_type(label).id_prefix) is not None},
            key=lambda prefix: (-len(prefix), prefix),
        )
        self._spec_id_pattern = (
            re.compile(rf"(?<![\w:])(?:{'|'.join(map(re.escape, prefixes))})\w+") if prefixes else None
        )
        # The extra parameters of each scope query, by query name: a ``via`` query's ``max_hops``
        # (its effective hop bound, the spine path's ``max``). Filled by ``_scope_paths``.
        self._scope_params: dict[str, dict[str, int]] = {}
        self._scope_queries = self._scope_paths()

    async def node(self, project_id: str, spec_id: str) -> GraphNode | None:
        """The spec node with this ``spec_id`` or alias, else None."""

        project_id = validate_project_id(project_id)
        nodes: list[GraphNode] = []
        for query in (READ_NODE_BY_SPEC_ID, READ_NODE_BY_ALIAS):
            rows = await self._executor.read(query, project_id, spec_id=spec_id, retired=RETIRED)
            nodes = self._nodes(rows)
            if nodes:
                break
        nodes = await self._inherited(project_id, nodes[:1])
        return nodes[0] if nodes else None

    async def nodes_by_spec_ids(self, project_id: str, spec_ids: Sequence[str]) -> dict[str, GraphNode]:
        """The spec nodes ``spec_ids`` name (own id or alias), keyed by their current ``spec_id``; one read."""

        return await self._nodes_by_spec_id(validate_project_id(project_id), spec_ids)

    async def nodes(
        self,
        project_id: str,
        labels: Sequence[str] | None = None,
        where: Mapping[str, AttributeValue] | None = None,
    ) -> list[GraphNode]:
        """All spec nodes, ordered by ``spec_id``; filtered by ``labels`` and ``where`` (equality on stored values; a
        node without the property has its label's declared ``default``, so it matches that value and not None).
        """

        project_id = validate_project_id(project_id)
        filters = self._filters(self._labels(labels), where)
        rows = await self._executor.read(READ_NODES, project_id, retired=RETIRED, **filters)
        return await self._inherited(project_id, self._nodes(rows))

    async def count_nodes(self, project_id: str, labels: Sequence[str] | None) -> int:
        """Number of spec nodes with any of ``labels``, as ``nodes`` filters (FR-MCP-TOOL-10)."""

        project_id = validate_project_id(project_id)
        selected = self._labels(labels)
        rows = await self._executor.read(COUNT_NODES, project_id, labels=list(selected))
        return int(rows[0]["total"]) if rows else 0

    async def nodes_page(
        self,
        project_id: str,
        labels: Sequence[str] | None,
        *,
        limit: int = 50,
        after: str | None = None,
    ) -> NodePage:
        """At most ``limit`` (1 to 500) spec nodes after ``after``, as ``nodes`` filters (FR-MCP-TOOL-10).

        Reads ``limit + 1`` rows: a further row means more exist and ``next_after`` is the last
        ``spec_id`` of the page. The decision uses the raw rows, so a row ``nodes`` would skip cannot
        hide a next page.

        ``after`` is a ``spec_id`` string, exclusive; it is not checked here, so a non-string gives an empty page.
        """

        project_id = validate_project_id(project_id)
        _check_limit(limit)
        selected = self._labels(labels)
        rows = await self._executor.read(
            READ_NODES_PAGE,
            project_id,
            labels=list(selected),
            after=after,
            limit=limit + 1,
            retired=RETIRED,
        )
        kept = rows[:limit]
        next_after = kept[-1]["props"]["spec_id"] if len(rows) > limit else None
        return NodePage(items=await self._inherited(project_id, self._nodes(kept)), next_after=next_after)

    async def neighbours(
        self, project_id: str, spec_id: str, relations: Sequence[str] | None = None
    ) -> list[GraphEdge]:
        """Domain edges of the node, either direction; an unknown ``spec_id`` gives no edges."""

        selected = self._relations(relations)
        node = await self.node(project_id, spec_id)
        if node is None:
            return []
        rows = await self._executor.read(
            READ_NEIGHBOURS,
            project_id,
            spec_id=node.spec_id,
            relations=list(selected),
            labels=list(self._spec_labels),
        )
        return [_edge(row) for row in rows]

    async def edges(self, project_id: str, *, include_inferred: bool = True) -> list[GraphEdge]:
        """All domain edges, ordered by source, relation and target ``spec_id``."""

        project_id = validate_project_id(project_id)
        rows = await self._executor.read(
            READ_EDGES,
            project_id,
            relations=list(self._domain_relations),
            labels=list(self._spec_labels),
            include_inferred=include_inferred,
            inferred=INFERRED,
        )
        return [_edge(row) for row in rows]

    async def documents(self, project_id: str) -> list[GraphDocument]:
        """Every document of the project with its status, ordered by path."""

        rows = await self._executor.read(READ_DOCUMENTS, validate_project_id(project_id))
        return [GraphDocument.model_validate(row["props"]) for row in rows]

    async def fragments(self, project_id: str, document_id: str | None = None) -> list[GraphFragment]:
        """Fragments of the project (or of one document) with their status, in document order."""

        rows = await self._executor.read(READ_FRAGMENTS, validate_project_id(project_id), document_id=document_id)
        return [GraphFragment.model_validate(row["props"]) for row in rows]

    async def scope(self, project_id: str, spec_id: str, labels: Sequence[str] | None = None) -> list[GraphNode]:
        """The nodes under a node: those reaching it by a declared spine path (or a contiguous part of a ``sequence``
        path), each edge in its direction (a symmetric relation either way), never through a spine-root node; ordered by
        ``spec_id``.
        """

        selected = self._labels(labels)
        root = await self.node(project_id, spec_id)
        if root is None:
            return []
        found: set[str] = set()
        for query in self._scope_queries:
            rows = await self._executor.read(
                query,
                project_id,
                spec_id=root.spec_id,
                symmetric=list(self._symmetric),
                roots=list(self._spine_roots),
                labels=list(selected),
                **self._scope_params.get(query.name, {}),
            )
            found.update(row["uuid"] for row in rows)
        return await self._nodes_by_uuid(project_id, sorted(found))

    async def impact(
        self,
        project_id: str,
        spec_ids: Sequence[str],
        depth: int = 2,
        relations: Sequence[str] | None = None,
    ) -> ImpactResult:
        """Nodes reachable over domain edges in both directions within ``depth`` (1-3) hops of any of ``spec_ids``
        (aliases resolve, unknown ids are ignored), the start nodes left out; nearest first, at most 500, ``truncated``
        when more were reachable.
        """

        project_id = validate_project_id(project_id)
        if not _is_depth(depth):
            raise ConfigurationError("invalid_depth", minimum=IMPACT_DEPTHS[0], maximum=IMPACT_DEPTHS[1])
        selected = self._relations(relations)
        if isinstance(spec_ids, str):
            spec_ids = [spec_ids]
        labels = list(self._spec_labels)
        starts = await self._executor.read(
            READ_NODE_UUIDS_BY_SPEC_ID, project_id, spec_ids=list(dict.fromkeys(spec_ids)), labels=labels
        )
        seen = [row["uuid"] for row in starts]
        frontier = list(seen)
        found: list[tuple[str, int]] = []
        truncated = False
        for distance in range(1, depth + 1):
            if not frontier or not selected:
                break
            room = IMPACT_MAX_NODES - len(found)
            rows = await self._executor.read(
                READ_IMPACT_HOP,
                project_id,
                frontier=frontier,
                seen=seen,
                relations=list(selected),
                labels=labels,
                limit=room + 1,
            )
            if len(rows) > room:
                truncated = True
                rows = rows[:room]
            frontier = [row["uuid"] for row in rows]
            seen.extend(frontier)
            found.extend((uuid, distance) for uuid in frontier)
            if truncated:
                break
        hops = dict(found)
        nodes = await self._nodes_by_uuid(project_id, list(hops))
        nodes.sort(key=lambda node: (hops[node.uuid], node.spec_id))
        return ImpactResult(
            sources=tuple(row["spec_id"] for row in starts),
            nodes=tuple(nodes),
            distance={node.spec_id: hops[node.uuid] for node in nodes},
            truncated=truncated,
        )

    async def trace(
        self,
        project_id: str,
        spec_ids: Sequence[str],
        *,
        direction: Direction,
        depth: int = 2,
        basis: Basis = TRUSTED,
        relations: Sequence[str] | None = None,
        include_owner: bool = True,
        limit: int = 200,
    ) -> TraceResult:
        """The directed walk by impact roles from ``spec_ids`` (trace.md "Walk"): what they are ``based_on`` or
        what they ``affect``, within ``depth`` (1-3) hops, at most ``limit`` (1-500) nodes by hop then ``spec_id``.
        """

        project_id = validate_project_id(project_id)
        if isinstance(spec_ids, str):
            spec_ids = [spec_ids]
        if not _is_int_between(len(spec_ids), 1, MAX_SPEC_IDS) or not all(isinstance(item, str) for item in spec_ids):
            raise ConfigurationError("invalid_spec_ids", maximum=MAX_SPEC_IDS)
        if direction not in DIRECTIONS:
            raise ConfigurationError("invalid_direction")
        if not _is_depth(depth):
            raise ConfigurationError("invalid_depth", minimum=IMPACT_DEPTHS[0], maximum=IMPACT_DEPTHS[1])
        if basis not in BASES:
            raise ConfigurationError("invalid_basis")
        selected = self._relations(relations)
        if not _is_int_between(limit, 1, MAX_TRACE_LIMIT):
            raise ConfigurationError("invalid_limit", maximum=MAX_TRACE_LIMIT)
        walk = TraceWalk(
            executor=self._executor,
            ontology=self._ontology,
            project_id=project_id,
            labels=self._spec_labels,
            domain_relations=self._domain_relations,
            relations=selected,
            direction=direction,
            depth=depth,
            any_basis=basis != TRUSTED,
            include_owner=bool(include_owner),
            limit=limit,
        )
        return await walk.run(spec_ids)

    async def hops(
        self, project_id: str, sources: Sequence[str], spec_ids: Sequence[str], *, max_hops: int = 3
    ) -> dict[str, int]:
        """The fewest edges from any of ``sources`` to each of ``spec_ids`` (current ids) over ``stated`` and
        ``implied`` domain edges either way, no spine root inside the chain; a source is 0 and an id not reached
        within ``max_hops`` (1-4) is left out."""

        project_id = validate_project_id(project_id)
        for ids in (sources, spec_ids):
            if isinstance(ids, str) or not _is_int_between(len(ids), 1, MAX_HOP_IDS):
                raise ConfigurationError("invalid_spec_ids", maximum=MAX_HOP_IDS)
        if not _is_int_between(max_hops, MIN_HOPS, MAX_HOPS):
            raise ConfigurationError("invalid_max_hops", minimum=MIN_HOPS, maximum=MAX_HOPS)
        rows = await self._executor.read(
            READ_NODE_UUIDS_BY_SPEC_ID,
            project_id,
            spec_ids=list(dict.fromkeys(sources)),
            labels=list(self._spec_labels),
        )
        starts = [HopNode(row["uuid"], row["spec_id"]) for row in rows]
        return await hop_distances(self._trusted_hop(project_id), starts, spec_ids, max_hops)

    async def similar(
        self,
        project_id: str,
        spec_ids: Sequence[str],
        *,
        exclude_linked: bool = True,
        min_score: float = 0.75,
        limit: int = 5,
    ) -> SimilarResult:
        """Spec nodes whose stored vector reads like a source's (score ``(1+cos)/2`` at least ``min_score``), without
        an embedding call: each node once with its best source (``like``), best first then by ``spec_id``, at most
        ``limit``. Sources are never items; ``exclude_linked`` leaves out nodes with a domain edge to a source. A
        source without a vector of the gateway's model, or a reader without a gateway, adds nothing."""

        project_id = validate_project_id(project_id)
        _check_ids(spec_ids, MAX_SIMILAR_SOURCES)
        if not _is_fraction(min_score):
            raise ConfigurationError("invalid_min_score")
        _check_limit(limit)
        sources, unknown = await self._resolve(project_id, spec_ids)
        excluded = {node.spec_id for node in sources}
        if exclude_linked:
            excluded |= await self._linked(project_id, sources)
        hits, uuids = await self._vector_scores(project_id, sources, limit + len(excluded))
        best = best_scores(hits, excluded)
        kept = top_scores({spec_id: score for spec_id, (score, _) in best.items() if score >= min_score}, limit)
        nodes = {node.spec_id: node for node in await self._nodes_by_uuid(project_id, [uuids[i] for i in kept])}
        items = tuple(
            SimilarItem(
                spec_id=spec_id,
                label=nodes[spec_id].label,
                name=nodes[spec_id].name,
                score=score,
                like=best[spec_id][1],
                aliases=nodes[spec_id].spec_id_aliases,
            )
            for spec_id, score in kept.items()
            if spec_id in nodes
        )
        return SimilarResult(sources=tuple(node.spec_id for node in sources), unknown=unknown, items=items)

    async def evidence(self, project_id: str, spec_id: str, *, context_lines: int = 2) -> list[EvidenceItem] | None:
        """One item per evidence fragment of the node (not retired), in document then line order: its lines, the
        quote with up to ``context_lines`` (0-5) lines before and after from the document's active fragments, and the
        other nodes whose evidence overlaps those lines. ``None`` when ``spec_id`` names no spec node."""

        project_id = validate_project_id(project_id)
        if not _is_int_between(context_lines, 0, MAX_CONTEXT_LINES):
            raise ConfigurationError("invalid_context_lines", maximum=MAX_CONTEXT_LINES)
        node = await self.node(project_id, spec_id)
        if node is None:
            return None
        documents: dict[str, list[FragmentLines]] = {}
        records = sorted(
            node.provenance,
            key=lambda item: (item.path or "", item.document_id or "", item.line_start or 0, item.fragment_id),
        )
        return [await self._evidence_item(project_id, node, record, context_lines, documents) for record in records]

    async def paths(self, project_id: str, from_id: str, to_id: str, *, max_hops: int = 4) -> PathResult | None:
        """At most 3 shortest chains of at most ``max_hops`` (1-4) trusted edges walked by the trace's roles: first
        ``based_on`` (``from_id`` depends on ``to_id``), only without one ``affects``; a spine root never inside a
        chain. ``None`` when an end names no spec node."""

        project_id = validate_project_id(project_id)
        if not _is_int_between(max_hops, MIN_HOPS, MAX_HOPS):
            raise ConfigurationError("invalid_max_hops", minimum=MIN_HOPS, maximum=MAX_HOPS)
        start = await self.node(project_id, from_id)
        end = await self.node(project_id, to_id)
        if start is None or end is None:
            return None
        if start.uuid == end.uuid:
            return PathResult()
        for direction in DIRECTIONS:
            walk = PathWalk(
                executor=self._executor,
                ontology=self._ontology,
                project_id=project_id,
                labels=self._spec_labels,
                domain_relations=self._domain_relations,
                relations=self._domain_relations,
                direction=direction,
                depth=max_hops,
                any_basis=False,
                include_owner=False,
                limit=MAX_TRACE_LIMIT,
            )
            chains = await walk.chains(Visit(start.uuid, start.spec_id, start.label, via_owner=False), end.uuid)
            if chains:
                return PathResult(direction=direction, chains=chains)
        return PathResult()

    async def expand(
        self, project_id: str, spec_ids: Sequence[str], *, exclude: Sequence[str] = (), budget_tokens: int = 3000
    ) -> Expansion:
        """What to read next around the seeds ``spec_ids`` (1-500): personalised PageRank near them mixed with stored
        vector scores, seeds and ``exclude`` left out, best first, taken until the next item would pass
        ``budget_tokens`` (100-20000; tokens are characters / 4). No model call."""

        project_id = validate_project_id(project_id)
        _check_ids(spec_ids, MAX_SEED_IDS)
        _check_ids(exclude, MAX_SEED_IDS, minimum=0, code="invalid_exclude")
        if not _is_int_between(budget_tokens, MIN_BUDGET, MAX_BUDGET):
            raise ConfigurationError("invalid_budget", minimum=MIN_BUDGET, maximum=MAX_BUDGET)
        seeds, _ = await self._resolve(project_id, spec_ids)
        if not seeds:
            return Expansion()
        seed_ids = [node.spec_id for node in seeds]
        left_out = {*seed_ids, *exclude}
        graph = await self._graph_ranks(project_id, seeds)
        close = await self._centroid_scores(project_id, seeds, VECTOR_CANDIDATES + len(left_out))
        kept = {k: v for k, v in close.items() if k not in left_out and v >= self._vector_floor}
        vector = top_scores(kept, VECTOR_CANDIDATES)
        ranked = ranked_scores({k: v for k, v in graph.items() if k not in left_out}, vector)
        nodes = await self._nodes_by_spec_id(project_id, [spec_id for spec_id, _, _ in ranked])
        items = [_expansion_item(nodes[spec_id], score, via) for spec_id, score, via in ranked if spec_id in nodes]
        return within_budget(items, budget_tokens)

    async def nodes_for_fragment(
        self, project_id: str, document_id: str, line_start: int, line_end: int
    ) -> list[GraphNode]:
        """Spec nodes whose evidence lines in the document overlap ``line_start``..``line_end`` (inclusive, lines of the
        built revision), ordered by ``spec_id``.
        """

        project_id = validate_project_id(project_id)
        bounds = (line_start, line_end)
        if any(isinstance(item, bool) or not isinstance(item, int) for item in bounds) or not (
            1 <= line_start <= line_end
        ):
            raise ConfigurationError("invalid_line_range")
        rows = await self._executor.read(
            READ_NODES_FOR_LINES,
            project_id,
            document_id=document_id,
            line_start=line_start,
            line_end=line_end,
            labels=list(self._spec_labels),
            retired=RETIRED,
        )
        return await self._inherited(project_id, self._nodes(rows))

    async def find_nodes(
        self,
        project_id: str,
        text: str,
        labels: Sequence[str] | None = None,
        where: Mapping[str, AttributeValue] | None = None,
        limit: int = 20,
    ) -> list[NodeHit]:
        """Hybrid search: the vector and fulltext lists fused by weighted reciprocal rank; fulltext only without a
        gateway or when embedding fails.
        """

        project_id = validate_project_id(project_id)
        _check_limit(limit)
        filters = self._filters(self._labels(labels), where)
        if _is_blank(text):
            return []
        depth = limit * OVERFETCH
        named = await self._named_hits(project_id, text, filters)
        fulltext = await self._fulltext_hits(
            SEARCH_NODES_FULLTEXT,
            project_id,
            text,
            want=depth,
            key="spec_id",
            index=SPEC_NODE_TEXT_INDEX,
            params=filters,
        )
        vector = await self._vector_hits(project_id, text, limit, depth, filters)
        scores: dict[str, float] = {}
        spec_ids: dict[str, str] = {}
        sources: dict[str, set[str]] = {}
        similarity: dict[str, float] = {}
        for kind, weight, rows in (("vector", VECTOR_WEIGHT, vector or []), ("fulltext", 1.0, fulltext)):
            for rank, row in enumerate(rows, start=1):
                uuid = row["uuid"]
                scores[uuid] = scores.get(uuid, 0.0) + weight / (RRF_K + rank)
                spec_ids[uuid] = row["spec_id"]
                sources.setdefault(uuid, set()).add(kind)
                if kind == "vector":
                    similarity[uuid] = float(row["score"])
        floored = [uuid for uuid in scores if sources[uuid] == {"vector"} and similarity[uuid] < self._vector_floor]
        for uuid in floored:
            del scores[uuid]
        fused = sorted(
            (uuid for uuid in scores if uuid not in named),
            key=lambda uuid: (-scores[uuid], spec_ids[uuid]),
        )
        ranked = [*named, *fused][:limit]
        nodes = {node.uuid: node for node in await self._nodes_by_uuid(project_id, ranked)}
        hits = [
            NodeHit(node=nodes[uuid], score=ID_HIT_SCORE, matched="id")
            if uuid in named
            else NodeHit(node=nodes[uuid], score=scores[uuid], matched=_match_kind(sources[uuid]))
            for uuid in ranked
            if uuid in nodes
        ]
        log_event(
            logger,
            DEBUG,
            "graph.read.find_nodes",
            vector_hits=len(vector) if vector is not None else None,
            fulltext_hits=len(fulltext),
            id_hits=len(named),
            below_floor=len(floored),
            returned=len(hits),
            mode="hybrid" if vector is not None else "fulltext",
        )
        return hits

    async def find_fragments(
        self, project_id: str, text: str, document_id: str | None = None, limit: int = 20
    ) -> list[FragmentHit]:
        """Fulltext search over ``active`` fragment text, best score first."""

        project_id = validate_project_id(project_id)
        _check_limit(limit)
        if _is_blank(text):
            return []
        hits = await self._fulltext_hits(
            SEARCH_FRAGMENTS,
            project_id,
            text,
            want=limit,
            key="fragment_id",
            index=FRAGMENT_TEXT_INDEX,
            params={"document_id": document_id, "active": ACTIVE},
        )
        if not hits:
            return []
        rows = await self._executor.read(
            READ_FRAGMENT_HITS, project_id, fragment_ids=[hit["fragment_id"] for hit in hits]
        )
        found = {row["props"]["fragment_id"]: row for row in rows}
        return [
            FragmentHit(
                fragment=GraphFragment.model_validate(found[hit["fragment_id"]]["props"]),
                document=(
                    GraphDocument.model_validate(found[hit["fragment_id"]]["document"])
                    if found[hit["fragment_id"]]["document"]
                    else None
                ),
                score=float(hit["score"]),
            )
            for hit in hits
            if hit["fragment_id"] in found
        ]

    # Search helpers

    async def _named_hits(self, project_id: str, text: str, filters: Mapping[str, Any]) -> list[str]:
        """Uuids of the nodes ``text`` names by explicit id or ``spec_id``, in the order it names
        them (nodes of one name by ``spec_id``), each once."""

        names: list[tuple[int, str, str]] = []  # (position, kind, value)
        if self._id_pattern is not None:
            for match in self._id_pattern.finditer(text):
                explicit = normalise_id(match.group(0))
                label = self._ontology.label_for_id_family(explicit)
                if label in self._spec_labels:
                    scope = self._ontology.identity_rule(label).scope
                    names.append((match.start(), "keys", identity_key(scope, "id", explicit)))
        if self._spec_id_pattern is not None:
            names.extend((match.start(), "spec_ids", match.group(0)) for match in self._spec_id_pattern.finditer(text))
        if not names:
            return []
        names.sort()
        rows = await self._executor.read(
            READ_NODES_BY_IDS,
            project_id,
            keys=[value for _, kind, value in names if kind == "keys"],
            spec_ids=[value for _, kind, value in names if kind == "spec_ids"],
            **filters,
        )
        found: dict[str, None] = {}
        for _, kind, value in names:
            for row in rows:
                if value in row[kind]:
                    found.setdefault(row["uuid"])
        return list(found)

    async def _fulltext_hits(
        self,
        query: Query,
        project_id: str,
        text: str,
        *,
        want: int,
        key: str,
        index: str,
        params: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Up to ``want`` project hits of the fulltext index in its score order (ties by ``key``)."""

        terms = _lucene_query(text)
        if not terms:
            return []
        hits: list[dict[str, Any]] = []
        skip, page = 0, want
        while True:
            try:
                rows = await self._executor.read(
                    query, project_id, index=index, query=terms, skip=skip, page=page, **params
                )
            except ConformanceError as error:
                if error.code == QUERY_TOO_COMPLEX:
                    raise ConfigurationError("query_too_long", query=query.name) from None
                raise
            fetched = rows[0]["fetched"]
            hits.extend(rows[0]["hits"])
            if len(hits) >= want or fetched < page:
                break
            skip, page = skip + fetched, page * 2
        hits.sort(key=lambda hit: (-hit["score"], hit[key]))
        return hits[:want]

    async def _vector_hits(
        self, project_id: str, text: str, limit: int, depth: int, filters: Mapping[str, Any]
    ) -> list[dict[str, Any]] | None:
        """Up to ``depth`` project hits of the search vector in similarity order; None (fulltext only) when there is no
        gateway, no candidate has a vector of the gateway's embedding model, or embedding failed.
        """

        matching = await self._matching_vectors(project_id, filters)
        if not matching:
            return None
        try:
            vectors = await self._gateway.embed([_embedding_text(text)])
        except LLMFatalError:
            raise
        except (LLMError, InfrastructureUnavailableError) as error:
            log_event(logger, WARNING, "graph.read.vector_unavailable", error_code=error.code)
            return None
        return await self._nearest(project_id, vectors[0], matching, limit=limit, depth=depth, filters=filters)

    async def _matching_vectors(self, project_id: str, filters: Mapping[str, Any]) -> int:
        """How many candidates have a vector of the gateway's embedding model (0 without a gateway)."""

        if self._gateway is None:
            return 0
        model = self._gateway.config.embedding_model
        counts = await self._executor.read(COUNT_SEARCH_VECTORS, project_id, model=model, **filters)
        matching, stored = counts[0]["matching"], counts[0]["stored"]
        if not matching and stored:
            log_event(logger, WARNING, "graph.read.embedding_model_mismatch", vectors=stored)
        return int(matching)

    async def _nearest(
        self,
        project_id: str,
        vector: Sequence[float],
        matching: int,
        *,
        limit: int,
        depth: int,
        filters: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Up to ``depth`` candidates nearest ``vector`` (``uuid``, ``spec_id``, ``score``), best first: the shared
        index above 2000 candidates while it yields ``limit`` project hits, else exact cosine."""

        model = self._gateway.config.embedding_model if self._gateway is not None else None
        params = {"vector": list(vector), "model": model, "limit": depth, **filters}
        if matching > EXACT_VECTOR_MAX:
            k = depth
            fetched = 0
            hits: list[dict[str, Any]] = []
            for _ in range(VECTOR_DOUBLINGS + 1):
                rows = await self._executor.read(
                    SEARCH_NODES_VECTOR, project_id, index=SPEC_NODE_EMBEDDING_INDEX, k=k, **params
                )
                fetched, hits = rows[0]["fetched"], rows[0]["hits"]
                if len(hits) >= limit:
                    return hits
                if fetched < k:
                    break
                k *= 2
            log_event(logger, DEBUG, "graph.read.vector_exact", fetched=fetched, project_hits=len(hits))
        return await self._executor.read(SEARCH_NODES_VECTOR_EXACT, project_id, **params)

    # Helpers of similar, evidence and expand

    def _trusted_hop(self, project_id: str) -> HopRead:
        """Reads the other ends of the ``stated`` and ``implied`` domain edges at a frontier, either way."""

        async def read(uuids: list[str]) -> list[tuple[str, HopNode]]:
            rows = await self._executor.read(
                READ_TRACE_HOP,
                project_id,
                frontier=uuids,
                relations=list(self._domain_relations),
                labels=list(self._spec_labels),
            )
            return [
                (
                    row["frontier"],
                    HopNode(row["uuid"], row["spec_id"], self._spec_label(row["labels"]) in self._spine_roots),
                )
                for row in rows
                if row["basis"] != INFERRED
            ]

        return read

    async def _resolve(self, project_id: str, spec_ids: Sequence[str]) -> tuple[list[GraphNode], tuple[str, ...]]:
        """The nodes ``spec_ids`` name (own id or alias), by ``spec_id``, and the ids naming none."""

        wanted = list(dict.fromkeys(spec_ids))
        rows = await self._executor.read(
            READ_NODE_UUIDS_BY_SPEC_ID, project_id, spec_ids=wanted, labels=list(self._spec_labels)
        )
        nodes = (
            self._nodes(
                await self._executor.read(
                    READ_NODES_BY_UUID, project_id, uuids=[row["uuid"] for row in rows], retired=RETIRED
                )
            )
            if rows
            else []
        )
        named = {name for node in nodes for name in (node.spec_id, *node.spec_id_aliases)}
        return nodes, tuple(spec_id for spec_id in wanted if spec_id not in named)

    async def _nodes_by_spec_id(self, project_id: str, spec_ids: Sequence[str]) -> dict[str, GraphNode]:
        if not spec_ids:
            return {}
        rows = await self._executor.read(
            READ_NODE_UUIDS_BY_SPEC_ID, project_id, spec_ids=list(spec_ids), labels=list(self._spec_labels)
        )
        nodes = await self._nodes_by_uuid(project_id, [row["uuid"] for row in rows])
        return {node.spec_id: node for node in nodes}

    async def _linked(self, project_id: str, sources: Sequence[GraphNode]) -> set[str]:
        """The nodes with a domain edge of any basis, either way, to one of ``sources``; one read."""

        rows = await self._executor.read(
            READ_TRACE_HOP,
            project_id,
            frontier=[node.uuid for node in sources],
            relations=list(self._domain_relations),
            labels=list(self._spec_labels),
        )
        return {row["spec_id"] for row in rows}

    async def _vector_scores(
        self, project_id: str, sources: Sequence[GraphNode], depth: int
    ) -> tuple[list[tuple[str, str, float]], dict[str, str]]:
        """``(source, spec_id, score)`` for up to ``depth`` nearest candidates of each source's stored vector of the
        gateway's model, and the uuid of each hit; nothing without a gateway or such vectors."""

        filters = self._filters(self._spec_labels, None)
        matching = await self._matching_vectors(project_id, filters) if sources else 0
        if not matching or self._gateway is None:
            return [], {}
        rows = await self._executor.read(
            READ_NODE_VECTORS,
            project_id,
            uuids=[node.uuid for node in sources],
            model=self._gateway.config.embedding_model,
        )
        hits: list[tuple[str, str, float]] = []
        uuids: dict[str, str] = {}
        for row in rows:
            for hit in await self._nearest(
                project_id, row["embedding"], matching, limit=depth, depth=depth, filters=filters
            ):
                hits.append((row["spec_id"], hit["spec_id"], float(hit["score"])))
                uuids[hit["spec_id"]] = hit["uuid"]
        return hits, uuids

    async def _centroid_scores(self, project_id: str, seeds: Sequence[GraphNode], depth: int) -> dict[str, float]:
        """The vector score of up to ``depth`` nearest candidates of the seeds' centroid: the mean of their stored
        vectors of the gateway's model; one nearest read. Nothing without a gateway or such vectors."""

        filters = self._filters(self._spec_labels, None)
        matching = await self._matching_vectors(project_id, filters)
        if not matching or self._gateway is None:
            return {}
        rows = await self._executor.read(
            READ_NODE_VECTORS,
            project_id,
            uuids=[node.uuid for node in seeds],
            model=self._gateway.config.embedding_model,
        )
        centroid = mean_vector([row["embedding"] for row in rows])
        if centroid is None:
            return {}
        hits = await self._nearest(project_id, centroid, matching, limit=depth, depth=depth, filters=filters)
        return {hit["spec_id"]: float(hit["score"]) for hit in hits}

    async def _graph_ranks(self, project_id: str, seeds: Sequence[GraphNode]) -> dict[str, float]:
        """Personalised PageRank from ``seeds`` over the trusted domain edges within 3 hops of them."""

        starts = [HopNode(node.uuid, node.spec_id, node.label in self._spine_roots) for node in seeds]
        near = await subgraph(self._trusted_hop(project_id), starts)
        return personalised_pagerank(near.nodes, near.neighbours, [node.spec_id for node in seeds], near.roots)

    async def _evidence_item(
        self,
        project_id: str,
        node: GraphNode,
        record: Provenance,
        context_lines: int,
        documents: dict[str, list[FragmentLines]],
    ) -> EvidenceItem:
        document_id = record.document_id
        fragments: list[FragmentLines] = []
        if document_id is not None:
            if document_id not in documents:
                documents[document_id] = [
                    FragmentLines.model_validate(fragment.model_dump())
                    for fragment in await self.fragments(project_id, document_id)
                ]
            fragments = documents[document_id]
        own = next((fragment for fragment in fragments if fragment.fragment_id == record.fragment_id), None)
        start = record.line_start if record.line_start is not None else own.line_start if own else None
        end = record.line_end if record.line_end is not None else own.line_end if own else None
        item = EvidenceItem(
            document_id=document_id,
            path=record.path,
            fragment_id=record.fragment_id,
            line_start=start,
            line_end=end,
            quote=record.quote or (own.text if own else ""),
        )
        if document_id is None or start is None or end is None or not 1 <= start <= end:
            return item
        quote, before, after = quote_lines(
            fragment_lines(own) if own else {}, document_lines(fragments), start, end, context_lines
        )
        others = await self.nodes_for_fragment(project_id, document_id, start, end)
        return item.model_copy(
            update={
                "quote": quote if quote is not None else item.quote,
                "before": before,
                "after": after,
                "others": tuple(
                    EvidenceNode(spec_id=other.spec_id, label=other.label, name=other.name)
                    for other in others
                    if other.spec_id != node.spec_id
                ),
            }
        )

    async def _nodes_by_uuid(self, project_id: str, uuids: Sequence[str]) -> list[GraphNode]:
        if not uuids:
            return []
        rows = await self._executor.read(READ_NODES_BY_UUID, project_id, uuids=list(uuids), retired=RETIRED)
        return await self._inherited(project_id, self._nodes(rows))

    async def _inherited(self, project_id: str, nodes: list[GraphNode]) -> list[GraphNode]:
        """``nodes`` with each null inheriting attribute filled from the node's owner one ``via`` hop away (owner-pair
        targets, lowest ``owner_rank`` then ``spec_id`` first; the first owner whose label stores the attribute gives
        its value, null stays null).
        """

        requests = self._inherit_requests(nodes)
        if not requests:
            return nodes
        rows = await self._executor.read(READ_OWNER_VALUES, project_id, requests=requests)
        nodes_by_uuid = {node.uuid: node for node in nodes}
        found = self._inherited_values(nodes_by_uuid, self._owner_candidates(nodes_by_uuid, rows))
        if not found:
            return nodes
        return [self._with_values(node, found[node.uuid]) if node.uuid in found else node for node in nodes]

    def _inherit_requests(self, nodes: Sequence[GraphNode]) -> list[dict[str, str | list[str]]]:
        """One owner-values request per node and ``via`` relation with a null inheriting attribute."""

        requests: list[dict[str, str | list[str]]] = []
        for node in nodes:
            by_via: dict[str, list[str]] = {}
            for name, via in self._inherit.get(node.label, {}).items():
                if node.attributes.get(name) is None:
                    by_via.setdefault(via, []).append(name)
            if node.uuid:
                requests.extend(
                    {"uuid": node.uuid, "relation": via, "attributes": names} for via, names in sorted(by_via.items())
                )
        return requests

    def _owner_candidates(
        self, nodes_by_uuid: Mapping[str, GraphNode], rows: Sequence[Mapping[str, Any]]
    ) -> dict[tuple[str, str], list[tuple[int, str, str, dict[str, Any]]]]:
        """Owner rows by ``(node uuid, via relation)``: ``(owner_rank, spec_id, label, values)``, owner pairs only."""

        owners: dict[tuple[str, str], list[tuple[int, str, str, dict[str, Any]]]] = {}
        for row in rows:
            owned = nodes_by_uuid.get(row["uuid"])
            label = self._spec_label(row["labels"])
            if owned is None or label is None:
                continue
            pair = self._ontology.pair_rule(row["relation"], owned.label, label)
            if pair is not None and pair.owner:
                owners.setdefault((owned.uuid, row["relation"]), []).append(
                    (pair.owner_rank, row["spec_id"], label, dict(row["attribute_values"]))
                )
        return owners

    def _inherited_values(
        self,
        nodes_by_uuid: Mapping[str, GraphNode],
        owners: Mapping[tuple[str, str], list[tuple[int, str, str, dict[str, Any]]]],
    ) -> dict[str, dict[str, Any]]:
        """Inherited values by node uuid: per null attribute, the first owner (by rank, then ``spec_id``)
        whose label stores it gives its value; a null there stays null."""

        found: dict[str, dict[str, Any]] = {}
        for (uuid, via), candidates in owners.items():
            node = nodes_by_uuid[uuid]
            candidates.sort(key=lambda owner: (owner[0], owner[1]))
            for name, rule in self._inherit[node.label].items():
                if rule != via or node.attributes.get(name) is not None:
                    continue
                stored = [values for _, _, label, values in candidates if self._stores(label, name)]
                if stored and stored[0].get(name) is not None:
                    found.setdefault(uuid, {})[name] = stored[0][name]
        return found

    def _with_values(self, node: GraphNode, values: Mapping[str, Any]) -> GraphNode:
        """``node`` with ``values`` added to its attributes, in the label's declared attribute order."""

        merged = {**node.attributes, **values}
        order = list(self._ontology.attribute_specs(node.label))
        attributes = {name: merged[name] for name in order if name in merged}
        return node.model_copy(update={"attributes": attributes})

    def _stores(self, label: str, name: str) -> bool:
        spec = self._ontology.attribute_specs(label).get(name)
        return spec is not None and spec.store

    def _spec_label(self, labels: Sequence[str]) -> str | None:
        """The one spec label among ``labels``, else None."""

        found = [label for label in labels if label in self._spec_labels]
        return found[0] if len(found) == 1 else None

    def _scope_paths(self) -> tuple[Query, ...]:
        """One scope query per spine path: a ``via`` path with its hop bounds; every contiguous part
        of a ``sequence`` path (for a module, ``REALIZED_BY`` alone gives the capabilities realising
        it). Only build-owned relations are rendered."""

        queries: dict[tuple[str, Any], Query] = {}
        for path in self._ontology.spine.paths:
            relations = tuple(path.relations)
            if not relations or not set(relations) <= set(self._domain_relations):
                raise ConfigurationError("spine_relation_not_domain")
            if not path.sequence:
                query = scope_via(tuple(sorted(set(relations))), path.min_hops, path.max_hops)
                queries[query.identity] = query
                self._scope_params[query.name] = {"max_hops": path.max_hops}
                continue
            symmetric = tuple(name for name in self._symmetric if name in relations)
            for start in range(len(relations)):
                for end in range(start + 1, len(relations) + 1):
                    query = scope_sequence(relations[start:end], symmetric)
                    queries[query.identity] = query
        return tuple(queries.values())

    # Validation

    def _labels(self, labels: Sequence[str] | None) -> tuple[str, ...]:
        if labels is None:
            return self._spec_labels
        if isinstance(labels, str):
            labels = [labels]
        unknown = sorted(set(labels) - set(self._spec_labels))
        if unknown:
            raise ConfigurationError("unknown_label", labels=",".join(unknown))
        return tuple(dict.fromkeys(labels))

    def _relations(self, relations: Sequence[str] | None) -> tuple[str, ...]:
        if relations is None:
            return self._domain_relations
        if isinstance(relations, str):
            relations = [relations]
        unknown = sorted(set(relations) - set(self._domain_relations))
        if unknown:
            raise ConfigurationError("unknown_relation", relations=",".join(unknown))
        return tuple(dict.fromkeys(relations))

    def _filters(self, labels: Sequence[str], where: Mapping[str, AttributeValue] | None) -> dict[str, Any]:
        """The node filter parameters: ``labels``, the checked ``where`` and ``defaulted``."""

        conditions = self._where(labels, where or {})
        defaulted = {
            name: found
            for name, value in conditions.items()
            if (
                found := [
                    label
                    for label in labels
                    if (default := self._ontology.attribute_default(label, name)) is not None
                    and (value is None or default == value)
                ]
            )
        }
        return {"labels": list(labels), "where": conditions, "defaulted": defaulted}

    def _where(self, labels: Iterable[str], where: Mapping[str, AttributeValue]) -> dict[str, AttributeValue]:
        specs: dict[str, list[AttributeSpec]] = {}
        for label in labels:
            for name, spec in self._ontology.attribute_specs(label).items():
                if spec.store:
                    specs.setdefault(name, []).append(spec)
        for name, value in where.items():
            if name not in specs:
                raise ConfigurationError("unknown_attribute", attribute=str(name))
            enums = [spec for spec in specs[name] if spec.kind == "enum"]
            if value is not None and enums and all(value not in spec.values for spec in enums):
                raise ConfigurationError("invalid_attribute_value", attribute=name)
        return dict(where)

    # Mapping records to models

    def _nodes(self, rows: Sequence[Mapping[str, Any]]) -> list[GraphNode]:
        """Rows as nodes; a node without exactly one spec domain label is skipped and counted."""

        nodes = []
        skipped = 0
        for row in rows:
            labels = [label for label in row["labels"] if label in self._spec_labels]
            if len(labels) != 1:
                skipped += 1
                continue
            nodes.append(self._node(labels[0], row["props"], row["provenance"]))
        if skipped:
            log_event(logger, WARNING, "graph.read.node_skipped", skipped=skipped, returned=len(nodes))
        return nodes

    def _node(self, label: str, props: Mapping[str, Any], provenance: Sequence[Mapping[str, Any] | None]) -> GraphNode:
        statements = _models(Statement, props.get("statements"))
        quotes = {item.fragment_id: item.text for item in statements}
        attributes = {
            name: props[name]
            for name, spec in self._ontology.attribute_specs(label).items()
            if spec.store and props.get(name) is not None
        }
        records = sorted(
            (
                Provenance(
                    document_id=item.get("document_id"),
                    path=item.get("path"),
                    fragment_id=item["fragment_id"],
                    line_start=item.get("line_start"),
                    line_end=item.get("line_end"),
                    quote=quotes.get(item["fragment_id"]),
                )
                for item in provenance
                if item and item.get("fragment_id")
            ),
            key=lambda record: (record.path or "", record.fragment_id, record.line_start or 0),
        )
        return GraphNode(
            spec_id=props["spec_id"],
            uuid=props.get("uuid", ""),
            label=label,
            name=props.get("name") or "",
            last_build_id=props.get("last_build_id"),
            spec_id_aliases=tuple(props.get("spec_id_aliases") or ()),
            attributes=attributes,
            statements=statements,
            provenance=tuple(records),
        )


def _is_int_between(value: object, low: int, high: int) -> bool:
    """Whether ``value`` is an ``int`` (not a ``bool``) within ``low``..``high``."""

    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _is_depth(value: object) -> bool:
    return _is_int_between(value, IMPACT_DEPTHS[0], IMPACT_DEPTHS[1])


def _is_fraction(value: object) -> bool:
    """Whether ``value`` is a number (not a ``bool``) within 0..1."""

    return isinstance(value, int | float) and not isinstance(value, bool) and 0.0 <= value <= 1.0


def _is_blank(text: object) -> bool:
    """Whether ``text`` is not a string or holds only whitespace."""

    return not isinstance(text, str) or not text.strip()


def _check_ids(spec_ids: object, maximum: int, *, minimum: int = 1, code: str = "invalid_spec_ids") -> None:
    """``minimum`` to ``maximum`` string ids, else ``code``."""

    if (
        isinstance(spec_ids, str)
        or not isinstance(spec_ids, Sequence)
        or not _is_int_between(len(spec_ids), minimum, maximum)
        or not all(isinstance(item, str) for item in spec_ids)
    ):
        raise ConfigurationError(code, maximum=maximum)


def _expansion_item(node: GraphNode, score: float, via: Via) -> ExpansionItem:
    """A ranked node with its text (its ``stmt``, else the quote of its first provenance record) and tokens."""

    stmt = node.attributes.get("stmt")
    text = stmt if isinstance(stmt, str) and stmt else next((p.quote for p in node.provenance if p.quote), "")
    return ExpansionItem(
        spec_id=node.spec_id,
        label=node.label,
        name=node.name,
        score=score,
        via=via,
        text=text,
        tokens=estimate_tokens(node.name, text),
    )


def _check_limit(limit: object) -> None:
    if not _is_int_between(limit, 1, MAX_SEARCH_LIMIT):
        raise ConfigurationError("invalid_limit", maximum=MAX_SEARCH_LIMIT)


def _match_kind(kinds: set[str]) -> MatchKind:
    if len(kinds) > 1:
        return "both"
    return "vector" if "vector" in kinds else "fulltext"


def _query_terms(text: str) -> tuple[list[str], int]:
    """The terms of ``text`` as the standard analyzer splits it, cut to ``MAX_QUERY_TERMS`` terms and
    ``MAX_QUERY_CHARS`` term characters, and the end offset in ``text`` of the last term kept."""

    terms: list[str] = []
    chars = end = 0
    for match in _QUERY_TERM.finditer(text):
        term = match.group(0)
        chars += len(term)
        if len(terms) >= MAX_QUERY_TERMS or chars > MAX_QUERY_CHARS:
            break
        terms.append(term)
        end = match.end()
    return terms, end


def _lucene_query(text: str) -> str:
    """``text`` as a Lucene query of plain terms (any term may match): English stop words left out,
    operator words lower-cased. Only stop words: an empty query (no fulltext hits)."""

    terms, _ = _query_terms(text)
    return " ".join(
        term.lower() if term in _LUCENE_OPERATORS else term for term in terms if term.lower() not in _STOP_WORDS
    )


def _embedding_text(text: str) -> str:
    """The search text that is embedded: ``text`` up to the fulltext cut (its last kept term), at
    most ``MAX_QUERY_CHARS`` characters, so search never sends an over-long input."""

    terms, end = _query_terms(text)
    cut = text[:end] if terms else text
    return cut.strip()[:MAX_QUERY_CHARS]


def _edge(row: Mapping[str, Any]) -> GraphEdge:
    props = row["props"]
    return GraphEdge(
        relation=row["relation"],
        source=row["source"],
        target=row["target"],
        edge_key=props.get("edge_key"),
        basis=props.get("basis"),
        fact=props.get("fact"),
        source_fragment_ids=tuple(props.get("source_fragment_ids") or ()),
        inference_rule=props.get("inference_rule"),
        confidence=props.get("confidence"),
        evidence=_models(EvidenceQuote, props.get("evidence")),
    )


def _models[ModelT: _Frozen](model: type[ModelT], value: Any) -> tuple[ModelT, ...]:
    """Entries of a JSON list property that validate as ``model``; malformed entries are skipped."""

    items = []
    for item in _json_list(value):
        try:
            items.append(model.model_validate(item))
        except ValidationError:
            continue
    return tuple(items)


def _json_list(value: Any) -> list[Any]:
    """A JSON list property (stored as a string) as a list; anything else as an empty list."""

    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []
