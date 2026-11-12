"""The story reads for people, each from one loaded plan: the area summary, the story lists and the story preview
(api.md "Story reads for people"). Holders are user ids; no row carries a claim token. A story without a story claim
whose task is ``in_progress`` under a task claim shows that task's holder (the oldest task claim).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Final, get_args

from graph.core.errors import ConfigurationError
from graph.plan.errors import PlanReadError
from graph.plan.invariants import OPEN
from graph.plan.models import CurrentTask, StoryInfo, StoryRef
from graph.plan.names import StoryView
from graph.plan.reader import LoadedPlan
from graph.plan.story_board import STATE_ORDER, StoryBoard
from graph.plan.story_models import AreaSummary, PreviewTask, StoryBlocker, StoryPreview, StoryRow

STORY_NOT_FOUND: Final = "story_not_found"
AREA_NOT_FOUND: Final = "area_not_found"
_OLDEST: Final = datetime.min.replace(tzinfo=UTC)


def area_summary(loaded: LoadedPlan) -> tuple[AreaSummary, ...]:
    """Per active area, the project area last: stories per state, the holders of its held stories, open tasks."""

    board = StoryBoard(loaded)
    areas = sorted(
        (area for area in loaded.current.areas if area.status == "active"),
        key=lambda area: (area.kind == "project", area.area_id),
    )
    summaries = []
    for area in areas:
        stories = [
            story for story in board.stories.values() if story.area_id == area.area_id and story.status == "active"
        ]
        counts: dict[str, int] = dict.fromkeys(STATE_ORDER, 0)
        for story in stories:
            counts[board.state(story.story_id)] += 1
        summaries.append(
            AreaSummary(
                area_id=area.area_id,
                title=area.title,
                kind=area.kind,
                stories=counts,
                holders=tuple(sorted({who for story in stories if (who := holder(loaded, story)[0])})),
                open_tasks=sum(
                    1 for task in loaded.current.tasks if task.area_id == area.area_id and task.status in OPEN
                ),
            )
        )
    return tuple(summaries)


def story_list(
    loaded: LoadedPlan, *, view: str, caller: str, area_id: str | None = None, finding_ids: Collection[str] = ()
) -> tuple[StoryRow, ...]:
    """The active stories as rows in the order of ``view`` (``open``, ``mine`` or ``done``); ``rank`` counts from 1."""

    if view not in get_args(StoryView):
        raise ConfigurationError("invalid_field")
    if area_id is not None and all(area.area_id != area_id for area in loaded.current.areas):
        raise PlanReadError(AREA_NOT_FOUND)
    board = StoryBoard(loaded)
    stories = [
        story
        for story in board.stories.values()
        if story.status == "active" and (area_id is None or story.area_id == area_id)
    ]
    blocked = frozenset(finding_ids)
    if view == "open":
        picked = [story for story in stories if board.state(story.story_id) != "completed"]
    elif view == "mine":
        picked = [story for story in stories if story.claimed_by == caller or _blocked_by(loaded, story, blocked)]
    else:
        picked = [story for story in stories if board.state(story.story_id) == "completed"]
    if view == "done":
        ordered = sorted(picked, key=lambda story: _newest(loaded, story.story_id))
    else:
        ordered = sorted(picked, key=lambda story: board.rank_key(story.story_id))
    return tuple(_row(loaded, board, story, rank) for rank, story in enumerate(ordered, start=1))


def story_preview(loaded: LoadedPlan, story_id: str) -> StoryPreview:
    """The story, state, claim, unit, open tasks in topological order, blockers outside the unit and ``unblocks``."""

    board = StoryBoard(loaded)
    story = board.stories.get(story_id)
    if story is None or story.status != "active":
        raise PlanReadError(STORY_NOT_FOUND)
    unit = board.unit_of(story_id)
    ready = loaded.ready()
    claimed_by, claimed_at = holder(loaded, story)
    by_id = {task.task_id: task for task in board.open_tasks(unit)}
    return StoryPreview(
        story_id=story_id,
        area_id=story.area_id,
        title=story.title,
        kind=story.kind,
        summary=story.summary,
        release=story.release,
        state=board.state(story_id),
        claimed_by=claimed_by,
        claimed_at=claimed_at,
        agent_label=story.agent_label,
        branch=story.branch,
        note=story.note,
        unit=unit,
        tasks=tuple(
            PreviewTask(
                task_id=task_id,
                title=by_id[task_id].title,
                status=by_id[task_id].status,
                ready=task_id in ready,
                complexity=by_id[task_id].complexity,
                progress=by_id[task_id].progress,
            )
            for task_id in board.ordered(by_id)
        ),
        blockers=tuple(
            StoryBlocker(
                story_id=other,
                title=_title(board, other),
                state=board.state(other),
                claimed_by=holder(loaded, board.stories[other])[0],
            )
            for other in board.blocking_stories(story_id)
        ),
        unblocks=tuple(StoryRef(story_id=other, title=_title(board, other)) for other in board.unblocks(story_id)),
    )


def _row(loaded: LoadedPlan, board: StoryBoard, story: StoryInfo, rank: int) -> StoryRow:
    state = board.state(story.story_id)
    tasks = loaded.story_tasks(story.story_id)
    last = _last_completed(tasks) if state == "completed" else None
    claimed_by, claimed_at = holder(loaded, story)
    return StoryRow(
        rank=rank,
        story_id=story.story_id,
        area_id=story.area_id,
        title=story.title,
        kind=story.kind,
        release=story.release,
        state=state,
        claimed_by=claimed_by,
        claimed_at=claimed_at,
        agent_label=story.agent_label,
        waits_on=()
        if state == "completed"
        else tuple(StoryRef(story_id=other, title=_title(board, other)) for other in board.waits_on(story.story_id)),
        tasks=sum(1 for task in tasks if task.status in OPEN),
        unblocks=len(board.unblocks(story.story_id)),
        blocked_by=tuple(sorted({task.blocked_by for task in tasks if task.status == "blocked" and task.blocked_by})),
        pr_url=story.pr_url if state == "completed" else None,
        commit=last.commit if last else None,
        completed_by=last.completed_by if last else None,
        completed_at=last.completed_at if last else None,
    )


def holder(loaded: LoadedPlan, story: StoryInfo) -> tuple[str | None, datetime | None]:
    """Who holds the story and since when: its story claim, else the oldest task claim of its ``in_progress`` tasks."""

    if story.claimed_by is not None:
        return story.claimed_by, story.claimed_at
    claimed = [task for task in loaded.story_tasks(story.story_id) if task.status == "in_progress" and task.claimed_by]
    first = min(claimed, key=lambda task: (task.claimed_at or _OLDEST, task.task_id), default=None)
    return (first.claimed_by, first.claimed_at) if first is not None else (None, None)


def _blocked_by(loaded: LoadedPlan, story: StoryInfo, findings: frozenset[str]) -> bool:
    return any(task.status == "blocked" and task.blocked_by in findings for task in loaded.story_tasks(story.story_id))


def _last_completed(tasks: list[CurrentTask]) -> CurrentTask | None:
    done = [task for task in tasks if task.status == "completed"]
    return max(done, key=lambda task: (task.completed_at or _OLDEST, task.task_id), default=None)


def _newest(loaded: LoadedPlan, story_id: str) -> tuple[float, str]:
    """Sort key for newest first: the negated time of the last completed task, then the story id."""

    last = _last_completed(loaded.story_tasks(story_id))
    stamp = (last.completed_at if last and last.completed_at else _OLDEST).timestamp()
    return (-stamp, story_id)


def _title(board: StoryBoard, story_id: str) -> str | None:
    story = board.stories.get(story_id)
    return story.title if story is not None and story.status == "active" else None
