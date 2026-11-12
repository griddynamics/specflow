"""The story brief: the brief parts of the open and completed tasks of one story, from one read of the spec graph and
the plan, merged by ``spec_id`` and without a model call ([lifecycle.md](lifecycle.md) "Story brief")."""

from __future__ import annotations

import json

from graph.plan.brief import brief_contracts, brief_neighbours, brief_node, brief_placement, open_questions
from graph.plan.brief_models import StoryBrief, StoryBriefTask
from graph.plan.changes import resolve_spec_id
from graph.plan.dag import topological_order
from graph.plan.errors import PlanReadError
from graph.plan.invariants import CLOSED
from graph.plan.models import BriefContract
from graph.plan.reader import LoadedPlan, PlanReader, node_summary, story_view
from graph.plan.rules import PlanRules
from graph.plan.story_board import StoryBoard

STORY_NOT_FOUND = "story_not_found"


async def story_brief(
    reader: PlanReader, loaded: LoadedPlan, project_id: str, story_id: str, *, rules: PlanRules, neighbours: int = 30
) -> StoryBrief:
    """The brief of an active story. Retired and unknown stories raise ``PlanReadError("story_not_found")``; closed
    tasks are history and never appear."""

    story = story_view(loaded, story_id)
    if story is None or story.status != "active":
        raise PlanReadError(STORY_NOT_FOUND)
    tasks = {task.task_id: task for task in loaded.story_tasks(story_id) if task.status not in CLOSED}
    order = topological_order(tasks, loaded.edges(tasks))
    nodes_list, edges, aliases = await reader.spec_graph(project_id)
    nodes = {node.spec_id: node for node in nodes_list}
    wanted = [spec_id for task_id in order for spec_id in tasks[task_id].implements]
    resolved = {spec_id: resolve_spec_id(spec_id, aliases) for spec_id in wanted}
    implemented = [nodes[spec_id] for spec_id in sorted(set(resolved.values())) if spec_id in nodes]
    plan = loaded.current.plan
    consumed = set(plan.consumed_build_ids) if plan is not None else set()
    contracts: dict[str, BriefContract] = {}
    for task_id in order:
        for contract in brief_contracts(loaded, task_id, aliases):
            if loaded.tasks[contract.task_id].story_id != story_id:
                contracts.setdefault(contract.task_id, contract)
    anchor = nodes.get(resolve_spec_id(story.anchor, aliases)) if story.anchor else None
    return StoryBrief(
        story=story,
        state=StoryBoard(loaded).state(story_id),
        anchor=node_summary(anchor) if anchor is not None else None,
        area=next((item for item in loaded.current.areas if item.area_id == story.area_id), None),
        profile=json.loads(plan.profile) if plan is not None and plan.profile else {},
        tasks=tuple(
            StoryBriefTask(
                task=loaded.plan_task(tasks[task_id]),
                done_when=tasks[task_id].done_when,
                progress=tasks[task_id].progress,
            )
            for task_id in order
        ),
        implements=tuple(brief_node(node, rules) for node in implemented),
        contracts=tuple(contracts[key] for key in sorted(contracts)),
        open_questions=open_questions(implemented, nodes, edges, rules),
        neighbours=brief_neighbours(implemented, nodes, edges, neighbours),
        placement=brief_placement(loaded, implemented, edges),
        spec_changed=any(node.last_build_id is not None and node.last_build_id not in consumed for node in implemented),
        missing=tuple(dict.fromkeys(spec_id for spec_id, survivor in resolved.items() if survivor not in nodes)),
    )
