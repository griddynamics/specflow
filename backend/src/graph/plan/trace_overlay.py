"""``PlanService.trace``: the graph trace with plan state and open finding counts on every node ([trace.md](trace.md)
"Plan overlay"). One read of the spec graph and one of the plan serve the whole answer."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final

from graph.core.errors import ConfigurationError
from graph.core.reader import GraphNode
from graph.core.trace import MAX_SPEC_IDS, TraceResult
from graph.plan.changes import resolve_spec_id
from graph.plan.errors import PlanReadError
from graph.plan.invariants import CLOSED
from graph.plan.models import BriefQuestion, CurrentTask
from graph.plan.reader import LoadedPlan
from graph.plan.trace_models import PlanState, PlanTrace, PlanTraceNode, PlanTraceOwner, TraceStory, TraceTask

NO_PLAN: Final = "no_plan"
STORY_NOT_FOUND: Final = "story_not_found"
STATES: Final = ("completed", "in_progress", "open", "unplanned")


def check_seeds(story_id: str | None, spec_ids: Sequence[str] | None) -> None:
    """Exactly one of ``story_id`` and ``spec_ids`` (``invalid_arguments``)."""

    if (story_id is None) == (spec_ids is None):
        raise ConfigurationError("invalid_arguments")


def story_seeds(loaded: LoadedPlan, story_id: str, aliases: Mapping[str, str]) -> tuple[tuple[str, ...], bool]:
    """The anchor and the nodes the story's open and completed tasks implement, current ids, at most 50; and whether
    more were dropped."""

    if loaded.current.plan is None:
        raise PlanReadError(NO_PLAN)
    story = next((item for item in loaded.current.stories if item.story_id == story_id), None)
    if story is None or story.status != "active":
        raise PlanReadError(STORY_NOT_FOUND)
    found: dict[str, None] = {}
    if story.anchor:
        found[resolve_spec_id(story.anchor, aliases)] = None
    for task in sorted(loaded.story_tasks(story_id), key=lambda item: item.task_id):
        if task.status not in CLOSED:
            found.update(dict.fromkeys(resolve_spec_id(spec_id, aliases) for spec_id in task.implements))
    ids = tuple(found)
    return ids[:MAX_SPEC_IDS], len(ids) > MAX_SPEC_IDS


def plan_state(tasks: Collection[CurrentTask]) -> PlanState:
    """``completed`` (every task completed), ``in_progress`` (any in progress), ``open`` (any pending, failed or
    blocked), ``unplanned`` (no task)."""

    if not tasks:
        return "unplanned"
    if all(task.status == "completed" for task in tasks):
        return "completed"
    if any(task.status == "in_progress" for task in tasks):
        return "in_progress"
    return "open"


def tasks_by_node(loaded: LoadedPlan, aliases: Mapping[str, str]) -> dict[str, list[CurrentTask]]:
    """The open and completed tasks per current spec id of the nodes they implement (merged ids resolved)."""

    found: dict[str, list[CurrentTask]] = {}
    for task in sorted(loaded.current.tasks, key=lambda item: item.task_id):
        if task.status in CLOSED:
            continue
        for spec_id in {resolve_spec_id(item, aliases) for item in task.implements}:
            found.setdefault(spec_id, []).append(task)
    return found


def _shown(task: CurrentTask) -> TraceTask:
    return TraceTask(
        task_id=task.task_id,
        story_id=task.story_id,
        status=task.status,
        claimed_by=task.claimed_by if task.status == "in_progress" else None,
        commit=task.commit if task.status == "completed" else None,
    )


def overlay(
    result: TraceResult,
    loaded: LoadedPlan,
    by_node: Mapping[str, list[CurrentTask]],
    counts: Mapping[str, int],
    questions: Sequence[BriefQuestion],
    *,
    traced_story: str | None,
    seeds_truncated: bool,
) -> PlanTrace:
    """``result`` with each node's tasks, plan state and open finding count; the stories of those tasks (not the
    traced one), the summary and the open questions."""

    planned = loaded.current.plan is not None
    held: set[str] = set()

    def fields(spec_id: str) -> dict[str, Any]:
        tasks = by_node.get(spec_id, []) if planned else []
        held.update(task.story_id for task in tasks)
        return {
            "plan_state": plan_state(tasks) if planned else None,
            "tasks": tuple(_shown(task) for task in tasks),
            "findings": counts.get(spec_id, 0),
        }

    nodes = tuple(PlanTraceNode(**node.model_dump(), **fields(node.spec_id)) for node in result.nodes)
    context = tuple(PlanTraceOwner(**owner.model_dump(), **fields(owner.spec_id)) for owner in result.context)
    summary = {state: sum(1 for node in nodes if node.plan_state == state) for state in STATES} if planned else None
    stories = {story.story_id: story for story in loaded.current.stories if story.story_id in held}
    return PlanTrace(
        direction=result.direction,
        sources=result.sources,
        context=context,
        nodes=nodes,
        stories=tuple(
            TraceStory(
                story_id=story_id,
                title=story.title if story.status == "active" else None,
                area_id=story.area_id,
                progress=loaded.progress(story_id),
                holders=tuple(
                    sorted(
                        {
                            task.claimed_by
                            for task in loaded.story_tasks(story_id)
                            if task.status == "in_progress" and task.claimed_by
                        }
                    )
                ),
            )
            for story_id, story in sorted(stories.items())
            if story_id != traced_story
        ),
        summary=summary,
        open_questions=tuple(questions),
        unknown=result.unknown,
        truncated=result.truncated,
        seeds_truncated=seeds_truncated,
    )


def question_nodes(result: TraceResult, nodes: Mapping[str, GraphNode]) -> list[GraphNode]:
    """The graph nodes of the seeds and the reached nodes, the ones whose open questions the trace shows."""

    ids = dict.fromkeys([*result.sources, *(node.spec_id for node in result.nodes)])
    return [nodes[spec_id] for spec_id in ids if spec_id in nodes]
