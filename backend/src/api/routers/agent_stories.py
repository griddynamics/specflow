"""The people tools and the first write routes: ``list_areas``, ``list_stories``, ``preview_story`` (``GET``) and
``claim_story``, ``release_story`` (``POST``, JSON body) (tools.md, architecture.md "Story and spec routes", "Write
routes").

Same ``AgentRoute``, auth and membership-first dependency chain as ``api.routers.agent``. The caller's user id comes
from the token (``AgentUserDep``), never from an argument. A write route checks the argument formats before the
library call, calls one ``PlanService`` story call and maps the library's refusals to the tool codes and typed
``details``; nothing is written on a refusal. Each write logs one ``api.agent.written`` event: route, ids, outcome and
``repeat``, never a label, branch, note, name or claim token. Nothing here writes SQL or Cypher.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Query

from agent_contracts import (
    AreaList,
    ClaimStoryBody,
    ErrorDetails,
    ListStoriesQuery,
    PreviewStoryArgs,
    ReleaseStoryBody,
    StoryClaim,
    StoryList,
    StoryPreview,
    StoryRelease,
)
from api import story_views as views
from api.agent_reads import AgentReads, agent_reads
from api.agent_views import holders_of
from api.deps import AgentProjectDep, AgentUserDep, RuntimeDep
from api.errors import ApiError, not_available, partly_claimed, story_claimed
from api.routers.agent import AgentRoute, cursor_within_limit
from api.runtime import Runtime
from api.write_support import plan_read, printable, write_refusal, written
from db.models import Project, User
from graph.plan.arguments import check_branch, check_label
from graph.plan.errors import TaskStateError

router = APIRouter(prefix="/agent", tags=["agent"], route_class=AgentRoute)

_FINDING_IDS: Final = 200  # the most blocking findings of the caller that the ``mine`` view looks at
StoryIdQuery = Annotated[str, *PreviewStoryArgs.model_fields["story_id"].metadata]


# --- reads ----------------------------------------------------------------------------------------------------


@router.get("/areas", response_model=AreaList)
async def list_areas(project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep) -> AreaList:
    """Every active area with its stories per state; the holders come from the held stories, oldest claim first."""

    graph_id = str(project.id)
    plan = (await agent_reads(runtime)).plan
    summaries = await plan_read(lambda: plan.area_summary(graph_id))
    rows = (
        await plan.story_list(graph_id, view="open", caller=str(user.id)) if any(s.holders for s in summaries) else ()
    )
    holder_ids = views.area_holder_ids(summaries, rows)
    holders = await holders_of(runtime, project.id, user.id, [who for ids in holder_ids.values() for who in ids])
    return AreaList(project_id=graph_id, items=views.area_items(summaries, holder_ids, holders))


@router.get("/stories", response_model=StoryList)
async def list_stories(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, args: Annotated[ListStoriesQuery, Query()]
) -> StoryList:
    """One page of a view of the plan's stories; the cursor holds the project, area, view and the rank it ended at."""

    graph_id = str(project.id)
    caller = str(user.id)
    if args.cursor is not None:
        cursor_within_limit(args.cursor)
    first = views.decode_story_cursor(args.cursor, graph_id, args.area_id, args.view) if args.cursor else 0
    reads = await agent_reads(runtime)
    blocking = await _blocking_findings(reads, graph_id, caller) if args.view == "mine" else ()
    rows = await plan_read(
        lambda: reads.plan.story_list(
            graph_id, view=args.view, caller=caller, area_id=args.area_id, finding_ids=blocking
        )
    )
    ordered = views.ordered_rows(rows, args.view, caller)
    page = ordered[first : first + args.limit]
    last = first + len(page)
    found = await reads.plan.story_findings(graph_id, [row.story_id for row in page]) if page else ()
    holders = await holders_of(runtime, project.id, user.id, views.story_row_user_ids(page))
    return StoryList(
        project_id=graph_id,
        area_id=args.area_id,
        view=args.view,
        total_count=len(ordered),
        items=views.story_items(page, first + 1, {item.story_id: item for item in found}, holders),
        next_cursor=views.encode_story_cursor(graph_id, args.area_id, args.view, last) if last < len(ordered) else None,
    )


