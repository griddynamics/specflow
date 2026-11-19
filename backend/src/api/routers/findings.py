"""The findings of a project for people (web audience): aliases of the development gaps' routes for one release
(docs/graph/gap/api.md "Web routes": ``GET /findings`` is ``GET /gaps?origin=development``; resolve and dismiss are
``dismiss_gap``).

A finding reported by a coding agent is a development gap (lifecycle.md "Development gaps"): the list shows the
project's development gaps in the gaps' order, ``node`` keeps those about one spec node (aliases resolve), ``status``
``open`` the open, answered and applying ones, ``resolved`` the resolved ones (a dismissed gap is gone, so
``dismissed`` lists none). Resolve and dismiss both dismiss the open gap with the note as its reason (``Resolved`` or
``Dismissed`` without one): the gap's holds end, so its story shows ``ready`` again. Access is the project membership
every web route applies. A refusal writes nothing. The ``api.findings.closed`` event carries ids and the outcome, never
a note or a question. ``503 gap_unavailable`` while gap runs are not configured.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Path, Query

from api.agent_reads import agent_reads
from api.agent_views import holders_of
from api.deps import RuntimeDep, UserDep
from api.errors import ApiError, invalid_arguments, plan_applying
from api.gap.development import OPEN_GAP_STATUSES, development_service
from api.routers.agent import AgentRoute
from api.routers.documents import require_project
from api.work_models import (
    ClosedFindingOut,
    CloseFindingBody,
    FindingsOut,
    FindingStatusQuery,
    finding_out,
    user_ids,
)
from graph.gap import AnswerInvalidError, GapNotFoundError, GapStateError, GapView
from graph.plan.errors import PlanReadError, TaskStateError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.findings")
router = APIRouter(prefix="/projects/{pid}/findings", tags=["findings"], route_class=AgentRoute)

_MAX_ID = 200
_NO_NOTE = CloseFindingBody()
_STATUSES: Final[dict[str, tuple[str, ...]]] = {
    "open": OPEN_GAP_STATUSES,
    "resolved": ("resolved",),
    "all": (*OPEN_GAP_STATUSES, "resolved"),
}
_REASONS: Final = {"resolve": "Resolved", "dismiss": "Dismissed"}


def finding_not_found() -> ApiError:
    return ApiError(404, "finding_not_found", "The finding does not exist.")


def finding_closed() -> ApiError:
    return ApiError(409, "finding_closed", "The finding is already resolved or dismissed.")


async def _story_titles(
    runtime: RuntimeDep, pid: uuid.UUID, caller: uuid.UUID, blocking: list[GapView]
) -> dict[str, str]:
    """The titles of the stories that open blocking findings hold back (none without a plan, or when none blocks)."""

    if not any(item.story_id for item in blocking):
        return {}
    try:
        rows = await (await agent_reads(runtime)).plan.story_list(str(pid), view="open", caller=str(caller))
    except PlanReadError:
        return {}
    return {row.story_id: row.title for row in rows}


@router.get("", response_model=FindingsOut)
async def list_findings(
    pid: uuid.UUID,
    runtime: RuntimeDep,
    user: UserDep,
    *,
    status: FindingStatusQuery = "open",
    node: Annotated[str | None, Query(min_length=1, max_length=_MAX_ID)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    after: Annotated[str | None, Query(min_length=1, max_length=500)] = None,
) -> FindingsOut:
    """The development gaps of the project as findings, in the gaps' order; ``node`` keeps those about one spec node
    (aliases resolve)."""

    await require_project(runtime, user.id, pid)
    service = await development_service(runtime)
    if status == "dismissed":
        return FindingsOut(findings=[])
    try:
        page = await service.list_gaps(
            str(pid),
            origin="development",
            status=_STATUSES[status],
            about=None if node is None else [node],
            limit=limit,
            cursor=after,
        )
    except AnswerInvalidError:
        raise invalid_arguments("after") from None
    holders = await holders_of(runtime, pid, user.id, user_ids(page.gaps))
    open_blocking = [item for item in page.gaps if item.blocking and item.status in OPEN_GAP_STATUSES]
    titles = await _story_titles(runtime, pid, user.id, open_blocking)
    return FindingsOut(findings=[finding_out(item, holders, titles) for item in page.gaps], next=page.next)


async def _close(
    pid: uuid.UUID,
    finding_id: str,
    runtime: RuntimeDep,
    user: UserDep,
    body: CloseFindingBody,
    *,
    action: Literal["resolve", "dismiss"],
) -> ClosedFindingOut:
    await require_project(runtime, user.id, pid)
    service = await development_service(runtime)
    outcome = "error"
    try:
        gap = await service.get_gap(str(pid), finding_id)
        reason = (body.note or "").strip() or _REASONS[action]
        released = await service.dismiss_gap(str(pid), gap.gap_id, reason=reason, by=str(user.id))
        outcome = "ok"
    except GapNotFoundError:
        outcome = "unknown_finding"
        raise finding_not_found() from None
    except GapStateError:
        outcome = "wrong_state"
        raise finding_closed() from None
    except TaskStateError as error:  # the plan refused to release the tasks: the finding stays open, retry
        outcome = error.code
        if error.code == "plan_applying":
            raise plan_applying() from None
        raise
    finally:
        log_event(
            logger,
            INFO,
            "api.findings.closed",
            project_id=str(pid),
            user_id=str(user.id),
            finding_id=finding_id,
            action=action,
            outcome=outcome,
        )
    holders = await holders_of(runtime, pid, user.id, user_ids([gap]))
    closed = finding_out(gap, holders, status="resolved" if action == "resolve" else "dismissed")
    return ClosedFindingOut(finding=closed, unblocked=len(released))


@router.post("/{finding_id}/resolve", response_model=ClosedFindingOut)
async def resolve_finding(
    pid: uuid.UUID,
    finding_id: Annotated[str, Path(max_length=_MAX_ID)],
    runtime: RuntimeDep,
    user: UserDep,
    body: CloseFindingBody = _NO_NOTE,
) -> ClosedFindingOut:
    """Close an open finding with an optional note (the gap is dismissed); a blocking one unblocks its tasks."""

    return await _close(pid, finding_id, runtime, user, body, action="resolve")


@router.post("/{finding_id}/dismiss", response_model=ClosedFindingOut)
async def dismiss_finding(
    pid: uuid.UUID,
    finding_id: Annotated[str, Path(max_length=_MAX_ID)],
    runtime: RuntimeDep,
    user: UserDep,
    body: CloseFindingBody = _NO_NOTE,
) -> ClosedFindingOut:
    """Dismiss an open finding with an optional note; a blocking one unblocks its tasks."""

    return await _close(pid, finding_id, runtime, user, body, action="dismiss")
