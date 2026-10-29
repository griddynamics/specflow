"""Diff of update builds offline (build.md step 6 and 7; data-model.md "Identity", "Edges"; architecture.md
W3, W4; tech-specs 5.4 "Diff carry-over"; FR-BLD-05, 06, 10, 12, 13; FR-CORE-06).

``diff`` is pure: each test builds a target graph by hand, diffs a ``first`` build against the empty
graph, turns that diff into the current graph as persist would leave it (``persisted``), and diffs
the next target against it. Persist routing runs ``persist.persist`` on a recording executor.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import AsyncIterator, Collection, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
import pytest

from graph.build import persist as writing
from graph.build.diff import (
    CARRY_OVER_JACCARD,
    EMPTY_GRAPH,
    CurrentEdge,
    CurrentEvidence,
    CurrentFragment,
    CurrentGraph,
    CurrentNode,
    DiffInput,
    _jaccard,
    _name_tokens,
    continuing_texts,
    diff,
    diff_summary,
)
from graph.build.models import (
    PERSIST_PHASES,
    ChangeSet,
    DiffArtifact,
    DocumentRec,
    DocumentUpsert,
    EdgeCreate,
    EdgeDelete,
    EdgeUpdate,
    EvidenceLines,
    FragmentRec,
    FragmentStatusChange,
    FragmentUpsert,
    NodeCarryOver,
    NodeCreate,
    NodeDelete,
    NodeLinks,
    NodeMerge,
    NodeUpdate,
    Operation,
    PersistCursor,
    PlanArtifact,
    SpecChanges,
    StatementRec,
    TargetEdge,
    TargetGraph,
    TargetNode,
    Unit,
)
from graph.build.vectors import text_hash
from graph.core.identity import SpecIdRow, edge_uuid, node_uuid
from graph_support import shipped_ontology

ONT = shipped_ontology()
PROJECT = "p-diff"
MODEL = "test-embedding"
AT1 = "2026-10-01T00:00:00+00:00"
AT2 = "2026-10-02T00:00:00+00:00"
OLDER = "2026-09-01T00:00:00+00:00"


# Corpus: a.md (two fragments) and b.md (one fragment)


def frag(fragment_id: str, lines: Sequence[str], start: int, ordinal: int = 0, **update: Any) -> FragmentRec:
    text = "\n".join(lines)
    document_id = fragment_id.split("#", maxsplit=1)[0]
    record = FragmentRec(
        fragment_id=fragment_id,
        document_id=document_id,
        heading_path=(document_id, fragment_id.split("#")[1]),
        ordinal=ordinal,
        line_start=start,
        line_end=start + len(lines) - 1,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        tokens=len(lines),
    )
    return record.model_copy(update=update)


A1 = frag("a.md#intro#1", ["# A", "The orders service keeps every order.", "FR-1 Staff can export orders as CSV."], 1)
A2 = frag("a.md#export#2", ["## Export", "Exports finish within 5 s.", "Exports are audited."], 4, 1)
B1 = frag("b.md#intro#3", ["# B", "Order export is a feature.", "Order export runs nightly."], 1)
FRAGMENTS = (A1, A2, B1)
DOC_A = DocumentRec(document_id="a.md", revision_id="r1", version=1, path="a.md", title="A")
DOC_B = DocumentRec(document_id="b.md", revision_id="r1", version=1, path="b.md", title="B")
DOCUMENTS = (DOC_A, DOC_B)

R1 = "requirement|id|FR-1"
R2 = "requirement|span|exports finish within 5 s"
H1 = "Feature|surface|order export"
M1 = "Module|surface|orders service"


def unchanged(fragment: FragmentRec) -> FragmentRec:
    return fragment.model_copy(update={"change": "unchanged", "extracted_hash": fragment.content_hash})


def plan_of(
    fragments: Sequence[FragmentRec],
    *,
    mode: str = "update",
    documents: Sequence[DocumentRec] = DOCUMENTS,
    dirty: Sequence[str] | None = None,
    retired: Sequence[str] = (),
    units: Sequence[Unit] = (),
) -> PlanArtifact:
    if dirty is None:
        dirty = [f.fragment_id for f in fragments if f.change in ("new", "changed", "retried")]
    return PlanArtifact(
        mode=mode,
        change_set=ChangeSet(),
        documents=tuple(documents),
        fragments=tuple(fragments),
        retired_ids=tuple(retired),
        dirty_ids=tuple(dirty),
        units=tuple(units),
    )


FIRST_PLAN = plan_of(FRAGMENTS, mode="first")
UPDATE_PLAN = plan_of([unchanged(f) for f in FRAGMENTS])


# Target graphs


def lines_of(fragment: FragmentRec, first: int, last: int) -> str:
    rows = fragment.text.split("\n")
    return "\n".join(rows[first - fragment.line_start : last - fragment.line_start + 1])


def node(
    key: str,
    name: str,
    label: str,
    evidence: Iterable[tuple[FragmentRec, int, int]],
    *,
    stmt: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    revision: Mapping[str, tuple[str, int]] | None = None,
    at: str = AT1,
    **update: Any,
) -> TargetNode:
    """A target node grounded on ``evidence`` with one statement per evidence entry (its lines).

    ``aliases`` (identity aliases) and ``quote`` come through ``update`` like any other node field."""

    aliases: Sequence[str] = update.pop("aliases", ())
    evidence = list(evidence)
    revisions = {"a.md": ("r1", 1), "b.md": ("r1", 1), **(revision or {})}
    values = dict(attributes or {})
    if stmt is not None:
        values["stmt"] = stmt
    name_text = f"{label}: {name}"
    return TargetNode(
        identity_key=key,
        identity_aliases=tuple(aliases),
        label=label,
        name=name,
        attributes=values,
        evidence=tuple(EvidenceLines(fragment_id=f.fragment_id, line_start=s, line_end=e) for f, s, e in evidence),
        statements=tuple(
            StatementRec(
                fragment_id=f.fragment_id,
                document_id=f.document_id,
                revision_id=revisions[f.document_id][0],
                version=revisions[f.document_id][1],
                text=lines_of(f, s, e),
                at=at,
            )
            for f, s, e in evidence
        ),
        embedding_text=f"{name_text}. {stmt}" if stmt else name_text,
        name_embedding_text=name_text,
        **update,
    )


def edge(source: str, relation: str, target: str, fragments: Sequence[FragmentRec], **update: Any) -> TargetEdge:
    if ONT.is_symmetric(relation) and target < source:
        source, target = target, source
    values: dict[str, Any] = {"basis": "stated", "fact": None, **update}
    return TargetEdge(
        edge_key=f"{source}|{relation}|{target}",
        relation=relation,
        source_key=source,
        target_key=target,
        fragment_ids=tuple(f.fragment_id for f in fragments),
        **values,
    )


def node_r1(**update: Any) -> TargetNode:
    values: dict[str, Any] = {
        "stmt": "Staff can export orders as CSV.",
        "attributes": {"acceptance_criteria": "The file opens in a spreadsheet."},
        "quote": "FR-1 Staff can export orders as CSV.",
        "explicit_ids": ("FR-1",),
        **update,
    }
    evidence = values.pop("evidence", [(A1, 3, 3)])
    return node(R1, values.pop("name", "Staff can export orders as CSV"), "FunctionalRequirement", evidence, **values)


def node_r2(**update: Any) -> TargetNode:
    evidence = update.pop("evidence", [(A2, 5, 5)])
    return node(
        R2,
        update.pop("name", "Exports finish within 5 s"),
        update.pop("label", "QualityRequirement"),
        evidence,
        **update,
    )


def node_h1(**update: Any) -> TargetNode:
    evidence = update.pop("evidence", [(B1, 2, 2)])
    return node(
        update.pop("key", H1), update.pop("name", "Order export"), update.pop("label", "Feature"), evidence, **update
    )


def node_m1(**update: Any) -> TargetNode:
    evidence = update.pop("evidence", [(A1, 2, 2)])
    return node(M1, update.pop("name", "Orders service"), "Module", evidence, **update)


E_R1_H1 = edge(R1, "PART_OF", H1, [A1], fact="FR-1 Staff can export orders as CSV.")
E_R2_M1 = edge(R2, "PART_OF", M1, [A2], fact="Exports finish within 5 s.")


def graph(*items: TargetNode | TargetEdge) -> TargetGraph:
    nodes = sorted((item for item in items if isinstance(item, TargetNode)), key=lambda n: n.identity_key)
    edges = sorted((item for item in items if isinstance(item, TargetEdge)), key=lambda e: (e.relation, e.edge_key))
    return TargetGraph(nodes=tuple(nodes), edges=tuple(edges))


FIRST_TARGET = graph(node_r1(), node_r2(), node_h1(), node_m1(), E_R1_H1, E_R2_M1)


def given(
    current: CurrentGraph = EMPTY_GRAPH,
    taken: Mapping[str, SpecIdRow] | None = None,
    *,
    at: str = AT2,
    build_id: str = "b2",
    skipped: Mapping[str, str] | None = None,
    edge_failed: Sequence[str] = (),
) -> DiffInput:
    return DiffInput(
        project_id=PROJECT,
        build_id=build_id,
        embedding_model=MODEL,
        at=at,
        taken=dict(taken or {}),
        skipped=dict(skipped or {}),
        current=current,
        edge_failed_fragments=tuple(edge_failed),
    )


# The current graph as persist leaves it


@dataclasses.dataclass
class Persisted:
    current: CurrentGraph
    taken: dict[str, SpecIdRow]
    artifact: DiffArtifact

    def node(self, key: str) -> CurrentNode:
        return next(n for n in self.current.nodes if n.identity_key == key)

    def edge(self, key: str) -> CurrentEdge:
        return next(e for e in self.current.edges if e.edge_key == key)

    def replace_node(self, key: str, **update: Any) -> None:
        nodes = tuple(dataclasses.replace(n, **update) if n.identity_key == key else n for n in self.current.nodes)
        self.current = dataclasses.replace(self.current, nodes=nodes)

    def add_edge(self, source: str, relation: str, target: str, **properties: Any) -> CurrentEdge:
        key = f"{source}|{relation}|{target}"
        uuid = edge_uuid(PROJECT, key)
        props = {"uuid": uuid, "edge_key": key, "created_at": AT1, "updated_at": AT1, **properties}
        added = CurrentEdge(
            uuid=uuid,
            relation=relation,
            edge_key=key,
            basis=str(props.get("basis", "")),
            source_uuid=self.node(source).uuid,
            target_uuid=self.node(target).uuid,
            source_key=source,
            target_key=target,
            properties=props,
        )
        self.current = dataclasses.replace(self.current, edges=(*self.current.edges, added))
        return added


def inferred(**extra: Any) -> dict[str, Any]:
    return {
        "basis": "inferred",
        "inference_rule": "orphan_part_of",
        "confidence": 0.9,
        "evidence": "[]",
        "judge_model": "test-main",
        "inferred_at": AT1,
        **extra,
    }


def persisted(plan: PlanArtifact = FIRST_PLAN, target: TargetGraph = FIRST_TARGET) -> Persisted:
    """The graph after persisting the ``first`` diff of ``target`` (null properties are not stored)."""

    artifact = diff(plan, target, ONT, given(at=AT1, build_id="b1"))
    fragments = {f.fragment_id: f for f in plan.fragments}
    links = {op.uuid: op for op in artifact.operations if isinstance(op, NodeLinks)}
    nodes, keys = [], {}
    for op in artifact.operations:
        if not isinstance(op, NodeCreate):
            continue
        props = {name: value for name, value in op.properties.items() if value is not None}
        props["uuid"] = op.uuid
        keys[op.uuid] = str(props["identity_key"])
        evidence = tuple(
            CurrentEvidence(
                fragment_id=lines.fragment_id,
                document_id=fragments[lines.fragment_id].document_id,
                line_start=lines.line_start,
                line_end=lines.line_end,
                lines=tuple(lines_of(fragments[lines.fragment_id], lines.line_start, lines.line_end).split("\n")),
            )
            for lines in links[op.uuid].evidence
        )
        nodes.append(
            CurrentNode(
                uuid=op.uuid,
                spec_id=op.spec_id,
                label=op.label,
                identity_key=keys[op.uuid],
                identity_aliases=tuple(props.get("identity_aliases") or ()),
                name=str(props["name"]),
                created_at=str(props["created_at"]),
                properties=props,
                evidence=evidence,
                mentions=dict(links[op.uuid].mentions),
            )
        )
    edges = tuple(
        CurrentEdge(
            uuid=op.uuid,
            relation=op.relation,
            edge_key=op.edge_key,
            basis=str(op.properties["basis"]),
            source_uuid=op.source_uuid,
            target_uuid=op.target_uuid,
            source_key=keys[op.source_uuid],
            target_key=keys[op.target_uuid],
            properties={
                "edge_key": op.edge_key,
                **{name: value for name, value in op.properties.items() if value is not None},
            },
        )
        for op in artifact.operations
        if isinstance(op, EdgeCreate)
    )
    stored = tuple(
        CurrentFragment(
            fragment_id=op.fragment.fragment_id,
            document_id=op.fragment.document_id,
            status=op.fragment.status,
            content_hash=op.fragment.content_hash,
            extracted_hash=op.fragment.extracted_hash,
            error_code=op.fragment.error_code,
            spec_id=op.spec_id,
            heading_path=op.fragment.heading_path,
            ordinal=op.fragment.ordinal,
            line_start=op.fragment.line_start,
            line_end=op.fragment.line_end,
        )
        for op in artifact.operations
        if isinstance(op, FragmentUpsert)
    )
    current = CurrentGraph(
        nodes=tuple(sorted(nodes, key=lambda n: n.uuid)), edges=edges, documents=plan.documents, fragments=stored
    )
    return Persisted(current, {row.spec_id: row for row in artifact.registry_rows}, artifact)


def ops(artifact: DiffArtifact, kind: type[Any]) -> list[Any]:
    return [op for op in artifact.operations if isinstance(op, kind)]


def kinds(artifact: DiffArtifact) -> list[str]:
    return [op.op for op in artifact.operations]


def statements(properties: Mapping[str, Any]) -> list[dict[str, Any]]:
    return json.loads(properties["statements"])


# First build and an unchanged re-diff


def test_a_first_build_creates_everything_and_an_unchanged_re_diff_is_empty() -> None:
    before = persisted()
    first = before.artifact
    assert kinds(first) == [
        *["node_create"] * 4,
        *["edge_create"] * 2,
        *["node_links"] * 4,
        *["document_upsert"] * 2,
        *["fragment_upsert"] * 3,
    ]
    created = {op.properties["identity_key"]: op for op in ops(first, NodeCreate)}
    assert created[R1].spec_id.startswith("REQ::FR::FR_1_")
    assert created[R1].uuid == node_uuid(PROJECT, R1)
    assert set(first.spec_changes.added) == {op.spec_id for op in created.values()}
    assert {row.state for row in first.registry_rows} == {"active"}

    again = diff(UPDATE_PLAN, FIRST_TARGET, ONT, given(before.current, before.taken))
    assert again.operations == ()
    assert again.spec_changes == SpecChanges()
    assert again.registry_rows == ()
    assert diff_summary(again) == {
        "operations": 0,
        "added": 0,
        "removed": 0,
        "modified": 0,
        "rekeyed": 0,
        "carried_over": 0,
        "inferred_added": 0,
        "inferred_removed": 0,
        "registry_rows": 0,
    }


def test_a_revision_that_leaves_a_nodes_statements_as_they_were_does_not_modify_it() -> None:
    before = persisted()
    revised = (DOC_A.model_copy(update={"revision_id": "r2", "version": 2}), DOC_B)
    # Conform stamps every statement with the new revision and this build's time; the text is the same
    target = graph(
        node_r1(revision={"a.md": ("r2", 2)}, at=AT2),
        node_r2(revision={"a.md": ("r2", 2)}, at=AT2),
        node_h1(at=AT2),
        node_m1(revision={"a.md": ("r2", 2)}, at=AT2),
        E_R1_H1,
        E_R2_M1,
    )
    result = diff(
        plan_of([unchanged(f) for f in FRAGMENTS], documents=revised), target, ONT, given(before.current, before.taken)
    )
    assert kinds(result) == ["document_upsert"]
    assert result.spec_changes == SpecChanges()


# Update in place


def test_an_update_sets_only_the_owned_properties_that_changed() -> None:
    before = persisted()
    r1 = before.node(R1)
    before.replace_node(R1, properties={**r1.properties, "reviewed_by": "alice"})  # a foreign property
    revised = (DOC_A.model_copy(update={"revision_id": "r2", "version": 2}), DOC_B)
    changed = node_r1(
        stmt="Staff can export orders as CSV or JSON.",
        attributes={},  # acceptance_criteria no longer extracted
        evidence=[(A1, 3, 3), (B1, 3, 3)],
        revision={"a.md": ("r2", 2)},
        at=AT2,
    )
    target = graph(changed, node_r2(), node_h1(), node_m1(), E_R1_H1, E_R2_M1)
    result = diff(
        plan_of([unchanged(f) for f in FRAGMENTS], documents=revised), target, ONT, given(before.current, before.taken)
    )

    [update] = ops(result, NodeUpdate)
    assert (update.uuid, update.spec_id, update.label) == (r1.uuid, r1.spec_id, "FunctionalRequirement")
    assert set(update.properties) == {
        "stmt",
        "acceptance_criteria",
        "statements",
        "statement_text",
        "updated_at",
        "last_build_id",
    }
    assert update.properties["acceptance_criteria"] is None  # a build-owned value the target lost is removed
    assert (update.properties["updated_at"], update.properties["last_build_id"]) == (AT2, "b2")
    # the unchanged statement keeps the revision and time it was stated at; the new one is this build's
    assert [(s["fragment_id"], s["revision_id"], s["at"]) for s in statements(update.properties)] == [
        (A1.fragment_id, "r1", AT1),
        (B1.fragment_id, "r1", AT2),
    ]
    # only the search vector's text changed (stmt); the name vector stays
    assert update.vectors == {"embedding": text_hash(changed.embedding_text or "")}
    [links] = ops(result, NodeLinks)
    assert links.uuid == r1.uuid and {e.fragment_id for e in links.evidence} == {A1.fragment_id, B1.fragment_id}
    assert result.spec_changes == SpecChanges(modified=(r1.spec_id,))
    assert result.registry_rows == ()  # nothing new issued


def test_a_node_whose_attributes_batch_failed_keeps_its_stored_values() -> None:
    before = persisted()
    failed = node_r1(stmt=None, attributes={}, quote=None, attributes_missing=True)
    target = graph(failed, node_r2(), node_h1(), node_m1(), E_R1_H1, E_R2_M1)
    result = diff(UPDATE_PLAN, target, ONT, given(before.current, before.taken))
    # stmt, acceptance_criteria and quote stay; the search vector of the kept stmt is still right
    assert result.operations == ()

    renamed = node_r1(
        name="Staff can export all orders as CSV", stmt=None, attributes={}, quote=None, attributes_missing=True
    )
    result = diff(
        UPDATE_PLAN,
        graph(renamed, node_r2(), node_h1(), node_m1(), E_R1_H1, E_R2_M1),
        ONT,
        given(before.current, before.taken),
    )
    [update] = ops(result, NodeUpdate)
    assert set(update.properties) == {"name", "updated_at", "last_build_id"}
    assert set(update.vectors) == {"embedding", "name_embedding"}


def test_a_node_keeps_its_label_and_records_the_disagreement() -> None:
    before = persisted()
    r2 = before.node(R2)
    disagreeing = node_r2(label_disagreement="FunctionalRequirement")
    result = diff(
        UPDATE_PLAN,
        graph(node_r1(), disagreeing, node_h1(), node_m1(), E_R1_H1, E_R2_M1),
        ONT,
        given(before.current, before.taken),
    )
    [update] = ops(result, NodeUpdate)
    assert (update.uuid, update.label) == (r2.uuid, "QualityRequirement")
    assert update.properties == {
        "label_disagreement": "FunctionalRequirement",
        "updated_at": AT2,
        "last_build_id": "b2",
    }
    assert update.vectors == {}

    # once extraction agrees again, the disagreement is removed
    before.replace_node(R2, properties={**r2.properties, "label_disagreement": "FunctionalRequirement"})
    result = diff(UPDATE_PLAN, FIRST_TARGET, ONT, given(before.current, before.taken))
    [update] = ops(result, NodeUpdate)
    assert update.properties == {"label_disagreement": None, "updated_at": AT2, "last_build_id": "b2"}


def test_a_target_of_another_label_under_the_same_key_keeps_the_persisted_label() -> None:
    """A target whose label differs from the persisted node's (no label kept upstream) continues it in
    place: the node keeps its label, records the target's, and its vectors are those of its texts
    under the persisted label, so nothing is re-embedded."""

    before = persisted()
    r2 = before.node(R2)
    relabelled = node_r2(label="FunctionalRequirement")
    assert relabelled.identity_key == r2.identity_key and relabelled.label_disagreement is None
    result = diff(
        UPDATE_PLAN,
        graph(node_r1(), relabelled, node_h1(), node_m1(), E_R1_H1, E_R2_M1),
        ONT,
        given(before.current, before.taken),
    )
    [update] = ops(result, NodeUpdate)
    assert (update.uuid, update.spec_id, update.label) == (r2.uuid, r2.spec_id, "QualityRequirement")
    assert update.properties == {
        "label_disagreement": "FunctionalRequirement",
        "updated_at": AT2,
        "last_build_id": "b2",
    }
    assert update.vectors == {}
    assert ops(result, NodeCreate) == [] and ops(result, NodeDelete) == [] and ops(result, NodeCarryOver) == []
    assert result.spec_changes == SpecChanges(modified=(r2.spec_id,))
    assert result.texts == {}


def test_graph_counts_after_the_diff_use_kept_labels_and_kept_edges() -> None:
    """``nodes_by_label`` and ``edges_by_relation`` (the build metrics) count the graph as Persist
    leaves it: a continuing node under its kept label, not the label computed now; a deleted node not
    at all; the current edges the diff keeps (an inferred one) as well as the target's."""

    before = persisted()
    assert before.artifact.nodes_by_label == {
        "Feature": 1,
        "FunctionalRequirement": 1,
        "Module": 1,
        "QualityRequirement": 1,
    }
    assert before.artifact.edges_by_relation == {"PART_OF": 2}

    # R2 is computed as a FunctionalRequirement now and keeps QualityRequirement
    before.add_edge(R1, "RELATES_TO", R2, **inferred())
    result = diff(
        UPDATE_PLAN,
        graph(node_r1(), node_r2(label="FunctionalRequirement"), node_h1(), node_m1(), E_R1_H1, E_R2_M1),
        ONT,
        given(before.current, before.taken),
    )
    assert result.nodes_by_label == {"Feature": 1, "FunctionalRequirement": 1, "Module": 1, "QualityRequirement": 1}
    assert result.edges_by_relation == {"PART_OF": 2, "RELATES_TO": 1}

    # H1 deleted with its stated edge; the inferred edge between surviving nodes stays
    result = diff(
        UPDATE_PLAN, graph(node_r1(), node_r2(), node_m1(), E_R2_M1), ONT, given(before.current, before.taken)
    )
    assert result.nodes_by_label == {"FunctionalRequirement": 1, "Module": 1, "QualityRequirement": 1}
    assert result.edges_by_relation == {"PART_OF": 1, "RELATES_TO": 1}


