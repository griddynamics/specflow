"""The inputs of the planner calls, rendered as messages.

Every specification or model text goes into a text block (``prompts.TextBlocks``); the JSON holds ids,
codes, counts and block ids only. ``estimate_plan`` renders the same messages without calling a model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from graph.plan.fallback import closing_story_id, setup_story_id, spec_interfaces
from graph.plan.identity import PROJECT_AREA_ID
from graph.plan.models import (
    DescribeArtifact,
    PlannedTask,
    PlanningPart,
    Profile,
    ProjectArtifact,
    TasksArtifact,
)
from graph.plan.planned import base_key, current_tasks
from graph.plan.prompts import TextBlocks, messages
from graph.plan.view import PlanView

_PROJECT_STORY_KINDS = frozenset({"setup", "closing"})
_TRUNCATION_FACTOR = 3


def describe_messages(view: PlanView, area_id: str, planner_members: int) -> tuple[list[dict[str, Any]], bool]:
    """The Describe call of an area, and whether its members were cut to ``3 * planner_members``."""

    blocks = TextBlocks()
    area = view.area(area_id)
    members = list(area.members)
    limit = _TRUNCATION_FACTOR * planner_members
    truncated = len(members) > limit
    if truncated:
        story_anchors = {story.anchor for story in view.area_stories(area_id) if story.anchor}
        first = [spec_id for spec_id in members if spec_id == area.anchor or spec_id in story_anchors]
        rest = [spec_id for spec_id in members if spec_id not in first]
        members = sorted([*first, *rest][: max(limit, len(first))])
    payload = {
        "area": {"area_id": area_id, "kind": area.kind, "anchor": area.anchor},
        "members": [_member(view, spec_id, blocks) for spec_id in members],
        "stories": _stories(view, area_id, blocks),
        "interfaces": _interfaces(view, area_id, blocks),
        "edges": _edges(view, members, blocks),
    }
    return messages("describe", payload, blocks), truncated


def project_inputs(view: PlanView) -> tuple[str, ...]:
    """The plannable nodes the Project planner sees: internal modules, ``project_input`` members and the
    members whose home story is ``SETUP`` (project-area nodes without a source chapter)."""

    found = set(view.internal_modules())
    found.update(spec_id for spec_id, node in view.nodes.items() if view.rules.rule_of(node.label).project_input)
    found.update(setup_members(view))
    return tuple(sorted(found))


def setup_members(view: PlanView) -> tuple[str, ...]:
    """Members the Project planner must cover: their home story is ``SETUP``."""

    setup = setup_story_id()
    return tuple(sorted(spec_id for spec_id, story in view.partition.home_story.items() if story == setup))


def project_edges(view: PlanView) -> tuple[Any, ...]:
    """Deployment edges and the edges between internal modules and external ones."""

    external = frozenset(
        spec_id for spec_id in view.context if view.rules.rule_of(view.context[spec_id].label).role != "context"
    )
    deployment = view.rules.relation("deploy")
    return tuple(
        edge
        for edge in sorted(view.snapshot.edges, key=lambda item: item.edge_key)
        if edge.relation == deployment or edge.source in external or edge.target in external
    )


def project_messages(view: PlanView, describe: DescribeArtifact) -> list[dict[str, Any]]:
    """The Project call."""

    blocks = TextBlocks()
    modules = view.internal_modules()
    external = sorted(
        spec_id for spec_id in view.context if view.rules.rule_of(view.context[spec_id].label).role != "context"
    )
    project_members = [spec_id for spec_id in project_inputs(view) if spec_id not in modules]
    payload = {
        "modules": [
            {**_member(view, spec_id, blocks, home=False), "area_id": view.area_of(spec_id)} for spec_id in modules
        ],
        "external": [
            {"spec_id": spec_id, "name": blocks.ref(f"name of {spec_id}", view.context[spec_id].name)}
            for spec_id in external
        ],
        "edges": [
            {"relation": edge.relation, "source": edge.source, "target": edge.target, "basis": edge.basis}
            for edge in project_edges(view)
        ],
        "members": [_member(view, spec_id, blocks) for spec_id in project_members],
        "areas": [
            {
                "area_id": area.area_id,
                "title": blocks.ref(f"title of {area.area_id}", _title(describe, area.area_id)),
                "interfaces": [item.spec_id for item in spec_interfaces(view, area.area_id)],
            }
            for area in view.active_areas
        ],
        "current": [
            _current_row(task, blocks)
            for task in current_tasks(view.snapshot.current, PROJECT_AREA_ID, [setup_story_id(), closing_story_id()])
        ],
    }
    return messages("project", payload, blocks)


def tasks_messages(
    view: PlanView,
    part: PlanningPart,
    *,
    describe: DescribeArtifact,
    project: ProjectArtifact,
    earlier: Sequence[PlannedTask],
    contract_tasks: Mapping[str, Sequence[str]],
    changed: Iterable[str],
    task_size: int,
) -> list[dict[str, Any]]:
    """The Tasks call of one planning part.

    ``contract_tasks`` maps an interface to the current or planned task keys that implement it;
    ``changed`` are the current task ids whose spec changed.
    """

    blocks = TextBlocks()
    area_id = part.area_id
    planned = describe.areas.get(area_id)
    changed_ids = frozenset(changed)
    questions = sorted({question for spec_id in part.members for question in view.open_questions(spec_id)})
    payload = {
        "part_id": part.part_id,
        "area": {
            "area_id": area_id,
            "title": blocks.ref(f"title of {area_id}", planned.title if planned else None),
            "description": blocks.ref(f"description of {area_id}", planned.description if planned else None),
            "interfaces": _interfaces(view, area_id, blocks),
        },
        "members": [_member(view, spec_id, blocks) for spec_id in part.members],
        "stories": [
            {
                "story_id": story_id,
                "kind": view.stories[story_id].kind,
                "title": blocks.ref(f"title of {story_id}", view.stories[story_id].title),
                "anchor": view.stories[story_id].anchor,
            }
            for story_id in part.story_ids
            if story_id in view.stories
        ],
        "open_questions": [
            {
                "spec_id": question,
                "statement": blocks.ref(f"open question {question}", view.statement(question) or None),
                "about": sorted(spec_id for spec_id in part.members if question in view.open_questions(spec_id)),
            }
            for question in questions
        ],
        "consumes": [
            {
                "interface": interface,
                "provider_area": provider,
                "contract_tasks": list(contract_tasks.get(interface, ())),
            }
            for interface, provider in view.consumed(area_id)
        ],
        "provides_to": list(view.consumers(area_id)),
        "integrates_with": list(view.integrations(area_id)),
        "profile": _profile(project.profile, blocks),
        "setup_tasks": [task.task_key for task in project.setup],
        "earlier": [_planned_row(task) for task in earlier],
        "current": [
            {**_current_row(task, blocks), "spec_changed": task.task_id in changed_ids}
            for task in current_tasks(view.snapshot.current, area_id)
            if area_id != PROJECT_AREA_ID or task.story_id not in (setup_story_id(), closing_story_id())
        ],
        "phase_guidance": view.rules.phase_guidance,
        "task_size": task_size,
    }
    return messages("tasks", payload, blocks)


def review_messages(
    view: PlanView,
    *,
    describe: DescribeArtifact,
    project: ProjectArtifact,
    tasks: Sequence[PlannedTask],
    waits: Mapping[str, Sequence[str]],
    tasks_artifact: TasksArtifact,
) -> list[dict[str, Any]]:
    """The Review call over the stitched plan; ``waits`` maps a task key to the keys it waits for."""

    blocks = TextBlocks()
    payload = {
        "areas": [
            {
                "area_id": area.area_id,
                "title": blocks.ref(f"title of {area.area_id}", _title(describe, area.area_id)),
                "interfaces": [item.spec_id or item.name for item in _area_interfaces(describe, area.area_id)],
                "members": list(area.members),
            }
            for area in view.active_areas
        ],
        "stories": [
            {"story_id": story.story_id, "title": blocks.ref(f"title of {story.story_id}", story.title)}
            for story in view.stories.values()
        ],
        "tasks": [
            {
                "task_id": task.task_key,
                "story": task.story_id,
                "phase": task.phase,
                "title": blocks.ref(f"title of {task.task_key}", task.title),
                "implements": list(task.implements),
                "waits_for": list(waits.get(task.task_key, ())),
                "done_when": [
                    {"text": blocks.ref(f"done when of {task.task_key}", item.text), "source": item.source}
                    for item in task.done_when
                ],
            }
            for task in tasks
        ],
        "not_buildable": [
            {"spec_id": entry.spec_id, "reason": entry.reason, "area_id": area_id}
            for area_id, entries in sorted(tasks_artifact.not_buildable.items())
            for entry in entries
        ],
        "conventions": [
            {"text": blocks.ref("convention", item.text), "spec_ids": list(item.spec_ids)}
            for item in project.profile.conventions
        ],
    }
    return messages("review", payload, blocks)


def _member(view: PlanView, spec_id: str, blocks: TextBlocks, *, home: bool = True) -> dict[str, Any]:
    node = view.nodes[spec_id]
    row: dict[str, Any] = {
        "spec_id": spec_id,
        "label": node.label,
        "name": blocks.ref(f"name of {spec_id}", node.name),
        "statement": blocks.ref(f"statement of {spec_id}", view.statement(spec_id) or None),
        "acceptance_criteria": blocks.refs(
            f"acceptance criterion of {spec_id}", view.rules.acceptance_of(node.attributes)
        ),
        "assumption": view.rules.is_assumption(node.attributes),
        "quote": blocks.ref(f"quote of {spec_id}", view.quote(spec_id) or None),
        "release": blocks.ref(f"release of {spec_id}", view.release_of(spec_id)),
    }
    if home:
        row["home_story"] = view.partition.home_story.get(spec_id)
    return row


def _stories(view: PlanView, area_id: str, blocks: TextBlocks) -> list[dict[str, Any]]:
    return [
        {
            "story_id": story.story_id,
            "kind": story.kind,
            "title": blocks.ref(f"title of {story.story_id}", story.title),
            "anchor": story.anchor,
        }
        for story in view.area_stories(area_id)
        if story.kind not in _PROJECT_STORY_KINDS
    ]


def _interfaces(view: PlanView, area_id: str, blocks: TextBlocks) -> list[dict[str, Any]]:
    return [
        {"spec_id": item.spec_id, "name": blocks.ref(f"name of {item.spec_id}", item.name), "kind": item.kind}
        for item in spec_interfaces(view, area_id)
    ]


def _edges(view: PlanView, members: Sequence[str], blocks: TextBlocks) -> list[dict[str, Any]]:
    ids = frozenset(members)
    rows = []
    for edge in view.touching(ids):
        for end, other, direction in ((edge.source, edge.target, "out"), (edge.target, edge.source, "in")):
            if end not in ids:
                continue
            node = view.nodes.get(other) or view.context.get(other)
            rows.append(
                {
                    "member": end,
                    "relation": edge.relation,
                    "direction": direction,
                    "node": other,
                    "name": blocks.ref(f"name of {other}", node.name if node else None),
                    "area": view.area_of(other),
                    "basis": edge.basis,
                    "confidence": edge.confidence,
                }
            )
    return rows


def _profile(profile: Profile, blocks: TextBlocks) -> dict[str, Any]:
    return {
        "languages": blocks.refs("language", profile.languages),
        "frameworks": blocks.refs("framework", profile.frameworks),
        "layout": blocks.ref("layout", profile.layout),
        "test_command": blocks.ref("test command", profile.test_command),
        "conventions": [
            {"text": blocks.ref("convention", item.text), "spec_ids": list(item.spec_ids)}
            for item in profile.conventions
        ],
    }


def _planned_row(task: PlannedTask) -> dict[str, Any]:
    return {
        "task_id": task.task_key,
        "action": task.action,
        "phase": task.phase,
        "story": task.story_id,
        "implements": list(task.implements),
    }


def _current_row(task: Any, blocks: TextBlocks) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "action": base_key(task.task_id).rsplit("::", 1)[-1],
        "phase": task.phase,
        "status": task.status,
        "story": task.story_id,
        "title": blocks.ref(f"title of {task.task_id}", task.title),
        "implements": list(task.implements),
    }


def _title(describe: DescribeArtifact, area_id: str) -> str | None:
    planned = describe.areas.get(area_id)
    return planned.title if planned else None


def _area_interfaces(describe: DescribeArtifact, area_id: str) -> Sequence[Any]:
    planned = describe.areas.get(area_id)
    return planned.interfaces if planned else ()
