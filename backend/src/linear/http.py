"""The real Linear GraphQL client (``https://api.linear.app/graphql``, ``Authorization: Bearer``): the queries and
mutations of ``linear.gateway.LinearGateway``, cursor pagination of 250, 429 and complexity refusals mapped onto
``linear.errors``. STUB of the foundation: stream S2 fills it (plans/LINEAR/LINEAR-PLAN.md section 5) and proves it
against ``linear.fake.FakeLinear`` with recorded responses.

``token`` is awaited before every call, so the caller's token cache (with its one refresh at a time) decides when the
access token is renewed; the gateway never stores a token."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Final, override

from linear.models import (
    Attachment,
    Comment,
    Document,
    Issue,
    Label,
    Milestone,
    NewIssue,
    Page,
    Project,
    Relation,
    RelationType,
    Team,
    Viewer,
    WorkflowState,
)

GRAPHQL_URL: Final = "https://api.linear.app/graphql"
PAGE_SIZE: Final = 250
_FILLED_BY: Final = "linear.http is filled by stream S2"

TokenProvider = Callable[[], Awaitable[str]]


class HttpLinearGateway:
    """``LinearGateway`` over GraphQL for one member. STUB: every method raises ``NotImplementedError`` (stream S2)."""

    def __init__(self, token: TokenProvider, *, url: str = GRAPHQL_URL) -> None:
        self._token = token
        self._url = url

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def aclose(self) -> None:
        """Nothing is held open yet."""

    async def viewer(self) -> Viewer:
        raise NotImplementedError(_FILLED_BY)

    async def teams(self) -> list[Team]:
        raise NotImplementedError(_FILLED_BY)

    async def projects(self, team_id: str) -> list[Project]:
        raise NotImplementedError(_FILLED_BY)

    async def project(self, project_id: str) -> Project:
        raise NotImplementedError(_FILLED_BY)

    async def workflow_states(self, team_id: str) -> list[WorkflowState]:
        raise NotImplementedError(_FILLED_BY)

    async def labels(self, team_id: str) -> list[Label]:
        raise NotImplementedError(_FILLED_BY)

    async def create_project(
        self, project_id: str, team_id: str, name: str, *, description: str | None = None
    ) -> Project:
        raise NotImplementedError(_FILLED_BY)

    async def create_milestone(
        self,
        project_id: str,
        milestone_id: str,
        name: str,
        *,
        description: str | None = None,
        sort_order: float | None = None,
    ) -> Milestone:
        raise NotImplementedError(_FILLED_BY)

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
        raise NotImplementedError(_FILLED_BY)

    async def create_issue(self, issue: NewIssue) -> Issue:
        raise NotImplementedError(_FILLED_BY)

    async def update_issue(self, issue_id: str, **fields: Any) -> Issue:
        raise NotImplementedError(_FILLED_BY)

    async def issue(self, issue_id: str) -> Issue:
        raise NotImplementedError(_FILLED_BY)

    async def issues_of_project(self, project_id: str, cursor: str | None = None) -> Page[Issue]:
        raise NotImplementedError(_FILLED_BY)

    async def children(self, issue_id: str) -> list[Issue]:
        raise NotImplementedError(_FILLED_BY)

    async def comments(self, issue_id: str, since: datetime | None = None) -> list[Comment]:
        raise NotImplementedError(_FILLED_BY)

    async def create_comment(self, issue_id: str, body: str, *, comment_id: str | None = None) -> Comment:
        raise NotImplementedError(_FILLED_BY)

    async def create_relation(
        self, relation_id: str, issue_id: str, related_issue_id: str, relation_type: RelationType
    ) -> Relation:
        raise NotImplementedError(_FILLED_BY)

    async def relations(self, issue_id: str) -> list[Relation]:
        raise NotImplementedError(_FILLED_BY)

    async def attach_link(self, issue_id: str, url: str, title: str) -> Attachment:
        raise NotImplementedError(_FILLED_BY)

    async def project_documents(self, project_id: str) -> list[Document]:
        raise NotImplementedError(_FILLED_BY)

    async def resolve_link(self, url_or_identifier: str) -> Issue | Project:
        raise NotImplementedError(_FILLED_BY)
