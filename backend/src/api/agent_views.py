"""The agent contract views of library answers (D25): graph and plan objects in, ``agent_contracts`` models out.

Holders are user ids in the library and names here: ``holders_of`` resolves them from the project's members, one
query per answer (tools.md "Holders"); no id, email or token leaves in a view.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from agent_contracts import (
    BriefExpansion,
    BriefStory,
    BriefTask,
    EdgeDirection,
    EdgeItem,
    Evidence,
    EvidenceSource,
    ExpansionHint,
    ExpansionHit,
    ExpansionTop,
    FindingItem,
    Holder,
    NodeBody,
    NodeEdges,
    NodePaths,
    NodePlanPart,
    NodeProvenance,
    NodeStatement,
    PathChainItem,
    PlanState,
    RelationCount,
    SimilarHit,
    SimilarNodes,
    SpecTrace,
    StoryBriefBody,
    TaskRef,
    TraceNodeItem,
    TraceOwnerItem,
    TraceStoryItem,
)
from api.runtime import Runtime
from db import repositories as repo
from graph.core.evidence import EvidenceItem
from graph.core.expand import Expansion, SimilarResult
from graph.core.paths import PathResult
from graph.core.reader import GraphEdge, GraphNode
from graph.plan.brief_models import StoryBrief
from graph.plan.findings_port import OpenFinding
from graph.plan.node_plan import NodePlan
from graph.plan.trace_models import PlanTrace, TraceTask

_BASIS_RANK = {None: 0, "stated": 0, "implied": 1, "inferred": 2}


class Holders:
    """Maps user ids of the library to ``Holder`` views for one caller."""

    def __init__(self, caller: uuid.UUID, names: Mapping[uuid.UUID, str | None] | None = None) -> None:
        self._caller = caller
        self._names = names or {}

    def of(self, user_id: str | None, agent_label: str | None = None) -> Holder | None:
        """The holder behind ``user_id``; ``agent_label`` is the label of a story claim (claims only)."""

        if not user_id:
            return None
        parsed = _parse(user_id)
        if parsed is None:
            return Holder(name=None, you=False, agent_label=agent_label)
        return Holder(name=self._names.get(parsed), you=parsed == self._caller, agent_label=agent_label)


def _parse(user_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(user_id)
    except ValueError:
        return None


async def holders_of(
    runtime: Runtime, project_id: uuid.UUID, caller: uuid.UUID, user_ids: Iterable[str | None]
) -> Holders:
    """The ``Holders`` of ``user_ids`` among the project's members (no query when none is a user id)."""

    wanted = {parsed for item in user_ids if item and (parsed := _parse(item)) is not None}
    if not wanted:
        return Holders(caller)
    async with runtime.transaction() as session:
        return Holders(caller, await repo.member_names(session, project_id, wanted))


def finding_items(findings: Iterable[OpenFinding], holders: Holders) -> list[FindingItem]:
    return [
        FindingItem(
            finding_id=item.finding_id,
            kind=item.kind,
            text=item.text,
            blocking=item.blocking,
            about=list(item.about_spec_ids),
            author=holders.of(item.created_by) or Holder(name=None, you=False),
        )
        for item in findings
    ]


def _task_ref(task: TraceTask, holders: Holders) -> TaskRef:
    return TaskRef(
        task_id=task.task_id,
        story_id=task.story_id,
        status=task.status,
        holder=holders.of(task.claimed_by),
        commit=task.commit,
    )


HINT_TOP: Final = 3


def story_brief_body(brief: StoryBrief, holders: Holders, expansion: Expansion | None = None) -> StoryBriefBody:
    """The brief; with ``expansion`` (what ``expand_brief`` returns at its default budget) its hint: how many nodes,
    the first three by rank."""

    story = brief.story
    return StoryBriefBody(
        story=BriefStory(
            story_id=story.story_id,
            kind=story.kind,
            title=story.title,
            summary=story.summary,
            anchor=story.anchor,
            release=story.release,
            state=brief.state,
        ),
        area=brief.area,
        profile=brief.profile,
        tasks=[
            BriefTask(
                task_id=item.task.task_id,
                title=item.task.title,
                phase=item.task.phase,
                status=item.task.status,
                complexity=item.task.complexity,
                progress=item.progress,
                waits_for=list(item.task.waits_for),
                done_when=list(item.done_when),
            )
            for item in brief.tasks
        ],
        implements=list(brief.implements),
        contracts=list(brief.contracts),
        open_questions=list(brief.open_questions),
        findings=finding_items(brief.findings, holders),
        neighbours=list(brief.neighbours),
        spec_changed=brief.spec_changed,
        missing=list(brief.missing),
        expansion=None
        if expansion is None
        else ExpansionHint(
            more=len(expansion.items),
            top=[ExpansionTop(spec_id=item.spec_id, title=item.name) for item in expansion.items[:HINT_TOP]],
        ),
    )


