"""The agent write routes: ``add_finding``, ``report_task`` and ``complete_story`` (``POST``, JSON body) (tools.md,
architecture.md "Write routes").

Same ``AgentRoute``, auth and membership-first dependency chain as ``api.routers.agent_stories``; the caller is the
token's user. A route checks the formats before any read or write (``refers_to`` values are checked, never fetched),
calls one library call (``FindingFlow.add_finding``, ``PlanService.report_progress`` or ``complete_story``) and maps
its refusals to the tool codes; nothing is written on a refusal. The library checks under the story lock that the
caller holds the story the token claims (the claim token alone is not enough). The ``api.agent.written`` event
carries ids, the outcome and ``repeat`` only: never finding text, a note, a label or a claim token. Nothing here
writes SQL or Cypher.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter

from agent_contracts import (
    AddFindingBody,
    CompleteStoryBody,
    ErrorDetails,
    FindingResult,
    ReportTaskBody,
    StoryCompletion,
    TaskReport,
)
from api import story_views
from api import write_views as views
from api.agent_reads import AgentReads, agent_reads
from api.agent_views import holders_of
from api.deps import AgentProjectDep, AgentUserDep, RuntimeDep
from api.errors import (
    finding_limit,
    invalid_arguments,
    invalid_commit,
    node_not_found,
    required,
)
from api.routers.agent import AgentRoute
from api.write_support import plan_read, write_refusal, written
from graph.core.errors import ConfigurationError
from graph.gap import FindingError, FindingFlow, FindingRef, FlowAdded, NewFinding
from graph.gap.arguments import references
from graph.plan.arguments import check_pr_url, commit_text
from graph.plan.errors import TaskStateError
from graph.plan.names import TaskProgress

router = APIRouter(prefix="/agent", tags=["agent"], route_class=AgentRoute)

# The argument a library check names, for ``details.argument`` of ``invalid_arguments``.
_ARGUMENTS: Final = {
    "invalid_kind": "kind",
    "invalid_text": "text",
    "invalid_about": "about",
    "invalid_ref": "refers_to",
    "invalid_story": "story_id",
    "invalid_done": "done",
    "invalid_field": "pr_url",
}


def _refused(error: ConfigurationError) -> Exception:
    """``invalid_arguments`` naming the argument a library check refused (never its value); any other
    ``ConfigurationError`` is a bug and stays as it is."""

    argument = _ARGUMENTS.get(error.code)
    return invalid_arguments(argument) if argument is not None else error


# --- add_finding ----------------------------------------------------------------------------------------------


@router.post("/findings", response_model=FindingResult)
async def add_finding(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, body: AddFindingBody
) -> FindingResult:
    """Record a finding; a repeat answers the open finding with the same dedupe key, before the blocking rules;
    ``blocking`` also blocks the caller's claimed story."""

    graph_id, caller = str(project.id), str(user.id)
    try:
        refs = references([(ref.type, ref.value) for ref in body.refers_to])
    except ConfigurationError:
        raise invalid_arguments("refers_to") from None
    reads = await agent_reads(runtime)
    ids: dict[str, str | None] = {"story_id": body.story_id, "finding_id": None}

    async def add() -> tuple[FindingResult, bool]:
        if body.story_id is not None:
            story_id = body.story_id
            await plan_read(lambda: reads.plan.story_preview(graph_id, story_id))
        try:
            added = await _flow(reads, graph_id, caller, refs, body)
        except FindingError as error:
            raise _finding_refusal(error) from None
        except ConfigurationError as error:
            raise _refused(error) from None
        except TaskStateError as error:
            raise await write_refusal(reads, graph_id, body.story_id or "", error) from None
        ids["finding_id"] = added.finding.finding_id
        holders = await holders_of(runtime, project.id, user.id, [added.finding.created_by])
        return views.finding_result(graph_id, added, holders), not added.created

    return await written("findings", graph_id, user, ids, add)


