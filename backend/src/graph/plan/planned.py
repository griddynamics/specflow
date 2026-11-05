"""Building target tasks from planner answers, current tasks and rules.

Code sets ``release`` (the distinct releases of the implemented nodes) and ``has_open_questions`` on
every task, whoever planned it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from graph.plan.answers import TaskAnswer
from graph.plan.identity import REVISION_PATTERN, task_key
from graph.plan.models import CurrentPlan, CurrentTask, DoneWhen, PlannedTask
from graph.plan.names import Complexity, DoneWhenSource, Phase
from graph.plan.view import PlanView

OPEN_OR_COMPLETED = frozenset({"pending", "in_progress", "failed", "blocked", "completed"})
_WAITS_FOR = "WAITS_FOR"
_PLANNER_SOURCE = "planner"


def planned_task(view: PlanView, *, area_id: str, action: str, **fields: Any) -> PlannedTask:
    """A target task of an area with the code-set fields filled in; ``fields`` are the other
    ``PlannedTask`` fields (``implements`` and ``waits`` are sorted and made unique)."""

    members = tuple(sorted(set(fields.pop("implements", ()))))
    waits = tuple(sorted(set(fields.pop("waits", ()))))
    return PlannedTask(
        task_key=task_key(area_id, action),
        area_id=area_id,
        action=action,
        implements=members,
        release=view.releases(members),
        has_open_questions=view.has_open_questions(members),
        waits=waits,
        **fields,
    )


def from_answer(
    view: PlanView,
    answer: TaskAnswer,
    *,
    area_id: str,
    story_id: str,
    prompt_ref: str,
    wait_keys: Mapping[str, str],
) -> PlannedTask:
    """A validated planner task; ``wait_keys`` maps every known action to its task key."""

    return planned_task(
        view,
        area_id=area_id,
        story_id=story_id,
        action=answer.action,
        phase=cast(Phase, answer.phase),
        title=answer.title.strip() or answer.action,
        description=answer.description or None,
        done_when=[DoneWhen(text=item.text, source=cast(DoneWhenSource, item.source)) for item in answer.done_when],
        complexity=cast(Complexity, answer.complexity),
        implements=answer.implements,
        waits=[wait_keys[action] for action in answer.waits_for],
        planned_by="model",
        prompt_ref=prompt_ref,
    )


def current_tasks(current: CurrentPlan, area_id: str, story_ids: Sequence[str] | None = None) -> list[CurrentTask]:
    """The latest revision of each open or completed current task of an area, optionally of some stories."""

    latest: dict[str, CurrentTask] = {}
    for task in current.tasks:
        if task.area_id != area_id or task.status not in OPEN_OR_COMPLETED:
            continue
        if story_ids is not None and task.story_id not in story_ids:
            continue
        key = base_key(task.task_id)
        if key not in latest or task.revision > latest[key].revision:
            latest[key] = task
    return [latest[key] for key in sorted(latest)]


def from_current(view: PlanView, current: CurrentPlan, task: CurrentTask) -> PlannedTask:
    """A current task kept as it is (a fallback keeps current work); planner waits inside its area kept."""

    by_id = {item.task_id: item for item in current.tasks}
    waits = [
        base_key(link.target)
        for link in current.links
        if link.type == _WAITS_FOR
        and link.source == task.task_id
        and dict(link.properties).get("source") == _PLANNER_SOURCE
        and link.target in by_id
        and by_id[link.target].area_id == task.area_id
    ]
    return planned_task(
        view,
        area_id=task.area_id,
        story_id=task.story_id,
        action=task.action,
        phase=task.phase,
        title=task.title,
        description=task.description,
        done_when=task.done_when,
        complexity=task.complexity,
        implements=[spec_id for spec_id in task.implements if spec_id in view.nodes],
        waits=waits,
        planned_by=task.planned_by,
        prompt_ref=task.prompt_ref,
    )


def base_key(task_id: str) -> str:
    """A task id without its revision suffix."""

    return REVISION_PATTERN.sub("", task_id)
