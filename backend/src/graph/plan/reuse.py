"""Reuse keys of the planner answers.

A key holds ids and meaning revisions only, never text (spec or model-written) and never node
fingerprints, so a reworded statement re-plans nothing and re-planning one area leaves every other
area's key unchanged. Prompt revisions are not part of a key. Every key includes the project id.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import Any

from graph.plan.models import PlanningPart, Profile, SpecEdgeView
from graph.plan.view import PlanView


def describe_key(view: PlanView, project_id: str, area_id: str) -> str:
    """The area's members with revisions, the edges touching them, its story ids and provided interfaces."""

    members = view.area(area_id).members
    return _digest(
        {
            "project": project_id,
            "area": area_id,
            "members": _revisions(view, members),
            "edges": _edges(view, members),
            "stories": sorted(story.story_id for story in view.area_stories(area_id)),
            "interfaces": list(view.provided(area_id)),
        }
    )


def project_key(view: PlanView, project_id: str, inputs: Iterable[str], edges: Sequence[SpecEdgeView]) -> str:
    """The project planner's input nodes with revisions, its edges, and every area's anchor and interfaces."""

    return _digest(
        {
            "project": project_id,
            "inputs": _revisions(view, inputs),
            "edges": sorted([edge.relation, edge.source, edge.target, edge.basis or ""] for edge in edges),
            "areas": [
                [area.area_id, area.anchor or "", list(view.provided(area.area_id))] for area in view.active_areas
            ],
        }
    )


def tasks_key(
    view: PlanView,
    project_id: str,
    part: PlanningPart,
    *,
    setup_tasks: Sequence[str],
    profile: Profile,
    task_size: int,
    planner_members: int,
) -> str:
    """The part's members with revisions and edges, its stories, related areas, setup tasks, the spec ids
    cited by conventions and the planning options."""

    return _digest(
        {
            "project": project_id,
            "part": part.part_id,
            "members": _revisions(view, part.members),
            "edges": _edges(view, part.members),
            "stories": list(part.story_ids),
            "related": list(view.related_areas(part.area_id)),
            "setup": sorted(setup_tasks),
            "conventions": sorted({spec_id for item in profile.conventions for spec_id in item.spec_ids}),
            "options": [task_size, planner_members],
        }
    )


def _revisions(view: PlanView, spec_ids: Iterable[str]) -> list[list[str]]:
    return [[spec_id, view.revision(spec_id)] for spec_id in sorted(set(spec_ids))]


def _edges(view: PlanView, spec_ids: Iterable[str]) -> list[list[str]]:
    ids = frozenset(spec_ids)
    rows = []
    for edge in view.touching(ids):
        for end, other, direction in ((edge.source, edge.target, "out"), (edge.target, edge.source, "in")):
            if end in ids:
                rows.append([end, edge.relation, direction, other, view.area_of(other) or "", edge.basis or ""])
    return sorted(rows)


def _digest(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
