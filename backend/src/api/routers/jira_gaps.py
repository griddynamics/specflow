"""The Jira gap channel of a project (``docs/jira-sync/gaps.md`` "Routes"): the target and the access check.

Any member reads, saves and checks (D6). The channel acts as the member the project's Atlassian is connected by (R1):
the check runs as them and answers ``409 jira_not_connected`` when there is none. ``503 atlassian_oauth_unavailable``
without Atlassian OAuth on the server and ``503 gap_unavailable`` while gap runs are off; ``GET`` answers ``available:
false`` instead. ``PUT`` never waits for Jira: the questions are posted in the background after the commit. Events carry
ids and codes only."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.jira_gaps import JiraGaps
from api.project_integrations import acting_member
from api.routers.atlassian import atlassian_oauth_unavailable
from api.routers.gaps import gap_unavailable
from api.routers.jira import jira_not_connected
from api.runtime import Runtime
from api.schemas import (
    JiraAccessFailed,
    JiraAccessOk,
    JiraAccessResult,
    JiraGapAccessCheckIn,
    JiraGapsIn,
    JiraGapsOut,
)
from db import jira_gaps as gaps_db
from db.integrations import ATLASSIAN
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.jira_gaps")
router = APIRouter(prefix="/projects/{pid}/jira-gaps", tags=["jira-gaps"])


def _channel(runtime: Runtime) -> JiraGaps:
    """The channel, or the 503 of what is missing."""

    if runtime.jira is None or runtime.jira.connections is None:
        raise atlassian_oauth_unavailable()
    if runtime.jira_gaps is None:
        raise gap_unavailable()
    channel: JiraGaps = runtime.jira_gaps
    return channel


@router.get("", response_model=JiraGapsOut)
async def get_jira_gaps(runtime: RuntimeDep, access: MemberDep) -> JiraGapsOut:
    channel: JiraGaps | None = runtime.jira_gaps
    available = channel is not None and runtime.jira is not None and runtime.jira.connections is not None
    if channel is None:
        return JiraGapsOut(
            available=False,
            jira_project=None,
            grouping=None,
            enabled=False,
            story_key=None,
            story_url=None,
            posted=0,
            answered=0,
            last_polled_at=None,
            error_code=None,
            poll_seconds=60,
        )
    return await channel.view(access.project.id, available=available)


@router.put("", response_model=JiraGapsOut)
async def put_jira_gaps(body: JiraGapsIn, runtime: RuntimeDep, user: UserDep, access: MemberDep) -> JiraGapsOut:
    channel = _channel(runtime)
    pid = access.project.id
    if await acting_member(runtime, pid, ATLASSIAN) is None:
        raise jira_not_connected()
    async with runtime.transaction() as session:
        saved = await gaps_db.save_target(
            session,
            pid,
            jira_project=body.jira_project,
            grouping=body.grouping,
            enabled=body.enabled,
            now=datetime.now(UTC),
        )
    log_event(
        logger,
        INFO,
        "api.jira_gaps.saved",
        user_id=str(user.id),
        project_id=str(pid),
        enabled=body.enabled,
        grouping=body.grouping,
        project_changed=saved.project_changed,
    )
    channel.after_save(str(pid), saved)  # after the commit; it never raises and never waits for Jira
    return await channel.view(pid, available=True)


@router.post("/access-check", response_model=JiraAccessResult)
async def check_jira_gaps_access(
    body: JiraGapAccessCheckIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> JiraAccessOk | JiraAccessFailed:
    channel = _channel(runtime)
    result = await channel.check_access(access.project.id, body.jira_project)
    log_event(
        logger,
        INFO,
        "api.jira_gaps.access_checked",
        user_id=str(user.id),
        project_id=str(access.project.id),
        result=result.result,
        reason=result.reason if isinstance(result, JiraAccessFailed) else None,
    )
    return result