def test_continuing_texts_use_the_persisted_label() -> None:
    target = node_r2(label="FunctionalRequirement", stmt="Every export finishes within 5 s.")
    texts = continuing_texts(target, "QualityRequirement")
    assert texts == {
        "embedding": "QualityRequirement: Exports finish within 5 s. Every export finishes within 5 s.",
        "name_embedding": "QualityRequirement: Exports finish within 5 s",
    }
    assert continuing_texts(target, "FunctionalRequirement") == {
        "embedding": target.embedding_text,
        "name_embedding": target.name_embedding_text,
    }
    # a text that does not start with the target's own label prefix is left as it is
    odd = target.model_copy(update={"embedding_text": "Something else", "name_embedding_text": None})
    assert continuing_texts(odd, "QualityRequirement") == {"embedding": "Something else"}


def test_a_relabelled_target_with_new_texts_names_the_vectors_of_the_persisted_label() -> None:
    before = persisted()
    r2 = before.node(R2)
    reworded = node_r2(label="FunctionalRequirement", name="Exports complete within 5 s")
    result = diff(
        UPDATE_PLAN,
        graph(node_r1(), reworded, node_h1(), node_m1(), E_R1_H1, E_R2_M1),
        ONT,
        given(before.current, before.taken),
    )
    [update] = ops(result, NodeUpdate)
    assert update.label == "QualityRequirement" and update.uuid == r2.uuid
    persisted_text = "QualityRequirement: Exports complete within 5 s"
    assert update.vectors == {"embedding": text_hash(persisted_text), "name_embedding": text_hash(persisted_text)}
    # Persist finds the persisted-label text (not the target's own) by its hash
    assert result.texts == {text_hash(persisted_text): persisted_text}


