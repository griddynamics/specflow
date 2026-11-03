"""Shared plan test helpers: ontology/rules, the synthetic shop reader and build reports."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from graph.build.models import BuildResult, ChangedEdge
from graph.core import load_ontology
from graph.core.ontology import Ontology
from graph.core.queries import DOCUMENT, FRAGMENT
from graph.core.reader import GraphDocument, GraphEdge, GraphFragment, GraphNode
from graph.core.writer import NodeRef, OwnedWriter
from graph.plan.models import (
    BuildChangeReport,
    ChangedSpecEdge,
    ChangedSpecNode,
    MeaningRevisions,
    PlanOptions,
)
from graph.plan.options import PlanContext
from graph.plan.partition import partition
from graph.plan.rules import PlanRules, load_plan_rules
from graph.plan.snapshot import snapshot
from test_graph_schema import SchemaDriver

__all__ = [
    "SchemaDriver",
    "change_reports",
    "plan_ontology",
    "plan_rules",
    "shop_graph",
    "shop_partition",
    "shop_snapshot",
    "write_spec_graph",
]

_SHOP_FIXTURE: Path = Path(__file__).resolve().parent / "fixtures" / "graph" / "plan" / "shop.yaml"
_EMPTY_REVISIONS = MeaningRevisions()


def plan_ontology() -> Ontology:
    """The merged ontology the shipped plan rules target."""

    return load_ontology()


def plan_rules(ontology: Ontology | None = None) -> PlanRules:
    """The shipped plan rules, validated against the merged ontology."""

    return load_plan_rules(ontology=ontology or plan_ontology())


@dataclass(frozen=True)
class _ShopFixture:
    build_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    documents: tuple[GraphDocument, ...]
    fragments: tuple[GraphFragment, ...]


@dataclass(frozen=True)
class ShopGraphReader:
    """A project-independent fake of the part of ``GraphReader`` plan reads use."""

    _fixture: _ShopFixture
    _ontology: Ontology

    async def nodes(
        self,
        _project_id: str,
        labels: Sequence[str] | None = None,
        where: Mapping[str, Any] | None = None,
    ) -> list[GraphNode]:
        selected = self._labels(labels)
        filters = dict(where or {})
        found = [
            node
            for node in self._fixture.nodes
            if not selected or node.label in selected
            if all(node.attributes.get(key) == value for key, value in filters.items())
        ]
        return sorted(found, key=lambda node: node.spec_id)

    async def node(self, _project_id: str, spec_id: str) -> GraphNode | None:
        return next((node for node in self._fixture.nodes if node.spec_id == spec_id), None)

    async def neighbours(
        self, _project_id: str, spec_id: str, relations: Sequence[str] | None = None
    ) -> list[GraphEdge]:
        selected = self._relations(relations)
        return sorted(
            (
                edge
                for edge in self._fixture.edges
                if spec_id in (edge.source, edge.target) and (not selected or edge.relation in selected)
            ),
            key=lambda edge: (edge.source, edge.relation, edge.target),
        )

    async def edges(self, _project_id: str, *, include_inferred: bool = True) -> list[GraphEdge]:
        return sorted(
            (edge for edge in self._fixture.edges if include_inferred or edge.inference_rule is None),
            key=lambda edge: (edge.source, edge.relation, edge.target),
        )

    async def documents(self, _project_id: str) -> list[GraphDocument]:
        return sorted(self._fixture.documents, key=lambda document: document.path or "")

    async def fragments(self, _project_id: str, document_id: str | None = None) -> list[GraphFragment]:
        return sorted(
            (
                fragment
                for fragment in self._fixture.fragments
                if document_id is None or fragment.document_id == document_id
            ),
            key=lambda fragment: (fragment.document_id, fragment.ordinal or 0, fragment.fragment_id),
        )

    def _labels(self, labels: Sequence[str] | None) -> frozenset[str]:
        if labels is None:
            return frozenset()
        return frozenset(labels)

    def _relations(self, relations: Sequence[str] | None) -> frozenset[str]:
        if relations is None:
            return frozenset()
        return frozenset(relations)


@dataclass(frozen=True)
class EmptyShopReader:
    """A fake reader with no spec nodes; used for the ``no_spec_graph`` error."""

    @classmethod
    def empty(cls) -> EmptyShopReader:
        return cls()

    async def nodes(
        self, _project_id: str, labels: Sequence[str] | None = None, where: Mapping[str, Any] | None = None
    ) -> list[GraphNode]:
        del labels, where
        return []

    async def edges(self, _project_id: str, *, include_inferred: bool = True) -> list[GraphEdge]:
        del include_inferred
        return []

    async def documents(self, _project_id: str) -> list[GraphDocument]:
        return []

    async def fragments(self, _project_id: str, document_id: str | None = None) -> list[GraphFragment]:
        del document_id
        return []


@dataclass(frozen=True)
class _MemoryPlanStore:
    """The minimal PlanStore Snapshot needs; tests pass prior state through it."""

    revisions: MeaningRevisions = _EMPTY_REVISIONS
    previous_result: Mapping[str, Any] | None = None
    result_exists: bool = False

    async def get_revisions(self, _project_id: str) -> MeaningRevisions:
        return self.revisions

    async def latest_run(
        self, _project_id: str, *, finished_only: bool = False, modes: Any = None
    ) -> _MemoryRun | None:
        del finished_only, modes
        return _MemoryRun() if self.result_exists else None

    async def get_result(self, _project_id: str, _run_id: str) -> Mapping[str, Any] | None:
        return self.previous_result


@dataclass(frozen=True)
class _MemoryRun:
    run_id: str = "run-1"


class _SnapshotQueries:
    """Offline current-plan reads return no persisted plan by default."""

    def __init__(self, plan_rows: Sequence[Mapping[str, Any]] = ()) -> None:
        self.plan_rows = tuple(plan_rows)

    async def read(self, query: Any, _project_id: str, /, **_params: Any) -> list[dict[str, Any]]:
        if query.name == "plan_read_plan":
            return [dict(row) for row in self.plan_rows]
        return []


def ontology_labels(rules: PlanRules, key: str) -> tuple[str, ...]:
    """Resolve a rules type key or node group to its ontology labels."""

    return rules.labels_of(key)


async def write_spec_graph(
    project: Any,
    project_id: str,
    fixture: ShopGraphReader,
    ontology: Ontology,
) -> None:
    """Write the shop fixture to a live project for live plan tests.

    Spec nodes and domain relationships go through ``OwnedWriter(owner="build")``; documents and
    fragments through its keyed upserts. Statements and provenance links are test data the owner
    writer refuses (structural relationships), so they are written by raw test Cypher.
    """

    writer = OwnedWriter(project.driver, ontology, "build", project_id, database=project.database)
    by_spec_id = {node.spec_id: node for node in fixture._fixture.nodes}
    for node in sorted(fixture._fixture.nodes, key=lambda item: item.spec_id):
        properties = {
            "spec_id": node.spec_id,
            "name": node.name,
            "last_build_id": node.last_build_id,
            **node.attributes,
        }
        await writer.upsert_node(node.label, f"{project_id}-{node.uuid}", properties)
        for statement in node.statements:
            await project.run(
                "MATCH (n:SpecNode {group_id: $p, spec_id: $id}), "
                "(f:Fragment {group_id: $p, fragment_id: $fid}) "
                "MERGE (n)-[r:HAS_STATEMENT]->(f) "
                "SET r.text = $text, r.revision_id = $revision_id, r.version = $version",
                p=project_id,
                id=node.spec_id,
                fid=statement.fragment_id,
                text=statement.text,
                revision_id=statement.revision_id,
                version=statement.version,
            )
        for provenance in node.provenance:
            await project.run(
                "MATCH (n:SpecNode {group_id: $p, spec_id: $id}), "
                "(f:Fragment {group_id: $p, fragment_id: $fid}) "
                "MERGE (n)-[r:REFERENCED_BY]->(f) "
                "SET r.line_start = $line_start, r.line_end = $line_end, r.quote = $quote",
                p=project_id,
                id=node.spec_id,
                fid=provenance.fragment_id,
                line_start=provenance.line_start,
                line_end=provenance.line_end,
                quote=provenance.quote,
            )
    for edge in fixture._fixture.edges:
        source = by_spec_id[edge.source]
        target = by_spec_id[edge.target]
        properties = {
            "basis": edge.basis,
            "fact": edge.fact,
            "source_fragment_ids": list(edge.source_fragment_ids),
            "inference_rule": edge.inference_rule,
            "confidence": edge.confidence,
        }
        await writer.upsert_relationship(
            edge.relation,
            NodeRef(source.label, f"{project_id}-{source.uuid}"),
            NodeRef(target.label, f"{project_id}-{target.uuid}"),
            edge.edge_key or f"{edge.source}|{edge.relation}|{edge.target}",
            properties,
        )
    for document in fixture._fixture.documents:
        properties = document.model_dump(exclude={"document_id"}, exclude_none=True)
        await writer.upsert_node(DOCUMENT, document.document_id, properties)
    for fragment in fixture._fixture.fragments:
        properties = fragment.model_dump(exclude={"fragment_id"}, exclude_none=True)
        properties["heading_path"] = list(fragment.heading_path or ())
        await writer.upsert_node(FRAGMENT, fragment.fragment_id, properties)
        await project.run(
            "MATCH (d:Document {group_id: $p, document_id: $did}), "
            "(f:Fragment {group_id: $p, fragment_id: $fid}) MERGE (d)-[:CONTAINS]->(f)",
            p=project_id,
            did=fragment.document_id,
            fid=fragment.fragment_id,
        )


def shop_graph(*mutations: str) -> ShopGraphReader:
    """The shop fixture as a fake reader; named mutations add test-only edges."""

    fixture = _load_shop_fixture()
    edges = list(fixture.edges)
    for mutation in mutations:
        if mutation == "empty":
            fixture = _ShopFixture("build-1", (), (), fixture.documents, fixture.fragments)
            return ShopGraphReader(fixture, plan_ontology())
        if mutation == "orphan_in_web_shop":
            edges.append(
                GraphEdge(
                    relation="PART_OF",
                    source="REQ::FR::ORPHAN",
                    target="MOD::WEB_SHOP",
                    edge_key="REQ_FR_ORPHAN|PART_OF|MOD_WEB_SHOP",
                    basis="inferred",
                    inference_rule="orphan_membership",
                    confidence=0.91,
                )
            )
        else:
            raise ValueError(f"unknown shop mutation: {mutation}")
    mutated = _ShopFixture(
        build_id=fixture.build_id,
        nodes=fixture.nodes,
        edges=tuple(edges),
        documents=fixture.documents,
        fragments=fixture.fragments,
    )
    return ShopGraphReader(mutated, plan_ontology())


async def shop_snapshot(
    *mutations: str,
    revisions: MeaningRevisions = _EMPTY_REVISIONS,
    previous_result: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    changes: tuple[BuildChangeReport, ...] = (),
    replan: bool = False,
    options: Any = None,
) -> Any:
    """The shop fixture snapshot, built through the production Snapshot function."""

    reader = shop_graph(*mutations)
    ontology = plan_ontology()
    context = PlanContext(
        project_id="shop",
        ontology=ontology,
        rules=plan_rules(ontology),
        driver=None,
        gateway=None,
        store=_MemoryPlanStore(
            revisions=revisions,
            previous_result=previous_result,
            result_exists=plan is not None,
        ),
        build_id="build-2",
        changes=changes,
        replan=replan,
        options=options or PlanOptions(),
    )
    plan_rows = () if plan is None else (plan,)
    return await snapshot(reader, _SnapshotQueries(plan_rows), context)


async def shop_partition(*mutations: str) -> Any:
    """The shop fixture partition, built through production Snapshot and Partition."""

    artifact = await shop_snapshot(*mutations)
    ontology = plan_ontology()
    return partition(artifact, plan_rules(ontology), ontology)


def change_reports(results: Iterable[BuildResult]) -> tuple[BuildChangeReport, ...]:
    """Convert shop-v2 build results, in chain order, to the plan-side report model."""

    reports: list[BuildChangeReport] = []
    previous_build_id: str | None = None
    for result in results:
        if result.change_report is None:
            continue
        reports.append(
            BuildChangeReport(
                build_id=result.build_id,
                previous_build_id=previous_build_id,
                nodes=tuple(
                    ChangedSpecNode(
                        spec_id=node.spec_id,
                        classes=node.classes,
                        previous_spec_ids=node.previous_spec_ids,
                    )
                    for node in result.change_report.nodes
                ),
                edges=tuple(_changed_edge(edge) for edge in result.change_report.edges),
            )
        )
        previous_build_id = result.build_id
    return tuple(reports)


def _changed_edge(edge: ChangedEdge) -> ChangedSpecEdge:
    parts = edge.ref.split("|")
    if len(parts) != 3:
        raise ValueError(f"invalid changed edge ref: {edge.ref}")
    source, relation, target = parts
    return ChangedSpecEdge(
        source=source,
        relation=relation,
        target=target,
        change=edge.change,
        basis=edge.basis,
    )


def _load_shop_fixture() -> _ShopFixture:
    with _SHOP_FIXTURE.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise TypeError("shop fixture must be a mapping")
    nodes = tuple(GraphNode.model_validate(item) for item in data.get("nodes", ()))
    edges = tuple(GraphEdge.model_validate(item) for item in data.get("edges", ()))
    documents = tuple(GraphDocument.model_validate(item) for item in data.get("documents", ()))
    fragments = tuple(GraphFragment.model_validate(item) for item in data.get("fragments", ()))
    _validate_shop_fixture(nodes, edges, documents, fragments)
    return _ShopFixture(
        build_id=str(data["build_id"]),
        nodes=nodes,
        edges=edges,
        documents=documents,
        fragments=fragments,
    )


def _validate_shop_fixture(
    nodes: tuple[GraphNode, ...],
    edges: tuple[GraphEdge, ...],
    documents: tuple[GraphDocument, ...],
    fragments: tuple[GraphFragment, ...],
) -> None:
    node_ids = [node.spec_id for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("shop fixture has duplicate node ids")
    known_nodes = frozenset(node_ids)
    for edge in edges:
        if edge.source not in known_nodes or edge.target not in known_nodes:
            raise ValueError(f"shop edge has an unknown endpoint: {edge.edge_key}")
    document_ids = frozenset(document.document_id for document in documents)
    if len(document_ids) != len(documents):
        raise ValueError("shop fixture has duplicate document ids")
    fragment_ids = [fragment.fragment_id for fragment in fragments]
    if len(fragment_ids) != len(set(fragment_ids)):
        raise ValueError("shop fixture has duplicate fragment ids")
    if any(fragment.document_id not in document_ids for fragment in fragments):
        raise ValueError("shop fragment names an unknown document")
