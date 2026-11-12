"""The list items of tasks: what an agent needs to pick one, with its area, story and readiness."""

from __future__ import annotations

from collections.abc import Iterable

from graph.plan.models import AreaRef, CurrentTask, PlanTask, StoryRef, TaskItem
from graph.plan.reader import LoadedPlan
from graph.plan.story_models import StoryTask


def task_items(loaded: LoadedPlan, tasks: Iterable[PlanTask], *, ordered: bool = False) -> tuple[TaskItem, ...]:
    """The list items of ``tasks`` with the titles of their areas and active stories, sorted by ``task_id`` unless
    ``ordered`` keeps the given order."""

    ready = loaded.ready()
    areas = {area.area_id: area.title for area in loaded.current.areas}
    stories = {story.story_id: story.title for story in loaded.current.stories if story.status == "active"}
    listed = list(tasks) if ordered else sorted(tasks, key=lambda task: task.task_id)
    return tuple(
        TaskItem(
            task_id=task.task_id,
            title=task.title,
            phase=task.phase,
            status=task.status,
            complexity=task.complexity,
            area=AreaRef(area_id=task.area_id, title=areas.get(task.area_id)),
            story=StoryRef(story_id=task.story_id, title=stories.get(task.story_id)),
            ready=task.task_id in ready,
            waits_for=task.waits_for,
            planned_by=task.planned_by,
        )
        for task in listed
    )


def story_task_items(loaded: LoadedPlan, tasks: Iterable[CurrentTask]) -> tuple[StoryTask, ...]:
    """The items of ``tasks`` in the given order, each with its ``progress`` and completion record."""

    listed = list(tasks)
    items = task_items(loaded, [loaded.plan_task(task) for task in listed], ordered=True)
    return tuple(
        StoryTask(
            **item.model_dump(),
            progress=task.progress,
            commit=task.commit,
            completed_by=task.completed_by,
            completed_at=task.completed_at,
        )
        for item, task in zip(items, listed, strict=True)
    )