# Merge, split


def test_a_merge_keeps_the_oldest_node_aliases_the_others_and_moves_their_edges() -> None:
    m2_key = "Module|surface|order api"
    m2 = node(m2_key, "Order API", "Module", [(A1, 2, 2)])
    before = persisted(
        target=graph(
            node_r1(), node_r2(), node_h1(), node_m1(), m2, E_R1_H1, E_R2_M1, edge(M1, "INTEGRATES_WITH", m2_key, [A1])
        )
    )
    before.replace_node(m2_key, created_at=OLDER, spec_id_aliases=("MOD::LEGACY_API",))  # older: it survives
    survivor, loser = before.node(m2_key), before.node(M1)
    moved = before.add_edge(R1, "PART_OF", M1, **inferred())
    taken = {
        **before.taken,
        "MOD::LEGACY_API": SpecIdRow("MOD::LEGACY_API", "Module|surface|legacy api", survivor.uuid, "alias"),
    }

    merged = node_m1(aliases=(m2_key,))
    target = graph(node_r1(), node_r2(), node_h1(), merged, E_R1_H1, E_R2_M1)
    result = diff(UPDATE_PLAN, target, ONT, given(before.current, taken))

    ranks = [PERSIST_PHASES.index(op.phase) for op in result.operations]
    assert ranks == sorted(ranks)
    [merge] = ops(result, NodeMerge)
    assert merge == NodeMerge(
        survivor_uuid=survivor.uuid,
        survivor_spec_id=survivor.spec_id,
        merged_uuids=(loser.uuid,),
        merged_spec_ids=(loser.spec_id,),
        aliases=tuple(sorted(("MOD::LEGACY_API", loser.spec_id))),
    )
    [update] = ops(result, NodeUpdate)
    assert update.uuid == survivor.uuid
    assert (update.properties["identity_key"], update.properties["identity_aliases"], update.properties["name"]) == (
        M1,
        [m2_key],
        "Orders service",
    )
    assert "created_at" not in update.properties
    # the edge between the merged nodes would be a self-loop: dropped (E18); the stated edge to the
    # loser is moved to the survivor (deleted and created again with its uuid)
    deletes = {(op.relation, op.uuid) for op in ops(result, EdgeDelete)}
    between = before.edge(f"{M1}|INTEGRATES_WITH|{m2_key}")
    assert ("INTEGRATES_WITH", between.uuid) in deletes
    stated = before.edge(E_R2_M1.edge_key)
    creates = {op.uuid: op for op in ops(result, EdgeCreate)}
    assert ("PART_OF", stated.uuid) in deletes and ("PART_OF", moved.uuid) in deletes
    assert creates[stated.uuid].target_uuid == survivor.uuid and creates[stated.uuid].edge_key == E_R2_M1.edge_key
    assert creates[stated.uuid].properties["created_at"] == AT1  # the moved edge keeps its creation stamp
    # the inferred edge moves with its end, rekeyed, still inferred
    assert creates[moved.uuid].target_uuid == survivor.uuid
    assert creates[moved.uuid].edge_key == f"{R1}|PART_OF|{M1}"
    assert creates[moved.uuid].properties["basis"] == "inferred"
    assert creates[moved.uuid].properties["inference_rule"] == "orphan_part_of"
    assert result.spec_changes.inferred_removed == ()
    assert result.spec_changes.rekeyed == tuple(sorted((survivor.spec_id, loser.spec_id)))
    assert result.spec_changes.removed == () and result.spec_changes.added == ()
    rows = {row.spec_id: row for row in result.registry_rows}
    assert rows[loser.spec_id] == SpecIdRow(loser.spec_id, M1, survivor.uuid, "alias")
    assert rows[survivor.spec_id] == SpecIdRow(survivor.spec_id, M1, survivor.uuid, "active")
    assert "MOD::LEGACY_API" not in rows  # unchanged alias row