async def _flow(
    reads: AgentReads, graph_id: str, caller: str, refs: tuple[FindingRef, ...], body: AddFindingBody
) -> FlowAdded:
    """The library call. A blocking finding that lacks what blocking needs is refused only after the dedupe, so a
    repeat answers whatever its flags say (tools.md "Repeats")."""

    findings = required(reads.findings, "the findings store")
    new = NewFinding(
        created_by=caller,
        kind=body.kind,
        text=body.text,
        about=tuple(body.about),
        refers_to=refs,
        story_id=body.story_id,
        blocking=body.blocking,
    )
    lacking = _lacking(body)
    if lacking is None:
        return await FindingFlow(findings, reads.plan).add_finding(graph_id, new, claim_token=body.claim_token)
    repeat = await findings.match(graph_id, created_by=caller, kind=body.kind, about=new.about, text=body.text)
    if repeat is None:
        raise invalid_arguments(lacking)
    return FlowAdded(finding=repeat, created=False)


def _lacking(body: AddFindingBody) -> str | None:
    """The argument a blocking finding lacks: a ``note`` never blocks; blocking needs a story and its claim token."""

    if not body.blocking:
        return None
    if body.kind == "note":
        return "kind"
    if body.story_id is None:
        return "story_id"
    return "claim_token" if body.claim_token is None else None


def _finding_refusal(error: FindingError) -> Exception:
    if error.code == "node_not_found":
        return node_not_found(story_views.details_dict(ErrorDetails(spec_ids=list(error.spec_ids))))
    if error.code == "finding_limit":
        return finding_limit()
    return error


# --- report_task ----------------------------------------------------------------------------------------------


@router.post("/task-progress", response_model=TaskReport)
async def report_task(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, body: ReportTaskBody
) -> TaskReport:
    """Record progress on one task of the caller's claimed story; the task status stays."""

    if body.progress == "skipped" and not (body.note and body.note.strip()):
        raise invalid_arguments("note")
    graph_id, caller = str(project.id), str(user.id)
    reads = await agent_reads(runtime)
    ids: dict[str, str | None] = {"task_id": body.task_id, "story_id": None}

    async def report() -> tuple[TaskReport, bool]:
        story_id, before = await _task_progress(reads, graph_id, body.task_id)
        ids["story_id"] = story_id
        try:
            task = await reads.plan.report_progress(
                graph_id,
                body.task_id,
                claimed_by=caller,
                claim_token=body.claim_token,
                progress=body.progress,
                note=body.note,
            )
        except TaskStateError as error:
            raise await write_refusal(reads, graph_id, story_id or body.task_id, error) from None
        return views.task_report(graph_id, task), before == body.progress

    return await written("task-progress", graph_id, user, ids, report)


async def _task_progress(reads: AgentReads, graph_id: str, task_id: str) -> tuple[str | None, TaskProgress | None]:
    """The story of the in-progress task and the progress it has now, for the audit event (ids, ``repeat``). The
    holder check is the library's, under the story lock."""

    items = await plan_read(lambda: reads.plan.list_tasks(graph_id, ready_only=False, status="in_progress"))
    item = next((found for found in items if found.task_id == task_id), None)
    if item is None:
        return None, None
    preview = await plan_read(lambda: reads.plan.story_preview(graph_id, item.story.story_id))
    return item.story.story_id, next((task.progress for task in preview.tasks if task.task_id == task_id), None)


# --- complete_story -------------------------------------------------------------------------------------------


@router.post("/story-completion", response_model=StoryCompletion)
async def complete_story(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, body: CompleteStoryBody
) -> StoryCompletion:
    """Close the caller's claimed story with the merge commit; the only path to ``completed`` for story tasks."""

    try:
        commit = commit_text(body.merge_commit)
    except ConfigurationError:
        raise invalid_commit() from None
    try:
        check_pr_url(body.pr_url)
    except ConfigurationError:
        raise invalid_arguments("pr_url") from None
    graph_id, caller = str(project.id), str(user.id)
    reads = await agent_reads(runtime)

    async def complete() -> tuple[StoryCompletion, bool]:
        try:
            done = await reads.plan.complete_story(
                graph_id,
                body.story_id,
                claim_token=body.claim_token,
                completed_by=caller,
                merge_commit=commit,
                pr_url=body.pr_url,
                done=body.done,
            )
        except ConfigurationError as error:
            raise _refused(error) from None
        except TaskStateError as error:
            raise await write_refusal(reads, graph_id, body.story_id, error) from None
        return views.completion_view(graph_id, body.story_id, done), done.repeat

    return await written("story-completion", graph_id, user, {"story_id": body.story_id}, complete)
