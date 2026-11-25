"""Shared fakes of the Jira gap channel tests (no tests in here): ``db.jira_gaps`` over dicts, a Jira that holds issues
with comments (the gateway of the acting account and the reader of its search), a gap service with the answer rules of
the real one, and the runtime they hang on. Every id, title and text is made up."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.jira_gaps import JiraGaps
from api.jira_gaps.poller import JiraGapPoller
from api.jira_gaps.poster import JiraGapPoster
from db import jira_gaps as module
from db.jira_gaps import GapIssueView, GapTargetSaved, GapTargetView
from graph.gap import GapNotFoundError, GapStateError
from integrations_db_fakes import FakeIntegrationsDb
from jira.gateway import JiraError, JiraProject
from jira.reasons import Reason

PROJECT = uuid.UUID(int=7)
ACTING = uuid.UUID(int=77)  # the member the project's Atlassian is connected by
ANA, BO = uuid.UUID(int=78), uuid.UUID(int=79)
ACTING_ACCOUNT, ANA_ACCOUNT, BO_ACCOUNT, STRANGER = "acct-acting", "acct-ana", "acct-bo", "acct-stranger"
START = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)

PUBLIC_FUNCTIONS = (
    "get_target",
    "enabled_targets",
    "save_target",
    "set_story",
    "record_poll",
    "record_error",
    "list_issues",
    "add_issue",
    "mark_answered",
    "member_accounts",
    "account_of",
)


class Clock:
    """A clock the test moves; every call is ``step`` later than the one before."""

    def __init__(self, start: datetime = START, step: timedelta = timedelta(seconds=1)) -> None:
        self.at = start
        self.step = step

    def __call__(self) -> datetime:
        self.at += self.step
        return self.at


class FakeJiraGapsDb:
    """The ``db.jira_gaps`` functions over dicts; ``accounts`` maps an Atlassian account id to the member."""

    def __init__(self) -> None:
        self.targets: dict[uuid.UUID, GapTargetView] = {}
        self.issues: dict[tuple[uuid.UUID, str], GapIssueView] = {}
        self.accounts: dict[str, uuid.UUID] = {}
        self.sessions: list[AsyncSession] = []  # the session each call got

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in PUBLIC_FUNCTIONS:
            monkeypatch.setattr(module, name, getattr(self, name))

    async def get_target(self, session: AsyncSession, project_id: uuid.UUID) -> GapTargetView | None:
        self.sessions.append(session)
        return self.targets.get(project_id)

    async def enabled_targets(self, session: AsyncSession) -> list[GapTargetView]:
        self.sessions.append(session)
        return [target for target in self.targets.values() if target.enabled]

    async def save_target(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        jira_project: str,
        grouping: str,
        enabled: bool,
        now: datetime,
    ) -> GapTargetSaved:
        self.sessions.append(session)
        before = self.targets.get(project_id)
        if before is None:
            self.targets[project_id] = GapTargetView(
                project_id, jira_project, grouping, None, enabled, now, None, None, None
            )
            return GapTargetSaved(turned_on=enabled, project_changed=False)
        changed = before.jira_project != jira_project
        if changed:
            self.issues = {key: row for key, row in self.issues.items() if key[0] != project_id}
        story = None if changed else before.story_key
        cursor = None if changed else before.poll_cursor
        self.targets[project_id] = replace(
            before, jira_project=jira_project, grouping=grouping, enabled=enabled, story_key=story, poll_cursor=cursor
        )
        return GapTargetSaved(turned_on=enabled and not before.enabled, project_changed=changed)

    async def set_story(self, session: AsyncSession, project_id: uuid.UUID, story_key: str) -> None:
        self.sessions.append(session)
        self.targets[project_id] = replace(self.targets[project_id], story_key=story_key)

    async def record_poll(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        cursor: datetime | None,
        now: datetime,
        error_code: str | None,
    ) -> None:
        self.sessions.append(session)
        target = self.targets[project_id]
        self.targets[project_id] = replace(
            target, poll_cursor=cursor or target.poll_cursor, last_polled_at=now, last_error_code=error_code
        )

    async def record_error(self, session: AsyncSession, project_id: uuid.UUID, error_code: str | None) -> None:
        self.sessions.append(session)
        self.targets[project_id] = replace(self.targets[project_id], last_error_code=error_code)

    async def list_issues(self, session: AsyncSession, project_id: uuid.UUID) -> list[GapIssueView]:
        self.sessions.append(session)
        return sorted((row for key, row in self.issues.items() if key[0] == project_id), key=lambda r: r.posted_at)

    async def add_issue(
        self,
        session: AsyncSession,
        project_id: uuid.UUID,
        *,
        gap_id: str,
        issue_key: str,
        area: str,
        number: int,
        now: datetime,
    ) -> bool:
        self.sessions.append(session)
        if (project_id, gap_id) in self.issues:
            return False
        self.issues[project_id, gap_id] = GapIssueView(project_id, gap_id, issue_key, area, number, now, None)
        return True

    async def mark_answered(self, session: AsyncSession, project_id: uuid.UUID, gap_id: str, *, now: datetime) -> bool:
        self.sessions.append(session)
        row = self.issues.get((project_id, gap_id))
        if row is None or row.answered_at is not None:
            return False
        self.issues[project_id, gap_id] = replace(row, answered_at=now)
        return True

    async def member_accounts(self, session: AsyncSession, project_id: uuid.UUID) -> dict[str, uuid.UUID]:
        self.sessions.append(session)
        del project_id
        return dict(self.accounts)

    async def account_of(self, session: AsyncSession, user_id: uuid.UUID) -> str | None:
        self.sessions.append(session)
        return next((account for account, user in self.accounts.items() if user == user_id), None)


@dataclass
class Comment:
    id: str
    author: str
    created: datetime
    doc: dict[str, Any]


@dataclass
class Issue:
    key: str
    fields: dict[str, Any]
    comments: list[Comment] = field(default_factory=list)
    updated: datetime = START


class FakeJira:
    """One Jira site: the gateway calls of the acting account (create, comment) and the search of its reader. A person's
    comment is ``say``; ``calls`` is every call, ``searches`` every ``updated_children``."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.issues: dict[str, Issue] = {}
        self.calls: list[tuple[str, Any]] = []
        self.searches: list[tuple[str, int | None]] = []
        self.missing: frozenset[str] = frozenset()
        self.read_error: JiraError | None = None
        self.write_error: JiraError | None = None
        self._keys = itertools.count(1)
        self._comments = itertools.count(100)

    # The gateway of the acting account ------------------------------------------------------------------------

    async def myself(self) -> str:
        return ACTING_ACCOUNT

    async def project(self, key: str) -> JiraProject:
        self.calls.append(("project", key))
        return JiraProject("Shop", "1", "10", "11")

    async def missing_permissions(self, key: str) -> frozenset[str]:
        self.calls.append(("missing_permissions", key))
        return self.missing

    async def create_issue(self, fields: Mapping[str, Any], *, project_id: str, node_id: str) -> str:
        if self.write_error is not None:
            raise self.write_error
        key = f"SHOP-{next(self._keys)}"
        self.calls.append(
            ("create", {"key": key, "node_id": node_id, "project_id": project_id, "fields": dict(fields)})
        )
        self.issues[key] = Issue(key, dict(fields), updated=self.clock())
        return key

    async def add_comment(self, key: str, doc: Mapping[str, Any]) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.calls.append(("comment", {"key": key, "doc": dict(doc)}))
        self._add(key, ACTING_ACCOUNT, dict(doc))

    def _add(self, key: str, author: str, doc: dict[str, Any]) -> None:
        issue = self.issues[key]
        issue.updated = self.clock()
        issue.comments.append(Comment(str(next(self._comments)), author, issue.updated, doc))

    # A person -------------------------------------------------------------------------------------------------

    def say(self, key: str, author: str, *lines: str) -> None:
        """``author`` comments on the issue, one paragraph per line."""

        content = [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in lines]
        self._add(key, author, {"type": "doc", "version": 1, "content": content})

    def created(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [call[1] for call in self.calls if call[0] == "create" and (kind is None or kind in call[1]["node_id"])]

    def comments_by_acting(self, key: str) -> list[Comment]:
        return [c for c in self.issues[key].comments if c.author == ACTING_ACCOUNT]

    # The reader of its search ---------------------------------------------------------------------------------

    @asynccontextmanager
    async def reader(self, _cloud_id: str, _tokens: Any) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(updated_children=self._children, comments=self._comments_of)

    async def _children(self, parent: str, within: int | None) -> list[dict[str, Any]]:
        self.searches.append((parent, within))
        if self.read_error is not None:
            raise self.read_error
        return [
            {"key": issue.key, "fields": {"updated": issue.updated.strftime("%Y-%m-%dT%H:%M:%S.000%z")}}
            for issue in self.issues.values()
            if issue.fields.get("parent", {}).get("key") == parent
        ]

    async def _comments_of(self, issue: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": c.id,
                "author": {"accountId": c.author},
                "created": c.created.strftime("%Y-%m-%dT%H:%M:%S.000%z"),
                "body": c.doc,
            }
            for c in self.issues[str(issue["key"])].comments
        ]


