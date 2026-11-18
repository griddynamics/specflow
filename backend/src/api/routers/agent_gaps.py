"""The agent routes of gaps: ``list_gaps`` (``GET /agent/gaps``; tools.md "list_gaps", docs/graph/gap/api.md "MCP
tools", FR-GWEB-03), ``browse_gaps``, ``open_gap`` and the two routes of ``answer_gap``: ``GET /agent/gap-card`` (the
card its dialog shows) and ``POST /agent/gap-answer`` (what the person chose in the dialog).

Same ``AgentRoute``, auth and membership-first dependency chain as ``api.routers.agent``. Exactly one of ``story_id``
and ``spec_ids`` (``invalid_arguments`` otherwise); a story is resolved through the plan to the nodes its tasks
implement (``no_plan``, ``story_not_found`` as for ``trace``), then ``GapService.list_gaps`` filters by ``about``
(aliases resolve). Agents see every open gap, also the ones a run held back from the inbox's default view
(``held_back`` is a people's inbox concern). Without a configured gap runner the project has no gaps: the list is
empty. A card links to the web app at ``PUBLIC_URL``. Nothing here writes; logs carry no question or answer text
(NFR-GSEC-02).

``browse_gaps`` (``GET /agent/gap-inbox``) lists what the web Gaps screen lists (route ``ask``, held back or not) with
its filters, as rows that name each assignee; ``open_gap`` (``GET /agent/gap-detail``) reads one gap whole, in any
status, as its web card shows it (``api.gap.cards.gap_detail``).

``answer_gap`` writes only what the person sent from the dialog that the client showed on the MCP server's request,
never the agent's text (decision D6): the answer is recorded as the token's user with ``via`` ``elicitation``, with the
web card's checks and apply (``api.gap.answering``), and audited as ``api.agent.written``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Query

from agent_contracts import (
    GAP_STAGES,
    AnswerGapBody,
    AssignedFilter,
    BrowseGapsQuery,
    GapAnswerRecord,
    GapCard,
    GapCardQuery,
    GapDetail,
    GapItem,
    GapList,
    GapRowList,
    ListGapsQuery,
)
from api.agent_reads import agent_reads
from api.agent_views import holders_of
from api.deps import AgentProjectDep, AgentUserDep, RuntimeDep
from api.errors import ApiError, invalid_arguments, invalid_cursor
from api.gap.answering import answer_invalid, gap_not_found, gap_stale, gap_wrong_state, record_answer
from api.gap.cards import detail_user_ids, gap_card, gap_detail, gap_row
from api.routers.agent import AgentRoute, cursor_within_limit
from api.runtime import Runtime
from api.write_support import plan_read, written
from db import members
from graph.gap import (
    AnswerInput,
    AnswerInvalidError,
    GapNotFoundError,
    GapStateError,
    GapView,
    StaleGapError,
)
from graph.gap.assignment import NO_ASSIGNEE

router = APIRouter(prefix="/agent", tags=["agent"], route_class=AgentRoute)

CURSOR: Final = "cursor"
ASK: Final = "ask"  # the route of the gaps people answer: the web inbox lists only these


def _refused(error: AnswerInvalidError) -> ApiError:
    return invalid_cursor() if error.field == CURSOR else invalid_arguments(error.field)


def gap_item(gap: GapView) -> GapItem:
    """A gap as ``list_gaps`` shows it: the answer's text once a clarification exists (its own text, else the
    chosen option's label, else, for a pointer, the answering lines of the spec: FR-GDEV-04)."""

    answer = None
    if gap.clarification is not None:
        lines = "\n".join(quote.quote for quote in gap.clarification.basis)
        answer = gap.clarification.text or gap.clarification.option_label or lines or None
    return GapItem(
        gap_id=gap.gap_id,
        kind=gap.kind,
        severity=gap.severity,
        status=gap.status,
        blocking=gap.blocking,
        question=gap.question,
        about=[node.spec_id for node in gap.about],
        answer=answer,
    )