def test_merged_nodes_of_equal_age_keep_the_lower_spec_id() -> None:
    m2_key = "Module|surface|order api"
    before = persisted(
        target=graph(node_r1(), node_r2(), node_h1(), node_m1(), node(m2_key, "Order API", "Module", [(A1, 2, 2)]))
    )
    first, second = sorted((before.node(M1), before.node(m2_key)), key=lambda n: n.spec_id)
    result = diff(
        UPDATE_PLAN,
        graph(node_r1(), node_r2(), node_h1(), node_m1(aliases=(m2_key,))),
        ONT,
        given(before.current, before.taken),
    )
    [merge] = ops(result, NodeMerge)
    assert (merge.survivor_spec_id, merge.merged_spec_ids) == (first.spec_id, (second.spec_id,))


def test_a_split_continues_the_node_as_its_best_match_and_creates_the_others() -> None:
    before = persisted()
    r1 = before.node(R1)
    r1b_key = "requirement|id|FR-1A"
    split_off = node(r1b_key, "Staff can export orders as JSON", "FunctionalRequirement", [(A1, 3, 3)], aliases=(R1,))
    target = graph(node_r1(), split_off, node_r2(), node_h1(), node_m1(), E_R1_H1, E_R2_M1)
    result = diff(UPDATE_PLAN, target, ONT, given(before.current, before.taken))
    assert ops(result, NodeUpdate) == []  # the node continues unchanged under its own key
    [create] = ops(result, NodeCreate)
    assert create.properties["identity_key"] == r1b_key
    assert create.properties["identity_aliases"] == []  # an alias naming another target's key is dropped
    assert create.spec_id != r1.spec_id and create.uuid == node_uuid(PROJECT, r1b_key)
    assert result.spec_changes == SpecChanges(added=(create.spec_id,))
    assert [row.spec_id for row in result.registry_rows] == [create.spec_id]


