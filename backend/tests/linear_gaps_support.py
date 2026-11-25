"""Shared fakes of the Linear gap channel tests (no tests in here): ``db.linear_gaps`` over dicts, the ``FakeLinear``
of the foundation (a Linear that holds issues, comments and workflow states) behind the member connections, the gap
service of ``jira_gaps_support`` with the answer rules of the real one, signed webhook deliveries, and the runtime
they hang on. Every id, title and text is made up."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.linear_gaps.answers import LinearAnswers
from api.linear_gaps.channel import LinearGaps
from api.linear_gaps.poster import LinearGapPoster
from api.linear_gaps.webhook import DELIVERY_HEADER, SIGNATURE_HEADER
from db import linear_gaps as module
from db.linear_gaps import DestinationChangedError, GapIssueView, GapTargetSaved, GapTargetView
from integrations_db_fakes import FakeIntegrationsDb
from jira_gaps_support import FakeGapService, gap, plan_of
from linear.errors import LinearUnauthorizedError
from linear.fake import FakeLinear
from linear.models import Comment, Organization, Team

PROJECT = uuid.UUID(int=7)
ACTING = uuid.UUID(int=77)  # the member the project's Linear is connected by
ANA, BO = uuid.UUID(int=78), uuid.UUID(int=79)
ORG, OTHER_ORG = "org-graphico", "org-elsewhere"
ACTING_LINEAR, ANA_LINEAR, BO_LINEAR, STRANGER = "lin-acting", "lin-ana", "lin-bo", "lin-stranger"
SIGNING_KEY = "whsec-made-up-for-tests"

PUBLIC_FUNCTIONS = (
    "get_target",
    "enabled_targets",
    "save_target",
    "require_destination",
    "set_parent",
    "record_error",
    "list_issues",
    "issue_rows",
    "add_issue",
    "mark_answered",
    "member_of",
    "revoke_connections",
    "url_key",
    "project_name",
)


class FakeLinearGapsDb:
    """The ``db.linear_gaps`` functions over dicts; ``members`` maps (workspace id, Linear user id) to the member."""

    def __init__(self) -> None:
        self.targets: dict[uuid.UUID, GapTargetView] = {}
        self.issues: dict[tuple[uuid.UUID, str], GapIssueView] = {}
        self.members: dict[tuple[str, str], uuid.UUID] = {}
        self.revoked: list[tuple[str, str | None]] = []
        self.names: dict[uuid.UUID, str] = {}
        self.url_keys: dict[uuid.UUID, str] = {}
        self.sessions: list[AsyncSession] = []  # the session each call got

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in PUBLIC_FUNCTIONS:
            monkeypatch.setattr(module, name, getattr(self, name))

    def _note(self, session: AsyncSession) -> None:
        """Remember the session a call got."""

        self.sessions.append(session)

    async def get_target(self, session: AsyncSession, project_id: uuid.UUID) -> GapTargetView | None:
        self._note(session)
        return self.targets.get(project_id)

    async def enabled_targets(self, session: AsyncSession) -> list[GapTargetView]:
        self._note(session)
        return [target for target in self.targets.values() if target.enabled]

    async def save_target(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        team_id: str,
        team_key: str,
        linear_project_id: str | None,
        grouping: str,
        enabled: bool,
        now: datetime,
    ) -> GapTargetSaved:
        self._note(session)
        before = self.targets.get(project_id)
        if before is None:
            self.targets[project_id] = GapTargetView(
                project_id, team_id, team_key, linear_project_id, grouping, None, None, enabled, now, None
            )
            return GapTargetSaved(turned_on=enabled, destination_changed=False)
        changed = (before.team_id, before.linear_project_id) != (team_id, linear_project_id)
        if changed:
            self.issues = {key: row for key, row in self.issues.items() if key[0] != project_id}
        self.targets[project_id] = replace(
            before,
            team_id=team_id,
            team_key=team_key,
            linear_project_id=linear_project_id,
            grouping=grouping,
            enabled=enabled,
            parent_issue_id=None if changed else before.parent_issue_id,
            parent_issue_identifier=None if changed else before.parent_issue_identifier,
        )
        return GapTargetSaved(turned_on=enabled and not before.enabled, destination_changed=changed)

    async def require_destination(
        self, session: AsyncSession, project_id: uuid.UUID, *, team_id: str, linear_project_id: str | None
    ) -> None:
        self._note(session)
        target = self.targets.get(project_id)
        if target is None or (target.team_id, target.linear_project_id) != (team_id, linear_project_id):
            raise DestinationChangedError

    async def set_parent(
        self, session: AsyncSession, project_id: uuid.UUID, issue_id: uuid.UUID, identifier: str
    ) -> None:
        self._note(session)
        self.targets[project_id] = replace(
            self.targets[project_id], parent_issue_id=issue_id, parent_issue_identifier=identifier
        )

    async def record_error(self, session: AsyncSession, project_id: uuid.UUID, error_code: str | None) -> None:
        self._note(session)
        self.targets[project_id] = replace(self.targets[project_id], last_error_code=error_code)

    async def list_issues(self, session: AsyncSession, project_id: uuid.UUID) -> list[GapIssueView]:
        self._note(session)
        return sorted((row for key, row in self.issues.items() if key[0] == project_id), key=lambda r: r.posted_at)

    async def issue_rows(self, session: AsyncSession, issue_id: uuid.UUID) -> list[GapIssueView]:
        self._note(session)
        return sorted((row for row in self.issues.values() if row.issue_id == issue_id), key=lambda r: r.number)

    async def add_issue(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        gap_id: str,
        issue_id: uuid.UUID,
        identifier: str,
        area: str,
        number: int,
        now: datetime,
    ) -> bool:
        self._note(session)
        if (project_id, gap_id) in self.issues:
            return False
        self.issues[project_id, gap_id] = GapIssueView(
            project_id, gap_id, issue_id, identifier, area, number, now, None
        )
        return True

    async def mark_answered(self, session: AsyncSession, project_id: uuid.UUID, gap_id: str, *, now: datetime) -> bool:
        self._note(session)
        row = self.issues.get((project_id, gap_id))
        if row is None or row.answered_at is not None:
            return False
        self.issues[project_id, gap_id] = replace(row, answered_at=now)
        return True

    async def member_of(
        self, session: AsyncSession, project_id: uuid.UUID, *, organization_id: str, linear_user_id: str
    ) -> uuid.UUID | None:
        self._note(session)
        del project_id
        return self.members.get((organization_id, linear_user_id))

    async def revoke_connections(
        self, session: AsyncSession, *, organization_id: str, linear_user_id: str | None
    ) -> int:
        self._note(session)
        self.revoked.append((organization_id, linear_user_id))
        return 1

    async def url_key(self, session: AsyncSession, project_id: uuid.UUID) -> str | None:
        self._note(session)
        return self.url_keys.get(project_id)

    async def project_name(self, session: AsyncSession, project_id: uuid.UUID) -> str | None:
        self._note(session)
        return self.names.get(project_id)


class SpyService(FakeGapService):
    """The gap service that also keeps the filters of every ``list_gaps`` call."""

    def __init__(self) -> None:
        super().__init__()
        self.listed: list[Mapping[str, Any]] = []

    async def list_gaps(self, project_id: str, *, cursor: str | None = None, limit: int = 50, **filters: Any) -> Any:
        self.listed.append(filters)
        return await super().list_gaps(project_id, cursor=cursor, limit=limit, **filters)


class FakeConnections:
    """``runtime.linear`` as the channel reads it: a connection per member (``active``, ``expired``) and the gateway of
    each, which is the one ``FakeLinear`` while the connection is active."""

    def __init__(self, linear: FakeLinear) -> None:
        self.linear = linear
        self.status: dict[uuid.UUID, str] = {ACTING: "active"}

    async def get(self, user_id: uuid.UUID) -> SimpleNamespace | None:
        status = self.status.get(user_id)
        if status is None:
            return None
        return SimpleNamespace(status=status, linear_user_id=ACTING_LINEAR, organization_id=ORG, organization_name="G")

    @asynccontextmanager
    async def gateway(self, user_id: uuid.UUID) -> AsyncIterator[FakeLinear]:
        """``runtime.linear.gateway(user_id)``: the member's gateway for the length of the ``async with``."""

        if self.status.get(user_id) != "active":
            raise LinearUnauthorizedError
        yield self.linear