@router.get("/gaps", response_model=GapList)
async def list_gaps(project: AgentProjectDep, runtime: RuntimeDep, args: Annotated[ListGapsQuery, Query()]) -> GapList:
    """The gaps about a story's nodes or about spec ids: blockers first, then newest first, one page."""

    if (args.story_id is None) == (args.spec_ids is None):
        raise invalid_arguments()
    if args.cursor is not None:
        cursor_within_limit(args.cursor)
    graph_id = str(project.id)
    if args.story_id is not None:
        plan = (await agent_reads(runtime)).plan
        story_id = args.story_id
        about = list(await plan_read(lambda: plan.story_nodes(graph_id, story_id)))
    else:
        about = list(args.spec_ids or ())
    if runtime.gaps is None or not about:
        return GapList(project_id=graph_id, items=[], next_cursor=None)
    service = await runtime.gaps.service()
    try:
        page = await service.list_gaps(graph_id, about=about, status=args.status, cursor=args.cursor, limit=args.limit)
    except AnswerInvalidError as error:
        raise _refused(error) from None
    return GapList(project_id=graph_id, items=[gap_item(gap) for gap in page.gaps], next_cursor=page.next)


def public_url(runtime: Runtime) -> str | None:
    """The web app's address the cards link to (``PUBLIC_URL``), None when unset."""

    return None if runtime.settings.mcp is None else runtime.settings.mcp.public_url


def _assigned_to(assigned: AssignedFilter | None, caller: uuid.UUID) -> str | None:
    """The ``assigned_to`` filter of ``list_gaps``: the caller's user id for ``me``, nobody for ``unassigned``."""

    if assigned is None:
        return None
    return str(caller) if assigned == "me" else NO_ASSIGNEE


@router.get("/gap-inbox", response_model=GapRowList)
async def browse_gaps(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, args: Annotated[BrowseGapsQuery, Query()]
) -> GapRowList:
    """The gaps of the web Gaps screen (route ``ask``, held back or not) with its filters, by default the ones not
    resolved; blockers first, then reach, then newest; one page of rows, each naming its assignee. ``assigned`` keeps
    the caller's gaps or the unassigned ones; ``assignee`` the gaps of the one member with that name (unknown, shared
    or given with ``assigned``: ``invalid_arguments``); ``story_id`` as for ``list_gaps``."""

    if args.cursor is not None:
        cursor_within_limit(args.cursor)
    if args.assigned is not None and args.assignee is not None:
        raise invalid_arguments("assignee")
    graph_id = str(project.id)
    if args.assignee is None:
        assigned_to = _assigned_to(args.assigned, user.id)
    else:
        assigned_to = await _member_named(runtime, project.id, user.id, args.assignee)
    about = None
    if args.story_id is not None:
        plan = (await agent_reads(runtime)).plan
        story_id = args.story_id
        about = list(await plan_read(lambda: plan.story_nodes(graph_id, story_id)))
    if runtime.gaps is None or about == []:
        return GapRowList(project_id=graph_id, items=[], next_cursor=None)
    service = await runtime.gaps.service()
    try:
        page = await service.list_gaps(
            graph_id,
            status=None if args.status is None else [status for name in args.status for status in GAP_STAGES[name]],
            kind=args.kind,
            severity=args.severity,
            route=[ASK],
            about=about,
            assigned_to=assigned_to,
            cursor=args.cursor,
            limit=args.limit,
        )
    except AnswerInvalidError as error:
        raise _refused(error) from None
    holders = await holders_of(runtime, project.id, user.id, [gap.assigned_to for gap in page.gaps])
    rows = [gap_row(gap, holders.of(gap.assigned_to)) for gap in page.gaps]
    return GapRowList(project_id=graph_id, items=rows, next_cursor=page.next)


def _folded(name: str) -> str:
    return " ".join(name.split()).casefold()


