"""Detection time of graph.gap on a generated spec graph (NFR-GPERF-01: 5,000 spec nodes within 30 seconds, no model
call). Run through the cross-store benchmark: ``make graph-bench ARGS="gap-detect --nodes 5000"``.

The graph is generated in memory: per module one ``Module``, five features of seven requirements each, two
interfaces, two data entities, three quality requirements and two open questions (50 nodes), every node quoting a
line of its module's document; one module in ten is a tracking document (a sprint plan with ticket keys). Defects are
planted at fixed rates so every slice 1 detector has work: ownerless requirements, interfaces without a provider,
entities without a store, vague qualities, assumptions, refused dedup pairs and a dependency cycle.

The steps ``relevance`` (rules only, no model), ``detect`` and ``sites`` run against an in-memory reader whose
``trace`` walks impact roles over an adjacency index, and in-memory relational fakes (``tests/gap_port_fakes``), so
the time is graph.gap's own work, not a store's latency. The JSON summary (timings and counts, no text) goes to
stdout.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from gap_port_fakes import MemoryGapRecords
from graph.core import Ontology, load_ontology
from graph.core.ontology.compiled import COUPLED, RELATED, USED_BY, USES
from graph.core.reader import GraphDocument, GraphEdge, GraphFragment, GraphNode, Provenance
from graph.core.trace import Direction, TraceNode, TraceResult
from graph.gap.models import BuildFacts, CutEdge, RefusedPair
from graph.gap.rules import load_gap_rules
from graph.gap.steps.detect import detect
from graph.gap.steps.relevance import classify_sections
from graph.gap.steps.sites import SiteRun, form_sites
from graph.gap.steps.snapshot import read_snapshot

PROJECT: Final = "bench-gap"
NODES_PER_MODULE: Final = 50
FEATURES: Final = 5
REQUIREMENTS: Final = 7
TRACKING_EVERY: Final = 10
LIMIT_S: Final = 30.0


@dataclass
class GeneratedGraph:
    """A generated project graph: nodes, edges, documents, fragments and the build facts of its defects."""

    ontology: Ontology
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    documents: list[GraphDocument] = field(default_factory=list)
    fragments: list[GraphFragment] = field(default_factory=list)
    refused: list[RefusedPair] = field(default_factory=list)
    cuts: list[CutEdge] = field(default_factory=list)
    _out: dict[str, list[GraphEdge]] = field(default_factory=lambda: defaultdict(list))
    _in: dict[str, list[GraphEdge]] = field(default_factory=lambda: defaultdict(list))
    _labels: dict[str, str] = field(default_factory=dict)

    async def trace(
        self, _project_id: str, spec_ids: Sequence[str], *, direction: Direction, depth: int = 2, **_: Any
    ) -> TraceResult:
        seen = set(spec_ids)
        frontier = [spec_id for spec_id in spec_ids if spec_id in self._labels]
        found: list[TraceNode] = []
        for hop in range(1, depth + 1):
            reached: dict[str, TraceNode] = {}
            for start in frontier:
                for edge, outgoing in [(edge, True) for edge in self._out[start]] + [
                    (edge, False) for edge in self._in[start]
                ]:
                    end = edge.target if outgoing else edge.source
                    if end in seen or end in reached:
                        continue
                    role = self.ontology.impact_role(
                        edge.relation, self._labels[edge.source], self._labels[edge.target]
                    )
                    if not _walks(role, outgoing=outgoing, direction=direction):
                        continue
                    reached[end] = TraceNode(
                        spec_id=end,
                        label=self._labels[end],
                        name=end,
                        hops=hop,
                        parent=start,
                        relation=edge.relation,
                        step="forward" if outgoing else "backward",
                        role=role,
                        basis="stated",
                    )
            seen |= set(reached)
            frontier = sorted(reached)
            found += [reached[spec_id] for spec_id in frontier]
        return TraceResult(direction=direction, sources=tuple(spec_ids), nodes=tuple(found))

    def index(self) -> None:
        self._labels = {node.spec_id: node.label for node in self.nodes}
        for edge in self.edges:
            self._out[edge.source].append(edge)
            self._in[edge.target].append(edge)


@dataclass(frozen=True)
class _Reader:
    graph: GeneratedGraph

    async def nodes(self, _project_id: str, labels: Any = None, where: Any = None) -> list[GraphNode]:
        del labels, where
        return list(self.graph.nodes)

    async def edges(self, _project_id: str, *, include_inferred: bool = True) -> list[GraphEdge]:
        del include_inferred
        return list(self.graph.edges)

    async def documents(self, _project_id: str) -> list[GraphDocument]:
        return list(self.graph.documents)

    async def fragments(self, _project_id: str, document_id: str | None = None) -> list[GraphFragment]:
        del document_id
        return list(self.graph.fragments)

    async def trace(self, project_id: str, spec_ids: Sequence[str], **kwargs: Any) -> TraceResult:
        return await self.graph.trace(project_id, spec_ids, **kwargs)


def _walks(role: str, *, outgoing: bool, direction: str) -> bool:
    if role in (COUPLED, RELATED):
        return True
    if role == USES:
        return outgoing == (direction == "based_on")
    if role == USED_BY:
        return outgoing == (direction == "affects")
    return False


class _Document:
    """Lines of one generated document, grouped in sections."""

    def __init__(self, graph: GeneratedGraph, index: int, *, tracking: bool) -> None:
        self.graph = graph
        self.lineage = f"upload:doc-{index:04d}"
        self.title = f"Sprint {index}" if tracking else f"Module {index} specification"
        self.line = 1
        graph.documents.append(
            GraphDocument(document_id=self.lineage, path=f"doc-{index:04d}.md", title=self.title, status="active")
        )

    def section(self, heading: str, lines: Sequence[str]) -> str:
        fragment_id = f"{self.lineage}#{len(self.graph.fragments):06d}"
        self.graph.fragments.append(
            GraphFragment(
                fragment_id=fragment_id,
                document_id=self.lineage,
                text="\n".join((f"## {heading}", *lines)),
                heading_path=(self.title, heading),
                ordinal=len(self.graph.fragments),
                line_start=self.line,
                line_end=self.line + len(lines),
                status="active",
            )
        )
        self.line += len(lines) + 2
        return fragment_id

    def provenance(self, fragment_id: str, offset: int, quote: str) -> tuple[Provenance, ...]:
        start = next(item for item in self.graph.fragments if item.fragment_id == fragment_id).line_start or 1
        line = start + offset
        return (
            Provenance(
                document_id=self.lineage,
                path=None,
                fragment_id=fragment_id,
                line_start=line,
                line_end=line,
                quote=quote,
            ),
        )


def _add(
    graph: GeneratedGraph, spec_id: str, label: str, provenance: tuple[Provenance, ...] = (), **attributes: Any
) -> None:
    graph.nodes.append(
        GraphNode(
            spec_id=spec_id,
            uuid=f"u-{len(graph.nodes):06d}",
            label=label,
            name=spec_id.rsplit("::", 1)[-1].replace("_", " ").title(),
            attributes=attributes,
            provenance=provenance,
        )
    )


def _edge(graph: GeneratedGraph, source: str, relation: str, target: str) -> None:
    graph.edges.append(GraphEdge(relation=relation, source=source, target=target, basis="stated"))


def _module(graph: GeneratedGraph, number: int, modules: int) -> None:
    tracking = number % TRACKING_EVERY == TRACKING_EVERY - 1
    document = _Document(graph, number, tracking=tracking)
    module = f"MOD::M{number}"
    _add(graph, module, "Module")
    for feature in range(FEATURES):
        feature_id = f"FEAT::M{number}_F{feature}"
        lines = [
            f"SHOP-{number * 100 + feature * 10 + item} item {item} of feature {feature}."
            if tracking
            else f"Customers must use feature {feature} step {item} in module {number}."
            for item in range(REQUIREMENTS)
        ]
        fragment = document.section(f"Feature {feature}", lines)
        _add(graph, feature_id, "Feature")
        _edge(graph, feature_id, "PART_OF", module)
        for item, line in enumerate(lines):
            requirement = f"REQ::FR::M{number}_F{feature}_R{item}"
            ownerless = item == REQUIREMENTS - 1 and feature == 0
            criteria = [] if item % 3 else ["done when it works"]
            _add(
                graph,
                requirement,
                "FunctionalRequirement",
                document.provenance(fragment, item + 1, line),
                acceptance_criteria=criteria,
            )
            if not ownerless:
                _edge(graph, requirement, "PART_OF", feature_id)
    lines = ["The service should be fast.", "Pages load in 2 s.", "We assume every user has an account."]
    fragment = document.section("Qualities", lines)
    _add(graph, f"REQ::NFR::M{number}_FAST", "QualityRequirement", document.provenance(fragment, 1, lines[0]))
    _add(graph, f"REQ::NFR::M{number}_LOAD", "QualityRequirement", document.provenance(fragment, 2, lines[1]))
    _add(
        graph,
        f"REQ::CON::M{number}_ACCOUNT",
        "Constraint",
        document.provenance(fragment, 3, lines[2]),
        is_assumption="yes",
    )
    for spec_id in (f"REQ::NFR::M{number}_FAST", f"REQ::NFR::M{number}_LOAD", f"REQ::CON::M{number}_ACCOUNT"):
        _edge(graph, spec_id, "PART_OF", module)
    for item in range(2):
        interface, entity = f"IFC::M{number}_API{item}", f"ENT::M{number}_E{item}"
        _add(graph, interface, "Interface")
        _add(graph, entity, "DataEntity")
        if item == 0:
            _edge(graph, interface, "PROVIDED_BY", module)
            _edge(graph, entity, "STORED_IN", module)
        _edge(graph, interface, "CONSUMED_BY", f"MOD::M{(number + 1) % modules}")
    lines = [f"Who approves feature {item} of module {number}?" for item in range(2)]
    fragment = document.section("Open questions", lines)
    for item, line in enumerate(lines):
        question = f"QST::M{number}_Q{item}"
        _add(graph, question, "OpenQuestion", document.provenance(fragment, item + 1, line))
        _edge(graph, question, "RELATES_TO", f"REQ::FR::M{number}_F{item}_R0")
    graph.refused.append(
        RefusedPair(
            first_spec_id=f"REQ::FR::M{number}_F1_R1",
            second_spec_id=f"REQ::FR::M{number}_F2_R1",
            label="FunctionalRequirement",
            guard="numbers",
        )
    )
    _edge(graph, f"FEAT::M{number}_F3", "DEPENDS_ON", f"FEAT::M{number}_F4")
    graph.cuts.append(
        CutEdge(relation="DEPENDS_ON", source_spec_id=f"FEAT::M{number}_F4", target_spec_id=f"FEAT::M{number}_F3")
    )


def generate(nodes: int) -> GeneratedGraph:
    """A graph of about ``nodes`` spec nodes (a multiple of 50)."""

    graph = GeneratedGraph(load_ontology())
    modules = max(1, nodes // NODES_PER_MODULE)
    for number in range(modules):
        _module(graph, number, modules)
    graph.index()
    return graph


async def run_gap_detect_bench(nodes: int) -> dict[str, Any]:
    """Generate the graph, run relevance, detect and sites, and return timings and counts."""

    graph = generate(nodes)
    ontology = graph.ontology
    rules = load_gap_rules(ontology=ontology)
    records = MemoryGapRecords()
    reader = _Reader(graph)
    timings: dict[str, float] = {}
    started = time.perf_counter()
    snapshot = await read_snapshot(reader, PROJECT, ontology)
    timings["snapshot"] = time.perf_counter() - started
    step = time.perf_counter()
    relevance = await classify_sections(snapshot, project_id=PROJECT, rules=rules, records=records, gateway=None)
    timings["relevance"] = time.perf_counter() - step
    step = time.perf_counter()
    build = BuildFacts(build_id="bench", refused_pairs=tuple(graph.refused), cut_edges=tuple(graph.cuts))
    detection = detect(snapshot, rules=rules, relevance=relevance, build=build, project_id=PROJECT)
    timings["detect"] = time.perf_counter() - step
    step = time.perf_counter()
    sites = await form_sites(
        snapshot,
        detection,
        SiteRun(PROJECT, "bench-run", "full", rules, rules.limits, records, reader),
    )
    timings["sites"] = time.perf_counter() - step
    total = time.perf_counter() - started
    return {
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "sections": len(snapshot.sections),
        "seeds": len(detection.seeds),
        "notes": len(detection.notes),
        "detector_failures": dict(detection.failures),
        "sites": dict(sites.counts["sites"]),
        "model_calls": relevance.model_calls,
        "timings_s": {name: round(value, 3) for name, value in timings.items()},
        "total_s": round(total, 3),
        "limit_s": LIMIT_S,
        "ok": total <= LIMIT_S and relevance.model_calls == 0,
    }
