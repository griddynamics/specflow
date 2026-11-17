"""The contract views of the agent writes: ``graph.gap`` and ``graph.plan`` results in, ``agent_contracts`` models out.
No user id, email or claim token leaves in a view; finding text is data."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from agent_contracts import (
    AddFindingBody,
    FindingDetail,
    FindingResult,
    Holder,
    RefersTo,
    ReportedTask,
    StoryCompletion,
    TaskReport,
)
from api.gap.development import finding_kind, finding_status
from graph.gap import GapView, RaisedGap, SourceRef
from graph.plan.models import TaskItem
from graph.plan.story_models import StoryCompletion as LibraryCompletion
from graph.plan.story_models import StoryTask

UNKNOWN_AUTHOR: Final = Holder(name=None, you=False)


def gap_finding_result(project_id: str, gap: GapView, raised: RaisedGap, refs: Sequence[SourceRef]) -> FindingResult:
    """``add_finding``'s answer for a development gap: the gap as a finding (its id is the gap id, FR-GDEV-01), whether
    it is new and the tasks a blocking one blocked. The author is filled by ``with_author``."""

    return FindingResult(
        project_id=project_id,
        finding=FindingDetail(
            finding_id=gap.gap_id,
            kind=finding_kind(gap.kind),
            text=gap.question,
            status=finding_status(gap.status),
            blocking=gap.blocking,
            about=[node.spec_id for node in gap.about],
            refers_to=[RefersTo(type=ref.type, value=ref.value) for ref in refs],
            story_id=gap.story_id,
            author=UNKNOWN_AUTHOR,
            created_at=gap.created_at,
            orphaned=not gap.about,
        ),
        created=raised.created,
        blocked=list(raised.held),
    )


def note_result(project_id: str, note_id: str, body: AddFindingBody, refs: Sequence[SourceRef]) -> FindingResult:
    """``add_finding``'s answer for a ``note``: no gap, a note of the story's nodes (status ``resolved``: nothing waits
    for a person)."""

    return FindingResult(
        project_id=project_id,
        finding=FindingDetail(
            finding_id=note_id,
            kind="note",
            text=body.text.strip(),
            status="resolved",
            blocking=False,
            about=list(dict.fromkeys(body.about)),
            refers_to=[RefersTo(type=ref.type, value=ref.value) for ref in refs],
            story_id=body.story_id,
            author=UNKNOWN_AUTHOR,
            created_at=datetime.now(UTC),
            orphaned=False,
        ),
        created=True,
        blocked=[],
    )


def with_author(result: FindingResult, author: Holder | None) -> FindingResult:
    finding = result.finding.model_copy(update={"author": author or UNKNOWN_AUTHOR})
    return result.model_copy(update={"finding": finding})


def task_report(project_id: str, task: StoryTask) -> TaskReport:
    shown = {name: getattr(task, name) for name in TaskItem.model_fields}
    return TaskReport(project_id=project_id, task=ReportedTask(**shown, progress=task.progress))


def completion_view(project_id: str, story_id: str, done: LibraryCompletion) -> StoryCompletion:
    return StoryCompletion(
        project_id=project_id,
        story_id=story_id,
        completed=list(done.completed),
        released=list(done.released),
        now_available=list(done.now_available),
        spec_changed=list(done.spec_changed),
    )
