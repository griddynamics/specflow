"""The agent routes of gaps: ``list_gaps`` (``GET /agent/gaps``; tools.md "list_gaps", docs/graph/gap/api.md "MCP
tools", FR-GWEB-03), ``list_open_gaps`` (``GET /agent/open-gaps``: the gaps that wait for a person's answer, as cards
for the chat, docs/mcp-server/apps.md "Terminal dialogs") and the two routes of ``answer_gap``: ``GET /agent/gap-card``
(the card its dialog shows) and ``POST /agent/gap-answer`` (what the person chose in the dialog).

Same ``AgentRoute``, auth and membership-first dependency chain as ``api.routers.agent``. Exactly one of ``story_id``
and ``spec_ids`` (``invalid_arguments`` otherwise); a story is resolved through the plan to the nodes its tasks
implement (``no_plan``, ``story_not_found`` as for ``trace``), then ``GapService.list_gaps`` filters by ``about``
(aliases resolve). Agents see every open gap, also the ones a run held back from the inbox's default view
(``held_back`` is a people's inbox concern). Without a configured gap runner the project has no gaps: the list is
empty. ``list_open_gaps`` lists what the web inbox's default view lists (open, route ``ask``, not held back) and reads
each gap whole for its card (``api.gap.cards``); the card links to the web app at ``PUBLIC_URL``. Nothing here writes;
logs carry no question or answer text (NFR-GSEC-02).

``answer_gap`` writes only what the person sent from the dialog that the client showed on the MCP server's request,
never the agent's text (decision D6): the answer is recorded as the token's user with ``via`` ``elicitation``, with the
web card's checks and apply (``api.gap.answering``), and audited as ``api.agent.written``.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Query

from agent_contracts import (
    AnswerGapBody,
    GapAnswerRecord,
    GapCard,
    GapCardQuery,
    GapItem,
    GapList,
    ListGapsQuery,
    ListOpenGapsQuery,
    OpenGapList,
)
from api.agent_reads import agent_reads
from api.deps import AgentProjectDep, AgentUserDep, RuntimeDep
from api.errors import invalid_arguments, invalid_cursor
from api.gap.answering import answer_invalid, gap_not_found, gap_stale, gap_wrong_state, record_answer
from api.gap.cards import gap_card, open_item
from api.routers.agent import AgentRoute, cursor_within_limit
from api.runtime import Runtime
from api.write_support import plan_read, written
from graph.gap import (
    AnswerInput,
    AnswerInvalidError,
    GapNotFoundError,
    GapStateError,
    GapView,
    StaleGapError,
)

router = APIRouter(prefix="/agent", tags=["agent"], route_class=AgentRoute)

CURSOR: Final = "cursor"


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
        raise (invalid_cursor() if error.field == CURSOR else invalid_arguments(error.field)) from None
    return GapList(project_id=graph_id, items=[gap_item(gap) for gap in page.gaps], next_cursor=page.next)


def public_url(runtime: Runtime) -> str | None:
    """The web app's address the cards link to (``PUBLIC_URL``), None when unset."""

    return None if runtime.settings.mcp is None else runtime.settings.mcp.public_url


@router.get("/open-gaps", response_model=OpenGapList)
async def list_open_gaps(
    project: AgentProjectDep, runtime: RuntimeDep, args: Annotated[ListOpenGapsQuery, Query()]
) -> OpenGapList:
    """The gaps that wait for a person's answer, blockers first, then newest first, one page of cards. A gap dismissed
    between the page and its read is left out."""

    if args.cursor is not None:
        cursor_within_limit(args.cursor)
    graph_id = str(project.id)
    if runtime.gaps is None:
        return OpenGapList(project_id=graph_id, items=[], next_cursor=None)
    service = await runtime.gaps.service()
    try:
        page = await service.list_gaps(
            graph_id, status=["open"], route=["ask"], held_back=False, cursor=args.cursor, limit=args.limit
        )
    except AnswerInvalidError as error:
        raise (invalid_cursor() if error.field == CURSOR else invalid_arguments(error.field)) from None
    base = public_url(runtime)
    items = []
    for gap in page.gaps:
        try:
            whole = await service.get_gap(graph_id, gap.gap_id)
        except GapNotFoundError:
            continue
        items.append(open_item(gap_card(whole, graph_id, base)))
    return OpenGapList(project_id=graph_id, items=items, next_cursor=page.next)


OPEN: Final = "open"


@router.get("/gap-card", response_model=GapCard)
async def get_gap_card(
    project: AgentProjectDep, runtime: RuntimeDep, args: Annotated[GapCardQuery, Query()]
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
    return gap_card(gap, graph_id, public_url(runtime))


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