def trace_view(trace: PlanTrace, holders: Holders) -> SpecTrace:
    def tasks(items: Iterable[TraceTask]) -> list[TaskRef]:
        return [_task_ref(item, holders) for item in items]

    return SpecTrace(
        direction=trace.direction,
        sources=list(trace.sources),
        context=[
            TraceOwnerItem(
                spec_id=owner.spec_id,
                label=owner.label,
                name=owner.name,
                plan_state=owner.plan_state,
                tasks=tasks(owner.tasks),
                findings=owner.findings,
            )
            for owner in trace.context
        ],
        nodes=[
            TraceNodeItem(
                **node.model_dump(exclude={"plan_state", "tasks", "findings"}),
                plan_state=node.plan_state,
                tasks=tasks(node.tasks),
                findings=node.findings,
            )
            for node in trace.nodes
        ],
        stories=[
            TraceStoryItem(
                story_id=story.story_id,
                title=story.title,
                area_id=story.area_id,
                progress=story.progress,
                holders=sorted(
                    (holder for item in story.holders if (holder := holders.of(item)) is not None),
                    key=lambda holder: (holder.name or "", not holder.you),
                ),
            )
            for story in trace.stories
        ],
        summary=trace.summary,
        open_questions=list(trace.open_questions),
        unknown=list(trace.unknown),
        truncated=trace.truncated,
        seeds_truncated=trace.seeds_truncated,
    )


def trace_user_ids(trace: PlanTrace) -> list[str | None]:
    """The user ids a trace names: task holders and the holders of its stories."""

    tasks = [task for owner in trace.context for task in owner.tasks] + [
        task for node in trace.nodes for task in node.tasks
    ]
    return [*(task.claimed_by for task in tasks), *(holder for story in trace.stories for holder in story.holders)]


def node_body(node: GraphNode) -> NodeBody:
    return NodeBody(
        spec_id=node.spec_id,
        label=node.label,
        name=node.name,
        spec_id_aliases=list(node.spec_id_aliases),
        attributes=node.attributes,
        statements=[
            NodeStatement(fragment_id=item.fragment_id, document_id=item.document_id, text=item.text)
            for item in node.statements
        ],
    )


def node_provenance(node: GraphNode) -> list[NodeProvenance]:
    return [
        NodeProvenance(
            document_id=item.document_id,
            path=item.path,
            fragment_id=item.fragment_id,
            line_start=item.line_start,
            line_end=item.line_end,
            quote=item.quote,
        )
        for item in node.provenance
    ]


def _other_end(edge: GraphEdge, spec_id: str) -> tuple[EdgeDirection, str]:
    """The edge's direction from the node and the spec id at its other end."""

    return ("out", edge.target) if edge.source == spec_id else ("in", edge.source)


def shown_edges(edges: Sequence[GraphEdge], spec_id: str, edge_limit: int) -> list[GraphEdge]:
    """At most ``edge_limit`` edges: stated first, then by relation, other end and direction."""

    def key(edge: GraphEdge) -> tuple[int, str, str, str]:
        direction, other = _other_end(edge, spec_id)
        return _BASIS_RANK.get(edge.basis, 3), edge.relation, other, direction

    return sorted(edges, key=key)[:edge_limit]


def node_edges(edges: Sequence[GraphEdge], spec_id: str, edge_limit: int, ends: Mapping[str, GraphNode]) -> NodeEdges:
    """The shown edges with their other ends, and every edge counted per relation and direction."""

    counts: dict[tuple[str, EdgeDirection], int] = {}
    for edge in edges:
        direction, _ = _other_end(edge, spec_id)
        counts[(edge.relation, direction)] = counts.get((edge.relation, direction), 0) + 1
    items = []
    for edge in shown_edges(edges, spec_id, edge_limit):
        direction, other = _other_end(edge, spec_id)
        end = ends.get(other)
        items.append(
            EdgeItem(
                relation=edge.relation,
                direction=direction,
                spec_id=other,
                label=end.label if end is not None else None,
                name=end.name if end is not None else None,
                basis=edge.basis,
                confidence=edge.confidence,
                rule=edge.inference_rule,
            )
        )
    by_relation = [
        RelationCount(relation=relation, direction=direction, count=count)
        for (relation, direction), count in sorted(counts.items())
    ]
    return NodeEdges(items=items, by_relation=by_relation, total=len(edges))


def node_plan_part(plan: NodePlan | None, holders: Holders) -> NodePlanPart | None:
    if plan is None:
        return None
    return NodePlanPart(plan_state=plan.plan_state, tasks=[_task_ref(task, holders) for task in plan.tasks])


def similar_view(project_id: str, found: SimilarResult, states: Mapping[str, PlanState] | None) -> SimilarNodes:
    """The similar nodes with their plan state (null without a plan)."""

    return SimilarNodes(
        project_id=project_id,
        sources=list(found.sources),
        unknown=list(found.unknown),
        items=[
            SimilarHit(
                **item.model_dump(exclude={"aliases"}),
                plan_state=states.get(item.spec_id, "unplanned") if states is not None else None,
            )
            for item in found.items
        ],
    )


def evidence_view(project_id: str, spec_id: str, items: Sequence[EvidenceItem]) -> Evidence:
    return Evidence(
        project_id=project_id,
        spec_id=spec_id,
        items=[EvidenceSource.model_validate(item.model_dump()) for item in items],
    )


def path_view(project_id: str, from_id: str, to_id: str, found: PathResult) -> NodePaths:
    return NodePaths(
        project_id=project_id,
        from_id=from_id,
        to_id=to_id,
        direction=found.direction,
        items=[PathChainItem.model_validate(chain.model_dump()) for chain in found.chains],
        reason=found.reason,
        link=PathChainItem.model_validate(found.link.model_dump()) if found.link is not None else None,
    )


def expansion_view(project_id: str, story_id: str, found: Expansion) -> BriefExpansion:
    return BriefExpansion(
        project_id=project_id,
        story_id=story_id,
        items=[ExpansionHit.model_validate(item.model_dump()) for item in found.items],
        tokens=found.tokens,
        left_out=found.left_out,
    )
