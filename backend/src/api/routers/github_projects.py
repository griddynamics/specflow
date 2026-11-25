"""The GitHub Projects plan destination of a project (``docs/github/projects.md`` "Routes"): the target, the projects
its repository can use, and the access check.

Any member reads, saves and checks (D6). The export acts as the member the project's GitHub is connected by (R1,
``PUT /projects/{pid}/integrations/github``): every call here runs as them, and ``409 github_not_connected`` says there
is none. ``503 github_unavailable`` when the GitHub integration is off. ``PUT`` never waits for a pass: the runner
is told after the commit. No GitHub call is made inside a transaction. Events carry ids and codes only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.github import github_connections, github_error, github_unavailable, repository_not_found, repository_read_only
from api.github_projects.access import AccessRefusedError, check_access, resolve
from api.github_projects.gateway import ProjectsGateway
from api.github_projects.runner import GitHubProjectsRunner
from api.project_integrations import acting_member
from api.runtime import Runtime
from api.schemas import (
    GitHubProjectChoice,
    GitHubProjectsAccessFailed,
    GitHubProjectsAccessIn,
    GitHubProjectsAccessOk,
    GitHubProjectsAccessResult,
    GitHubProjectsIn,
    GitHubProjectsListOut,
    GitHubProjectsOut,
)
from db import github as github_db
from db import github_projects as projects_db
from db.github_projects import ProjectChoice
from db.integrations import GITHUB
from github.codes import GitHubCode, GitHubError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.github_projects")
router = APIRouter(prefix="/projects/{pid}/github-projects", tags=["github-projects"])


def _runner(runtime: Runtime) -> GitHubProjectsRunner:
    runner: GitHubProjectsRunner | None = runtime.github_projects
    if runner is None:
        raise github_unavailable()
    return runner


async def _acting(runtime: Runtime, pid: uuid.UUID) -> uuid.UUID:
    member = await acting_member(runtime, pid, GITHUB)
    if member is None:
        raise github_error(GitHubError(GitHubCode.NOT_CONNECTED))
    return member


async def _repository_name(runtime: Runtime, pid: uuid.UUID, repository_id: uuid.UUID) -> str:
    async with runtime.transaction() as session:
        found = await github_db.get_repository(session, pid, repository_id)
    if found is None:
        raise repository_not_found()
    return found.repository.full_name


def _refused(refused: AccessRefusedError) -> Exception:
    if refused.reason == "github_repo_read_only":
        return repository_read_only()
    return github_error(GitHubError(GitHubCode(refused.reason)))


async def _gateway(runtime: Runtime, pid: uuid.UUID) -> ProjectsGateway:
    return await _runner(runtime).gateway_of(await _acting(runtime, pid))


async def _out(runtime: Runtime, pid: uuid.UUID, user_id: uuid.UUID) -> GitHubProjectsOut:
    runner: GitHubProjectsRunner | None = runtime.github_projects
    async with runtime.transaction() as session:
        target = await projects_db.get_target(session, pid) if runner is not None else None
    bound = await acting_member(runtime, pid, GITHUB) if target is not None else None  # who acts now, not who saved
    connection = None
    if bound is not None and runtime.github is not None:
        connection = await github_connections(runtime).get(bound)
    if runner is None or target is None:
        return GitHubProjectsOut(
            available=runner is not None,
            repository_id=None,
            repository=None,
            project_node_id=None,
            project_title=None,
            project_url=None,
            enabled=False,
            sync=None,
            connected_by_name=None,
            connected_by_me=False,
        )
    return GitHubProjectsOut(
        available=True,
        repository_id=target.repository_id,
        repository=target.repository_name,
        project_node_id=target.project_node_id,
        project_title=target.project_title,
        project_url=target.project_url,
        enabled=target.enabled,
        sync=runner.status_from(target),
        connected_by_name=None if connection is None else connection.login,
        connected_by_me=bound == user_id,
    )


@router.get("", response_model=GitHubProjectsOut)
async def get_github_projects(runtime: RuntimeDep, user: UserDep, access: MemberDep) -> GitHubProjectsOut:
    return await _out(runtime, access.project.id, user.id)


@router.get("/projects", response_model=GitHubProjectsListOut)
async def list_github_projects(
    repository_id: uuid.UUID, runtime: RuntimeDep, access: MemberDep
) -> GitHubProjectsListOut:
    """The open projects of the organization that owns the project's repository (empty for a user's repository)."""

    pid = access.project.id
    gateway = await _gateway(runtime, pid)
    name = await _repository_name(runtime, pid, repository_id)
    try:
        info = await gateway.repository(name)
        found = [] if info.organization is None else await gateway.organization_projects(info.organization)
    except GitHubError as error:
        raise github_error(error) from None
    return GitHubProjectsListOut(
        organization=info.organization,
        projects=[GitHubProjectChoice(node_id=p.node_id, number=p.number, title=p.title, url=p.url) for p in found],
    )


@router.put("", response_model=GitHubProjectsOut)
async def put_github_projects(
    body: GitHubProjectsIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> GitHubProjectsOut:
    runner = _runner(runtime)
    pid = access.project.id
    acting = await _acting(runtime, pid)
    name = await _repository_name(runtime, pid, body.repository_id)
    gateway = await runner.gateway_of(acting)
    try:
        found = await resolve(gateway, name, body.project_node_id)
        project = found.project
        if project is None:  # a new project named after the typed title, in the repository's organization
            project = await gateway.create_project(found.organization, str(body.new_project_title))
    except AccessRefusedError as refused:
        raise _refused(refused) from None
    except GitHubError as error:
        raise github_error(error) from None
    async with runtime.transaction() as session:
        saved = await projects_db.save_target(
            session,
            pid,
            repository_id=body.repository_id,
            project=ProjectChoice(project.node_id, project.title, project.url),
            enabled=body.enabled,
            now=datetime.now(UTC),
            connected_user_id=acting,
        )
    log_event(
        logger,
        INFO,
        "api.github_projects.saved",
        user_id=str(user.id),
        project_id=str(pid),
        enabled=body.enabled,
        destination_changed=saved.destination_changed,
    )
    await runner.after_settings(str(pid), saved)  # after the commit; it never raises and never waits for the pass
    return await _out(runtime, pid, user.id)


@router.post("/access-check", response_model=GitHubProjectsAccessResult)
async def check_github_projects_access(
    body: GitHubProjectsAccessIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> GitHubProjectsAccessOk | GitHubProjectsAccessFailed:
    pid = access.project.id
    gateway = await _gateway(runtime, pid)
    name = await _repository_name(runtime, pid, body.repository_id)
    result = await check_access(gateway, name, body.project_node_id)
    log_event(
        logger,
        INFO,
        "api.github_projects.access_checked",
        user_id=str(user.id),
        project_id=str(pid),
        result=result.result,
        reason=result.reason if isinstance(result, GitHubProjectsAccessFailed) else None,
    )
    return result
