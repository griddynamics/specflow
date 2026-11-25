"""The integrations of a project (docs/mission-control/integrations-api.md).

An integration binds one member's per-user connection to the project: every use of the app in the project (a Jira
target, a repository, a Slack channel) acts as that member, and the card says so. A member reads them; any member
connects Atlassian, GitHub or Notion with their own active connection (one workspace per app per project); the member
who connected an app, or the owner, removes it. Slack is bound by its own command in the channel, not here. No
transaction is open across a call to an app; events carry ids and codes only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel

from api.contained import Contained
from api.deps import MemberDep, RuntimeDep, UserDep
from api.errors import ApiError
from api.github import github_connections, github_error
from api.notion import notion_connections, notion_error
from api.project_integrations import (
    atlassian_not_connected,
    integration_forbidden,
    integration_held,
    integration_not_found,
    integration_workspace_mismatch,
)
from api.runtime import Runtime
from db import integrations as integrations_db
from db.integrations import ATLASSIAN, GITHUB, NOTION, SLACK, BindingHeldError, IntegrationView, WorkspaceMismatchError
from github.codes import GitHubCode, GitHubError
from logger.logger import INFO, WARNING, get_logger, log_event
from notion.reasons import NotionCode, NotionError

logger = get_logger("api.routers.integrations")
router = APIRouter(prefix="/projects/{pid}/integrations", tags=["integrations"])

AppName = Literal["atlassian", "github", "slack", "notion"]
BindableApp = Literal["atlassian", "github", "notion"]  # Slack is bound by ``/specflow connect`` in the channel
Health = Literal["active", "reconnect_required", "none"]
APPS: Final[tuple[AppName, ...]] = ("atlassian", "github", "slack", "notion")


class IntegrationMemberOut(BaseModel):
    user_id: uuid.UUID
    name: str | None


class IntegrationWorkspaceOut(BaseModel):
    id: str
    name: str


class ProjectIntegrationOut(BaseModel):
    """One app of the project. ``available``: the server runs the app. ``connected_by``: the member whose account
    the project's uses act as (none when not connected, or when that member left). ``health``: ``active`` when that
    account works, ``reconnect_required`` when its connection expired, ``none`` when nothing acts."""

    app: AppName
    available: bool
    connected_by: IntegrationMemberOut | None
    workspace: IntegrationWorkspaceOut | None
    connected_at: datetime | None
    health: Health


class ProjectIntegrationsOut(BaseModel):
    integrations: list[ProjectIntegrationOut]


def _atlassian_site(runtime: Runtime) -> str | None:
    """The configured site, as the name of the one Atlassian workspace (the host of its address)."""

    jira = runtime.settings.jira
    return None if jira is None else (urlsplit(jira.site_url).netloc or jira.site_url)


def _available(runtime: Runtime, app: str) -> bool:
    if app == ATLASSIAN:
        return runtime.jira is not None and runtime.jira.connections is not None
    if app == GITHUB:
        return runtime.github is not None
    if app == NOTION:  # C1: the Notion branch of the card
        return runtime.notion is not None
    return runtime.slack is not None


async def _health(runtime: Runtime, pid: uuid.UUID, row: IntegrationView | None) -> Health:
    """What the bound member's connection says: read through the services of the apps, outside any transaction."""

    if row is None or row.user_id is None:
        return "none"
    if row.app == ATLASSIAN:
        atlassian = None if runtime.jira is None or runtime.jira.connections is None else runtime.jira.connections
        found = None if atlassian is None else await atlassian.get(row.user_id)
    elif row.app == GITHUB:
        found = None if runtime.github is None else await runtime.github.get(row.user_id)
    elif row.app == NOTION:  # C1: the Notion branch of the card
        found = None if runtime.notion is None else await runtime.notion.get(row.user_id)
    else:
        threads = None if runtime.slack is None else runtime.slack.threads
        bound = None if threads is None else await threads.channels.binding(str(pid))
        return "active" if bound is not None else "none"
    if found is None:
        return "none"
    return "active" if found.status == "active" else "reconnect_required"


async def _out(runtime: Runtime, pid: uuid.UUID, app: AppName, row: IntegrationView | None) -> ProjectIntegrationOut:
    connected_by = (
        None if row is None or row.user_id is None else IntegrationMemberOut(user_id=row.user_id, name=row.user_name)
    )
    name = row.workspace_name if row is not None else ""
    if row is not None and app == ATLASSIAN:
        name = _atlassian_site(runtime) or name
    return ProjectIntegrationOut(
        app=app,
        available=_available(runtime, app),
        connected_by=connected_by,
        workspace=None if row is None else IntegrationWorkspaceOut(id=row.workspace_id, name=name),
        connected_at=None if row is None else row.connected_at,
        health=await _health(runtime, pid, row),
    )


async def _rows(runtime: Runtime, pid: uuid.UUID) -> dict[str, IntegrationView]:
    async with runtime.transaction() as session:
        return {row.app: row for row in await integrations_db.list_integrations(session, pid)}


@router.get("", response_model=ProjectIntegrationsOut)
async def list_project_integrations(runtime: RuntimeDep, access: MemberDep) -> ProjectIntegrationsOut:
    pid = access.project.id
    rows = await _rows(runtime, pid)
    return ProjectIntegrationsOut(integrations=[await _out(runtime, pid, app, rows.get(app)) for app in APPS])