async def _member_named(runtime: Runtime, project_id: uuid.UUID, caller: uuid.UUID, name: str) -> str:
    """The user id of the one member of the project named ``name``, ignoring case and spacing, as the rows name them
    (a display name that is an address is no name, ``db.members.shown_name``); else ``invalid_arguments``."""

    wanted = _folded(name)
    async with runtime.transaction() as session:
        listing = await members.list_members(session, project_id, caller)
    found = [row.user_id for row in listing.members if row.name is not None and _folded(row.name) == wanted]
    if len(found) != 1:
        raise invalid_arguments("assignee")
    return str(found[0])


@router.get("/gap-detail", response_model=GapDetail)
async def open_gap(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, args: Annotated[GapCardQuery, Query()]
) -> GapDetail:
    """One gap whole, in any status, as its web card shows it, with the members it names: ``gap_not_found`` for a gap
    the project does not have (none while gap runs are not configured). An alias the gap keeps finds it too."""

    graph_id = str(project.id)
    if runtime.gaps is None:
        raise gap_not_found()
    service = await runtime.gaps.service()
    try:
        gap = await service.get_gap(graph_id, args.gap_id)
    except GapNotFoundError:
        raise gap_not_found() from None
    holders = await holders_of(runtime, project.id, user.id, detail_user_ids(gap))
    return gap_detail(gap, graph_id, public_url(runtime), holders)


OPEN: Final = "open"


@router.get("/gap-card", response_model=GapCard)
async def get_gap_card(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, args: Annotated[GapCardQuery, Query()]
) -> GapCard:
    """The card of one gap that waits for a person's answer, with its parts for the dialog of ``answer_gap``:
    ``gap_not_found`` for a gap the project does not have (none while gap runs are not configured),
    ``gap_wrong_state`` once it is no longer open."""

    graph_id = str(project.id)
    if runtime.gaps is None:
        raise gap_not_found()
    service = await runtime.gaps.service()
    try:
        gap = await service.get_gap(graph_id, args.gap_id)
    except GapNotFoundError:
        raise gap_not_found() from None
    if gap.status != OPEN:
        raise gap_wrong_state(GapStateError(status=gap.status, allowed=(OPEN,), gap_id=gap.gap_id))
    holders = await holders_of(runtime, project.id, user.id, [gap.assigned_to])
    return gap_card(gap, graph_id, public_url(runtime), holders.of(gap.assigned_to))


@router.post("/gap-answer", response_model=GapAnswerRecord)
async def answer_gap(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, body: AnswerGapBody
) -> GapAnswerRecord:
    """Record what the person chose in the dialog: an option by rank, or their own text (exactly one, else
    ``invalid_arguments``), checked against the gap's ``updated_at`` as the dialog showed it. Refusals:
    ``gap_not_found`` (also while gap runs are not configured), ``gap_wrong_state``, ``gap_stale``, ``answer_invalid``;
    nothing is written on a refusal."""

    if (body.option_rank is None) == (body.own_text is None):
        raise invalid_arguments("answer")
    graph_id = str(project.id)
    runner = runtime.gaps
    if runner is None:
        raise gap_not_found()
    answer = AnswerInput(
        gap_id=body.gap_id,
        option_rank=body.option_rank,
        own_text=body.own_text,
        answered_by=str(user.id),
        updated_at=body.updated_at,
        via="elicitation",
    )
    ids: dict[str, str | None] = {"gap_id": body.gap_id, "clr_id": None}

    async def record() -> tuple[GapAnswerRecord, bool]:
        try:
            result = await record_answer(runner, graph_id, answer)
        except GapNotFoundError:
            raise gap_not_found() from None
        except GapStateError as error:
            raise gap_wrong_state(error) from None
        except StaleGapError:
            raise gap_stale() from None
        except AnswerInvalidError as error:
            raise answer_invalid(error) from None
        ids["clr_id"] = result.clr_id
        recorded = GapAnswerRecord(
            gap_id=result.gap_id,
            clr_id=result.clr_id,
            placement=result.placement,
            apply_at=result.apply_at,
            immediate=result.immediate,
        )
        return recorded, False

    return await written("gap-answer", graph_id, user, ids, record)
