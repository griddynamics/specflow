"""The access check and the destination lookup of the GitHub Projects form (docs/github/projects.md "Access check"):
what the account that will export can reach before a destination is saved. Results are closed reasons, never GitHub
text. Permission to create issues cannot be tried without writing, so a missing Issues approval shows on the first pass
(``github_permissions_missing``); a missing Organization projects approval shows here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from api.github_projects.gateway import ProjectsGateway
from api.schemas import (
    GitHubProjectsAccessFailed,
    GitHubProjectsAccessField,
    GitHubProjectsAccessOk,
    GitHubProjectsAccessReason,
)
from github.codes import GitHubCode, GitHubError
from github.projects import ProjectInfo

_REASONS: Final[dict[GitHubCode, GitHubProjectsAccessReason]] = {
    GitHubCode.NOT_CONNECTED: "github_not_connected",
    GitHubCode.RECONNECT_REQUIRED: "github_reconnect_required",
    GitHubCode.UNREACHABLE: "github_unreachable",
    GitHubCode.REPO_NOT_ACCESSIBLE: "github_repo_not_accessible",
    GitHubCode.NOT_AN_ORGANIZATION: "github_not_an_organization",
    GitHubCode.PERMISSIONS_MISSING: "github_permissions_missing",
    GitHubCode.PROJECT_NOT_FOUND: "github_project_not_found",
}
_REPOSITORY_FIELD: Final = frozenset(
    {"github_repo_not_accessible", "github_not_an_organization", "github_repo_read_only"}
)


@dataclass(frozen=True)
class Resolved:
    """The organization of a repository and, when one was named, its project."""

    organization: str
    project: ProjectInfo | None


class AccessRefusedError(Exception):
    """The destination cannot be used; ``reason`` is the closed code."""

    def __init__(self, reason: GitHubProjectsAccessReason) -> None:
        super().__init__(reason)
        self.reason: GitHubProjectsAccessReason = reason


def reason_of(error: GitHubError) -> GitHubProjectsAccessReason:
    return _REASONS.get(error.code, "github_auth_failed")


async def resolve(gateway: ProjectsGateway, repository: str, project_node_id: str | None) -> Resolved:
    """The repository's organization (an organization repository the account may write to) and, with
    ``project_node_id``, that organization's project; with none, proof that the account can list its projects."""

    try:
        info = await gateway.repository(repository)
        if info.organization is None:
            raise AccessRefusedError("github_not_an_organization")
        if not info.can_write:
            raise AccessRefusedError("github_repo_read_only")
        if project_node_id is None:
            await gateway.organization_projects(info.organization)
            return Resolved(info.organization, None)
        project, owner = await gateway.project(project_node_id)
    except GitHubError as error:
        raise AccessRefusedError(reason_of(error)) from None
    if owner != info.organization:
        raise AccessRefusedError("github_project_not_found")
    return Resolved(info.organization, project)


def _field(reason: GitHubProjectsAccessReason) -> GitHubProjectsAccessField | None:
    if reason == "github_project_not_found":
        return "project"
    return "repository" if reason in _REPOSITORY_FIELD else None


async def check_access(
    gateway: ProjectsGateway, repository: str, project_node_id: str | None
) -> GitHubProjectsAccessOk | GitHubProjectsAccessFailed:
    try:
        found = await resolve(gateway, repository, project_node_id)
    except AccessRefusedError as refused:
        return GitHubProjectsAccessFailed(reason=refused.reason, field=_field(refused.reason))
    return GitHubProjectsAccessOk(
        organization=found.organization, project_title=None if found.project is None else found.project.title
    )
