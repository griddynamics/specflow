"""The GitHub calls a pass makes, as the member the project's GitHub is connected by: ``ProjectsGateway`` is the port
(tests swap in a fake) and ``MemberGateway`` binds ``ProjectsApi`` to one member's connection, so every call gets a
fresh token and a ``401`` refreshes once (``GitHubConnections.call``)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol

from github.connections import GitHubConnections
from github.projects import IssueContent, IssueRef, IssueState, ProjectInfo, ProjectsApi, RepositoryInfo


class ProjectsGateway(Protocol):
    async def repository(self, full_name: str) -> RepositoryInfo: ...

    async def organization_projects(self, organization: str) -> list[ProjectInfo]: ...

    async def project(self, node_id: str) -> tuple[ProjectInfo, str | None]: ...

    async def create_project(self, organization: str, title: str) -> ProjectInfo: ...

    async def issue_states(self, project_id: str, issue_node_ids: Sequence[str]) -> dict[str, IssueState]: ...

    async def create_issue(self, repository: str, content: IssueContent) -> IssueRef: ...

    async def edit_issue(self, repository: str, number: int, content: IssueContent) -> None: ...

    async def comment(self, repository: str, number: int, body: str) -> None: ...

    async def add_labels(self, repository: str, number: int, labels: Sequence[str]) -> None: ...

    async def remove_label(self, repository: str, number: int, label: str) -> None: ...

    async def add_sub_issue(self, repository: str, parent: int, sub_issue_id: int) -> None: ...

    async def add_item(self, project_id: str, issue_node_id: str) -> str: ...

    async def move_item(self, project_id: str, item_id: str, after: str | None) -> None: ...


class MemberGateway:
    """``ProjectsGateway`` over one member's GitHub connection."""

    def __init__(self, connections: GitHubConnections, api: ProjectsApi, user_id: uuid.UUID) -> None:
        self._connections = connections
        self._api = api
        self._user = user_id

    async def repository(self, full_name: str) -> RepositoryInfo:
        return await self._connections.call(self._user, lambda t: self._api.repository(t, full_name))

    async def organization_projects(self, organization: str) -> list[ProjectInfo]:
        return await self._connections.call(self._user, lambda t: self._api.organization_projects(t, organization))

    async def project(self, node_id: str) -> tuple[ProjectInfo, str | None]:
        return await self._connections.call(self._user, lambda t: self._api.project(t, node_id))

    async def create_project(self, organization: str, title: str) -> ProjectInfo:
        return await self._connections.call(self._user, lambda t: self._api.create_project(t, organization, title))

    async def issue_states(self, project_id: str, issue_node_ids: Sequence[str]) -> dict[str, IssueState]:
        return await self._connections.call(self._user, lambda t: self._api.issue_states(t, project_id, issue_node_ids))

    async def create_issue(self, repository: str, content: IssueContent) -> IssueRef:
        return await self._connections.call(self._user, lambda t: self._api.create_issue(t, repository, content))

    async def edit_issue(self, repository: str, number: int, content: IssueContent) -> None:
        await self._connections.call(self._user, lambda t: self._api.edit_issue(t, repository, number, content))

    async def comment(self, repository: str, number: int, body: str) -> None:
        await self._connections.call(self._user, lambda t: self._api.comment(t, repository, number, body))

    async def add_labels(self, repository: str, number: int, labels: Sequence[str]) -> None:
        await self._connections.call(self._user, lambda t: self._api.add_labels(t, repository, number, labels))

    async def remove_label(self, repository: str, number: int, label: str) -> None:
        await self._connections.call(self._user, lambda t: self._api.remove_label(t, repository, number, label))

    async def add_sub_issue(self, repository: str, parent: int, sub_issue_id: int) -> None:
        await self._connections.call(self._user, lambda t: self._api.add_sub_issue(t, repository, parent, sub_issue_id))

    async def add_item(self, project_id: str, issue_node_id: str) -> str:
        return await self._connections.call(self._user, lambda t: self._api.add_item(t, project_id, issue_node_id))

    async def move_item(self, project_id: str, item_id: str, after: str | None) -> None:
        await self._connections.call(self._user, lambda t: self._api.move_item(t, project_id, item_id, after))
