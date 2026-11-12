"""Open findings per story for the story lists (api.md ``story_findings``): how many are open about the story and the
nodes its tasks implement, and the oldest open blocking finding raised on a story of its claim unit (a blocking
finding about a shared node raised on another story is counted, never shown as this story's question). Read through
the ``FindingsReads`` port, one read per story; without a port every story has none. Finding text is the agent's own
words: never logged."""

from __future__ import annotations

from collections.abc import Sequence

from graph.plan.findings_port import FindingsReads, OpenFinding, open_findings_about
from graph.plan.models import PlanModel
from graph.plan.reader import LoadedPlan
from graph.plan.story_board import StoryBoard


class StoryFindings(PlanModel):
    """The open findings of one story: their count (at most 200) and the oldest blocking one raised on its unit."""

    story_id: str
    count: int
    question: OpenFinding | None


async def story_findings(
    loaded: LoadedPlan, port: FindingsReads | None, project_id: str, story_ids: Sequence[str]
) -> tuple[StoryFindings, ...]:
    """Per story of ``story_ids`` (unknown or retired ones count 0), in the order asked."""

    stories = {story.story_id: story for story in loaded.current.stories if story.status == "active"}
    board = StoryBoard(loaded)
    found = []
    for story_id in dict.fromkeys(story_ids):
        story = stories.get(story_id)
        implemented = [spec_id for task in loaded.story_tasks(story_id) for spec_id in task.implements]
        ids = [story.anchor, *implemented] if story is not None and story.anchor else implemented
        open_ones = await open_findings_about(port, project_id, ids) if story is not None else ()
        unit = set(board.unit_of(story_id)) if story is not None else set()
        blocking = [item for item in open_ones if item.blocking and item.story_id in unit]
        found.append(
            StoryFindings(
                story_id=story_id,
                count=len(open_ones),
                question=min(blocking, key=lambda item: (item.created_at, item.finding_id), default=None),
            )
        )
    return tuple(found)