def test_a_split_off_target_never_takes_the_uuid_or_spec_id_of_the_node_created_under_its_key() -> None:
    old_key, new_key = "requirement|id|FR-9", "requirement|id|FR-10"
    before = persisted(target=graph(node(old_key, "Staff can archive orders", "FunctionalRequirement", [(A2, 6, 6)])))
    created = before.node(old_key)
    # a later build rekeyed it (registry row and node now name the new key)
    before.replace_node(
        old_key,
        identity_key=new_key,
        identity_aliases=(old_key,),
        properties={**created.properties, "identity_key": new_key, "identity_aliases": [old_key]},
    )
    taken = {created.spec_id: SpecIdRow(created.spec_id, new_key, created.uuid, "active")}

    target = graph(
        node(new_key, "Staff can archive orders", "FunctionalRequirement", [(A2, 6, 6)]),
        node(old_key, "Staff can archive orders", "FunctionalRequirement", [(A2, 6, 6)]),
    )
    result = diff(UPDATE_PLAN, target, ONT, given(before.current, taken))
    [create] = ops(result, NodeCreate)
    assert create.properties["identity_key"] == old_key
    assert create.uuid == node_uuid(PROJECT, f"{old_key}|2") != created.uuid
    assert create.spec_id == f"{created.spec_id}_2"
    [update] = ops(result, NodeUpdate)
    assert update.uuid == created.uuid and update.properties["identity_aliases"] == []


# spec_id issue rules (FR-CORE-06)


@pytest.mark.parametrize(
    ("key", "aliases", "reused"),
    [
        ("requirement|id|FR-7", (), True),  # the same key gets its retired spec_id back
        ("requirement|span|staff can purge orders", (), False),  # another key with the same name never does
        # a node created under this key and later carried over to another key, then deleted: its row
        # names the other key, so neither the key nor the uuid path gives the id back...
        ("requirement|id|FR-8", (), False),
        # ...unless the target names that key as an identity alias
        ("requirement|id|FR-8", ("requirement|id|FR-8B",), True),
    ],
)
def test_a_retired_spec_id_returns_only_for_its_own_key(key: str, aliases: tuple[str, ...], *, reused: bool) -> None:
    name = "Staff can purge orders"
    retired_key = "requirement|id|FR-8B" if key.endswith("FR-8") else "requirement|id|FR-7"
    spec_id = "REQ::FR::STAFF_PURGE_ORDERS"
    row_uuid = node_uuid(PROJECT, "requirement|id|FR-8") if key.endswith("FR-8") else node_uuid(PROJECT, retired_key)
    taken = {spec_id: SpecIdRow(spec_id, retired_key, row_uuid, "retired")}
    target = graph(node(key, name, "FunctionalRequirement", [(A2, 6, 6)], aliases=aliases))
    result = diff(UPDATE_PLAN, target, ONT, given(EMPTY_GRAPH, taken))
    [create] = ops(result, NodeCreate)
    assert (create.spec_id == spec_id) is reused
    if not reused:
        assert create.spec_id == f"{spec_id}_2"
    assert [row.spec_id for row in result.registry_rows] == [create.spec_id]


# Carry-over (tech-specs 5.4 "Diff carry-over")


def test_a_renamed_node_with_overlapping_evidence_is_carried_over() -> None:
    before = persisted()
    h1 = before.node(H1)
    hub_edge = before.add_edge(R2, "GROUPED_BY", H1, **inferred())
    new_key = "Feature|surface|order export job"
    renamed = node_h1(key=new_key, name="Order export job")
    target = graph(
        node_r1(), node_r2(), renamed, node_m1(), edge(R1, "PART_OF", new_key, [A1], fact=E_R1_H1.fact), E_R2_M1
    )
    result = diff(UPDATE_PLAN, target, ONT, given(before.current, before.taken))

    [carry] = ops(result, NodeCarryOver)
    assert (carry.uuid, carry.spec_id, carry.label, carry.from_identity_key) == (h1.uuid, h1.spec_id, "Feature", H1)
    # F1: the carried node takes the new name and key and keeps the old key as an identity alias
    assert carry.properties["name"] == "Order export job"
    assert carry.properties["identity_key"] == new_key
    assert carry.properties["identity_aliases"] == [H1]
    assert set(carry.vectors) == {"embedding", "name_embedding"}
    assert ops(result, NodeCreate) == [] and ops(result, NodeDelete) == []
    assert result.spec_changes == SpecChanges(carried_over=(h1.spec_id,))
    assert result.registry_rows == (SpecIdRow(h1.spec_id, new_key, h1.uuid, "active"),)
    # the node's edges stay where they are, rekeyed in place (stated and inferred alike)
    updates = {op.uuid: op for op in ops(result, EdgeUpdate)}
    assert updates[before.edge(E_R1_H1.edge_key).uuid].properties["edge_key"] == f"{R1}|PART_OF|{new_key}"
    assert updates[hub_edge.uuid].properties["edge_key"] == f"{R2}|GROUPED_BY|{new_key}"
    assert "basis" not in updates[hub_edge.uuid].properties
    assert ops(result, EdgeDelete) == [] and ops(result, EdgeCreate) == []


