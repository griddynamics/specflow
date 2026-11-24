"""``FakeJiraGateway``: the ``JiraGateway`` in memory, with error, 429, crash and hold injection (sessions 03 to 05)."""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from jira.gateway import IssueState, IssueSummary, JiraError, JiraProject
from jira.reasons import Reason

RATE_WAIT_MAX: Final = 60
RATE_TRIES: Final = 5
CATEGORY_TODO: Final = "new"


@dataclass
class FakeIssue:
    key: str
    project_key: str
    fields: dict[str, Any]
    labels: set[str] = field(default_factory=set)
    properties: dict[str, Any] = field(default_factory=dict)
    category: str = CATEGORY_TODO
    status: str = "Backlog"
    deleted: bool = False
    created_at: datetime | None = None


@dataclass
class _Injection:
    error: JiraError
    key: str | None
    times: int | None
    after_effect: bool = False


class FakeJiraGateway:
    """In-memory Jira: tests set status categories, delete issues and inject failures; ``calls`` logs every call."""

    def __init__(self, *, project_key: str = "PRJ", name: str = "Project") -> None:
        self.project_key = project_key
        self.projects = {project_key: JiraProject(name, "10000", "10001", "10003")}
        self.issues: dict[str, FakeIssue] = {}
        self.links: dict[str, tuple[str, str]] = {}  # link id -> (prerequisite, waiter)
        self.comments: dict[str, list[Mapping[str, Any]]] = {}
        self.remote_links: dict[tuple[str, str], tuple[str, str]] = {}
        self.rank_order: dict[str | None, list[str]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.waits: list[float] = []
        self.points_field: str | None = "customfield_90001"
        self.missing: frozenset[str] = frozenset()
        self.on_call: Callable[[str], None] | None = None
        self.now: Callable[[], datetime] | None = None  # the harness clock: stamps creates, bounds ``find_created``
        self.index_lag = timedelta(0)  # the search lists an issue only once it is this old by the harness clock
        self.holds: dict[str, Callable[[], Awaitable[None]]] = {}
        self._injections: dict[str, list[_Injection]] = {}
        self._rates: dict[str, list[tuple[float, int]]] = {}
        self._keys = itertools.count(1)
        self._link_ids = itertools.count(10000)
        self.closed = False

    def add_issue(self, fields: Mapping[str, Any] | None = None, *, key: str | None = None, **state: Any) -> FakeIssue:
        """Seed an issue (an Epic, a stranger's issue); ``state`` sets ``category``, ``status``, ``labels``..."""

        fields = dict(fields or {})
        project = (fields.get("project") or {}).get("key", self.project_key)
        number = next(self._keys)  # advances for a given key too, so a later create never reuses a seeded number
        issue = FakeIssue(key or f"{project}-{number}", project, fields, **state)
        self.issues[issue.key] = issue
        self.rank_order.setdefault((fields.get("parent") or {}).get("key"), []).append(issue.key)
        return issue

    def set_category(self, key: str, category: str, status: str = "In Progress") -> None:
        self.issues[key].category, self.issues[key].status = category, status

    def delete(self, key: str) -> None:
        """The issue disappears from Jira (a person deleted it)."""

        self.issues[key].deleted = True

    def fail(
        self,
        method: str,
        reason: Reason,
        *,
        status: int | None = None,
        key: str | None = None,
        times: int | None = 1,
        after_effect: bool = False,
    ) -> None:
        """Fail ``method`` (for ``key`` only when given) ``times`` times (``None`` = always); ``after_effect`` applies
        the call first: the crash after the create reached Jira."""

        error = JiraError(reason, http_status=status)
        self._injections.setdefault(method, []).append(_Injection(error, key, times, after_effect))

    def rate_limit(self, method: str, retry_after: float, *, times: int = 1) -> None:
        """``times`` answers 429 with ``Retry-After`` for the next call of ``method`` (the real gateway retries)."""

        self._rates.setdefault(method, []).append((retry_after, times))

    def issue_properties(self, key: str) -> dict[str, Any]:
        return self.issues[key].properties

    def called(self, method: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == method]

    def _parent(self, fields: Mapping[str, Any]) -> None:
        """Jira answers 400 (not 404) for a parent that does not exist or was deleted."""

        parent = (fields.get("parent") or {}).get("key")
        if parent is not None and (parent not in self.issues or self.issues[parent].deleted):
            raise JiraError(Reason.INVALID_FIELD, http_status=400)

    def _issue(self, key: str) -> FakeIssue:
        issue = self.issues.get(key)
        if issue is None or issue.deleted:
            raise JiraError(Reason.NOT_FOUND, http_status=404)
        return issue

    async def _enter(self, method: str, key: str | None = None, **args: Any) -> _Injection | None:
        self.calls.append((method, {"key": key, **args} if key else args))
        if self.on_call is not None:
            self.on_call(method)
        if method in self.holds:
            await self.holds[method]()
        rates = self._rates.get(method)
        if rates:
            retry_after, times = rates.pop(0)
            if retry_after > RATE_WAIT_MAX or times > RATE_TRIES:
                raise JiraError(Reason.UNREACHABLE, http_status=429)
            self.waits += [retry_after] * times
        for injection in self._injections.get(method, []):
            if injection.key not in (None, key) or injection.times == 0:
                continue
            if injection.times is not None:
                injection.times -= 1
            if not injection.after_effect:
                raise injection.error
            return injection
        return None

    @staticmethod
    def _crash(injection: _Injection | None) -> None:
        if injection is not None:
            raise injection.error

    async def myself(self) -> str:
        await self._enter("myself")
        return "fake-account"

    async def project(self, key: str) -> JiraProject:
        await self._enter("project", key)
        if key not in self.projects:
            raise JiraError(Reason.NOT_FOUND, http_status=404)
        return self.projects[key]

    async def missing_permissions(self, key: str) -> frozenset[str]:
        await self._enter("missing_permissions", key)
        return self.missing

    async def issue_summary(self, key: str) -> IssueSummary:
        await self._enter("issue_summary", key)
        issue = self._issue(key)
        kind = issue.fields.get("issuetype", {}).get("name", "")
        return IssueSummary(issue.project_key, kind, issue.fields.get("summary", ""))

    async def story_points_field(self) -> str | None:
        await self._enter("story_points_field")
        return self.points_field

    async def create_issue(self, fields: Mapping[str, Any], *, project_id: str, node_id: str) -> str:
        injection = await self._enter("create_issue", fields=dict(fields), project_id=project_id, node_id=node_id)
        self._parent(fields)
        issue = self.add_issue(fields, properties={"specflow": {"project_id": project_id, "node_id": node_id}})
        issue.labels = set(fields.get("labels") or [])
        issue.created_at = self.now() if self.now else None
        self._crash(injection)
        return issue.key

    async def edit_issue(
        self,
        key: str,
        *,
        fields: Mapping[str, Any],
        add_labels: Sequence[str] = (),
        remove_labels: Sequence[str] = (),
    ) -> None:
        args = {"fields": dict(fields), "add": list(add_labels), "remove": list(remove_labels)}
        injection = await self._enter("edit_issue", key, **args)
        issue = self._issue(key)
        self._parent(fields)
        issue.fields.update(fields)
        issue.labels = (issue.labels | set(add_labels)) - set(remove_labels)
        self._crash(injection)

    async def issue_states(self, keys: Sequence[str]) -> dict[str, IssueState | None]:
        await self._enter("issue_states", keys=list(keys))
        states: dict[str, IssueState | None] = {}
        for key in keys:
            issue = self.issues.get(key)
            if issue is None or issue.deleted:
                states[key] = None
                continue
            blocked = {pre: link for link, (pre, waiter) in self.links.items() if waiter == key}
            states[key] = IssueState(issue.category, issue.status, blocked)
        return states

    async def find_created(self, project_key: str, *, project_id: str, minutes: int) -> dict[str, str]:
        await self._enter("find_created", project_key=project_key, project_id=project_id, minutes=minutes)
        since = self.now() - timedelta(minutes=minutes) if self.now else None
        return {
            issue.properties["specflow"]["node_id"]: issue.key
            for issue in self.issues.values()
            if issue.project_key == project_key
            and not issue.deleted
            and "specflow" in issue.labels
            and (since is None or issue.created_at is None or issue.created_at >= since)
            and (self.now is None or issue.created_at is None or self.now() - issue.created_at >= self.index_lag)
            and (issue.properties.get("specflow") or {}).get("project_id") == project_id
        }

    async def add_comment(self, key: str, doc: Mapping[str, Any]) -> None:
        injection = await self._enter("add_comment", key, doc=doc)
        self._issue(key)
        self.comments.setdefault(key, []).append(doc)
        self._crash(injection)

    async def add_blocks_link(self, *, prerequisite: str, waiter: str) -> None:
        injection = await self._enter("add_blocks_link", waiter, prerequisite=prerequisite, waiter=waiter)
        self._issue(prerequisite)
        self._issue(waiter)
        self.links[str(next(self._link_ids))] = (prerequisite, waiter)
        self._crash(injection)

    async def delete_link(self, link_id: str) -> None:
        await self._enter("delete_link", link_id=link_id)
        if link_id not in self.links:
            raise JiraError(Reason.NOT_FOUND, http_status=404)
        del self.links[link_id]

    async def rank(self, keys: Sequence[str], *, before: str | None = None, after: str | None = None) -> None:
        await self._enter("rank", keys=list(keys), before=before, after=after)
        if len(keys) > 50 or (before is None) == (after is None):
            raise JiraError(Reason.INVALID_FIELD, http_status=400)
        scope = (self._issue(keys[0]).fields.get("parent") or {}).get("key")
        order = [key for key in self.rank_order.get(scope, []) if key not in keys]
        anchor = before if before is not None else after
        if anchor not in order:
            raise JiraError(Reason.INVALID_FIELD, http_status=400)
        at = order.index(anchor) + (0 if before is not None else 1)
        self.rank_order[scope] = order[:at] + list(keys) + order[at:]

    async def upsert_remote_link(self, key: str, *, global_id: str, url: str, title: str) -> None:
        await self._enter("upsert_remote_link", key, global_id=global_id, url=url, title=title)
        self._issue(key)
        self.remote_links[(key, global_id)] = (url, title)

    async def aclose(self) -> None:
        self.closed = True