@dataclass
class Rig:
    """A runtime with the channel on fakes: ``linear`` the workspace, ``service`` the gaps, ``db`` the rows."""

    runtime: Any
    db: FakeLinearGapsDb
    linear: FakeLinear
    team: Team
    service: SpyService
    connections: FakeConnections
    plan: SimpleNamespace
    integrations: FakeIntegrationsDb
    names: dict[str, str] = field(default_factory=lambda: {str(PROJECT): "Shop"})

    def poster(self) -> LinearGapPoster:
        async def name(project_id: str) -> str | None:
            return self.names.get(project_id)

        async def load_plan(_project_id: str) -> Any:
            return self.plan

        return LinearGapPoster(self.runtime, name_of=name, plan_of=load_plan, clock=self.linear.now)

    def channel(self) -> LinearGaps:
        """A new channel (a restarted process): it keeps nothing but the database."""

        return LinearGaps(self.runtime, self.poster(), LinearAnswers(self.runtime, now=self.linear.now))

    def answers(self) -> LinearAnswers:
        return LinearAnswers(self.runtime, now=self.linear.now)

    async def enable(self, grouping: str = "one", *, linear_project_id: str | None = None) -> None:
        await self.db.save_target(
            None,
            PROJECT,
            team_id=self.team.id,
            team_key=self.team.key,
            linear_project_id=linear_project_id,
            grouping=grouping,
            enabled=True,
            now=self.linear.now(),
        )

    def say(self, issue_id: uuid.UUID | str, user_id: str, *lines: str) -> Comment:
        """``user_id`` comments on the issue, one line per argument."""

        return self.linear.add_comment_as(str(issue_id), user_id, "\n".join(lines))

    def issues(self) -> list[Any]:
        """The issues of the workspace the channel made, in the order it made them."""

        return self.linear.all_issues()

    def children(self) -> list[Any]:
        return [issue for issue in self.issues() if issue.parent_id is not None]

    async def comments_of(self, issue_id: uuid.UUID | str) -> list[Comment]:
        """The comments of the acting member (the channel's own) on an issue, oldest first."""

        return [c for c in await self.linear.comments(str(issue_id)) if c.user_id == ACTING_LINEAR]


