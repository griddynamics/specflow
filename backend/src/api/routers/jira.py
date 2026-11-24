"""The Jira settings of a project and the access check (``docs/jira-sync/api.md`` "Web routes", "Access check").

Any member reads and saves the settings and runs the access check, and the web app never starts a sync (the agent
routes do). ``PUT`` saves in one transaction, with the target row locked first (``db.jira.save_target``,
``FOR UPDATE``) and no ``projects`` lock at all (a running pass holds the target ``FOR SHARE`` and then needs
``projects`` ``FOR KEY SHARE``); it never waits for a pass: the runner is told after the commit. No Jira call is made
inside a transaction. Events carry ids and codes only.

In ``oauth`` mode (docs/jira-sync/oauth.md) saving needs the project's Atlassian connection (``PUT
/projects/{pid}/integrations/atlassian``, docs/mission-control/integrations-api.md) and makes the member it is bound to
the target's acting account, not whoever saves; the access check runs as the caller; ``connection`` shows the acting
account.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.errors import ApiError
from api.jira.access import check_access, not_usable
from api.jira.desired import EPIC
from api.jira.runner import JiraSyncRunner
from api.jira.state import status_of
from api.project_integrations import acting_member
from api.runtime import Runtime
from api.schemas import (
    JiraAccessCheckIn,
    JiraAccessFailed,
    JiraAccessOk,
    JiraAccessResult,
    JiraConnectionOut,
    JiraSettingsIn,
    JiraSettingsOut,
)
from db import atlassian as atlassian_db
from db import jira as jira_db
from db.integrations import ATLASSIAN
from db.jira import JiraTargetView
from jira.gateway import JiraError
from jira.reasons import Reason
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.jira")
router = APIRouter(prefix="/projects/{pid}/jira", tags=["jira"])


def jira_unavailable() -> ApiError:
    return ApiError(503, "jira_unavailable", "Jira sync is not configured on this server.")


def jira_not_connected() -> ApiError:
    return ApiError(409, Reason.NOT_CONNECTED.value, "Connect your Atlassian account before you save the Jira target.")


def jira_runner(runtime: Runtime) -> JiraSyncRunner:
    if runtime.jira is None:
        raise jira_unavailable()
    runner: JiraSyncRunner = runtime.jira
    return runner


async def _connection_out(session: Any, target: JiraTargetView, user_id: uuid.UUID) -> JiraConnectionOut:
    acting = target.connected_user_id
    row = await atlassian_db.get_connection(session, acting) if acting is not None else None
    return JiraConnectionOut(
        connected_by_name=row.account_name if row is not None else None,
        connected_by_me=acting == user_id,
        status="missing" if row is None else "expired" if row.status == atlassian_db.EXPIRED else "active",
    )


async def _settings_out(runtime: Runtime, pid: uuid.UUID, user_id: uuid.UUID, *, editable: bool) -> JiraSettingsOut:
    oauth = runtime.jira is not None and runtime.jira.connections is not None
    async with runtime.transaction() as session:
        target = await jira_db.get_target(session, pid)
        issues = await jira_db.list_issues(session, pid) if target is not None else []
        connection = await _connection_out(session, target, user_id) if oauth and target is not None else None
    available = runtime.jira is not None
    jira = runtime.settings.jira
    site_url = jira.site_url if available and jira is not None else None
    if target is None:
        return JiraSettingsOut(
            available=available,
            editable=editable,
            site_url=site_url,
            jira_project=None,
            epic_key=None,
            created_epic_key=None,
            enabled=False,
            sync=None,
        )
    created = next((row.jira_key for row in issues if row.node_id == EPIC and row.jira_key), None)
    if runtime.jira is not None:
        running, queued = runtime.jira.live(str(pid))
        sync = runtime.jira.status_from(str(pid), target, created, running=running, queued=queued)
    else:
        sync = status_of(target, live=False, queued=False, created_epic=created)
    return JiraSettingsOut(
        available=available,
        editable=editable,
        site_url=site_url,
        jira_project=target.jira_project,
        epic_key=target.epic_key,
        created_epic_key=created,
        enabled=target.enabled,
        sync=sync,
        connection=connection,
    )


@router.get("", response_model=JiraSettingsOut)
async def get_jira_settings(runtime: RuntimeDep, user: UserDep, access: MemberDep) -> JiraSettingsOut:
    return await _settings_out(runtime, access.project.id, user.id, editable=True)


@router.put("", response_model=JiraSettingsOut)
async def put_jira_settings(
    body: JiraSettingsIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> JiraSettingsOut:
    runner = jira_runner(runtime)
    pid = access.project.id
    acting = None
    if runner.connections is not None:  # oauth: the sync acts as the member the project's Atlassian is connected by
        acting = await acting_member(runtime, pid, ATLASSIAN)
        if acting is None:
            raise jira_not_connected()
    async with runtime.transaction() as session:
        saved = await jira_db.save_target(
            session,
            pid,
            jira_project=body.jira_project,
            epic_key=body.epic_key,
            enabled=body.enabled,
            now=datetime.now(UTC),
            connected_user_id=acting,
        )
    log_event(
        logger,
        INFO,
        "api.jira.settings.saved",
        user_id=str(user.id),
        project_id=str(pid),
        enabled=body.enabled,
        project_changed=saved.project_changed,
    )
    await runner.after_settings(str(pid), saved)  # after the commit; it never raises and never waits for the pass
    return await _settings_out(runtime, pid, user.id, editable=True)


@router.post("/access-check", response_model=JiraAccessResult)
async def check_jira_access(
    body: JiraAccessCheckIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> JiraAccessOk | JiraAccessFailed:
    runner = jira_runner(runtime)
    result: JiraAccessOk | JiraAccessFailed
    try:
        gateway = await runner.gateway_for(user.id)  # oauth: the caller's own connection
    except JiraError as error:
        result = not_usable(error)
    else:
        result = await check_access(gateway, body.jira_project, body.epic_key)  # no transaction is open
    log_event(
        logger,
        INFO,
        "api.jira.access.checked",
        user_id=str(user.id),
        project_id=str(access.project.id),
        result=result.result,
        reason=result.reason if isinstance(result, JiraAccessFailed) else None,
    )
    return result