def test_carry_over_by_name_needs_overlapping_evidence_and_the_same_scope() -> None:
    before = persisted()
    h1 = before.node(H1)
    elsewhere = node_h1(key="Feature|surface|order export job", name="Order export job", evidence=[(A2, 6, 6)])
    as_module = node("Module|surface|order export job", "Order export job", "Module", [(B1, 2, 2)])
    for moved in (elsewhere, as_module):
        result = diff(
            UPDATE_PLAN,
            graph(node_r1(), node_r2(), node_m1(), moved, E_R2_M1),
            ONT,
            given(before.current, before.taken),
        )
        assert ops(result, NodeCarryOver) == []
        assert [op.uuid for op in ops(result, NodeDelete)] == [h1.uuid]
        [create] = ops(result, NodeCreate)
        assert create.spec_id != h1.spec_id
        assert result.spec_changes.removed == (h1.spec_id,)


def test_a_requirement_with_another_name_but_the_same_evidence_lines_is_carried_over() -> None:
    before = persisted()
    r2 = before.node(R2)
    new_key = "requirement|span|export latency"
    drifted = node(new_key, "Export latency", "QualityRequirement", [(A2, 5, 5)])
    result = diff(
        UPDATE_PLAN, graph(node_r1(), drifted, node_h1(), node_m1(), E_R1_H1), ONT, given(before.current, before.taken)
    )
    [carry] = ops(result, NodeCarryOver)
    assert (carry.uuid, carry.from_identity_key) == (r2.uuid, R2)
    assert (carry.properties["name"], carry.properties["identity_aliases"]) == ("Export latency", [R2])
    assert result.spec_changes.carried_over == (r2.spec_id,)

    # never across labels, even within one identity scope
    relabelled = node(new_key, "Export latency", "FunctionalRequirement", [(A2, 5, 5)])
    result = diff(
        UPDATE_PLAN,
        graph(node_r1(), relabelled, node_h1(), node_m1(), E_R1_H1),
        ONT,
        given(before.current, before.taken),
    )
    assert ops(result, NodeCarryOver) == []
    assert [op.uuid for op in ops(result, NodeDelete)] == [r2.uuid]

    # hubs are never carried over by evidence alone
    h1 = before.node(H1)
    other_hub = node_h1(key="Feature|surface|nightly batch", name="Nightly batch")
    result = diff(
        UPDATE_PLAN,
        graph(node_r1(), node_r2(), other_hub, node_m1(), E_R2_M1),
        ONT,
        given(before.current, before.taken),
    )
    assert ops(result, NodeCarryOver) == []
    assert [op.uuid for op in ops(result, NodeDelete)] == [h1.uuid]


@pytest.mark.parametrize(
    ("name", "jaccard", "carried"),
    [
        ("Order export job run", 0.5, True),  # exactly CARRY_OVER_JACCARD is alike
        ("Order export job run now", 0.4, False),
    ],
)
def test_carry_over_by_name_starts_at_a_name_jaccard_of_one_half(name: str, jaccard: float, *, carried: bool) -> None:
    before = persisted()
    h1 = before.node(H1)
    key = f"Feature|surface|{name.lower()}"
    renamed = node_h1(key=key, name=name)
    assert _jaccard(_name_tokens("Order export", hub=True), _name_tokens(name, hub=True)) == pytest.approx(jaccard)
    assert CARRY_OVER_JACCARD == 0.5
    result = diff(
        UPDATE_PLAN, graph(node_r1(), node_r2(), renamed, node_m1(), E_R2_M1), ONT, given(before.current, before.taken)
    )
    if carried:
        [carry] = ops(result, NodeCarryOver)
        assert (carry.uuid, carry.properties["identity_key"]) == (h1.uuid, key)
        assert ops(result, NodeDelete) == []
    else:
        assert ops(result, NodeCarryOver) == []
        assert [op.uuid for op in ops(result, NodeDelete)] == [h1.uuid]
        [create] = ops(result, NodeCreate)
        assert create.properties["identity_key"] == key and create.spec_id != h1.spec_id


@pytest.mark.parametrize(
    ("lines", "carried"),
    [
        ((5, 6), True),  # 1 of 2 evidence lines shared: exactly one half
        ((4, 6), False),  # 1 of 3
    ],
)
def test_carry_over_by_evidence_starts_at_an_evidence_jaccard_of_one_half(
    lines: tuple[int, int], *, carried: bool
) -> None:
    before = persisted()
    r2 = before.node(R2)
    key = "requirement|span|archive latency"
    drifted = node(key, "Archive latency", "QualityRequirement", [(A2, *lines)])
    assert _jaccard(_name_tokens(r2.name, hub=False), _name_tokens("Archive latency", hub=False)) == 0.0  # not by name
    result = diff(
        UPDATE_PLAN, graph(node_r1(), drifted, node_h1(), node_m1(), E_R1_H1), ONT, given(before.current, before.taken)
    )
    if carried:
        [carry] = ops(result, NodeCarryOver)
        assert (carry.uuid, carry.from_identity_key, carry.properties["identity_key"]) == (r2.uuid, R2, key)
    else:
        assert ops(result, NodeCarryOver) == []
        assert [op.uuid for op in ops(result, NodeDelete)] == [r2.uuid]


def test_carry_over_is_one_to_one_and_best_pair_first() -> None:
    """Two orphans want one new target: the better name match wins although the other orphan comes
    first in identity key order; the other is deleted (hubs are not carried by evidence)."""

    h2_key = "Feature|surface|order export nightly"
    h2 = node_h1(key=h2_key, name="Order export nightly", evidence=[(B1, 3, 3)])
    before = persisted(target=graph(node_r1(), node_r2(), node_h1(), h2, node_m1(), E_R1_H1, E_R2_M1))
    h1, kept_h2 = before.node(H1), before.node(h2_key)
    assert h2_key > H1  # the weaker orphan is the first one
    key = "Feature|surface|nightly order export run"
    target = node_h1(key=key, name="Nightly order export run", evidence=[(B1, 2, 3)])
    tokens = _name_tokens(target.name, hub=True)
    assert _jaccard(_name_tokens(h1.name, hub=True), tokens) == 0.5
    assert _jaccard(_name_tokens(kept_h2.name, hub=True), tokens) == 0.75
    result = diff(
        UPDATE_PLAN, graph(node_r1(), node_r2(), target, node_m1(), E_R2_M1), ONT, given(before.current, before.taken)
    )
    [carry] = ops(result, NodeCarryOver)
    assert (carry.uuid, carry.from_identity_key) == (kept_h2.uuid, h2_key)
    assert [op.uuid for op in ops(result, NodeDelete)] == [h1.uuid]
    assert ops(result, NodeCreate) == []
    assert result.spec_changes.carried_over == (kept_h2.spec_id,) and result.spec_changes.removed == (h1.spec_id,)


