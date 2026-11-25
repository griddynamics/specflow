"""The calls of the GitHub Projects export (docs/github/projects.md): REST for issues, labels, comments and sub-issues,
GraphQL for organization Projects v2. Every call is made as the user (``GitHubClient.send``/``graphql``) and every
refusal is one closed ``GitHubCode``; no token, GitHub text, title or body goes into an error or a log line.

Only organization projects are used (a GitHub App cannot reach a user's Projects v2). A call that GitHub refuses with
"Resource not accessible" means the installation has not approved the permission yet: ``github_permissions_missing``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx
from pydantic import SecretStr

from github.client import GitHubClient
from github.codes import GitHubCode, GitHubError

ITEM_QUERY_LIMIT: Final = 50  # issues one state query asks about
_GRAPHQL_MISSING: Final = frozenset({"FORBIDDEN", "INSUFFICIENT_SCOPES"})
_NOT_STARTED: Final = frozenset({None, "", "todo", "to do", "backlog"})

_LIST_QUERY = (
    "query($org:String!){organization(login:$org){id projectsV2(first:50,orderBy:{field:UPDATED_AT,direction:DESC})"
    "{nodes{id title number url closed}}}}"
)
_ORG_QUERY = "query($org:String!){organization(login:$org){id}}"
_CREATE = (
    "mutation($owner:ID!,$title:String!){createProjectV2(input:{ownerId:$owner,title:$title})"
    "{projectV2{id title number url closed}}}"
)
_PROJECT_QUERY = (
    "query($id:ID!){node(id:$id){... on ProjectV2{id title number url closed owner{... on Organization{login}}}}}"
)
_ADD_ITEM = (
    "mutation($project:ID!,$issue:ID!){addProjectV2ItemById(input:{projectId:$project,contentId:$issue}){item{id}}}"
)
_MOVE_ITEM = (
    "mutation($project:ID!,$item:ID!,$after:ID){updateProjectV2ItemPosition(input:{projectId:$project,itemId:$item,"
    "afterId:$after}){clientMutationId}}"
)
_STATES = (
    "query($ids:[ID!]!){nodes(ids:$ids){... on Issue{id state projectItems(first:10){nodes{project{id} "
    'fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}}}}}}'
)


@dataclass(frozen=True)
class ProjectInfo:
    """An organization project: its node id, number, title and web address."""

    node_id: str
    number: int
    title: str
    url: str
    closed: bool = False


@dataclass(frozen=True)
class RepositoryInfo:
    """The repository an export writes to: its organization (``None`` for a user) and whether the user may push."""

    full_name: str
    organization: str | None
    can_write: bool


@dataclass(frozen=True)
class IssueContent:
    """What the export owns of an issue: its title, Markdown body and labels."""

    title: str
    body: str
    labels: Sequence[str]

    def payload(self) -> dict[str, Any]:
        return {"title": self.title, "body": self.body, "labels": list(self.labels)}


@dataclass(frozen=True)
class IssueRef:
    """A created issue: the number the URL shows, the REST id (sub-issues) and the GraphQL node id (projects)."""

    number: int
    issue_id: int
    node_id: str


@dataclass(frozen=True)
class IssueState:
    """An issue as the export reads it: open or closed, and its Status on the export's project (``None`` = none)."""

    open: bool
    status: str | None

    @property
    def not_started(self) -> bool:
        return self.open and (self.status or "").strip().lower() in _NOT_STARTED


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _project(node: Any) -> ProjectInfo | None:
    if not isinstance(node, dict):
        return None
    node_id, number, title, url = node.get("id"), _int(node.get("number")), node.get("title"), node.get("url")
    if not isinstance(node_id, str) or number is None or not isinstance(title, str) or not isinstance(url, str):
        return None
    return ProjectInfo(node_id, number, title, url, node.get("closed") is True)


def _already_linked(response: httpx.Response) -> bool:
    """Whether a 422 of the sub-issue call says the issue is already one (read, never kept or logged)."""

    text = response.text.lower()
    if "parent" in text:  # "already has a parent": it hangs under another issue, which is not our link
        return False
    return "duplicate" in text or "already a sub-issue" in text or "already a sub issue" in text


