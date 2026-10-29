"""Reads of the project graph (docs/graph/api.md "Read and search").

``GraphReader`` returns spec nodes only: nodes with ``SpecNode`` and a domain label owned by
``build``. Documents, fragments and nodes of other owners are never returned as spec nodes
(FR-READ-06). A ``spec_id`` resolves directly or through ``spec_id_aliases`` (FR-READ-01).
Domain edges are the build-owned ontology relations between spec nodes.

Search (``find_nodes``, ``find_fragments``) runs on database-wide indexes, so hits are filtered by
project, label and attributes after the index call. Fulltext pages through the index in its score
order (page sizes doubling) until enough project hits are found or the index has no more matches.
Vector search ranks a small candidate set by exact cosine at once; otherwise it asks the shared
index, doubling the fetch while too few project hits come back, and falls back to exact cosine over
the project's own vectors when the index still does not yield enough. Only vectors made by the
gateway's embedding model are compared. Query text is never logged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from graph.core.db import Executor
from graph.core.errors import (
    ConfigurationError,
    ConformanceError,
    InfrastructureUnavailableError,
    LLMError,
    LLMFatalError,
)
from graph.core.identity import (
    explicit_id_pattern,
    identity_key,
    normalise_id,
    validate_project_id,
)
from graph.core.ontology import AttributeSpec, Ontology
from graph.core.queries import (
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
    READ_NODES,
    READ_NODES_BY_IDS,
    READ_NODES_BY_UUID,
    READ_NODES_FOR_LINES,
    SEARCH_FRAGMENTS,
    SEARCH_NODES_FULLTEXT,
    SEARCH_NODES_VECTOR,
    SEARCH_NODES_VECTOR_EXACT,
    Query,
    scope_sequence,
    scope_via,
)
from graph.core.schema import FRAGMENT_TEXT_INDEX, SPEC_NODE_EMBEDDING_INDEX, SPEC_NODE_TEXT_INDEX
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
_PROCEDURE_FAILED: Final = "Neo.ClientError.Procedure.ProcedureCallFailed"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class Statement(_Frozen):
    """What one supporting fragment says about a node (data-model.md "Statements")."""

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


class FragmentHit(_Frozen):
    """A fulltext hit on an ``active`` fragment, with its document and Lucene score."""

    fragment: GraphFragment
    document: GraphDocument | None
    score: float


class ImpactResult(_Frozen):
    """Nodes reachable from ``sources`` (resolved ``spec_id``s), nearest first then by ``spec_id``.

    ``distance`` maps each returned node's ``spec_id`` to its hop count; ``truncated``: more nodes
    were reachable than returned.
    """

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
        self._executor = (
            driver_or_executor if isinstance(driver_or_executor, Executor) else Executor(driver_or_executor, database)
        )
        self._ontology = ontology
        self._gateway = gateway
        self._spec_labels = tuple(label for label in ontology.labels if ontology.owner_of(label) == BUILD)
        self._domain_relations = tuple(sorted(name for name in ontology.relations if ontology.owner_of(name) == BUILD))
        self._symmetric = tuple(name for name in self._domain_relations if ontology.is_symmetric(name))
        self._spine_roots = tuple(sorted(ontology.spine.roots))
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
        return nodes[0] if nodes else None

    async def nodes(
        self,
        project_id: str,
        labels: Sequence[str] | None = None,
        where: Mapping[str, AttributeValue] | None = None,
    ) -> list[GraphNode]:
        """All spec nodes, ordered by ``spec_id``; filtered by ``labels`` and ``where`` (equality).

        A label that is not a spec label, an attribute that no selected label declares (or never
        stores), or an enum value not declared raises ``ConfigurationError`` (FR-READ-08).
        """

        project_id = validate_project_id(project_id)
        selected = self._labels(labels)
        conditions = self._where(selected, where or {})
        rows = await self._executor.read(
            READ_NODES, project_id, labels=list(selected), where=conditions, retired=RETIRED
        )
        return self._nodes(rows)

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
        """The nodes under a node: those reaching it by a declared spine path (or a contiguous part
        of a ``sequence`` path), each edge in its direction (a symmetric relation either way), never
        through a spine-root node; ordered by ``spec_id`` (FR-READ-07)."""

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
        """Nodes reachable over domain edges in both directions within ``depth`` (1-3) hops of any
        of ``spec_ids`` (aliases resolve, unknown ids are ignored), the start nodes left out; nearest
        first, at most 500, ``truncated`` when more were reachable (FR-READ-05)."""

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

    async def nodes_for_fragment(
        self, project_id: str, document_id: str, line_start: int, line_end: int
    ) -> list[GraphNode]:
        """Spec nodes whose evidence lines in the document overlap ``line_start``..``line_end``
        (inclusive, lines of the built revision), ordered by ``spec_id`` (FR-READ-04)."""

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
        return self._nodes(rows)

    async def find_nodes(
        self,
        project_id: str,
        text: str,
        labels: Sequence[str] | None = None,
        where: Mapping[str, AttributeValue] | None = None,
        limit: int = 20,
    ) -> list[NodeHit]:
        """Hybrid search: the vector and fulltext lists fused by weighted reciprocal rank
        (FR-READ-02); fulltext only without a gateway or when embedding fails (FR-READ-03). A hit
        only the vector list found is dropped below ``vector_floor``. Ties by ``spec_id``.

        Nodes the text names by explicit id (identity key or identity alias) or by ``spec_id`` (own
        or alias) come first, in the order the text names them, ``matched="id"``; the fused hits
        follow without them. Labels, ``where`` and the project apply to both; ``limit`` to all."""

        project_id = validate_project_id(project_id)
        _check_limit(limit)
        selected = list(self._labels(labels))
        conditions = self._where(selected, where or {})
        if _is_blank(text):
            return []
        depth = limit * OVERFETCH
        filters = {"labels": selected, "where": conditions}
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
        """Fulltext search over ``active`` fragment text, best score first (FR-READ-09)."""

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
        """Up to ``want`` project hits of the fulltext index in its score order (ties by ``key``).

        Pages of the index are read in score order, each twice the last, until ``want`` hits passed
        the query's filters or the index has no more matches. A query Lucene still rejects as too
        complex raises ``ConfigurationError("query_too_long")``.
        """

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
                if error.fields.get("neo4j_code") == _PROCEDURE_FAILED:
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
        """Up to ``depth`` project hits of the search vector in similarity order; None (fulltext
        only) when there is no gateway, no candidate has a vector of the gateway's embedding model,
        or embedding failed.

        At most ``EXACT_VECTOR_MAX`` candidates are ranked by exact cosine at once. Otherwise the
        shared index is asked for ``depth`` neighbours, doubled while fewer than ``limit`` of them
        pass the filters; when the index returns fewer than asked (an approximate index may do so
        before it is exhausted) or ``VECTOR_DOUBLINGS`` doublings did not suffice, exact cosine
        over the project's own vectors is used instead.
        """

        if self._gateway is None:
            return None
        model = self._gateway.config.embedding_model
        counts = await self._executor.read(COUNT_SEARCH_VECTORS, project_id, model=model, **filters)
        matching, stored = counts[0]["matching"], counts[0]["stored"]
        if not matching:
            if stored:
                log_event(logger, WARNING, "graph.read.embedding_model_mismatch", vectors=stored)
            return None
        try:
            vectors = await self._gateway.embed([_embedding_text(text)])
        except LLMFatalError:
            raise
        except (LLMError, InfrastructureUnavailableError) as error:
            log_event(logger, WARNING, "graph.read.vector_unavailable", error_code=error.code)
            return None
        params = {"vector": vectors[0], "model": model, "limit": depth, **filters}
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

    async def _nodes_by_uuid(self, project_id: str, uuids: Sequence[str]) -> list[GraphNode]:
        if not uuids:
            return []
        rows = await self._executor.read(READ_NODES_BY_UUID, project_id, uuids=list(uuids), retired=RETIRED)
        return self._nodes(rows)

    def _scope_paths(self) -> tuple[Query, ...]:
        """One scope query per spine path: a ``via`` path with its hop bounds; every contiguous part
        of a ``sequence`` path (for a module, ``REALIZED_BY`` alone gives the capabilities realising
        it). Only build-owned relations are rendered."""

        queries: dict[str, Query] = {}
        for path in self._ontology.spine.paths:
            relations = tuple(path.relations)
            if not relations or not set(relations) <= set(self._domain_relations):
                raise ConfigurationError("spine_relation_not_domain")
            if not path.sequence:
                query = scope_via(tuple(sorted(set(relations))), path.min_hops, path.max_hops)
                queries[query.text] = query
                self._scope_params[query.name] = {"max_hops": path.max_hops}
                continue
            symmetric = tuple(name for name in self._symmetric if name in relations)
            for start in range(len(relations)):
                for end in range(start + 1, len(relations) + 1):
                    query = scope_sequence(relations[start:end], symmetric)
                    queries[query.text] = query
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