# Delete


def test_a_node_without_support_is_deleted_with_its_edges_and_its_ids_retire() -> None:
    before = persisted()
    r2, m1 = before.node(R2), before.node(M1)
    before.replace_node(R2, spec_id_aliases=("REQ::NFR::OLD",))
    taken = {**before.taken, "REQ::NFR::OLD": SpecIdRow("REQ::NFR::OLD", "requirement|span|old", r2.uuid, "alias")}
    dropped = before.add_edge(R1, "RELATES_TO", R2, **inferred())
    kept = before.add_edge(R1, "PART_OF", M1, **inferred())
    target = graph(node_r1(), node_h1(), node_m1(), E_R1_H1)
    retired_plan = plan_of(
        [unchanged(A1), unchanged(B1)],
        documents=(DOC_A.model_copy(update={"revision_id": "r2"}), DOC_B),
        retired=(A2.fragment_id,),
    )
    result = diff(retired_plan, target, ONT, given(before.current, taken))

    assert ops(result, NodeDelete) == [NodeDelete(uuid=r2.uuid, spec_id=r2.spec_id)]
    deletes = {op.uuid for op in ops(result, EdgeDelete)}
    assert deletes == {before.edge(E_R2_M1.edge_key).uuid, dropped.uuid}
    assert kept.uuid not in deletes  # an inferred edge between surviving nodes is outside the diff
    dropped_ref = f"{before.node(R1).spec_id}|RELATES_TO|{r2.spec_id}"  # edge_ref form, never the key
    assert result.spec_changes == SpecChanges(removed=(r2.spec_id,), inferred_removed=(dropped_ref,))
    rows = {row.spec_id: row for row in result.registry_rows}
    assert rows == {
        r2.spec_id: SpecIdRow(r2.spec_id, R2, r2.uuid, "retired"),
        "REQ::NFR::OLD": SpecIdRow("REQ::NFR::OLD", "requirement|span|old", r2.uuid, "retired"),
    }
    assert ops(result, FragmentStatusChange) == [FragmentStatusChange(fragment_id=A2.fragment_id, status="retired")]
    assert m1.uuid not in {op.uuid for op in ops(result, NodeUpdate)}
    ranks = [PERSIST_PHASES.index(op.phase) for op in result.operations]
    assert ranks == sorted(ranks)


# Edges


def test_edges_match_by_key_and_inferred_edges_are_kept_or_become_stated() -> None:
    before = persisted()
    kept = before.add_edge(R1, "RELATES_TO", R2, **inferred())
    promoted = before.add_edge(R1, "PART_OF", M1, **inferred())
    restated = E_R1_H1.model_copy(update={"fact": "Order export: FR-1 Staff can export orders as CSV."})
    new = edge(R2, "GROUPED_BY", H1, [A2])
    stating = edge(R1, "PART_OF", M1, [A1], fact="The orders service keeps every order.")
    target = graph(node_r1(), node_r2(), node_h1(), node_m1(), restated, new, stating)
    result = diff(UPDATE_PLAN, target, ONT, given(before.current, before.taken))

    updates = {op.uuid: op for op in ops(result, EdgeUpdate)}
    assert updates[before.edge(E_R1_H1.edge_key).uuid].properties == {
        "fact": restated.fact,
        "updated_at": AT2,
        "last_build_id": "b2",
    }
    promotion = updates[promoted.uuid].properties
    assert (promotion["basis"], promotion["fact"], promotion["source_fragment_ids"]) == (
        "stated",
        stating.fact,
        [A1.fragment_id],
    )
    assert all(
        promotion[name] is None for name in ("inference_rule", "confidence", "evidence", "judge_model", "inferred_at")
    )
    assert kept.uuid not in updates
    [create] = ops(result, EdgeCreate)
    assert (create.edge_key, create.uuid) == (new.edge_key, edge_uuid(PROJECT, new.edge_key))
    assert (create.properties["created_at"], create.properties["basis"]) == (AT2, "stated")
    assert [op.uuid for op in ops(result, EdgeDelete)] == [before.edge(E_R2_M1.edge_key).uuid]  # no longer stated
    assert result.spec_changes == SpecChanges()


# Skipped fragments (FR-BLD-12) and unextracted edges


def test_a_skipped_changed_fragment_keeps_the_last_extracted_support() -> None:
    before = persisted()
    r2 = before.node(R2)
    edited = frag("a.md#export#9", ["## Export", "Exports finish within 10 s.", "Exports are audited."], 4, 1)
    edited = edited.model_copy(
        update={"change": "changed", "previous_id": A2.fragment_id, "extracted_hash": A2.content_hash}
    )
    plan = plan_of([unchanged(A1), edited, unchanged(B1)], retired=(A2.fragment_id,))
    # the reused nodes of the skipped fragment are grounded in its new text: whole fragment, drifted key
    drifted_key = "requirement|span|exports finish within 10 s"
    drifted = node(drifted_key, "Exports finish within 5 s", "QualityRequirement", [(edited, 4, 6)])
    reused_edge = edge(drifted_key, "PART_OF", M1, [edited])
    target = graph(node_r1(), drifted, node_h1(), node_m1(), E_R1_H1, reused_edge)
    result = diff(plan, target, ONT, given(before.current, before.taken, skipped={edited.fragment_id: "llm_malformed"}))

    [update] = ops(result, NodeUpdate)
    assert update.uuid == r2.uuid
    # the key and quote stay; the drifted key becomes an alias; the statement keeps the old text
    assert "identity_key" not in update.properties and "quote" not in update.properties
    assert update.properties["identity_aliases"] == [drifted_key]
    [entry] = statements(update.properties)
    assert (entry["fragment_id"], entry["text"], entry["revision_id"], entry["at"]) == (
        edited.fragment_id,
        "Exports finish within 5 s.",
        "r1",
        AT1,
    )
    [links] = [op for op in ops(result, NodeLinks) if op.uuid == r2.uuid]
    assert links.evidence == (EvidenceLines(fragment_id=edited.fragment_id, line_start=5, line_end=5),)
    # the edge keeps its fact and evidence, re-pointed to the skipped fragment
    [edge_update] = ops(result, EdgeUpdate)
    assert edge_update.uuid == before.edge(E_R2_M1.edge_key).uuid
    assert edge_update.properties["source_fragment_ids"] == [edited.fragment_id]
    assert "fact" not in edge_update.properties and ops(result, EdgeDelete) == []
    assert result.spec_changes == SpecChanges(modified=(r2.spec_id,))
    upserts = {op.fragment.fragment_id: op.fragment for op in ops(result, FragmentUpsert)}
    assert (upserts[edited.fragment_id].status, upserts[edited.fragment_id].error_code) == ("skipped", "llm_malformed")
    assert upserts[edited.fragment_id].extracted_hash == A2.content_hash
    assert ops(result, FragmentStatusChange) == [FragmentStatusChange(fragment_id=A2.fragment_id, status="retired")]


