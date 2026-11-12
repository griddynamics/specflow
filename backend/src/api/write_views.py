"""The contract views of the agent writes: ``graph.gap`` and ``graph.plan`` results in, ``agent_contracts`` models out.
No user id, email or claim token leaves in a view; finding text is data."""

from __future__ import annotations

from agent_contracts import FindingDetail, FindingResult, Holder, RefersTo, ReportedTask, StoryCompletion, TaskReport
from api.agent_views import Holders
from graph.gap import FlowAdded
from graph.plan.models import TaskItem
from graph.plan.story_models import StoryCompletion as LibraryCompletion
from graph.plan.story_models import StoryTask


def finding_result(project_id: str, added: FlowAdded, holders: Holders) -> FindingResult:
    finding = added.finding
    return FindingResult(
        project_id=project_id,
        finding=FindingDetail(
            finding_id=finding.finding_id,
            kind=finding.kind,
            text=finding.text,
            status=finding.status,
            blocking=finding.blocking,
            about=list(finding.about_spec_ids),
            refers_to=[RefersTo(type=ref.type, value=ref.value) for ref in finding.refers_to],
            story_id=finding.story_id,
            author=holders.of(finding.created_by) or Holder(name=None, you=False),
            created_at=finding.created_at,
            orphaned=finding.orphaned,
        ),
        created=added.created,
        blocked=list(added.blocked),
    )


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
