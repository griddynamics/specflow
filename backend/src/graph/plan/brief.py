"""The task brief: what one coding agent needs for one task, assembled from the plan and the current spec
graph when it is asked for (so finished prerequisites show). It makes no model call and never contains
other tasks' briefs, prompts or the whole graph.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Literal

from graph.core.reader import GraphEdge, GraphNode
from graph.plan.changes import resolve_spec_id
from graph.plan.errors import TaskStateError
from graph.plan.invariants import CLOSED
from graph.plan.models import (
    BriefContract,
    BriefNeighbour,
    BriefNode,
    BriefPlacement,
    BriefQuestion,
    TaskBrief,
)
from graph.plan.reader import LoadedPlan, PlanReader, story_view
from graph.plan.rules import PlanRules

_IN_AREA = "IN_AREA"


async def task_brief(
    reader: PlanReader,
    loaded: LoadedPlan,
    project_id: str,
    task_id: str,
    *,
    rules: PlanRules,
    neighbours: int = 30,
) -> TaskBrief:
    """The brief of an open or completed task."""

    task = loaded.tasks.get(task_id)
    if task is None:
        raise TaskStateError("unknown_task")
    if task.status in CLOSED:
        raise TaskStateError("wrong_state", status=task.status)
    nodes_list, edges, aliases = await reader.spec_graph(project_id)
    nodes = {node.spec_id: node for node in nodes_list}
    resolved = [resolve_spec_id(spec_id, aliases) for spec_id in task.implements]
    implemented = [nodes[spec_id] for spec_id in dict.fromkeys(resolved) if spec_id in nodes]
    plan = loaded.current.plan
    consumed = set(plan.consumed_build_ids) if plan is not None else set()
    area = next((item for item in loaded.current.areas if item.area_id == task.area_id), None)
    return TaskBrief(
        task=loaded.plan_task(task),
        done_when=task.done_when,
        story=story_view(loaded, task.story_id),
        area=area,
        profile=json.loads(plan.profile) if plan is not None and plan.profile else {},
        implements=tuple(brief_node(node, rules) for node in implemented),
        contracts=brief_contracts(loaded, task_id, aliases),
        open_questions=open_questions(implemented, nodes, edges, rules),
        neighbours=brief_neighbours(implemented, nodes, edges, neighbours),
        placement=brief_placement(loaded, implemented, edges),
        spec_changed=any(node.last_build_id is not None and node.last_build_id not in consumed for node in implemented),
        missing=tuple(
            spec_id for spec_id, survivor in zip(task.implements, resolved, strict=True) if survivor not in nodes
        ),
    )


def brief_node(node: GraphNode, rules: PlanRules) -> BriefNode:
    return BriefNode(
        spec_id=node.spec_id,
        label=node.label,
        name=node.name,
        attributes=dict(node.attributes),
        statements=tuple(statement.model_dump() for statement in node.statements),
        provenance=tuple(item.model_dump() for item in node.provenance),
        assumption=rules.is_assumption(node.attributes),
    )


def brief_contracts(loaded: LoadedPlan, task_id: str, aliases: Mapping[str, str]) -> tuple[BriefContract, ...]:
    """Completed direct prerequisites, each with the interfaces of its own area that cite its nodes."""

    interfaces = {area.area_id: area.interfaces for area in loaded.current.areas}
    found = []
    for prerequisite in loaded.waits.get(task_id, ()):
        done = loaded.tasks.get(prerequisite)
        if done is None or done.status != "completed":
            continue
        cited = {resolve_spec_id(spec_id, aliases) for spec_id in done.implements}
        found.append(
            BriefContract(
                task_id=done.task_id,
                title=done.title,
                commit=done.commit,
                implements=done.implements,
                interfaces=tuple(
                    dict(item)
                    for item in interfaces.get(done.area_id, ())
                    if item.get("spec_id") in cited or cited & set(item.get("spec_ids") or ())
                ),
            )
        )
    return tuple(found)


def open_questions(
    implemented: Sequence[GraphNode], nodes: Mapping[str, GraphNode], edges: Sequence[GraphEdge], rules: PlanRules
) -> tuple[BriefQuestion, ...]:
    reference = rules.relation("reference")
    ids = {node.spec_id for node in implemented}
    found: dict[tuple[str, str], BriefQuestion] = {}
    for edge in edges:
        if edge.relation != reference:
            continue
        for about, other in ((edge.source, edge.target), (edge.target, edge.source)):
            node = nodes.get(other)
            if about in ids and node is not None and rules.rule_of(node.label).role == "context":
                found[(other, about)] = BriefQuestion(
                    spec_id=other,
                    statement=" ".join(statement.text for statement in node.statements),
                    provenance=tuple(item.model_dump() for item in node.provenance),
                    about=about,
                )
    return tuple(found[key] for key in sorted(found))


def brief_neighbours(
    implemented: Sequence[GraphNode], nodes: Mapping[str, GraphNode], edges: Sequence[GraphEdge], limit: int
) -> tuple[BriefNeighbour, ...]:
    ids = {node.spec_id for node in implemented}
    found = []
    for edge in edges:
        ends: tuple[tuple[str, str, Literal["out", "in"]], ...] = (
            (edge.source, edge.target, "out"),
            (edge.target, edge.source, "in"),
        )
        for of, other, direction in ends:
            node = nodes.get(other)
            if of in ids and node is not None and other not in ids:
                found.append(
                    BriefNeighbour(
                        spec_id=other,
                        label=node.label,
                        name=node.name,
                        relation=edge.relation,
                        direction=direction,
                        of=of,
                        basis=edge.basis,
                        confidence=edge.confidence,
                    )
                )
    found.sort(key=lambda item: (item.relation, item.spec_id, item.of))
    return tuple(found[: max(limit, 0)])


def brief_placement(
    loaded: LoadedPlan, implemented: Sequence[GraphNode], edges: Sequence[GraphEdge]
) -> tuple[BriefPlacement, ...]:
    by_key = {edge.edge_key: edge for edge in edges if edge.edge_key}
    links = {link.source: dict(link.properties) for link in loaded.current.links if link.type == _IN_AREA}
    found = []
    for node in implemented:
        properties = links.get(node.spec_id, {})
        via = []
        for key in properties.get("via") or ():
            edge = by_key.get(key)
            via.append(
                {
                    "edge_key": key,
                    "relation": edge.relation if edge else None,
                    "source": edge.source if edge else None,
                    "target": edge.target if edge else None,
                    "basis": edge.basis if edge else None,
                    "confidence": edge.confidence if edge else None,
                }
            )
        rule = properties.get("rule")
        found.append(BriefPlacement(spec_id=node.spec_id, rule=int(rule) if rule is not None else None, via=tuple(via)))
    return tuple(found)