def unit(*fragments: FragmentRec) -> Unit:
    return Unit(
        unit_id=f"unit:{fragments[0].fragment_id}",
        document_id=fragments[0].document_id,
        fragment_ids=tuple(f.fragment_id for f in fragments),
        tokens=1,
        content_hash="u",
    )


@pytest.mark.parametrize(
    ("units", "skipped", "edge_failed", "kept"),
    [
        ((), {}, (), False),  # the call answered without it: the edge is gone
        ((), {}, (A1.fragment_id,), True),  # the unit's edge call failed without a fallback
        ((unit(A1, A2),), {A2.fragment_id: "llm_malformed"}, (), True),  # its unit's call skipped a fragment
    ],
)
def test_a_stated_edge_no_call_answered_for_is_kept(
    units: tuple[Unit, ...], skipped: dict[str, str], edge_failed: tuple[str, ...], *, kept: bool
) -> None:
    before = persisted()
    stated = before.edge(E_R1_H1.edge_key)
    plan = plan_of([unchanged(f) for f in FRAGMENTS], units=units, dirty=list(skipped))
    result = diff(
        plan,
        graph(node_r1(), node_r2(), node_h1(), node_m1(), E_R2_M1),
        ONT,
        given(before.current, before.taken, skipped=skipped, edge_failed=edge_failed),
    )
    assert (stated.uuid in {op.uuid for op in ops(result, EdgeDelete)}) is not kept
    assert ops(result, EdgeUpdate) == [] and ops(result, EdgeCreate) == []


# Summary


def test_diff_summary_counts_operations_by_kind_spec_changes_and_registry_rows() -> None:
    before = persisted()
    target = graph(
        node_r1(stmt="Staff can export orders as JSON."),
        node_h1(),
        node_m1(),
        node(R1 + "B", "Staff can print orders", "FunctionalRequirement", [(A1, 3, 3)]),
        E_R1_H1,
    )
    result = diff(UPDATE_PLAN, target, ONT, given(before.current, before.taken))
    summary = diff_summary(result)
    assert summary == {
        "operations": len(result.operations),
        "edge_delete": 1,
        "node_update": 1,
        "node_delete": 1,
        "node_create": 1,
        "node_links": 1,
        "added": 1,
        "removed": 1,
        "modified": 1,
        "rekeyed": 0,
        "carried_over": 0,
        "inferred_added": 0,
        "inferred_removed": 0,
        "registry_rows": 2,
    }


# Persist routing of merges, carry-overs and deletes (build.md step 7; W3, W4)


class RecordingExecutor:
    """Records each transaction as ``(query name, rows, extra params)``."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    @asynccontextmanager
    async def tx(self, project_id: str) -> AsyncIterator[Any]:
        assert project_id == PROJECT
        runs = self.runs

        class Transaction:
            async def run(self, query: Any, *, rows: list[dict[str, Any]], **params: Any) -> list[dict[str, Any]]:
                params.pop("build_id")
                runs.append((query.name, rows, params))
                return [{"written": len(rows)}]

        yield Transaction()


async def test_persist_applies_merges_updates_carry_overs_and_deletes_in_phase_order() -> None:
    operations: list[Operation] = [
        EdgeDelete(relation="PART_OF", edge_key="x|PART_OF|y", uuid="e1"),
        NodeMerge(
            survivor_uuid="s",
            survivor_spec_id="MOD::S",
            merged_uuids=("l1", "l2"),
            merged_spec_ids=("MOD::L1", "MOD::L2"),
            aliases=("MOD::L1", "MOD::L2"),
        ),
        NodeUpdate(
            uuid="s", spec_id="MOD::S", label="Module", properties={"name": "S"}, vectors={"name_embedding": "h1"}
        ),
        NodeCarryOver(
            uuid="c",
            spec_id="FEAT::C",
            label="Feature",
            from_identity_key="Feature|surface|c",
            properties={"name": "C2"},
        ),
        NodeDelete(uuid="d", spec_id="REQ::FR::D"),
        NodeCreate(uuid="n", spec_id="REQ::FR::N", label="FunctionalRequirement", properties={"name": "N"}),
        EdgeCreate(
            uuid="e2",
            relation="PART_OF",
            edge_key="n|PART_OF|s",
            source_uuid="n",
            target_uuid="s",
            properties={"basis": "stated"},
        ),
        NodeLinks(uuid="n", evidence=(EvidenceLines(fragment_id="a.md#x#1", line_start=1, line_end=1),)),
        DocumentUpsert(document=DOC_A, spec_id="DOC::a.md"),
        FragmentStatusChange(fragment_id="a.md#old#1", status="retired"),
    ]
    artifact = DiffArtifact(operations=tuple(operations))
    executor = RecordingExecutor()
    saved: list[PersistCursor] = []

    async def save(cursor: PersistCursor) -> None:
        saved.append(cursor)

    async def fetch(digests: Collection[str]) -> Mapping[str, np.ndarray]:
        return {digest: np.array([0.5, 0.5]) for digest in digests}

    final = await writing.persist(
        artifact,
        PersistCursor(),
        target=writing.PersistTarget(executor=executor, project_id=PROJECT, build_id="b2"),
        ontology=ONT,
        save=save,
        fetch_vectors=fetch,
    )
    assert final.done and saved[-1] == final
    assert [name for name, _, _ in executor.runs] == [
        "build_delete_edges_PART_OF",
        "build_merge_spec_nodes",
        "build_update_spec_nodes",  # an update and a carry-over share one query (the node keeps its uuid)
        "build_delete_spec_nodes",
        "build_create_spec_nodes",
        "build_upsert_edges_PART_OF",
        "build_replace_node_links",
        "build_upsert_documents",
        "build_set_fragment_status",
    ]
    merge_rows, merge_params = executor.runs[1][1], executor.runs[1][2]
    assert merge_rows == [{"survivor": "s", "merged": ["l1", "l2"], "aliases": ["MOD::L1", "MOD::L2"]}]
    # every relationship type build does not own moves to the survivor (W3)
    assert merge_params["owned_types"] == sorted({"CONTAINS", "REFERENCED_BY", "MENTIONED_IN", *ONT.relations})
    # What the merge query does with foreign relationships (moved both ways, no duplicate, none between
    # the merged nodes, E18) is asserted on Neo4j: test_graph_update_live.py, FR-BLD-10.
    assert executor.runs[2][1] == [
        {"uuid": "s", "props": {"name": "S", "name_embedding": [0.5, 0.5]}},
        {"uuid": "c", "props": {"name": "C2"}},
    ]
    assert executor.runs[3][1] == [{"uuid": "d"}]


def test_a_diff_artifact_refuses_operations_out_of_phase_order() -> None:
    with pytest.raises(ValueError, match="persist phase order"):
        DiffArtifact(
            operations=(
                NodeDelete(uuid="d", spec_id="X"),
                NodeMerge(survivor_uuid="s", survivor_spec_id="S", merged_uuids=("l",), merged_spec_ids=("L",)),
            )
        )
