"""Snapshot reads of the spec graph, current plan, changes and revisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from graph.core.errors import ConfigurationError
from graph.core.ontology import Ontology
from graph.core.reader import GraphEdge, GraphNode
from graph.plan.changes import change_set, check_chain, meaning_revisions
from graph.plan.models import (
    BuildChangeReport,
    ChapterRef,
    CurrentPlan,
    CurrentTask,
    PlanInfo,
    PlanLink,
    ProvenanceView,
    SnapshotArtifact,
    SpecEdgeView,
    SpecNodeView,
    StoryInfo,
    WorkAreaInfo,
)
from graph.plan.models import ChangeSet as ChangeSetModel
from graph.plan.names import PlanMode, PlanRelation, plan_ontology_version
from graph.plan.options import PlanContext
from graph.plan.queries import READ_AREAS, READ_PLAN, READ_STORIES, READ_TASKS, read_links
from graph.plan.rules import PlanRules

_CHAPTER_HEADING_DEPTH = 2


def _attribute(node: GraphNode, name: str) -> Any:
    return node.attributes.get(name)


def _matches_exclusions(node: GraphNode, exclusions: Mapping[str, tuple[str, ...]]) -> bool:
    return any(_attribute(node, key) in values for key, values in exclusions.items())


def _owner_excluded(
    node: GraphNode,
    edges: Sequence[GraphEdge],
    nodes: Mapping[str, GraphNode],
    rules: PlanRules,
) -> bool:
    relation = rules.relation("containment")
    return any(
        (owner := nodes.get(edge.target)) is not None and _matches_exclusions(owner, rules.exclude_all)
        for edge in edges
        if edge.relation == relation and edge.source == node.spec_id
    )


def _node_excluded(
    node: GraphNode,
    edges: Sequence[GraphEdge],
    nodes: Mapping[str, GraphNode],
    rules: PlanRules,
) -> bool:
    return (
        _matches_exclusions(node, rules.exclude_all)
        or _owner_excluded(node, edges, nodes, rules)
        or _matches_exclusions(node, rules.rule_of(node.label).exclude)
    )


def _chapter(node: GraphNode, fragments: Mapping[str, Any], documents: Mapping[str, Any]) -> ChapterRef | None:
    def document_title(provenance: Any) -> str:
        if provenance.document_id is None or provenance.document_id not in documents:
            return ""
        return documents[provenance.document_id].title or ""

    ordered = sorted(
        node.provenance,
        key=lambda provenance: (
            document_title(provenance),
            fragments[provenance.fragment_id].ordinal or 0
            if provenance.fragment_id in fragments and fragments[provenance.fragment_id].ordinal is not None
            else 0,
            provenance.fragment_id,
        ),
    )
    for provenance in ordered:
        fragment = fragments.get(provenance.fragment_id)
        if fragment is None:
            continue
        heading_path = fragment.heading_path
        if isinstance(heading_path, str):
            path = tuple(part.strip() for part in heading_path.split(" > ") if part.strip())
        else:
            path = tuple(heading_path or ())

        document = documents.get(fragment.document_id)
        document_name = document.title if document is not None and document.title else fragment.document_id
        if len(path) >= _CHAPTER_HEADING_DEPTH:
            return ChapterRef(document=document_name, heading=path[1])
        if path:
            return ChapterRef(document=document_name, heading=path[0])
        if document is not None and document.title:
            return ChapterRef(document=document_name, heading=document.title)
    return None


def _node_view(
    node: GraphNode,
    fragments: Mapping[str, Any],
    documents: Mapping[str, Any],
) -> SpecNodeView:
    return SpecNodeView(
        spec_id=node.spec_id,
        uuid=node.uuid,
        label=node.label,
        name=node.name,
        attributes=dict(node.attributes),
        statements=tuple(statement.model_dump() for statement in node.statements),
        provenance=tuple(ProvenanceView.model_validate(item.model_dump()) for item in node.provenance),
        aliases=tuple(node.spec_id_aliases),
        last_build_id=node.last_build_id,
        chapter=_chapter(node, fragments, documents),
    )


def _edge_view(edge: GraphEdge) -> SpecEdgeView:
    return SpecEdgeView(
        relation=edge.relation,
        source=edge.source,
        target=edge.target,
        basis=edge.basis,
        confidence=edge.confidence,
        edge_key=edge.edge_key or f"{edge.source}|{edge.relation}|{edge.target}",
    )


def fingerprint(
    ontology: Ontology,
    rules: PlanRules,
    nodes: Sequence[SpecNodeView],
    edges: Sequence[SpecEdgeView],
    *,
    task_size: int,
    planner_members: int,
) -> str:
    """The graph fingerprint, changed by graph and planning options only."""

    payload = {
        "ontology": ontology.version,
        "plan_ontology": plan_ontology_version(),
        "rules": rules.version,
        "options": {"planner_members": planner_members, "task_size": task_size},
        "nodes": [
            [node.spec_id, node.last_build_id]
            for node in sorted(nodes, key=lambda item: (item.spec_id, item.last_build_id or ""))
        ],
        "edges": [
            [edge.relation, edge.source, edge.target, edge.basis]
            for edge in sorted(edges, key=lambda item: (item.relation, item.source, item.target, item.basis or ""))
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def snapshot(reader: Any, executor: Any, context: PlanContext) -> SnapshotArtifact:
    """Read the graph and current plan, then build the Snapshot artifact."""

    project_id = context.project_id
    nodes = await reader.nodes(project_id)
    edges = await reader.edges(project_id)
    fragments_list = await reader.fragments(project_id)
    documents_list = await reader.documents(project_id)
    fragments = {fragment.fragment_id: fragment for fragment in fragments_list}
    documents = {document.document_id: document for document in documents_list}
    by_spec_id = {node.spec_id: node for node in nodes}

    plannable_nodes = []
    context_nodes = []
    for node in nodes:
        rule = context.rules.rule_of(node.label)
        if _matches_exclusions(node, context.rules.exclude_all) or _owner_excluded(
            node, edges, by_spec_id, context.rules
        ):
            continue
        if rule.role == "context" or _matches_exclusions(node, rule.exclude):
            context_nodes.append(node)
            continue
        plannable_nodes.append(node)
    if not plannable_nodes:
        raise ConfigurationError("no_spec_graph")

    plannable = tuple(_node_view(node, fragments, documents) for node in plannable_nodes)
    context_views = tuple(_node_view(node, fragments, documents) for node in context_nodes)
    plannable_ids = {node.spec_id for node in plannable}
    selected_edges = tuple(
        _edge_view(edge) for edge in edges if edge.source in plannable_ids or edge.target in plannable_ids
    )
    aliases = {alias: node.spec_id for node in nodes for alias in node.spec_id_aliases if alias != node.spec_id}
    current = await read_current_plan(executor, project_id)
    mode: PlanMode = "replan" if context.replan else ("first" if current.plan is None else "update")
    reports = check_chain(context.changes, current.plan.last_build_id if current.plan else None, context.build_id, mode)
    changes = change_set(reports)
    change_set_model = ChangeSetModel(
        meaning=dict(changes.meaning),
        links=changes.links,
    )
    stored_revisions = await context.store.get_revisions(project_id)
    consumed = _consumed_chain(stored_revisions.consumed, reports, context.build_id)
    node_build_ids = {node.spec_id: node.last_build_id or context.build_id for node in (*plannable, *context_views)}
    revisions = meaning_revisions(
        reports,
        node_build_ids,
        stored_revisions,
        context.build_id,
        aliases=aliases,
        consumed=consumed,
        replan_without_reports=context.replan,
    )
    previous_run = await context.store.latest_run(project_id, finished_only=True)
    previous_result = (
        await context.store.get_result(project_id, previous_run.run_id) if previous_run is not None else None
    )
    previous_open = _previous_open(previous_result)
    unchanged = mode == "update" and changes.empty and not previous_open
    return SnapshotArtifact(
        mode=mode,
        unchanged=unchanged,
        build_id=context.build_id,
        plannable=plannable,
        context=context_views,
        edges=selected_edges,
        aliases=aliases,
        current=current,
        change_set=change_set_model,
        revisions=revisions,
        fingerprint=fingerprint(
            context.ontology,
            context.rules,
            (*plannable, *context_views),
            selected_edges,
            task_size=context.options.task_size,
            planner_members=context.options.planner_members,
        ),
        previous_open=previous_open,
    )


def _previous_open(result: Mapping[str, Any] | None) -> bool:
    if not isinstance(result, Mapping):
        return False
    return result.get("status") == "partially_succeeded" or any(
        bool(result.get(field)) for field in ("in_progress_changed", "conflicts")
    )


def _consumed_chain(
    stored: Sequence[str],
    reports: Sequence[BuildChangeReport],
    build_id: str,
) -> tuple[str, ...]:
    consumed = list(stored)
    consumed.extend(report.build_id for report in reports)
    if build_id not in consumed:
        consumed.append(build_id)
    return tuple(dict.fromkeys(consumed))


async def read_current_plan(executor: Any, project_id: str) -> CurrentPlan:
    """Read the persisted plan head, areas, stories, tasks and links."""

    plan_rows = await executor.read(READ_PLAN, project_id)
    if not plan_rows:
        return CurrentPlan(plan=None)

    area_rows = await executor.read(READ_AREAS, project_id)
    story_rows = await executor.read(READ_STORIES, project_id)
    task_rows = await executor.read(READ_TASKS, project_id)
    links: list[PlanLink] = []
    for relation in ("IN_AREA", "IN_STORY", "IMPLEMENTS", "WAITS_FOR", "SUPERSEDES"):
        typed: PlanRelation = relation
        link_rows = await executor.read(read_links(typed), project_id)
        links.extend(PlanLink.model_validate(row) for row in link_rows)

    return CurrentPlan(
        plan=PlanInfo.model_validate(plan_rows[0]),
        areas=tuple(WorkAreaInfo.model_validate(row) for row in area_rows),
        stories=tuple(StoryInfo.model_validate(row) for row in story_rows),
        tasks=tuple(CurrentTask.model_validate(row) for row in task_rows),
        links=tuple(sorted(links, key=lambda link: link.edge_key)),
    )
