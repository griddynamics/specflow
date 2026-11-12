"""The findings of a project for people (web audience, not the agent route group): list by node and status, resolve
and dismiss with a note.

Access is the project membership every web route applies: any member may resolve or dismiss; there is no role
(access-control.md AC12). Both calls are ``FindingFlow.close_finding``: a blocking finding is followed by the plan's
unblock, so its story shows ``ready`` again at once, or at the next plan run when a run already reset the tasks (D13).
A refusal writes nothing. The ``api.findings.closed`` event carries ids and the outcome, never a note or a finding text.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from api.agent_reads import agent_reads
from api.agent_views import holders_of
from api.deps import RuntimeDep, UserDep
from api.errors import ApiError, invalid_arguments, plan_applying, required
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
from graph.core.errors import ConfigurationError
from graph.gap import Finding, FindingError, FindingFlow
from graph.plan.errors import PlanReadError, TaskStateError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.findings")
router = APIRouter(prefix="/projects/{pid}/findings", tags=["findings"], route_class=AgentRoute)

_MAX_ID = 200
_NO_NOTE = CloseFindingBody()


def finding_not_found() -> ApiError:
    return ApiError(404, "finding_not_found", "The finding does not exist.")


def finding_closed() -> ApiError:
    return ApiError(409, "finding_closed", "The finding is already resolved or dismissed.")


def _refused(error: FindingError) -> ApiError | FindingError:
    mapped = {"unknown_finding": finding_not_found, "wrong_state": finding_closed}
    make = mapped.get(error.code)
    return make() if make is not None else error


async def _story_titles(
    runtime: RuntimeDep, pid: uuid.UUID, caller: uuid.UUID, blocking: list[Finding]
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
    """The findings of the project, blocking first, then newest first; ``node`` keeps those about one spec node
    (aliases resolve)."""

    await require_project(runtime, user.id, pid)
    findings = required((await agent_reads(runtime)).findings, "the findings store")
    try:
        page = await findings.findings(
            str(pid), status=status, spec_ids=None if node is None else [node], limit=limit, after=after
        )
    except FindingError as error:
        raise _refused(error) from None
    except ConfigurationError:
        raise invalid_arguments("after") from None
    holders = await holders_of(runtime, pid, user.id, user_ids(page.findings))
    titles = await _story_titles(
        runtime, pid, user.id, [item for item in page.findings if item.blocking and item.status == "open"]
    )
    return FindingsOut(findings=[finding_out(item, holders, titles) for item in page.findings], next=page.next)


async def _close(
    pid: uuid.UUID, finding_id: str, runtime: RuntimeDep, user: UserDep, body: CloseFindingBody, *, dismiss: bool
) -> ClosedFindingOut:
    await require_project(runtime, user.id, pid)
    reads = await agent_reads(runtime)
    flow = FindingFlow(required(reads.findings, "the findings store"), reads.plan)
    outcome = "error"
    try:
        closed = await flow.close_finding(str(pid), finding_id, by=str(user.id), note=body.note, dismiss=dismiss)
        outcome = "ok"
    except FindingError as error:
        outcome = error.code
        raise _refused(error) from None
    except TaskStateError as error:  # the finding is closed; its tasks stay blocked until the plan is free
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
            action="dismiss" if dismiss else "resolve",
            outcome=outcome,
        )
    holders = await holders_of(runtime, pid, user.id, user_ids([closed.finding]))
    return ClosedFindingOut(finding=finding_out(closed.finding, holders), unblocked=len(closed.unblocked))


@router.post("/{finding_id}/resolve", response_model=ClosedFindingOut)
async def resolve_finding(
    pid: uuid.UUID,
    finding_id: Annotated[str, Path(max_length=_MAX_ID)],
    runtime: RuntimeDep,
    user: UserDep,
    body: CloseFindingBody = _NO_NOTE,
) -> ClosedFindingOut:
    """Resolve an open finding with an optional note; a blocking one unblocks its tasks (A20)."""

    return await _close(pid, finding_id, runtime, user, body, dismiss=False)


@router.post("/{finding_id}/dismiss", response_model=ClosedFindingOut)
async def dismiss_finding(
    pid: uuid.UUID,
    finding_id: Annotated[str, Path(max_length=_MAX_ID)],
    runtime: RuntimeDep,
    user: UserDep,
    body: CloseFindingBody = _NO_NOTE,
) -> ClosedFindingOut:
    """Dismiss an open finding with an optional note; a blocking one unblocks its tasks (A20)."""

    return await _close(pid, finding_id, runtime, user, body, dismiss=True)