def _rest_refusal(response: httpx.Response, missing: GitHubCode) -> GitHubError:
    """The code of a refused REST call: 403 is the permission the installation lacks, 404 is ``missing``, 410 and 422
    are the content GitHub will not take."""

    status = response.status_code
    if status == httpx.codes.FORBIDDEN:
        return GitHubError(GitHubCode.PERMISSIONS_MISSING)
    if status == httpx.codes.NOT_FOUND:
        return GitHubError(missing)
    if status in (httpx.codes.GONE, httpx.codes.UNPROCESSABLE_ENTITY):
        return GitHubError(GitHubCode.ISSUE_GONE if status == httpx.codes.GONE else GitHubCode.ITEM_INVALID)
    return GitHubError(GitHubCode.REFUSED)


class ProjectsApi:
    """The Projects export's calls; each takes the user's access token (``GitHubConnections.call`` hands it in)."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    async def _rest(
        self, token: SecretStr, method: str, path: str, *, missing: GitHubCode, body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._client.send(token, method, path, body)
        if not response.is_success:
            raise _rest_refusal(response, missing)
        return _body(response)

    async def _graphql(
        self, token: SecretStr, query: str, variables: Mapping[str, Any], missing: GitHubCode
    ) -> dict[str, Any]:
        response = await self._client.graphql(token, query, variables)
        if not response.is_success:
            raise _rest_refusal(response, missing)
        answer = _body(response)
        errors = answer.get("errors")
        if isinstance(errors, list) and errors:
            kinds = {error.get("type") for error in errors if isinstance(error, dict)}
            if "RATE_LIMITED" in kinds:
                raise GitHubError(GitHubCode.UNREACHABLE)
            if kinds & _GRAPHQL_MISSING:
                raise GitHubError(GitHubCode.PERMISSIONS_MISSING)
            raise GitHubError(missing if "NOT_FOUND" in kinds else GitHubCode.REFUSED)
        data = answer.get("data")
        return data if isinstance(data, dict) else {}

    # Reads ---------------------------------------------------------------------------------------------------------

    async def repository(self, token: SecretStr, full_name: str) -> RepositoryInfo:
        """The repository and its owner; one the user cannot read is ``github_repo_not_accessible``."""

        answer = await self._rest(token, "GET", f"/repos/{full_name}", missing=GitHubCode.REPO_NOT_ACCESSIBLE)
        owner, permissions = answer.get("owner"), answer.get("permissions")
        login = owner.get("login") if isinstance(owner, dict) else None
        is_org = isinstance(owner, dict) and owner.get("type") == "Organization" and isinstance(login, str)
        can_write = isinstance(permissions, dict) and bool(permissions.get("push") or permissions.get("admin"))
        return RepositoryInfo(full_name, login if is_org else None, can_write)

    async def organization_projects(self, token: SecretStr, organization: str) -> list[ProjectInfo]:
        """The open projects of the organization, the most recently updated first (at most 50)."""

        data = await self._graphql(token, _LIST_QUERY, {"org": organization}, GitHubCode.NOT_AN_ORGANIZATION)
        org = data.get("organization")
        nodes = ((org or {}).get("projectsV2") or {}).get("nodes") if isinstance(org, dict) else None
        found = [_project(node) for node in nodes] if isinstance(nodes, list) else []
        return [project for project in found if project is not None and not project.closed]

    async def project(self, token: SecretStr, node_id: str) -> tuple[ProjectInfo, str | None]:
        """One project by node id and the login of its organization (``None`` when a user owns it)."""

        data = await self._graphql(token, _PROJECT_QUERY, {"id": node_id}, GitHubCode.PROJECT_NOT_FOUND)
        node = data.get("node")
        project = _project(node)
        if project is None:
            raise GitHubError(GitHubCode.PROJECT_NOT_FOUND)
        owner = node.get("owner") if isinstance(node, dict) else None
        login = owner.get("login") if isinstance(owner, dict) else None
        return project, login if isinstance(login, str) else None

    async def issue_states(
        self, token: SecretStr, project_id: str, issue_node_ids: Sequence[str]
    ) -> dict[str, IssueState]:
        """The state and the Status on ``project_id`` of each issue; an issue GitHub no longer finds is left out."""

        found: dict[str, IssueState] = {}
        for start in range(0, len(issue_node_ids), ITEM_QUERY_LIMIT):
            ids = list(issue_node_ids[start : start + ITEM_QUERY_LIMIT])
            data = await self._graphql(token, _STATES, {"ids": ids}, GitHubCode.ISSUE_GONE)
            for node in data.get("nodes") or []:
                if isinstance(node, dict) and isinstance(node.get("id"), str):
                    found[node["id"]] = IssueState(node.get("state") == "OPEN", _status(node, project_id))
        return found

    # Writes --------------------------------------------------------------------------------------------------------

    async def create_project(self, token: SecretStr, organization: str, title: str) -> ProjectInfo:
        data = await self._graphql(token, _ORG_QUERY, {"org": organization}, GitHubCode.NOT_AN_ORGANIZATION)
        org = data.get("organization")
        owner = org.get("id") if isinstance(org, dict) else None
        if not isinstance(owner, str):
            raise GitHubError(GitHubCode.NOT_AN_ORGANIZATION)
        data = await self._graphql(token, _CREATE, {"owner": owner, "title": title}, GitHubCode.REFUSED)
        created = _project((data.get("createProjectV2") or {}).get("projectV2"))
        if created is None:
            raise GitHubError(GitHubCode.REFUSED)
        return created

    async def create_issue(self, token: SecretStr, repository: str, content: IssueContent) -> IssueRef:
        path = f"/repos/{repository}/issues"
        answer = await self._rest(token, "POST", path, missing=GitHubCode.REPO_NOT_ACCESSIBLE, body=content.payload())
        number, issue_id, node_id = _int(answer.get("number")), _int(answer.get("id")), answer.get("node_id")
        if number is None or issue_id is None or not isinstance(node_id, str):
            raise GitHubError(GitHubCode.REFUSED)
        return IssueRef(number, issue_id, node_id)

    async def edit_issue(self, token: SecretStr, repository: str, number: int, content: IssueContent) -> None:
        path = f"/repos/{repository}/issues/{number}"
        await self._rest(token, "PATCH", path, missing=GitHubCode.ISSUE_GONE, body=content.payload())

    async def comment(self, token: SecretStr, repository: str, number: int, body: str) -> None:
        path = f"/repos/{repository}/issues/{number}/comments"
        await self._rest(token, "POST", path, missing=GitHubCode.ISSUE_GONE, body={"body": body})

    async def add_labels(self, token: SecretStr, repository: str, number: int, labels: Sequence[str]) -> None:
        path = f"/repos/{repository}/issues/{number}/labels"
        await self._rest(token, "POST", path, missing=GitHubCode.ISSUE_GONE, body={"labels": list(labels)})

    async def remove_label(self, token: SecretStr, repository: str, number: int, label: str) -> None:
        response = await self._client.send(token, "DELETE", f"/repos/{repository}/issues/{number}/labels/{label}")
        if not response.is_success and response.status_code != httpx.codes.NOT_FOUND:  # a label already gone is fine
            raise _rest_refusal(response, GitHubCode.ISSUE_GONE)

    async def add_sub_issue(self, token: SecretStr, repository: str, parent: int, sub_issue_id: int) -> None:
        path = f"/repos/{repository}/issues/{parent}/sub_issues"
        response = await self._client.send(token, "POST", path, {"sub_issue_id": sub_issue_id})
        if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY and _already_linked(response):
            return  # already a sub-issue (a repeated pass): nothing to do
        if not response.is_success:
            raise _rest_refusal(response, GitHubCode.ISSUE_GONE)

    async def add_item(self, token: SecretStr, project_id: str, issue_node_id: str) -> str:
        data = await self._graphql(
            token, _ADD_ITEM, {"project": project_id, "issue": issue_node_id}, GitHubCode.PROJECT_NOT_FOUND
        )
        item = (data.get("addProjectV2ItemById") or {}).get("item")
        item_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(item_id, str):
            raise GitHubError(GitHubCode.REFUSED)
        return item_id

    async def move_item(self, token: SecretStr, project_id: str, item_id: str, after: str | None) -> None:
        variables = {"project": project_id, "item": item_id, "after": after}
        await self._graphql(token, _MOVE_ITEM, variables, GitHubCode.PROJECT_NOT_FOUND)


def _status(node: Mapping[str, Any], project_id: str) -> str | None:
    items = (node.get("projectItems") or {}).get("nodes") or []
    for item in items:
        project = item.get("project") if isinstance(item, dict) else None
        if isinstance(project, dict) and project.get("id") == project_id:
            value = item.get("fieldValueByName")
            name = value.get("name") if isinstance(value, dict) else None
            return name if isinstance(name, str) else None
    return None
