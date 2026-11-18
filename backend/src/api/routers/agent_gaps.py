"""The agent reads of gaps: ``list_gaps`` (``GET /agent/gaps``; tools.md "list_gaps", docs/graph/gap/api.md "MCP
tools", FR-GWEB-03) and ``list_open_gaps`` (``GET /agent/open-gaps``: the gaps that wait for a person's answer, as cards
for the chat, docs/mcp-server/apps.md "Terminal dialogs").

Same ``AgentRoute``, auth and membership-first dependency chain as ``api.routers.agent``. Exactly one of ``story_id``
and ``spec_ids`` (``invalid_arguments`` otherwise); a story is resolved through the plan to the nodes its tasks
implement (``no_plan``, ``story_not_found`` as for ``trace``), then ``GapService.list_gaps`` filters by ``about``
(aliases resolve). Agents see every open gap, also the ones a run held back from the inbox's default view
(``held_back`` is a people's inbox concern). Without a configured gap runner the project has no gaps: the list is
empty. ``list_open_gaps`` lists what the web inbox's default view lists (open, route ``ask``, not held back) and reads
each gap whole for its card (``api.gap.cards``); the card links to the web app at ``PUBLIC_URL``. Nothing here writes;
logs carry no question or answer text (NFR-GSEC-02).
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Query

from agent_contracts import GapItem, GapList, ListGapsQuery, ListOpenGapsQuery, OpenGapList
from api.agent_reads import agent_reads
from api.deps import AgentProjectDep, RuntimeDep
from api.errors import invalid_arguments, invalid_cursor
from api.gap.cards import gap_card, open_item
from api.routers.agent import AgentRoute, cursor_within_limit
from api.runtime import Runtime
from api.write_support import plan_read
from graph.gap import AnswerInvalidError, GapNotFoundError, GapView

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
