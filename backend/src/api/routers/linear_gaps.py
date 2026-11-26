"""The Linear gap channel of a project (``docs/linear/gaps.md`` "Routes"): the target and the access check.

Any member reads, saves and checks (D6). The channel acts as the member the project's Linear is connected by (R1): the
check and the save run as them and answer ``409 linear_not_connected`` when there is none. ``503 linear_unavailable``
without the Linear OAuth app on the server and ``503 gap_unavailable`` while gap runs are off; ``GET`` answers
``available: false`` instead. ``PUT`` posts the open questions in the background after the commit. Events carry ids and
codes only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.errors import ApiError
from api.linear.errors import linear_error, linear_not_connected, linear_unavailable
from api.linear_gaps.acting import NOT_CONNECTED, UNAVAILABLE, ActingError
from api.linear_gaps.channel import PROJECT_NOT_FOUND, TEAM_NOT_FOUND, LinearGaps, Resolved, UnresolvedError
from api.linear_gaps.models import (
    LinearAccessFailed,
    LinearAccessOk,
    LinearAccessResult,
    LinearGapAccessCheckIn,
    LinearGapsIn,
    LinearGapsOut,
)
from api.routers.gaps import gap_unavailable
from api.runtime import Runtime
from db import linear_gaps as gaps_db
from linear.errors import LinearError, LinearUnauthorizedError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.linear_gaps")
router = APIRouter(prefix="/projects/{pid}/linear-gaps", tags=["linear-gaps"])


def _channel(runtime: Runtime) -> LinearGaps:
    """The channel, or the 503 of what is missing."""

    if runtime.linear is None:
        raise linear_unavailable()
    if runtime.linear_gaps is None:
        raise gap_unavailable()
    channel: LinearGaps = runtime.linear_gaps
    return channel


def _off() -> LinearGapsOut:
    return LinearGapsOut(
        available=False,
        enabled=False,
        team_id=None,
        team_key=None,
        linear_project_id=None,
        grouping=None,
        parent_identifier=None,
        parent_url=None,
        posted=0,
        answered=0,
        error_code=None,
    )


def _acting_error(error: ActingError) -> ApiError:
    """The API error of a project with no member to act as."""

    if error.code == UNAVAILABLE:
        return linear_unavailable()
    if error.code == NOT_CONNECTED:
        return linear_not_connected()
    return linear_error(LinearUnauthorizedError())


def _unresolved(error: UnresolvedError) -> ApiError:
    if error.reason == TEAM_NOT_FOUND:
        return ApiError(404, TEAM_NOT_FOUND, "The Linear team does not exist or your account cannot see it.")
    return ApiError(404, PROJECT_NOT_FOUND, "The Linear project does not exist or your account cannot see it.")


@router.get("", response_model=LinearGapsOut)
async def get_linear_gaps(runtime: RuntimeDep, access: MemberDep) -> LinearGapsOut:
    channel: LinearGaps | None = runtime.linear_gaps
    if channel is None:
        return _off()
    return await channel.view(access.project.id, available=runtime.linear is not None)


@router.put("", response_model=LinearGapsOut)
async def put_linear_gaps(body: LinearGapsIn, runtime: RuntimeDep, user: UserDep, access: MemberDep) -> LinearGapsOut:
    channel = _channel(runtime)
    pid = access.project.id
    async with runtime.transaction() as session:
        current = await gaps_db.get_target(session, pid)
    if current is not None and not body.enabled:  # turning off needs no Linear call
        resolved: tuple[str, str, str | None] = (current.team_id, current.team_key, current.linear_project_id)
    else:
        resolved = _ids(await _resolve(channel, pid, body))
    async with runtime.transaction() as session:
        saved = await gaps_db.save_target(
            session,
            pid,
            team_id=resolved[0],
            team_key=resolved[1],
            linear_project_id=resolved[2],
            grouping=body.grouping,
            enabled=body.enabled,
            now=datetime.now(UTC),
        )
    log_event(
        logger,
        INFO,
        "api.linear_gaps.saved",
        user_id=str(user.id),
        project_id=str(pid),
        enabled=body.enabled,
        grouping=body.grouping,
        destination_changed=saved.destination_changed,
    )
    channel.after_save(str(pid), saved)  # after the commit; it never raises and never waits for Linear
    return await channel.view(pid, available=True)


def _ids(found: Resolved) -> tuple[str, str, str | None]:
    return found.team.id, found.team.key, None if found.project is None else found.project.id


async def _resolve(channel: LinearGaps, pid: uuid.UUID, body: LinearGapAccessCheckIn) -> Resolved:
    try:
        return await channel.resolve(pid, body.team_key, body.linear_project)
    except ActingError as error:
        raise _acting_error(error) from None
    except UnresolvedError as error:
        raise _unresolved(error) from None
    except LinearError as error:
        raise linear_error(error) from None


@router.post("/access-check", response_model=LinearAccessResult)
async def check_linear_gaps_access(
    body: LinearGapAccessCheckIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> LinearAccessOk | LinearAccessFailed:
    channel = _channel(runtime)
    result = await _check(channel, access.project.id, body)
    log_event(
        logger,
        INFO,
        "api.linear_gaps.access_checked",
        user_id=str(user.id),
        project_id=str(access.project.id),
        result=result.result,
        reason=result.reason if isinstance(result, LinearAccessFailed) else None,
    )
    return result


async def _check(
    channel: LinearGaps, pid: uuid.UUID, body: LinearGapAccessCheckIn
) -> LinearAccessOk | LinearAccessFailed:
    """What the channel needs of the bound member: they authenticate and see the team (and the Linear project). A failed
    check is an answer, not an error."""

    try:
        found = await channel.resolve(pid, body.team_key, body.linear_project)
    except ActingError as error:
        return LinearAccessFailed(reason=error.code, field=None)
    except UnresolvedError as error:
        return LinearAccessFailed.model_validate({"reason": error.reason, "field": error.field})
    except LinearError as error:
        return LinearAccessFailed(reason=linear_error(error).code, field=None)
    name = None if found.project is None else found.project.name
    return LinearAccessOk(team_name=found.team.name, linear_project_name=name)
