"""The real Linear GraphQL client (``https://api.linear.app/graphql``, ``Authorization: Bearer``): the queries and
mutations of ``linear.gateway.LinearGateway``, cursor pagination (100 per page, well under the 10,000 point complexity
limit), HTTP statuses, GraphQL ``errors[]`` and rate limits mapped onto ``linear.errors``.

``token`` is awaited before every call, so the caller's token cache (with its one refresh at a time) decides when the
access token is renewed; the gateway never stores a token. No log line, error or ``repr`` holds a token, a header or
Linear's text: errors are closed codes and a log line has the operation, the status, the code and the request id.
The gateway does not retry: a rate limit is ``LinearRateLimitedError(retry_after)`` and the caller decides."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime
from typing import Any, Final, override

import httpx

from linear.errors import (
    LinearAlreadyExistsError,
    LinearError,
    LinearForbiddenError,
    LinearInvalidError,
    LinearNotFoundError,
    LinearRateLimitedError,
    LinearUnauthorizedError,
    LinearUnavailableError,
)
from linear.models import (
    STATE_TYPES,
    Attachment,
    Comment,
    Document,
    Issue,
    Label,
    Milestone,
    NewIssue,
    Organization,
    Page,
    Project,
    Relation,
    RelationType,
    Team,
    Viewer,
    WorkflowState,
)
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("linear.http")

GRAPHQL_URL: Final = "https://api.linear.app/graphql"
PAGE_SIZE: Final = 100  # Linear allows 250; 100 keeps an issue page far below the 10,000 point complexity limit
LABELS_PER_ISSUE: Final = 20
TIMEOUT_SECONDS: Final = 30

TokenProvider = Callable[[], Awaitable[str]]
# Called with the token Linear refused; returns a new one (the member's connection refreshes under its lock).
TokenRefresher = Callable[[str], Awaitable[str]]

_ISSUE_URL: Final = re.compile(r"https?://linear\.app/([^/]+)/issue/([A-Za-z][A-Za-z0-9]*-\d+)(?:[/?#].*)?")
_PROJECT_URL: Final = re.compile(r"https?://linear\.app/([^/]+)/project/([^/?#]+)(?:[/?#].*)?")
_IDENTIFIER: Final = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")
_CAMEL: Final = re.compile(r"(?<!^)(?=[A-Z])")
_INPUT_NAMES: Final = ("estimate", "priority", "title", "description", "name", "body", "url", "color")

# update_issue field -> IssueUpdateInput field
_UPDATE_FIELDS: Final = {
    "title": "title",
    "description": "description",
    "state_id": "stateId",
    "estimate": "estimate",
    "priority": "priority",
    "label_ids": "labelIds",
    "project_id": "projectId",
    "milestone_id": "projectMilestoneId",
    "parent_id": "parentId",
}
_FIELD_NAMES: Final = {"project_milestone_id": "milestone_id"}

_PAGE_INFO: Final = "pageInfo { hasNextPage endCursor }"
_ISSUE: Final = f"""
fragment IssueFields on Issue {{
  id identifier url title description estimate priority createdAt updatedAt
  team {{ id }}
  state {{ id name type position }}
  parent {{ id }}
  project {{ id }}
  projectMilestone {{ id }}
  assignee {{ name }}
  labels(first: {LABELS_PER_ISSUE}) {{ nodes {{ ...LabelFields }} }}
}}
"""
_LABEL: Final = """
fragment LabelFields on IssueLabel { id name color isGroup parent { id } team { id } }
"""
_PROJECT: Final = """
fragment ProjectFields on Project { id name url description teams(first: 25) { nodes { id } } }
"""
_COMMENT: Final = """
fragment CommentFields on Comment { id body createdAt user { id name } issue { id } }
"""
_RELATION: Final = """
fragment RelationFields on IssueRelation { id type issue { id } relatedIssue { id } }
"""
_ISSUE_FRAGMENTS: Final = _ISSUE + _LABEL


def _connection(field: str, fragment_or_fields: str, arguments: str = "") -> str:
    """A paged field of the selected object: ``nodes`` of ``fragment_or_fields`` and ``pageInfo``."""

    extra = f", {arguments}" if arguments else ""
    return f"{field}(first: {PAGE_SIZE}, after: $after{extra}) {{ nodes {{ {fragment_or_fields} }} {_PAGE_INFO} }}"


# --- error mapping ------------------------------------------------------------------------------------------------


def _codes(error: Mapping[str, Any]) -> str:
    """``extensions.code`` and ``extensions.type`` as one upper-case token string (classification only)."""

    extensions = error.get("extensions") or {}
    parts = (str(extensions.get(key) or "") for key in ("code", "type"))
    return " ".join(parts).upper().replace(" ", "_")


def _text(error: Mapping[str, Any]) -> str:
    """Linear's own words, lower-cased, used to classify and never kept or surfaced."""

    extensions = error.get("extensions") or {}
    return f"{extensions.get('userPresentableMessage') or ''} {error.get('message') or ''}".lower()