@asynccontextmanager
async def transaction() -> AsyncIterator[None]:
    yield None


def build_rig(monkeypatch: pytest.MonkeyPatch, runtime: Any = None, project: uuid.UUID = PROJECT) -> Rig:
    db = FakeLinearGapsDb()
    linear = FakeLinear(organization=Organization(ORG, "Graphico", "graphico-app"), user_id=ACTING_LINEAR)
    team = linear.add_team("ENG", "Engineering")
    for name, user in (("Ana", ANA_LINEAR), ("Bo", BO_LINEAR), ("Stranger", STRANGER)):
        linear.add_user(name, user)
    db.install(monkeypatch)
    db.members |= {(ORG, ACTING_LINEAR): ACTING, (ORG, ANA_LINEAR): ANA, (ORG, BO_LINEAR): BO}
    db.names[project] = "Shop"
    integrations = FakeIntegrationsDb()
    integrations.install(monkeypatch)
    integrations.put(project, "linear", ACTING, workspace_id=ORG, workspace_name="Graphico")
    service, connections = SpyService(), FakeConnections(linear)

    async def gaps_service() -> SpyService:
        return service

    wired = runtime if runtime is not None else SimpleNamespace(transaction=transaction)
    wired.gaps = SimpleNamespace(service=gaps_service)
    wired.linear = connections
    rig = Rig(wired, db, linear, team, service, connections, plan_of({}, {}), integrations)
    rig.names = {str(project): "Shop"}
    return rig


def severe(gap_id: str, severity: str, **kwargs: Any) -> SimpleNamespace:
    """A gap (``jira_gaps_support.gap``) with a severity."""

    item = gap(gap_id, **kwargs)
    item.severity = severity
    return item


# Signed deliveries ---------------------------------------------------------------------------------------------------


def sign(raw: bytes, secret: str = SIGNING_KEY) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def comment_event(comment: Comment, **over: Any) -> dict[str, Any]:
    """The JSON Linear sends for a created comment, as far as the channel reads it."""

    body: dict[str, Any] = {
        "action": "create",
        "type": "Comment",
        "organizationId": ORG,
        "webhookId": "wh-1",
        "webhookTimestamp": int(time.time() * 1000),
        "createdAt": comment.created_at.isoformat(),
        "data": {"id": comment.id, "issueId": comment.issue_id, "userId": comment.user_id, "body": comment.body},
    }
    return body | over


def delivery(
    payload: Mapping[str, Any], *, secret: str = SIGNING_KEY, delivery_id: str | None = "d-1"
) -> tuple[bytes, dict[str, str]]:
    """The raw body and the headers of one signed delivery (a test changes either to break it)."""

    raw = json.dumps(payload).encode()
    headers = {SIGNATURE_HEADER: sign(raw, secret), "Content-Type": "application/json"}
    if delivery_id is not None:
        headers[DELIVERY_HEADER] = delivery_id
    return raw, headers
