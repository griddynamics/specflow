"""Detection on the shop gap corpus for offline tests: a fake reader with trace, the build facts of ``graph.yaml``
and one call that runs the steps ``relevance``, ``detect`` and ``sites`` (docs/graph/gap/detection.md).

``TracingReader`` adds ``trace`` to the fixture reader: the directed walk by impact roles of the core reader
(``docs/graph/trace.md``), without hubs and owners, which is all the site ranking needs offline. The live tests
use the core ``GraphReader``.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from gap_acceptance_support import GapCorpusGraph, RecordedGapGateway, gap_corpus_graph
from gap_port_fakes import MemoryGapRecords
from graph.core import Ontology, load_ontology
from graph.core.ontology.compiled import COUPLED, RELATED, USED_BY, USES
from graph.core.reader import EvidenceQuote, GraphDocument, GraphEdge, GraphFragment, GraphNode, Provenance
from graph.core.trace import Direction, TraceNode, TraceResult
from graph.gap.models import BuildFacts, CutEdge, RefusedPair
from graph.gap.names import RunMode
from graph.gap.ports import PlanView
from graph.gap.rules import GapRules, load_gap_rules
from graph.gap.steps.detect import DetectOutcome, detect
from graph.gap.steps.relevance import RelevanceOutcome, classify_sections
from graph.gap.steps.sites import ExistingGap, SiteRun, SitesOutcome, form_sites
from graph.gap.steps.snapshot import GraphSnapshot, read_snapshot
from graph_plan_support import fixture_graph

PROJECT = "shop"
RUN_ID = "run-1"


@dataclass(frozen=True)
class TracingReader:
    """A reader over fixture parts (``nodes``, ``edges``, ``documents``, ``fragments``) with an in-memory ``trace``."""

    base: Any
    ontology: Ontology

    async def nodes(self, project_id: str, labels: Sequence[str] | None = None, where: Any = None) -> list[Any]:
        return await self.base.nodes(project_id, labels, where)

    async def edges(self, project_id: str, *, include_inferred: bool = True) -> list[GraphEdge]:
        return await self.base.edges(project_id, include_inferred=include_inferred)

    async def documents(self, project_id: str) -> list[Any]:
        return await self.base.documents(project_id)

    async def fragments(self, project_id: str, document_id: str | None = None) -> list[Any]:
        return await self.base.fragments(project_id, document_id)

    async def trace(
        self, project_id: str, spec_ids: Sequence[str], *, direction: Direction, depth: int = 2, **_: Any
    ) -> TraceResult:
        nodes = {node.spec_id: node for node in await self.nodes(project_id)}
        edges = await self.edges(project_id)
        seen = set(spec_ids)
        frontier = [spec_id for spec_id in spec_ids if spec_id in nodes]
        found: list[TraceNode] = []
        for hop in range(1, depth + 1):
            reached: dict[str, TraceNode] = {}
            for edge in edges:
                for start, end, outgoing in ((edge.source, edge.target, True), (edge.target, edge.source, False)):
                    if start not in frontier or end in seen or end in reached:
                        continue
                    role = self.ontology.impact_role(edge.relation, nodes[edge.source].label, nodes[edge.target].label)
                    if not _walks(role, outgoing=outgoing, direction=direction):
                        continue
                    node = nodes[end]
                    reached[end] = TraceNode(
                        spec_id=end,
                        label=node.label,
                        name=node.name,
                        hops=hop,
                        parent=start,
                        relation=edge.relation,
                        step="forward" if outgoing else "backward",
                        role=role,
                        basis=edge.basis or "stated",
                    )
            seen |= set(reached)
            frontier = sorted(reached)
            found += [reached[spec_id] for spec_id in frontier]
        return TraceResult(direction=direction, sources=tuple(spec_ids), nodes=tuple(found))


def _walks(role: str, *, outgoing: bool, direction: str) -> bool:
    if role in (COUPLED, RELATED):
        return True
    if role == USES:
        return outgoing == (direction == "based_on")
    if role == USED_BY:
        return outgoing == (direction == "affects")
    return False


def build_facts(graph: GapCorpusGraph | None = None, cut_edges: Sequence[CutEdge] = ()) -> BuildFacts:
    """The build facts of ``graph.yaml``: its dedup refusals (and the given cycle cuts)."""

    found = graph or gap_corpus_graph()
    return BuildFacts(
        build_id=found.build_id,
        refused_pairs=tuple(
            RefusedPair(
                first_spec_id=item.first_spec_id or "",
                second_spec_id=item.second_spec_id or "",
                label=item.label,
                guard=item.guard,
            )
            for item in found.dedup_refused
        ),
        cut_edges=tuple(cut_edges),
    )


@dataclass
class Detection:
    """The outcome of the three detection steps on one graph, and what they used."""

    snapshot: GraphSnapshot
    relevance: RelevanceOutcome
    detection: DetectOutcome
    sites: SitesOutcome
    records: MemoryGapRecords
    gateway: RecordedGapGateway | None
    rules: GapRules

    def seeds_naming(self, spec_id: str) -> list[Any]:
        return [seed for seed in self.detection.seeds if spec_id in seed.about]

    def site_of(self, spec_id: str) -> Any:
        return next((site for site in self.sites.sites if spec_id in site.about), None)

    def relevance_of(self, document: str, heading: str) -> str:
        lineage = f"upload:{document.removesuffix('.md')}"
        for (found, path), item in self.relevance.sections.items():
            if found == lineage and path and path[-1] == heading:
                return item.relevance
        raise KeyError(f"{document} > {heading}")


@dataclass
class DetectionSetup:
    """The inputs of ``run_detection`` beyond the graph."""

    records: MemoryGapRecords = field(default_factory=MemoryGapRecords)
    gateway: RecordedGapGateway | None = field(default_factory=RecordedGapGateway)
    build: BuildFacts | None = None
    plan: PlanView | None = None
    existing: Sequence[ExistingGap] = ()
    mode: RunMode = "full"
    registry: Mapping[str, Any] | None = None
    deferred_first: Collection[str] = ()


async def run_detection(
    reader: Any = None, setup: DetectionSetup | None = None, *, run_id: str = RUN_ID, project_id: str = PROJECT
) -> Detection:
    """``relevance``, ``detect`` and ``sites`` on ``reader`` (default: the shop corpus graph with trace)."""

    ontology = load_ontology()
    rules = load_gap_rules(ontology=ontology)
    given = setup or DetectionSetup()
    source = reader or TracingReader(gap_corpus_graph().reader, ontology)
    snapshot = await read_snapshot(source, project_id, ontology)
    relevance = await classify_sections(
        snapshot, project_id=project_id, rules=rules, records=given.records, gateway=given.gateway
    )
    kwargs: dict[str, Any] = {} if given.registry is None else {"registry": given.registry}
    detection = detect(
        snapshot,
        rules=rules,
        relevance=relevance,
        build=given.build if given.build is not None else build_facts(),
        project_id=project_id,
        **kwargs,
    )
    sites = await form_sites(
        snapshot,
        detection,
        SiteRun(
            project_id=project_id,
            run_id=run_id,
            mode=given.mode,
            rules=rules,
            limits=rules.limits,
            records=given.records,
            reader=source,
            plan=given.plan,
            existing=given.existing,
            deferred_first=given.deferred_first,
        ),
    )
    return Detection(snapshot, relevance, detection, sites, given.records, given.gateway, rules)


# Small graphs for detector and site unit tests


@dataclass
class MiniGraph:
    """A small spec graph built in a test: documents of sections, nodes quoting one of their lines, edges."""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    documents: dict[str, GraphDocument] = field(default_factory=dict)
    fragments: list[GraphFragment] = field(default_factory=list)

    def section(self, heading: str, *lines: str, document: str = "spec.md", title: str = "Shop specification") -> str:
        """Add a section (one fragment) to ``document`` and return its fragment id."""

        lineage = f"upload:{document.removesuffix('.md')}"
        if lineage not in self.documents:
            self.documents[lineage] = GraphDocument(
                document_id=lineage, path=document, title=title, status="active", revision_id=f"REV::{lineage}::1"
            )
        start = 1 + sum(len(item.text.splitlines()) + 1 for item in self.fragments if item.document_id == lineage)
        fragment_id = f"{lineage}#{heading.casefold().replace(' ', '-')}#{len(self.fragments):04d}"
        self.fragments.append(
            GraphFragment(
                fragment_id=fragment_id,
                document_id=lineage,
                text="\n".join((f"## {heading}", *lines)),
                heading_path=(title, heading),
                ordinal=len(self.fragments),
                line_start=start,
                line_end=start + len(lines),
                status="active",
            )
        )
        return fragment_id

    def node(
        self,
        spec_id: str,
        label: str,
        name: str,
        *,
        quote: str | None = None,
        fragment: str | None = None,
        **attributes: Any,
    ) -> GraphNode:
        """Add a node; ``quote`` (a line of ``fragment``, default the last section) becomes its evidence."""

        provenance: list[Provenance] = []
        if quote is not None:
            fragment_id = fragment or self.fragments[-1].fragment_id
            found = next(item for item in self.fragments if item.fragment_id == fragment_id)
            offset = found.text.splitlines().index(quote)
            line = (found.line_start or 1) + offset
            provenance.append(
                Provenance(
                    document_id=found.document_id,
                    path=self.documents[found.document_id].path,
                    fragment_id=fragment_id,
                    line_start=line,
                    line_end=line,
                    quote=quote,
                )
            )
        node = GraphNode(
            spec_id=spec_id,
            uuid=f"uuid-{len(self.nodes):04d}",
            label=label,
            name=name,
            attributes=dict(attributes),
            provenance=tuple(provenance),
        )
        self.nodes.append(node)
        return node

    def edge(self, source: str, relation: str, target: str, *, evidence: Sequence[tuple[str, str]] = ()) -> None:
        self.edges.append(
            GraphEdge(
                relation=relation,
                source=source,
                target=target,
                basis="stated",
                evidence=tuple(EvidenceQuote(fragment_id=fragment, quote=quote) for fragment, quote in evidence),
            )
        )

    def snapshot(self) -> GraphSnapshot:
        return GraphSnapshot(load_ontology(), self.nodes, self.edges, self.documents.values(), self.fragments)

    def reader(self) -> TracingReader:
        return TracingReader(_ListReader(self), load_ontology())


@dataclass(frozen=True)
class _ListReader:
    graph: MiniGraph

    async def nodes(self, _project_id: str, labels: Sequence[str] | None = None, where: Any = None) -> list[GraphNode]:
        del where
        return [node for node in self.graph.nodes if not labels or node.label in labels]

    async def edges(self, _project_id: str, *, include_inferred: bool = True) -> list[GraphEdge]:
        del include_inferred
        return list(self.graph.edges)

    async def documents(self, _project_id: str) -> list[GraphDocument]:
        return list(self.graph.documents.values())

    async def fragments(self, _project_id: str, document_id: str | None = None) -> list[GraphFragment]:
        return [item for item in self.graph.fragments if document_id is None or item.document_id == document_id]


def corpus_reader(
    *,
    replace: Mapping[str, str] | None = None,
    append: Mapping[tuple[str, str], str] | None = None,
    nodes: Sequence[Mapping[str, Any]] = (),
    edges: Sequence[Mapping[str, Any]] = (),
) -> TracingReader:
    """A variant of the corpus graph: text replaced everywhere (``replace``), a line appended to a section
    (``append`` by document and heading; returns its line number through the node data the caller adds), and extra
    nodes and edges in the fixture format."""

    data = json.loads(json.dumps(gap_corpus_graph().data))
    for old, new in (replace or {}).items():
        data = _replaced(data, old, new)
    for (document, heading), line in (append or {}).items():
        lineage = f"upload:{document.removesuffix('.md')}"
        fragment = next(
            item for item in data["fragments"] if item["document_id"] == lineage and item["heading_path"][-1] == heading
        )
        fragment["text"] += f"\n{line}"
        fragment["line_end"] += 1
    data["nodes"] += [dict(item) for item in nodes]
    data["edges"] += [dict(item) for item in edges]
    return TracingReader(fixture_graph(data, load_ontology()), load_ontology())


def _replaced(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [_replaced(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replaced(item, old, new) for key, item in value.items()}
    return value


def fragment_of(document: str, heading: str) -> Mapping[str, Any]:
    """The fixture data of a corpus section's fragment."""

    lineage = f"upload:{document.removesuffix('.md')}"
    return next(
        item
        for item in gap_corpus_graph().data["fragments"]
        if item["document_id"] == lineage and item["heading_path"][-1] == heading
    )