async def _own_workspace(runtime: Runtime, user_id: uuid.UUID, app: BindableApp) -> tuple[str, str]:
    """The workspace of the caller's own active connection; ``409 <app>_not_connected`` without one."""

    if app == ATLASSIAN:
        if runtime.jira is None or runtime.jira.connections is None:
            raise ApiError(
                503, "atlassian_oauth_unavailable", "Atlassian connections are not configured on this server."
            )
        mine = await runtime.jira.connections.get(user_id)
        if mine is None or mine.status != "active":
            raise atlassian_not_connected()
        return mine.cloud_id, _atlassian_site(runtime) or mine.cloud_id
    if app == NOTION:  # C1: the Notion branch of the own workspace
        notion = await notion_connections(runtime).get(user_id)
        if notion is None or notion.status != "active":
            raise notion_error(NotionError(NotionCode.NOT_CONNECTED))
        return notion.workspace_id, notion.workspace_name
    github = github_connections(runtime)
    found = await github.get(user_id)
    if found is None or found.status != "active":
        raise github_error(GitHubError(GitHubCode.NOT_CONNECTED))
    return found.login, found.login


@router.put("/{app}", response_model=ProjectIntegrationOut)
async def put_project_integration(
    app: Annotated[BindableApp, Path()], runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> ProjectIntegrationOut:
    """Connect the project's ``app`` as the caller (their own active connection); a member of the same workspace may
    take over a connection the earlier member left or lost (never one whose connection is still active: 409
    ``integration_already_connected``). The Jira target and the repositories then act as the
    caller."""

    pid = access.project.id
    workspace_id, workspace_name = await _own_workspace(runtime, user.id, app)  # the app is called, no transaction open
    try:
        async with runtime.transaction() as session:
            await integrations_db.bind_integration(
                session,
                pid,
                app,
                user_id=user.id,
                workspace_id=workspace_id,
                workspace_name=workspace_name,
                now=datetime.now(UTC),
            )
            if app == ATLASSIAN:
                await integrations_db.point_jira_target(session, pid, user.id)
            elif app == GITHUB:
                await integrations_db.point_repositories(session, pid, user.id)
            # C1: Notion has no acting-account column to point; its uses read the binding on every call (R1)
    except WorkspaceMismatchError:
        log_event(logger, INFO, "api.integrations.refused", project_id=str(pid), app=app, user_id=str(user.id))
        raise integration_workspace_mismatch() from None
    except BindingHeldError:
        log_event(logger, INFO, "api.integrations.held", project_id=str(pid), app=app, user_id=str(user.id))
        raise integration_held() from None
    log_event(logger, INFO, "api.integrations.connected", project_id=str(pid), app=app, user_id=str(user.id))
    rows = await _rows(runtime, pid)
    return await _out(runtime, pid, app, rows.get(app))


@router.delete("/{app}", status_code=204)
async def delete_project_integration(
    app: Annotated[AppName, Path()], runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> Response:
    """Remove the project's connection to ``app`` (the member who made it, or the owner): everything that used it
    stops, and no other member's account takes over."""

    pid = access.project.id
    threads = None if runtime.slack is None else runtime.slack.threads
    only = None if access.is_owner else user.id  # the connector's own binding only, whatever happens meanwhile
    # The checks come first and are the same for every app (Slack too when it is off on this server); a Slack channel
    # is unbound before its row goes, so a row that says "connected" never outlives a failed unbind.
    async with runtime.transaction() as session:
        bound = await integrations_db.get_integration(session, pid, app)
    channel = app == SLACK and threads is not None and await _is_bound(threads, pid)
    if bound is None and not channel:
        raise integration_not_found()
    if bound is not None and only is not None and bound.user_id != only:
        raise integration_forbidden()
    if channel:
        await _unbind_channel(threads, pid)
    async with runtime.transaction() as session:
        deleted = await integrations_db.delete_integration(session, pid, app, only_user=only)
        if deleted and app == ATLASSIAN:
            await integrations_db.point_jira_target(session, pid, None)
        elif deleted and app == GITHUB:
            await integrations_db.point_repositories(session, pid, None)
        elif not deleted and (bound is not None or app != SLACK):
            exists = await integrations_db.get_integration(session, pid, app) is not None
            raise integration_forbidden() if exists else integration_not_found()  # taken over, or removed, meanwhile
    log_event(logger, INFO, "api.integrations.removed", project_id=str(pid), app=app, user_id=str(user.id))
    return Response(status_code=204)


async def _is_bound(threads: Any, pid: uuid.UUID) -> bool:
    """Whether the project's Slack channel is bound; a failed read (the binding file) is a closed answer, no 500."""

    with Contained(passing=(ApiError,)) as read:
        bound = await threads.channels.binding(str(pid)) is not None
    if read.failed:
        log_event(logger, WARNING, "api.integrations.binding_failed", project_id=str(pid), error_type=read.error_type)
        raise ApiError(502, "slack_unreachable", "Slack could not be reached. Try again later.")
    return bound


async def _unbind_channel(threads: Any, pid: uuid.UUID) -> None:
    """Unbind the project's Slack channel; a failed unbind (the binding file) is a closed answer, never a 500."""

    with Contained(passing=(ApiError,)) as unbound:
        await threads.channels.unbind(str(pid))
    if unbound.failed:
        log_event(logger, WARNING, "api.integrations.unbind_failed", project_id=str(pid), error_type=unbound.error_type)
        raise ApiError(502, "slack_unreachable", "Slack could not be reached. Try again later.")