def gap(gap_id: str, *, kind: str = "missing", title: str | None = None, tasks: Sequence[str] = ()) -> SimpleNamespace:
    options = [
        SimpleNamespace(rank=1, label="Keep one cart", detail="One cart per shopper", recommended=True),
        SimpleNamespace(rank=2, label="Allow many carts", detail="", recommended=False),
    ]
    return SimpleNamespace(
        gap_id=gap_id,
        kind=kind,
        status="open",
        title=title or f"Cart question {gap_id}",
        question=f"Which cart rule applies to {gap_id}?",
        why="The documents disagree.",
        options=options,
        reach=SimpleNamespace(tasks=tuple(tasks)),
        assigned_to=None,
    )


class FakeGapService:
    """The gap service rules the channel relies on: only an open gap takes an answer, an answer closes it."""

    def __init__(self) -> None:
        self.gaps: dict[str, SimpleNamespace] = {}
        self.answers: list[Any] = []
        self.applied: list[str] = []
        self.assigned: list[tuple[str, str]] = []

    def add(self, *gaps: SimpleNamespace) -> None:
        for item in gaps:
            self.gaps[item.gap_id] = item

    async def list_gaps(self, _project_id: str, **_: Any) -> Any:
        return SimpleNamespace(gaps=[g for g in self.gaps.values() if g.status == "open"])

    async def get_gap(self, _project_id: str, gap_id: str) -> Any:
        if gap_id not in self.gaps:
            raise GapNotFoundError(gap_id=gap_id)
        return self.gaps[gap_id]

    async def answer_gap(self, _project_id: str, answer: Any) -> Any:
        item = await self.get_gap(_project_id, answer.gap_id)
        if item.status != "open":
            raise GapStateError(status=item.status, allowed=["open"], gap_id=answer.gap_id)
        self.answers.append(answer)
        item.status = "answered"
        return SimpleNamespace(gap_id=answer.gap_id)

    async def assign_gap(self, _project_id: str, gap_id: str, user_id: str, *, by: str) -> Any:
        self.assigned.append((gap_id, user_id))
        del by
        self.gaps[gap_id].assigned_to = user_id

    async def apply_now(self, _project_id: str, *, by: str) -> Any:
        self.applied.append(by)
        return SimpleNamespace(gap_ids=())


