"""The agent write routes: ``add_finding``, ``report_task`` and ``complete_story`` (``POST``, JSON body) (tools.md,
architecture.md "Write routes").

Same ``AgentRoute``, auth and membership-first dependency chain as ``api.routers.agent_stories``; the caller is the
token's user. A route checks the formats before any read or write (``refers_to`` values are checked, never fetched),
calls one library call (``GapService.raise_gap`` or ``add_note``, ``PlanService.report_progress`` or
``complete_story``) and maps its refusals to the tool codes; nothing is written on a refusal. The library checks under
the story lock that the caller holds the story the token claims (the claim token alone is not enough). The
``api.agent.written`` event carries ids, the outcome and ``repeat`` only: never finding text, a note, a label or a
claim token. Nothing here writes SQL or Cypher.
"""

from __future__ import annotations

from typing import Final, cast

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
)
from api.finding_refs import InvalidRefError, checked_refs
from api.gap.development import development_service, gap_kind
from api.routers.agent import AgentRoute
from api.write_support import plan_read, write_refusal, written
from graph.core.errors import ConfigurationError
from graph.gap import AnswerInvalidError, GapKind, GapService, NewGap, SourceRef
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
    """Record a finding as a development gap (docs/graph/gap/lifecycle.md "Development gaps", FR-GDEV-01): a repeat
    answers the open gap with the same question, story and nodes; ``blocking`` also blocks the caller's claimed story
    (FR-GDEV-02); a new gap is investigated at once in a ``site`` run (FR-GDEV-03). A ``note`` is no gap: it is kept
    as a note of the story's nodes."""

    graph_id, caller = str(project.id), str(user.id)
    try:
        refs = checked_refs((ref.type, ref.value) for ref in body.refers_to)
    except InvalidRefError:
        raise invalid_arguments("refers_to") from None
    lacking = _lacking(body)
    if lacking is not None:
        raise invalid_arguments(lacking)
    reads = await agent_reads(runtime)
    service = await development_service(runtime)
    ids: dict[str, str | None] = {"story_id": body.story_id, "finding_id": None}

    async def add() -> tuple[FindingResult, bool]:
        if body.story_id is not None:
            story_id = body.story_id
            await plan_read(lambda: reads.plan.story_preview(graph_id, story_id))
        try:
            result = await _added(service, graph_id, caller, refs, body)
        except AnswerInvalidError as error:
            raise await _gap_refusal(reads, graph_id, body, error) from None
        except TaskStateError as error:
            raise await write_refusal(reads, graph_id, body.story_id or "", error) from None
        if result.created and result.finding.kind != "note" and runtime.gaps is not None:
            await runtime.gaps.start_site(graph_id, result.finding.finding_id)
        ids["finding_id"] = result.finding.finding_id
        author = await _author(service, graph_id, result, caller)
        holders = await holders_of(runtime, project.id, user.id, [author])
        return views.with_author(result, holders.of(author)), not result.created

    return await written("findings", graph_id, user, ids, add)


async def _added(
    service: GapService, graph_id: str, caller: str, refs: tuple[SourceRef, ...], body: AddFindingBody
) -> FindingResult:
    """The library call: ``GapService.add_note`` for a note, else ``raise_gap`` with origin ``development``."""

    if body.kind == "note":
        note_id = await service.add_note(
            graph_id, spec_ids=body.about, text=body.text, created_by=caller, story_id=body.story_id
        )
        return views.note_result(graph_id, note_id, body, refs)
    new = NewGap(
        kind=cast(GapKind, gap_kind(body.kind)),
        question=body.text,
        about=tuple(body.about),
        origin="development",
        blocking=body.blocking,
        story_id=body.story_id,
        claim_token=body.claim_token if body.blocking else None,
        created_by=caller,
        refs=refs,
    )
    raised = await service.raise_gap(graph_id, new)
    gap = await service.get_gap(graph_id, raised.gap_id)
    return views.gap_finding_result(graph_id, gap, raised, refs if raised.created else ())


async def _author(service: GapService, graph_id: str, result: FindingResult, caller: str) -> str:
    """Who reported the finding: the caller of a new gap or a note, the first reporter of a repeat (another member
    may have asked the same question about the same nodes from the same story)."""

    if result.created or result.finding.kind == "note":
        return caller
    return (await service.get_gap(graph_id, result.finding.finding_id)).created_by


def _lacking(body: AddFindingBody) -> str | None:
    """The argument a blocking finding lacks: a ``note`` never blocks; blocking needs a story and its claim token."""

    if not body.blocking:
        return None
    if body.kind == "note":
        return "kind"
    if body.story_id is None:
        return "story_id"
    return "claim_token" if body.claim_token is None else None


async def _gap_refusal(reads: AgentReads, graph_id: str, body: AddFindingBody, error: AnswerInvalidError) -> Exception:
    """The tool error of a refused development gap: unknown ids are ``node_not_found`` naming them, the open-gap
    limit ``finding_limit``, a text out of bounds ``invalid_arguments`` (``text``)."""

    if error.field == "about" and error.reason == "unknown_ids":
        missing = [spec_id for spec_id in body.about if await reads.reader.node(graph_id, spec_id) is None]
        return node_not_found(story_views.details_dict(ErrorDetails(spec_ids=missing)))
    if error.field == "limit":
        return finding_limit()
    if error.field in ("question", "text"):
        return invalid_arguments("text")
    return invalid_arguments("about")


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
