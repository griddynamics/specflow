"""The plan's view of spec nodes: the state and the tasks that implement them (get_node, similar, trace)."""

from __future__ import annotations

from collections.abc import Collection, Mapping

from graph.plan.invariants import CLOSED
from graph.plan.models import PlanModel
from graph.plan.reader import LoadedPlan
from graph.plan.trace_models import PlanState, TraceTask
from graph.plan.trace_overlay import plan_state, shown_task


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