class FakeConnections:
    """``AtlassianConnections`` as the channel reads it: a connection per member (``active`` or ``expired``)."""

    def __init__(self) -> None:
        self.status: dict[uuid.UUID, str] = {ACTING: "active"}

    async def get(self, user_id: uuid.UUID) -> SimpleNamespace | None:
        status = self.status.get(user_id)
        return None if status is None else SimpleNamespace(status=status, cloud_id="cloud-1")

    def token_source(self, _user_id: uuid.UUID) -> object:
        return object()


@dataclass
class Rig:
    """A runtime with the channel on fakes: ``jira`` the site, ``service`` the gaps, ``db`` the rows."""

    runtime: Any
    db: FakeJiraGapsDb
    jira: FakeJira
    service: FakeGapService
    connections: FakeConnections
    clock: Clock
    plan: SimpleNamespace
    integrations: FakeIntegrationsDb
    names: dict[str, str] = field(default_factory=lambda: {str(PROJECT): "Shop"})

    def poster(self) -> JiraGapPoster:
        async def name(project_id: str) -> str | None:
            return self.names.get(project_id)

        async def load_plan(_project_id: str) -> Any:
            return self.plan

        return JiraGapPoster(self.runtime, project_name=name, load_plan=load_plan, now=self.clock)

    def poller(self) -> JiraGapPoller:
        """A new poller (a restarted process): it keeps nothing but the database."""

        return JiraGapPoller(self.runtime, now=self.clock, reader=self.jira.reader)

    def channel(self) -> JiraGaps:
        return JiraGaps(self.runtime, self.poster(), self.poller())

    async def enable(self, grouping: str = "one") -> None:
        await self.db.save_target(None, PROJECT, jira_project="SHOP", grouping=grouping, enabled=True, now=self.clock())


def plan_of(areas: Mapping[str, str], tasks: Mapping[str, str]) -> SimpleNamespace:
    """A plan with work areas (id -> title) and tasks (id -> area id)."""

    return SimpleNamespace(
        current=SimpleNamespace(areas=[SimpleNamespace(area_id=a, title=t, anchor=None) for a, t in areas.items()]),
        tasks={task: SimpleNamespace(area_id=area) for task, area in tasks.items()},
    )


@asynccontextmanager
async def _transaction() -> AsyncIterator[None]:
    yield None


def build_rig(monkeypatch: pytest.MonkeyPatch, runtime: Any = None, project: uuid.UUID = PROJECT) -> Rig:
    db, clock = FakeJiraGapsDb(), Clock()
    jira = FakeJira(clock)
    db.install(monkeypatch)
    db.accounts |= {ACTING_ACCOUNT: ACTING, ANA_ACCOUNT: ANA, BO_ACCOUNT: BO}
    integrations = FakeIntegrationsDb()
    integrations.install(monkeypatch)
    integrations.put(project, "atlassian", ACTING)
    service, connections = FakeGapService(), FakeConnections()

    async def gateway_for(user_id: uuid.UUID | None) -> FakeJira:
        view = await connections.get(user_id) if user_id is not None else None
        if view is None:
            raise JiraError(Reason.NOT_CONNECTED)
        if view.status != "active":
            raise JiraError(Reason.RECONNECT_REQUIRED)
        return jira

    async def gaps_service() -> FakeGapService:
        return service

    wired = runtime if runtime is not None else SimpleNamespace(transaction=_transaction)
    wired.gaps = SimpleNamespace(service=gaps_service)
    wired.jira = SimpleNamespace(connections=connections, gateway_for=gateway_for)
    if runtime is None:
        wired.settings = SimpleNamespace(jira=SimpleNamespace(site_url="https://example.atlassian.net"))
    rig = Rig(wired, db, jira, service, connections, clock, plan_of({}, {}), integrations)
    rig.names = {str(project): "Shop"}
    return rig