@router.get("/story-preview", response_model=StoryPreview)
async def preview_story(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, story_id: StoryIdQuery
) -> StoryPreview:
    """One story before a claim: state, holder, unit, open tasks in build order, blockers, findings, unblocks."""

    graph_id = str(project.id)
    plan = (await agent_reads(runtime)).plan
    preview = await plan_read(lambda: plan.story_preview(graph_id, story_id))
    holders = await holders_of(runtime, project.id, user.id, views.preview_user_ids(preview))
    return views.preview_view(graph_id, preview, holders)


async def _blocking_findings(reads: AgentReads, graph_id: str, caller: str) -> tuple[str, ...]:
    """The ids of the caller's open blocking findings: they put the stories they blocked into the ``mine`` view."""

    if reads.findings is None:
        return ()
    page = await reads.findings.findings(graph_id, created_by=caller, blocking=True, limit=_FINDING_IDS)
    return tuple(item.finding_id for item in page.findings)


# --- writes ---------------------------------------------------------------------------------------------------


@router.post("/story-claim", response_model=StoryClaim)
async def claim_story(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, body: ClaimStoryBody
) -> StoryClaim:
    """Claim the story's unit for the caller (idempotent: the holder claiming again gets the same token)."""

    printable(agent_label=(body.agent_label, check_label), branch=(body.branch, check_branch))
    graph_id = str(project.id)
    reads = await agent_reads(runtime)

    async def claim() -> tuple[StoryClaim, bool]:
        try:
            claimed = await reads.plan.claim_story(
                graph_id, body.story_id, claimed_by=str(user.id), agent_label=body.agent_label, branch=body.branch
            )
        except TaskStateError as error:
            raise await _claim_refusal(runtime, reads, project, user, story_id=body.story_id, error=error) from None
        return views.claim_view(graph_id, body.story_id, claimed), claimed.repeat

    return await written("story-claim", graph_id, user, {"story_id": body.story_id}, claim)


@router.post("/story-release", response_model=StoryRelease)
async def release_story(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, body: ReleaseStoryBody
) -> StoryRelease:
    """End the caller's claim of the story; a repeat with the same token answers ``released: []``."""

    printable(branch=(body.branch, check_branch))
    graph_id = str(project.id)
    reads = await agent_reads(runtime)

    async def release() -> tuple[StoryRelease, bool]:
        try:
            released = await reads.plan.release_story(
                graph_id,
                body.story_id,
                claimed_by=str(user.id),
                claim_token=body.claim_token,
                note=body.note,
                branch=body.branch,
            )
        except TaskStateError as error:
            raise await write_refusal(reads, graph_id, body.story_id, error) from None
        return views.release_view(graph_id, body.story_id, released), released.repeat

    return await written("story-release", graph_id, user, {"story_id": body.story_id}, release)


async def _claim_refusal(
    runtime: Runtime, reads: AgentReads, project: Project, user: User, *, story_id: str, error: TaskStateError
) -> ApiError:
    """The API error of a refused claim, with the typed details of tools.md ("claim_story")."""

    graph_id = str(project.id)
    caller = str(user.id)
    if error.code == "story_claimed":
        rows = await reads.plan.story_list(graph_id, view="open", caller=caller)
        by_id = {row.story_id: row for row in rows}
        holder_ids = [error.claimed_by, *(by_id[other].claimed_by for other in error.alternatives if other in by_id)]
        holders = await holders_of(runtime, project.id, user.id, holder_ids)
        asked = by_id.get(story_id)
        since = datetime.fromisoformat(error.claimed_at) if error.claimed_at else None
        details = ErrorDetails(
            holder=holders.of(error.claimed_by, asked.agent_label if asked else None),
            since=since,
            alternatives=views.capped_alternatives(
                [by_id[other] for other in error.alternatives if other in by_id], holders
            ),
        )
        return story_claimed(views.details_dict(details))
    if error.code == "partly_claimed":
        holders = await holders_of(runtime, project.id, user.id, [error.claimed_by])
        return partly_claimed(
            views.details_dict(ErrorDetails(holder=holders.of(error.claimed_by), task_id=error.task_id))
        )
    if error.code == "not_available":
        preview = await reads.plan.story_preview(graph_id, story_id)
        holders = await holders_of(runtime, project.id, user.id, [blocker.claimed_by for blocker in preview.blockers])
        found = [await reads.findings.finding(graph_id, item) for item in preview.blocked_by] if reads.findings else []
        details = ErrorDetails(
            state=preview.state,
            blockers=views.capped_blockers(preview.blockers, holders),
            findings=views.finding_refs(found),
        )
        return not_available(views.details_dict(details))
    return await write_refusal(reads, graph_id, story_id, error)
