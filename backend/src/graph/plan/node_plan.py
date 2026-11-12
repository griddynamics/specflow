"""The plan's view of spec nodes: the state and the tasks that implement them (get_node, similar, trace), and the
stories a caller holds that implement them (get_node, search_spec)."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Final

from graph.plan.invariants import CLOSED
from graph.plan.models import PlanModel
from graph.plan.reader import LoadedPlan
from graph.plan.trace_models import PlanState, TraceTask
from graph.plan.trace_overlay import plan_state, shown_task

MAX_HELD: Final = 5  # the most held stories one answer names


class NodePlan(PlanModel):
    """``plan_state`` of a node and its open and completed tasks, by task id (no claim token)."""

    plan_state: PlanState
    tasks: tuple[TraceTask, ...] = ()


def node_plan(loaded: LoadedPlan, spec_ids: Collection[str]) -> NodePlan | None:
    """The plan state and tasks of a node known by any of ``spec_ids`` (its id and aliases); ``None`` without a
    plan."""

    if loaded.current.plan is None:
        return None
    wanted = set(spec_ids)
    tasks = sorted(
        (task for task in loaded.current.tasks if task.status not in CLOSED and wanted & set(task.implements)),
        key=lambda task: task.task_id,
    )
    return NodePlan(plan_state=plan_state(tasks), tasks=tuple(shown_task(task) for task in tasks))


def plan_states(loaded: LoadedPlan, nodes: Mapping[str, Collection[str]]) -> dict[str, PlanState] | None:
    """The plan state of each node (``spec_id`` to its aliases), from one plan; ``None`` without a plan."""

    if loaded.current.plan is None:
        return None
    found: dict[str, PlanState] = {}
    for spec_id, aliases in nodes.items():
        plan = node_plan(loaded, {spec_id, *aliases})
        if plan is not None:
            found[spec_id] = plan.plan_state
    return found


class HeldStory(PlanModel):
    """A story ``caller`` holds and the given nodes its anchor or its open and completed tasks implement (possibly
    none), in the order the nodes were given."""

    story_id: str
    title: str
    spec_ids: tuple[str, ...] = ()


def held_stories(loaded: LoadedPlan, caller: str, nodes: Mapping[str, Collection[str]]) -> tuple[HeldStory, ...]:
    """The active stories ``caller`` holds (story claims), those implementing one of ``nodes`` (``spec_id`` to its
    aliases) first, then by ``story_id``; at most ``MAX_HELD``. Empty without a plan."""

    if loaded.current.plan is None:
        return ()
    held = sorted(
        (story for story in loaded.current.stories if story.status == "active" and story.claimed_by == caller),
        key=lambda story: story.story_id,
    )
    found: list[HeldStory] = []
    for story in held:
        names = {
            *([story.anchor] if story.anchor else ()),
            *(
                spec_id
                for task in loaded.story_tasks(story.story_id)
                if task.status not in CLOSED
                for spec_id in task.implements
            ),
        }
        matched = tuple(spec_id for spec_id, aliases in nodes.items() if names & {spec_id, *aliases})
        found.append(HeldStory(story_id=story.story_id, title=story.title, spec_ids=matched))
    found.sort(key=lambda story: not story.spec_ids)
    return tuple(found[:MAX_HELD])
