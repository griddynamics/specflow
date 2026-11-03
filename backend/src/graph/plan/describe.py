"""The Describe step: title, description and interfaces of every active area, and its story summaries.

One call per area, areas in parallel. The spec Interfaces an area provides are always listed with the
name, kind and ``spec_id`` code takes from the node; the describer writes their summaries and may add
interfaces the text implies (``planned_by: model``).
"""

from __future__ import annotations

import asyncio
from typing import cast

from graph.plan.answers import DescribeAnswer
from graph.plan.fallback import describe_fallback, spec_interfaces
from graph.plan.inputs import describe_messages
from graph.plan.models import AreaInterface, DescribeArtifact, PlannedArea, PlannerOutcome
from graph.plan.names import InterfaceKind, PlanMode
from graph.plan.options import PlanContext
from graph.plan.planners import PlannerCall, run_planner
from graph.plan.prompts import prompt_ref
from graph.plan.reuse import describe_key
from graph.plan.validate import validate_describe
from graph.plan.view import PlanView


async def describe(context: PlanContext, view: PlanView, *, mode: PlanMode) -> DescribeArtifact:
    """Describe every active area."""

    areas = [area.area_id for area in view.active_areas]
    results = await asyncio.gather(*(_describe_area(context, view, area_id, mode=mode) for area_id in areas))
    planned: dict[str, PlannedArea] = {}
    summaries: dict[str, str] = {}
    outcomes: dict[str, PlannerOutcome] = {}
    truncated: list[str] = []
    for area_id, (area, area_summaries, outcome, was_truncated) in zip(areas, results, strict=True):
        planned[area_id] = area
        summaries.update(area_summaries)
        outcomes[area_id] = outcome
        if was_truncated:
            truncated.append(area_id)
    return DescribeArtifact(areas=planned, story_summaries=summaries, outcomes=outcomes, truncated=tuple(truncated))


async def _describe_area(
    context: PlanContext, view: PlanView, area_id: str, *, mode: PlanMode
) -> tuple[PlannedArea, dict[str, str], PlannerOutcome, bool]:
    messages, truncated = describe_messages(view, area_id, context.options.planner_members)
    call = PlannerCall(
        kind="describe",
        key=area_id,
        reuse_key=describe_key(view, context.project_id, area_id),
        messages=messages,
        answer_type=DescribeAnswer,
        validate=lambda answer: validate_describe(answer, view=view, area_id=area_id),
    )
    answer, outcome = await run_planner(context, call, mode=mode)
    if answer is None:
        area, summaries = describe_fallback(view, area_id)
        return area, summaries, outcome, truncated
    return _planned_area(view, area_id, answer), _summaries(answer), outcome, truncated


def _planned_area(view: PlanView, area_id: str, answer: DescribeAnswer) -> PlannedArea:
    summaries = {item.spec_id: item.summary for item in answer.interfaces if item.spec_id}
    interfaces: list[AreaInterface] = [
        item.model_copy(update={"summary": summaries.get(item.spec_id or "") or None})
        for item in spec_interfaces(view, area_id)
    ]
    interfaces.extend(
        AreaInterface(
            name=item.name,
            kind=cast(InterfaceKind, item.kind),
            spec_ids=tuple(sorted(set(item.spec_ids))),
            summary=item.summary or None,
            planned_by="model",
        )
        for item in answer.interfaces
        if not item.spec_id and item.name.strip()
    )
    return PlannedArea(
        title=answer.title.strip(),
        description=answer.description.strip() or None,
        interfaces=tuple(interfaces),
        planned_by="model",
        prompt_ref=prompt_ref("describe"),
    )


def _summaries(answer: DescribeAnswer) -> dict[str, str]:
    return {item.story_id: item.summary.strip() for item in answer.stories if item.summary.strip()}
