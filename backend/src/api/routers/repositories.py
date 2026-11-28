"""The GitHub repositories of a project (docs/github/api.md "Project repository routes").

Any member reads them, adds or removes one. A repository is added when the GitHub connection of the project
(the member ``PUT /projects/{pid}/integrations/github`` bound, docs/mission-control/integrations-api.md) can read it
through the app (the list of ``GET /integrations/github/repositories``, as that member); every clone check then uses
that member's connection (``added_by``, kept equal to the bound member). The clone check runs on the agents worker
(``GithubSnapshotWorkflow``); a start that fails marks the repository ``failed`` with ``github_clone_failed``. Events
carry ids and codes only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Response
from temporalio.exceptions import TemporalError

from api.deps import MemberDep, RuntimeDep, UserDep
from api.github import github_connections, github_error, repository_exists, repository_not_found
from api.project_integrations import acting_member
from api.runtime import Runtime
from api.schemas import ProjectRepositoriesOut, ProjectRepositoryIn, ProjectRepositoryOut
from api.workflows import start_github_snapshot
from db import github as github_db
from db.github import RepositoryExistsError, RepositoryRow
from db.integrations import GITHUB
from github.client import Repository
from github.codes import GitHubCode, GitHubError
from github.connections import GitHubConnections
from logger.logger import INFO, WARNING, get_logger, log_event
from storage.base import ObjectStoreError
from storage.keys import repository_prefix

logger = get_logger("api.routers.repositories")
router = APIRouter(prefix="/projects/{pid}/repositories", tags=["repositories"])


def _repository_id(rid: str) -> uuid.UUID:
    """``rid`` is caller text: a malformed id is ``repository_not_found`` (like ``pid``), never logged."""

    try:
        return uuid.UUID(rid)
    except ValueError:
        raise repository_not_found() from None


def _out(found: RepositoryRow, user_id: uuid.UUID) -> ProjectRepositoryOut:
    row = found.repository
    return ProjectRepositoryOut.model_validate(
        {
            "id": row.id,
            "github_repo_id": row.github_repo_id,
            "full_name": row.full_name,
            "html_url": row.html_url,
            "private": row.private,
            "added_by_name": found.added_by_name,
            "added_by_me": row.added_by == user_id,
            "clone_state": row.clone_state,
            "failure_code": row.failure_code,
            "default_branch": row.default_branch,
            "head_sha": row.head_sha,
            "file_count": row.file_count,
            "size_bytes": row.size_bytes,
            "snapshot_bytes": row.snapshot_bytes,
            "checked_at": row.checked_at,
            "cloned_at": row.cloned_at,
            "created_at": row.created_at,
        }
    )


async def _read(runtime: Runtime, pid: uuid.UUID, rid: uuid.UUID, user_id: uuid.UUID) -> ProjectRepositoryOut:
    async with runtime.transaction() as session:
        found = await github_db.get_repository(session, pid, rid)
    if found is None:
        raise repository_not_found()
    return _out(found, user_id)


async def _start(runtime: Runtime, pid: uuid.UUID, rid: uuid.UUID) -> None:
    """Start the clone check; a start that fails is ``failed`` with ``github_clone_failed`` (the owner retries)."""

    try:
        await start_github_snapshot(runtime, pid, rid)
    except TemporalError as error:
        log_event(
            logger, WARNING, "api.github.snapshot.start_failed", repository_id=str(rid), error=type(error).__name__
        )
        async with runtime.transaction() as session:
            await github_db.fail_clone(session, pid, rid, GitHubCode.CLONE_FAILED.value, now=datetime.now(UTC))
        return
    log_event(logger, INFO, "api.github.snapshot.started", repository_id=str(rid))


async def _readable(connections: GitHubConnections, user_id: uuid.UUID, github_repo_id: int) -> Repository:
    """The repository when it is in the caller's own list (read through the app) and not too large."""

    repositories, _truncated = await connections.repositories(user_id)
    repository = next((r for r in repositories if r.id == github_repo_id), None)
    if repository is None:
        raise GitHubError(GitHubCode.REPO_NOT_ACCESSIBLE)
    if repository.too_large:
        raise GitHubError(GitHubCode.REPO_TOO_LARGE)
    return repository


@router.get("", response_model=ProjectRepositoriesOut)
async def list_project_repositories(
    membership: MemberDep, runtime: RuntimeDep, user: UserDep
) -> ProjectRepositoriesOut:
    async with runtime.transaction() as session:
        rows = await github_db.list_repositories(session, membership.project.id)
    return ProjectRepositoriesOut(
        enabled=runtime.github is not None,
        can_manage=True,
        repositories=[_out(row, user.id) for row in rows],
    )


@router.post("", status_code=201, response_model=ProjectRepositoryOut)
async def create_project_repository(
    body: ProjectRepositoryIn, membership: MemberDep, runtime: RuntimeDep, user: UserDep
) -> ProjectRepositoryOut:
    """Add a repository the project's GitHub connection can read, then start its clone check."""

    connections = github_connections(runtime)
    pid = membership.project.id
    acting = await acting_member(runtime, pid, GITHUB)
    if acting is None:
        raise github_error(GitHubError(GitHubCode.NOT_CONNECTED))
    try:
        repository = await _readable(connections, acting, body.github_repo_id)
    except GitHubError as error:
        log_event(logger, INFO, "api.github.add_refused", project_id=str(pid), code=error.code.value)
        raise github_error(error) from None
    try:
        async with runtime.transaction() as session:
            rid = await github_db.add_repository(session, pid, repository, added_by=acting)
    except RepositoryExistsError:
        raise repository_exists() from None
    log_event(logger, INFO, "api.github.repository_added", project_id=str(pid), repository_id=str(rid))
    await _start(runtime, pid, rid)
    return await _read(runtime, pid, rid, user.id)


@router.delete("/{rid}", status_code=204)
async def remove_project_repository(rid: str, membership: MemberDep, runtime: RuntimeDep) -> Response:
    """Remove the repository and its snapshots (a snapshot a running check stores after this is deleted by it)."""

    pid, repository_id = membership.project.id, _repository_id(rid)
    async with runtime.transaction() as session:
        deleted = await github_db.delete_repository(session, pid, repository_id)
    if not deleted:
        raise repository_not_found()
    try:
        removed = await runtime.store.delete_by_prefix(repository_prefix(str(pid), str(repository_id)))
    except ObjectStoreError as error:  # the row is gone; a snapshot left behind is only logged
        log_event(logger, WARNING, "api.github.snapshot.delete_failed", repository_id=rid, error=type(error).__name__)
    else:
        log_event(logger, INFO, "api.github.repository_removed", repository_id=rid, objects=removed)
    return Response(status_code=204)