def _field(error: Mapping[str, Any]) -> str | None:
    """The input name Linear refused: from its validation details, else a known input name in its text."""

    for detail in (error.get("extensions") or {}).get("validationErrors") or []:
        name = detail.get("property") if isinstance(detail, Mapping) else None
        if isinstance(name, str) and name:
            snake = _CAMEL.sub("_", name).lower()
            return _FIELD_NAMES.get(snake, snake)
    text = _text(error)
    return next((name for name in _INPUT_NAMES if re.search(rf"\b{name}\b", text)), None)


def _reset_seconds(headers: Mapping[str, str], family: str) -> float | None:
    remaining = headers.get(f"X-RateLimit-{family}-Remaining")
    reset = headers.get(f"X-RateLimit-{family}-Reset")
    if remaining is None or reset is None or remaining.strip() not in ("0", "0.0"):
        return None
    try:
        return max(float(reset) / 1000 - time.time(), 0.0)
    except ValueError:
        return None


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds to wait: ``Retry-After``, else the reset of the exhausted rate-limit bucket; ``None`` when unknown."""

    try:
        return max(float(headers["Retry-After"]), 0.0)
    except (KeyError, ValueError):
        pass
    waits = [wait for wait in (_reset_seconds(headers, "Requests"), _reset_seconds(headers, "Complexity")) if wait]
    return max(waits) if waits else None


def _has(haystack: str, needles: Iterable[str]) -> bool:
    return any(needle in haystack for needle in needles)


# (words of extensions.code/type, words of the text) -> the error; the first match wins
_SIMPLE: Final = (
    (("AUTHENTICATION", "UNAUTHENTICATED"), (), LinearUnauthorizedError),
    (("FORBIDDEN",), (), LinearForbiddenError),
    (("NOT_FOUND",), ("not found", "could not find"), LinearNotFoundError),
    (("ALREADY_EXISTS",), ("already exist", "must be unique", "duplicate"), LinearAlreadyExistsError),
)


def _classify(error: Mapping[str, Any], status: int, headers: Mapping[str, str]) -> LinearError:
    codes, text = _codes(error), _text(error)
    http_status = status or None
    if _has(codes, ("RATELIMITED", "RATE_LIMIT", "USAGE_LIMIT")) or "too complex" in text:
        return LinearRateLimitedError(_retry_after(headers), http_status=http_status)
    for code_words, text_words, cls in _SIMPLE:
        if _has(codes, code_words) or _has(text, text_words):
            return cls(http_status=http_status)
    if not _has(codes, ("INVALID", "INPUT", "VALIDATION")):
        return _from_status(status, headers)
    return LinearInvalidError(_field(error), http_status=http_status)


def _from_status(status: int, headers: Mapping[str, str]) -> LinearError:
    if status == httpx.codes.UNAUTHORIZED:
        return LinearUnauthorizedError(http_status=status)
    if status == httpx.codes.FORBIDDEN:
        return LinearForbiddenError(http_status=status)
    if status == httpx.codes.NOT_FOUND:
        return LinearNotFoundError(http_status=status)
    if status == httpx.codes.TOO_MANY_REQUESTS:
        return LinearRateLimitedError(_retry_after(headers), http_status=status)
    if status == httpx.codes.BAD_REQUEST:
        return LinearInvalidError(http_status=status)
    return LinearUnavailableError(http_status=status or None)


# --- parsing --------------------------------------------------------------------------------------------------------


def _nodes(connection: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    return list((connection or {}).get("nodes") or [])


def _when(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _ref(node: Mapping[str, Any], name: str) -> str | None:
    target = node.get(name)
    return None if target is None else str(target["id"])


def _state(node: Mapping[str, Any], team_id: str) -> WorkflowState:
    return WorkflowState(str(node["id"]), node["name"], node["type"], float(node["position"]), team_id)


def _label(node: Mapping[str, Any]) -> Label:
    return Label(
        id=str(node["id"]),
        name=node["name"],
        color=node.get("color"),
        parent_id=_ref(node, "parent"),
        is_group=bool(node.get("isGroup")),
        team_id=_ref(node, "team"),
    )


def _project(node: Mapping[str, Any]) -> Project:
    return Project(
        id=str(node["id"]),
        name=node["name"],
        url=node["url"],
        team_ids=tuple(str(team["id"]) for team in _nodes(node.get("teams"))),
        description=node.get("description") or None,
    )


def _issue(node: Mapping[str, Any]) -> Issue:
    team_id = str(node["team"]["id"])
    return Issue(
        id=str(node["id"]),
        identifier=node["identifier"],
        url=node["url"],
        title=node["title"],
        state=_state(node["state"], team_id),
        team_id=team_id,
        description=node.get("description") or None,
        parent_id=_ref(node, "parent"),
        project_id=_ref(node, "project"),
        milestone_id=_ref(node, "projectMilestone"),
        labels=tuple(_label(label) for label in _nodes(node.get("labels"))),
        estimate=None if node.get("estimate") is None else int(node["estimate"]),
        priority=int(node.get("priority") or 0),
        created_at=_when(node.get("createdAt")),
        updated_at=_when(node.get("updatedAt")),
        assignee_name=(node.get("assignee") or {}).get("name"),
    )


def _comment(node: Mapping[str, Any]) -> Comment:
    user = node.get("user") or {}
    return Comment(
        id=str(node["id"]),
        issue_id=str(node["issue"]["id"]),
        body=node["body"],
        user_id=None if not user else str(user["id"]),
        created_at=datetime.fromisoformat(node["createdAt"]),
        user_name=user.get("name"),
    )


def _relation(node: Mapping[str, Any]) -> Relation:
    return Relation(str(node["id"]), node["type"], str(node["issue"]["id"]), str(node["relatedIssue"]["id"]))


def _document(node: Mapping[str, Any], project_id: str) -> Document:
    return Document(
        id=str(node["id"]),
        project_id=project_id,
        title=node["title"],
        content=node.get("content") or "",
        url=node["url"],
        updated_at=_when(node.get("updatedAt")),
    )


def _present[T](node: T | None) -> T:
    """A null object in a successful answer is an object the member cannot see."""

    if node is None:
        raise LinearNotFoundError
    return node


def _shaped[T](parse: Callable[[Mapping[str, Any]], T], node: Mapping[str, Any] | None) -> T:
    """Parse one node; an answer that is not the shape the query asked for is an unreachable Linear."""

    try:
        return parse(_present(node))
    except (KeyError, TypeError, ValueError):
        raise LinearUnavailableError from None


def _shaped_all[T](parse: Callable[[Mapping[str, Any]], T], nodes: Iterable[Mapping[str, Any]]) -> list[T]:
    return [_shaped(parse, node) for node in nodes]


class HttpLinearGateway:
    """``LinearGateway`` over GraphQL for one member."""

    def __init__(
        self,
        token: TokenProvider,
        *,
        client: httpx.AsyncClient | None = None,
        url: str = GRAPHQL_URL,
        refresh: TokenRefresher | None = None,
    ) -> None:
        self._token = token
        self._refresh = refresh
        self._url = url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS, trust_env=False, follow_redirects=False)

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- transport ----------------------------------------------------------------------------------------------

    async def _call(self, operation: str, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One GraphQL request; the ``data`` object, or the ``linear.errors`` class of whatever went wrong. A refused
        token is refreshed once (``refresh``) and the request sent again."""

        token = await self._token()
        try:
            return await self._send(operation, token, query, variables)
        except LinearUnauthorizedError:
            if self._refresh is None:
                raise
        return await self._send(operation, await self._refresh(token), query, variables)

    async def _send(
        self, operation: str, token: str, query: str, variables: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {"query": query, "variables": dict(variables or {})}
        try:
            response = await self._client.post(self._url, json=payload, headers=headers)
        except httpx.HTTPError:
            unreachable = LinearUnavailableError()
            self._log(operation, unreachable, None, {})
            raise unreachable from None
        body = _json_object(response)
        refused = self._failure(response, body)
        data = (body or {}).get("data")
        if refused is None:
            if isinstance(data, dict):
                return data
            refused = LinearUnavailableError(http_status=response.status_code)
        self._log(operation, refused, response.status_code, response.headers)
        raise refused

    @staticmethod
    def _failure(response: httpx.Response, body: Mapping[str, Any] | None) -> LinearError | None:
        errors = [error for error in (body or {}).get("errors") or [] if isinstance(error, Mapping)]
        if errors:
            return _classify(errors[0], response.status_code, response.headers)
        if response.is_success:
            return None if body is not None else LinearUnavailableError(http_status=response.status_code)
        return _from_status(response.status_code, response.headers)

    @staticmethod
    def _log(operation: str, failure: LinearError, status: int | None, headers: Mapping[str, str]) -> None:
        log_event(
            logger,
            WARNING,
            "linear.http.failed",
            operation=operation,
            status=status,
            code=failure.code.value,
            linear_request=headers.get("x-request-id"),
        )

    async def _mutate(self, operation: str, query: str, variables: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        """A mutation's payload; ``success: false`` is Linear refusing the input."""

        payload = (await self._call(operation, query, variables)).get(key)
        if not isinstance(payload, Mapping) or not payload.get("success"):
            raise LinearInvalidError
        return payload

    async def _pages(
        self, operation: str, query: str, variables: Mapping[str, Any], *path: str
    ) -> list[Mapping[str, Any]]:
        """Every node of the connection at ``path`` of the answer, following ``endCursor`` until the last page."""

        found: list[Mapping[str, Any]] = []
        cursor: str | None = None
        while True:
            connection = _at(await self._call(operation, query, {**variables, "after": cursor}), path)
            found.extend(_nodes(connection))
            info = (connection or {}).get("pageInfo") or {}
            cursor = info.get("endCursor")
            if not info.get("hasNextPage") or not cursor:
                return found

    # --- reads ----------------------------------------------------------------------------------------------------

    async def viewer(self) -> Viewer:
        query = "query Viewer { viewer { id name organization { id name urlKey } } }"
        node = (await self._call("viewer", query)).get("viewer")

        def parse(found: Mapping[str, Any]) -> Viewer:
            org = found["organization"]
            return Viewer(str(found["id"]), found["name"], Organization(str(org["id"]), org["name"], org["urlKey"]))

        return _shaped(parse, node)

    async def teams(self) -> list[Team]:
        query = f"query Teams($after: String) {{ {_connection('teams', 'id key name')} }}"
        nodes = await self._pages("teams", query, {}, "teams")
        return _shaped_all(lambda node: Team(str(node["id"]), node["key"], node["name"]), nodes)

    async def projects(self, team_id: str) -> list[Project]:
        query = (
            f"query Projects($team: String!, $after: String) {{ team(id: $team) {{ "
            f"{_connection('projects', '...ProjectFields')} }} }} {_PROJECT}"
        )
        return _shaped_all(_project, await self._pages("projects", query, {"team": team_id}, "team", "projects"))

    async def project(self, project_id: str) -> Project:
        query = f"query Project($id: String!) {{ project(id: $id) {{ ...ProjectFields }} }} {_PROJECT}"
        return _shaped(_project, (await self._call("project", query, {"id": project_id})).get("project"))

    async def workflow_states(self, team_id: str) -> list[WorkflowState]:
        query = (
            f"query States($team: String!, $after: String) {{ team(id: $team) {{ "
            f"{_connection('states', 'id name type position')} }} }}"
        )
        nodes = await self._pages("workflow_states", query, {"team": team_id}, "team", "states")
        states = _shaped_all(lambda node: _state(node, team_id), nodes)
        return sorted(states, key=lambda state: (STATE_TYPES.index(state.type), state.position))

    async def labels(self, team_id: str) -> list[Label]:
        scope = "{ or: [{ team: { id: { eq: $team } } }, { team: { null: true } }] }"
        query = (
            f"query Labels($team: String!, $after: String) {{ team(id: $team) {{ id }} "
            f"{_connection('issueLabels', '...LabelFields', f'filter: {scope}')} }} {_LABEL}"
        )
        return _shaped_all(_label, await self._pages("labels", query, {"team": team_id}, "issueLabels"))

    async def issue(self, issue_id: str) -> Issue:
        query = f"query Issue($id: String!) {{ issue(id: $id) {{ ...IssueFields }} }} {_ISSUE_FRAGMENTS}"
        return _shaped(_issue, (await self._call("issue", query, {"id": issue_id})).get("issue"))

    async def issues_of_project(self, project_id: str, cursor: str | None = None) -> Page[Issue]:
        query = (
            f"query ProjectIssues($id: String!, $after: String) {{ project(id: $id) {{ "
            f"{_connection('issues', '...IssueFields')} }} }} {_ISSUE_FRAGMENTS}"
        )
        data = await self._call("issues_of_project", query, {"id": project_id, "after": cursor})
        connection = _at(data, ("project", "issues"))
        info = (connection or {}).get("pageInfo") or {}
        more = bool(info.get("hasNextPage")) and bool(info.get("endCursor"))
        return Page(tuple(_shaped_all(_issue, _nodes(connection))), info["endCursor"] if more else None)

    async def children(self, issue_id: str) -> list[Issue]:
        query = (
            f"query Children($id: String!, $after: String) {{ issue(id: $id) {{ "
            f"{_connection('children', '...IssueFields')} }} }} {_ISSUE_FRAGMENTS}"
        )
        return _shaped_all(_issue, await self._pages("children", query, {"id": issue_id}, "issue", "children"))

    async def comments(self, issue_id: str, since: datetime | None = None) -> list[Comment]:
        query = (
            f"query Comments($id: String!, $filter: CommentFilter, $after: String) {{ issue(id: $id) {{ "
            f"{_connection('comments', '...CommentFields', 'filter: $filter')} }} }} {_COMMENT}"
        )
        window = None if since is None else {"createdAt": {"gt": since.isoformat()}}
        nodes = await self._pages("comments", query, {"id": issue_id, "filter": window}, "issue", "comments")
        return sorted(_shaped_all(_comment, nodes), key=lambda comment: comment.created_at)

    async def relations(self, issue_id: str) -> list[Relation]:
        def query(field: str) -> str:
            return (
                f"query Relations($id: String!, $after: String) {{ issue(id: $id) {{ "
                f"{_connection(field, '...RelationFields')} }} }} {_RELATION}"
            )

        found: dict[str, Mapping[str, Any]] = {}
        for field in ("relations", "inverseRelations"):
            for node in await self._pages("relations", query(field), {"id": issue_id}, "issue", field):
                found[str(node["id"])] = node
        return _shaped_all(_relation, found.values())

    async def project_documents(self, project_id: str) -> list[Document]:
        query = (
            f"query Documents($id: String!, $after: String) {{ project(id: $id) {{ id "
            f"{_connection('documents', 'id title content url updatedAt')} }} }}"
        )
        nodes = await self._pages("project_documents", query, {"id": project_id}, "project", "documents")
        return _shaped_all(lambda node: _document(node, project_id), nodes)

    async def resolve_link(self, url_or_identifier: str) -> Issue | Project:
        text = url_or_identifier.strip()
        issue_url = _ISSUE_URL.fullmatch(text)
        if issue_url:
            await self._same_workspace(issue_url.group(1))
            return await self.issue(issue_url.group(2))
        project_url = _PROJECT_URL.fullmatch(text)
        if project_url:
            await self._same_workspace(project_url.group(1))
            return await self._project_by_slug(project_url.group(2))
        if _IDENTIFIER.fullmatch(text):
            return await self.issue(text)
        raise LinearInvalidError("link")

    async def _same_workspace(self, url_key: str) -> None:
        if (await self.viewer()).organization.url_key != url_key:
            raise LinearNotFoundError

    async def _project_by_slug(self, slug: str) -> Project:
        slug_id = slug.rsplit("-", 1)[-1]
        query = (
            "query ProjectBySlug($slug: String!) { projects(first: 1, filter: { slugId: { eq: $slug } }) "
            f"{{ nodes {{ ...ProjectFields }} }} }} {_PROJECT}"
        )
        nodes = _nodes((await self._call("resolve_link", query, {"slug": slug_id})).get("projects"))
        return _shaped(_project, nodes[0] if nodes else None)

    # --- writes ---------------------------------------------------------------------------------------------------

    async def create_project(
        self, project_id: str, team_id: str, name: str, *, description: str | None = None
    ) -> Project:
        query = (
            "mutation ProjectCreate($input: ProjectCreateInput!) { projectCreate(input: $input) "
            f"{{ success project {{ ...ProjectFields }} }} }} {_PROJECT}"
        )
        fields = {"id": project_id, "name": name, "teamIds": [team_id], "description": description}
        payload = await self._mutate("create_project", query, {"input": _present_only(fields)}, "projectCreate")
        return _shaped(_project, payload.get("project"))

    async def create_milestone(
        self,
        project_id: str,
        milestone_id: str,
        name: str,
        *,
        description: str | None = None,
        sort_order: float | None = None,
    ) -> Milestone:
        query = (
            "mutation MilestoneCreate($input: ProjectMilestoneCreateInput!) { projectMilestoneCreate(input: $input) "
            "{ success projectMilestone { id name description sortOrder project { id } } } }"
        )
        fields = {
            "id": milestone_id,
            "projectId": project_id,
            "name": name,
            "description": description,
            "sortOrder": sort_order,
        }
        payload = await self._mutate(
            "create_milestone", query, {"input": _present_only(fields)}, "projectMilestoneCreate"
        )

        def parse(node: Mapping[str, Any]) -> Milestone:
            return Milestone(
                str(node["id"]),
                str(node["project"]["id"]),
                node["name"],
                node.get("description") or None,
                float(node.get("sortOrder") or 0.0),
            )

        return _shaped(parse, payload.get("projectMilestone"))

    async def create_label(
        self,
        team_id: str,
        label_id: str,
        name: str,
        *,
        color: str | None = None,
        parent_id: str | None = None,
        is_group: bool = False,
    ) -> Label:
        query = (
            "mutation LabelCreate($input: IssueLabelCreateInput!) { issueLabelCreate(input: $input) "
            f"{{ success issueLabel {{ ...LabelFields }} }} }} {_LABEL}"
        )
        fields = {
            "id": label_id,
            "teamId": team_id,
            "name": name,
            "color": color,
            "parentId": parent_id,
            "isGroup": True if is_group else None,
        }
        payload = await self._mutate("create_label", query, {"input": _present_only(fields)}, "issueLabelCreate")
        return _shaped(_label, payload.get("issueLabel"))

    async def create_issue(self, issue: NewIssue) -> Issue:
        query = (
            "mutation IssueCreate($input: IssueCreateInput!) { issueCreate(input: $input) "
            f"{{ success issue {{ ...IssueFields }} }} }} {_ISSUE_FRAGMENTS}"
        )
        fields = {
            "id": issue.issue_id,
            "teamId": issue.team_id,
            "title": issue.title,
            "description": issue.description,
            "parentId": issue.parent_id,
            "projectId": issue.project_id,
            "projectMilestoneId": issue.milestone_id,
            "labelIds": list(issue.label_ids) or None,
            "estimate": issue.estimate,
            "priority": issue.priority,
            "stateId": issue.state_id,
        }
        payload = await self._mutate("create_issue", query, {"input": _present_only(fields)}, "issueCreate")
        return _shaped(_issue, payload.get("issue"))

    async def update_issue(self, issue_id: str, **fields: Any) -> Issue:
        unknown = set(fields) - set(_UPDATE_FIELDS)
        if unknown:
            raise LinearInvalidError(min(unknown))
        query = (
            "mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) "
            f"{{ success issue {{ ...IssueFields }} }} }} {_ISSUE_FRAGMENTS}"
        )
        values = {_UPDATE_FIELDS[name]: _listed(value) for name, value in fields.items()}
        payload = await self._mutate("update_issue", query, {"id": issue_id, "input": values}, "issueUpdate")
        return _shaped(_issue, payload.get("issue"))

    async def create_comment(self, issue_id: str, body: str, *, comment_id: str | None = None) -> Comment:
        query = (
            "mutation CommentCreate($input: CommentCreateInput!) { commentCreate(input: $input) "
            f"{{ success comment {{ ...CommentFields }} }} }} {_COMMENT}"
        )
        fields = {"id": comment_id, "issueId": issue_id, "body": body}
        payload = await self._mutate("create_comment", query, {"input": _present_only(fields)}, "commentCreate")
        return _shaped(_comment, payload.get("comment"))

    async def create_relation(
        self, relation_id: str, issue_id: str, related_issue_id: str, relation_type: RelationType
    ) -> Relation:
        query = (
            "mutation RelationCreate($input: IssueRelationCreateInput!) { issueRelationCreate(input: $input) "
            f"{{ success issueRelation {{ ...RelationFields }} }} }} {_RELATION}"
        )
        fields = {"id": relation_id, "issueId": issue_id, "relatedIssueId": related_issue_id, "type": relation_type}
        payload = await self._mutate("create_relation", query, {"input": fields}, "issueRelationCreate")
        return _shaped(_relation, payload.get("issueRelation"))

    async def attach_link(self, issue_id: str, url: str, title: str) -> Attachment:
        query = (
            "mutation AttachLink($issueId: String!, $url: String!, $title: String) { "
            "attachmentLinkURL(issueId: $issueId, url: $url, title: $title) "
            "{ success attachment { id url title issue { id } } } }"
        )
        variables = {"issueId": issue_id, "url": url, "title": title}
        payload = await self._mutate("attach_link", query, variables, "attachmentLinkURL")

        def parse(node: Mapping[str, Any]) -> Attachment:
            return Attachment(str(node["id"]), str(node["issue"]["id"]), node["url"], node.get("title") or title)

        return _shaped(parse, payload.get("attachment"))


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _at(data: Mapping[str, Any], path: tuple[str, ...]) -> Mapping[str, Any] | None:
    """The object at ``path`` of the answer; a null on the way is an object the member cannot see."""

    node: Any = data
    for step in path:
        node = _present(node.get(step) if isinstance(node, Mapping) else None)
    return node if isinstance(node, Mapping) else None


def _present_only(fields: Mapping[str, Any]) -> dict[str, Any]:
    """A create input without the fields that were not given (Linear applies its defaults to those)."""

    return {name: value for name, value in fields.items() if value is not None}


def _listed(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value
