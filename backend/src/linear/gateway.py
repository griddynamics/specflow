"""The Linear gateway contract of every Linear stream (plan export, source importer, gap channel). Every method is
async and acts as the one member the gateway was built for (OAuth ``actor=user``). Failures are ``linear.errors``;
no error carries Linear's text. Creates take a client UUID (see ``linear.ids``): the same id again is
``LinearAlreadyExists``, which makes a retried create idempotent. Implemented by ``linear.http.HttpLinearGateway``
(real) and ``linear.fake.FakeLinear`` (tests)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

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


class LinearGateway(Protocol):
    async def viewer(self) -> Viewer:
        """The member and the workspace of the token; ``LinearUnauthorized`` when it is refused."""

    async def teams(self) -> list[Team]:
        """Every team the member can see."""

    async def projects(self, team_id: str) -> list[Project]:
        """The projects of a team (not archived)."""

    async def project(self, project_id: str) -> Project:
        """``LinearNotFound`` when it does not exist or the member cannot see it."""

    async def workflow_states(self, team_id: str) -> list[WorkflowState]:
        """The states of a team, ordered by type and ``position``."""

    async def labels(self, team_id: str) -> list[Label]:
        """The team's labels, groups included, and the workspace labels the team can use."""

    async def create_project(
        self, project_id: str, team_id: str, name: str, *, description: str | None = None
    ) -> Project:
        """Create a project of the team under the client id ``project_id``."""

    async def create_milestone(
        self,
        project_id: str,
        milestone_id: str,
        name: str,
        *,
        description: str | None = None,
        sort_order: float | None = None,
    ) -> Milestone:
        """Create a milestone of the project under the client id ``milestone_id``."""

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
        """Create a team label under the client id ``label_id``; ``parent_id`` is a group label (single select)."""

    async def create_issue(self, issue: NewIssue) -> Issue:
        """Create an issue under the client id ``issue.issue_id``; ``LinearInvalid(field="estimate")`` when the team's
        estimate scale refuses the value."""

    async def update_issue(self, issue_id: str, **fields: Any) -> Issue:
        """Overwrite the given fields: ``title``, ``description``, ``state_id``, ``estimate``, ``priority``,
        ``label_ids``, ``project_id``, ``milestone_id``, ``parent_id``; an unknown name is ``LinearInvalid``."""

    async def issue(self, issue_id: str) -> Issue:
        """One issue by its id or its identifier (``ENG-12``); ``LinearNotFound`` when gone."""

    async def issues_of_project(self, project_id: str, cursor: str | None = None) -> Page[Issue]:
        """One page (at most 250) of the project's issues; pass ``next_cursor`` for the next."""

    async def children(self, issue_id: str) -> list[Issue]:
        """The sub-issues of an issue."""

    async def comments(self, issue_id: str, since: datetime | None = None) -> list[Comment]:
        """The issue's comments, oldest first; only those created after ``since`` when given."""

    async def create_comment(self, issue_id: str, body: str, *, comment_id: str | None = None) -> Comment:
        """Comment as the member (a client ``comment_id`` makes the create idempotent)."""

    async def create_relation(
        self, relation_id: str, issue_id: str, related_issue_id: str, relation_type: RelationType
    ) -> Relation:
        """``issue_id`` ``relation_type`` ``related_issue_id`` under the client id ``relation_id`` (``blocks`` |
        ``related`` | ``duplicate``)."""

    async def relations(self, issue_id: str) -> list[Relation]:
        """Every relation the issue is on either side of."""

    async def attach_link(self, issue_id: str, url: str, title: str) -> Attachment:
        """Attach a link to the issue; the same url on the same issue updates the title (no duplicate)."""

    async def project_documents(self, project_id: str) -> list[Document]:
        """The documents of the project."""

    async def resolve_link(self, url_or_identifier: str) -> Issue | Project:
        """An issue or project from a pasted Linear URL or an issue identifier (``ENG-12``);
        ``LinearNotFound`` when it names nothing the member can see, ``LinearInvalid`` when it is neither."""
