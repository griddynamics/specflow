"""One Linear pass of a project (plans/LINEAR/LINEAR-PLAN.md section 3): the plan's stories become issues of the target
Linear project, its tasks sub-issues, its areas labels, its releases milestones and its WAITS_FOR "blocks" relations
(task level and the story level derived from it). The rules are the Jira sync's (docs/jira-sync/sync.md): an issue still
in Triage, Backlog or Todo takes the plan's fields, any other gets one comment listing the changes, an item that leaves
the plan is labelled and commented, never deleted.

The pass writes map rows only (the runner records the start and the end). Linear calls never run inside a transaction;
every row write checks that the destination is unchanged. Creates carry a client UUID (``linear.ids``), so a create
that crashed before its row was written is adopted, not repeated: the retry answers "already exists" and the pass reads
the issue. Issues are never deleted; relations are only added (the gateway has no delete).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from api.contained import Contained
from api.exports.digest import digest
from api.exports.status import FAILED, PARTIAL, SKIPPED, SUCCEEDED
from api.jira.desired import Desired, Row, SpecRef, build, is_deleted, spec_ids
from api.linear_plan import content as rules
from api.linear_plan.gateway import NotConnectedError
from api.linear_plan.scaffold import Scaffold
from api.linear_plan.store import PlanStore, TargetChangedError
from db.linear_plan import LinearTargetView
from db.models import LinearIssue
from graph.plan.reader import LoadedPlan
from jira.reasons import Reason
from linear.errors import (
    LinearAlreadyExistsError,
    LinearCode,
    LinearError,
    LinearInvalidError,
    LinearNotFoundError,
    LinearRateLimitedError,
)
from linear.gateway import LinearGateway
from linear.ids import KIND_COMMENT, KIND_RELATION, KIND_STORY, KIND_TASK, node_uuid
from linear.models import Issue, NewIssue
from logger.logger import WARNING, get_logger, log_event

logger = get_logger(__name__)

NOT_CONFIGURED: Final = "linear_not_configured"
NOT_CONNECTED: Final = "linear_not_connected"
RECONNECT_REQUIRED: Final = "linear_reconnect_required"
RATE_LIMITED: Final = "linear_rate_limited"
PROJECT_NOT_FOUND: Final = "linear_project_not_found"
ISSUE_GONE: Final = "linear_issue_gone"  # a person deleted the issue in Linear: it is never made again
ESTIMATE_REFUSED: Final = "linear_estimate_refused"  # the team's estimate scale refused the points: no estimate
NO_PLAN, PLAN_APPLYING = "no_plan", "plan_applying"
TARGET_CHANGED, UNEXPECTED_ERROR = "target_changed", "unexpected_error"
EDITABLE_STATES: Final = frozenset({"triage", "backlog", "unstarted"})
DEFAULT_RETRY_SECONDS: Final = 60.0
_STORY_PARENT: Final = "-"  # the Epic placeholder of the shared builder: a story has no parent issue
_RUN_CODES: Final[dict[LinearCode, str]] = {
    LinearCode.UNAUTHORIZED: RECONNECT_REQUIRED,
    LinearCode.FORBIDDEN: "linear_forbidden",
    LinearCode.NOT_FOUND: "linear_not_found",
    LinearCode.RATE_LIMITED: RATE_LIMITED,
    LinearCode.INVALID: "linear_invalid",
    LinearCode.UNAVAILABLE: "linear_unreachable",
}


@dataclass
class PushCounts:
    created: int = 0
    updated: int = 0
    commented: int = 0
    removed: int = 0
    relations_added: int = 0
    estimate_refused: int = 0
    unchanged: int = 0
    failed: int = 0


@dataclass(frozen=True)
class PushResult:
    """``retry_after``: seconds Linear asked to wait (a rate-limited pass), for the runner's retry."""

    status: str
    error_code: str | None
    counts: PushCounts
    retry_after: float | None = None


@dataclass(frozen=True)
class PushProgress:
    step: str  # issues | relations
    done: int
    total: int


@dataclass(frozen=True)
class PushDeps:
    """What a pass needs from its host; ``gateway_for`` is the gateway of the member the project's Linear is bound to
    NOW, read for every pass (a refusal ends the pass ``failed`` with no Linear call)."""

    store: PlanStore
    gateway_for: Callable[[uuid.UUID], AbstractAsyncContextManager[LinearGateway]]
    load_plan: Callable[[str], Awaitable[LoadedPlan]]
    spec_names: Callable[[str, Sequence[str]], Awaitable[Mapping[str, SpecRef]]]
    now: Callable[[], datetime]
    public_url: str | None = None
    report: Callable[[str, PushProgress], None] | None = None


def _row(item: LinearIssue) -> Row:
    gone = item.error_code == ISSUE_GONE  # the shared builder knows one "deleted" code
    return Row(
        item.node_id,
        item.kind,
        None if item.issue_id is None else str(item.issue_id),
        item.requested_at,
        item.content,
        item.content_hash,
        item.position,
        item.removed_at,
        Reason.NOT_FOUND.value if gone else item.error_code,
        item.synced_at,
    )


def run_code(code: LinearCode) -> str:
    return _RUN_CODES.get(code, _RUN_CODES[LinearCode.INVALID])


async def push_once(project_id: str, deps: PushDeps) -> PushResult:
    """Run one pass; the result holds a status and a code, never text."""

    run = _Pass(project_id, deps)
    status: str = FAILED
    code: str | None = UNEXPECTED_ERROR
    retry_after: float | None = None
    with Contained() as step:
        try:
            status, code = await run.execute()
        except TargetChangedError:
            status, code = SKIPPED, TARGET_CHANGED
        except NotConnectedError:
            status, code = FAILED, NOT_CONNECTED
        except LinearRateLimitedError as error:
            status, code = FAILED, RATE_LIMITED
            retry_after = DEFAULT_RETRY_SECONDS if error.retry_after is None else error.retry_after
        except LinearError as error:
            status, code = FAILED, run_code(error.code)
    if step.failed:
        log_event(logger, WARNING, "api.linear_plan.sync.failed", project_id=project_id, error_type=step.error_type)
    return PushResult(status, code, run.counts, retry_after)


class _Pass:
    def __init__(self, project_id: str, deps: PushDeps) -> None:
        self.project_id = project_id
        self.pid = uuid.UUID(project_id)
        self.deps = deps
        self.counts = PushCounts()
        self.rows: dict[str, Row] = {}
        self.ids: dict[str, LinearIssue] = {}
        self.target: LinearTargetView
        self.gateway: LinearGateway
        self.scaffold: Scaffold
        self.plan: LoadedPlan
        self.names: Mapping[str, SpecRef] = {}
        self.desired: Desired
        self.fresh: set[str] = set()
        self.failed: set[str] = set()
        self.step, self.done, self.total = "issues", 0, 0

    # The pass --------------------------------------------------------------------------------------------------------

    async def execute(self) -> tuple[str, str | None]:
        if not await self._open():
            return SKIPPED, NOT_CONFIGURED
        self.plan = await self.deps.load_plan(self.project_id)
        if self.plan.current.plan is None:
            return SKIPPED, NO_PLAN
        if self.plan.applying:
            return SKIPPED, PLAN_APPLYING
        ids = spec_ids(self.plan)
        self.names = await self.deps.spec_names(self.project_id, ids) if ids else {}
        self.desired = self._build()
        creates, changed = self._diff()
        revived, removed = len(self.desired.revived), len(self.desired.removed)
        self.counts.unchanged = len(self.desired.items) - len(creates) - len(changed) - revived
        if not (creates or changed or removed or revived or self._related()):
            return SUCCEEDED, None
        async with self.deps.gateway_for(self.pid) as gateway:  # closed with the pass; a refused token is renewed once
            self.gateway = gateway
            return await self._export(creates)

    async def _export(self, creates: list[str]) -> tuple[str, str | None]:
        target = self.target
        if not await self._project_exists():
            return FAILED, PROJECT_NOT_FOUND
        self.scaffold = Scaffold(
            self.gateway, self.pid, team_id=target.team_id, linear_project_id=target.linear_project_id
        )
        events = len(self._diff()[1]) + len(self.desired.revived) + len(self.desired.removed)
        self._begin("issues", len(creates) + events)
        for node in creates:
            await self._guard(node, self._create(node))
            self._tick()
        self.desired = self._build()
        _, changed = self._diff()
        await self._reconcile(changed)
        related = self._related()
        self._begin("relations", len(related))
        for node in related:
            await self._guard(node, self._relate(node))
            self._tick()
        return (PARTIAL if self.counts.failed else SUCCEEDED), None

    async def _open(self) -> bool:
        """Read the target and the rows; false when the project has no enabled target."""

        found = await self.deps.store.get_target(self.pid)
        if found is None or not found.enabled:
            return False
        self.target = found
        await self._reload()
        return True

    async def _project_exists(self) -> bool:
        try:
            await self.gateway.project(self.target.linear_project_id)
        except LinearNotFoundError:
            return False
        return True

    async def _reload(self) -> None:
        self.ids = {item.node_id: item for item in await self.deps.store.list_items(self.pid)}
        self.rows = {node: _row(item) for node, item in self.ids.items()}

    def _build(self) -> Desired:
        return build(self.plan, project_name="", epic_key=_STORY_PARENT, rows=self.rows, names=self.names)

    def _has_issue(self, node: str) -> bool:
        row = self.ids.get(node)
        return row is not None and row.issue_id is not None and not is_deleted(self.rows[node])

    def _diff(self) -> tuple[list[str], list[str]]:
        creates = [node for node in self.desired.items if node not in self.ids or self.ids[node].issue_id is None]
        changed = [
            node
            for node, item in self.desired.items.items()
            if self._has_issue(node)
            and node not in self.fresh
            and node not in self.desired.revived
            and digest(rules.canonical(item)) != self.ids[node].content_hash
        ]
        return creates, changed

    # Progress --------------------------------------------------------------------------------------------------------

    def _begin(self, step: str, total: int) -> None:
        self.step, self.done, self.total = step, 0, total
        self._report()

    def _tick(self) -> None:
        self.done += 1
        self._report()

    def _report(self) -> None:
        if self.deps.report is not None:
            self.deps.report(self.project_id, PushProgress(self.step, self.done, self.total))

    # Rows ------------------------------------------------------------------------------------------------------------

    async def _store(self, node: str, **columns: Any) -> None:
        await self.deps.store.update_item(self.target, node, **columns)
        for name, value in columns.items():
            setattr(self.ids[node], name, value)
        self.rows[node] = _row(self.ids[node])

    async def _fail(self, node: str, code: str) -> None:
        if node in self.failed:
            return
        self.failed.add(node)
        self.counts.failed += 1
        log_event(
            logger,
            WARNING,
            "api.linear_plan.sync.item_failed",
            project_id=self.project_id,
            node_id=node,
            error_code=code,
        )
        await self._store(node, error_code=code)

    async def _gone(self, node: str) -> None:
        """A person deleted the issue in Linear: it is never made again."""

        row = self.rows.get(node)
        if row is None or is_deleted(row) or node not in self.ids:
            return
        now = self.deps.now()
        if row.removed_at is None:
            self.counts.removed += 1
        await self._store(node, removed_at=row.removed_at or now, error_code=ISSUE_GONE, synced_at=now)

    async def _guard(self, node: str, call: Awaitable[None]) -> None:
        """Item errors end the item, not the pass: Linear's validation fails it, a missing issue (or the parent of a
        sub-issue) means a person deleted it. Everything else (rate limit, access, network) ends the pass."""

        try:
            await call
        except LinearNotFoundError:
            culprit = node
            if node not in self.ids or self.ids[node].issue_id is None:
                culprit = self.desired.items[node].parent or node
            if culprit in self.ids and self.ids[culprit].issue_id is not None:
                await self._gone(culprit)
            else:
                raise
        except LinearInvalidError:
            await self._fail(node, run_code(LinearCode.INVALID))

    # Creating --------------------------------------------------------------------------------------------------------

    async def _create(self, node: str) -> None:
        item = self.desired.items[node]
        parent_issue: str | None = None
        if item.kind == "subtask":
            parent = self.ids.get(item.parent or "")
            if parent is None or item.parent in self.failed or not self._has_issue(parent.node_id):
                return  # its story has no issue (refused or gone): the task waits for the next pass
            parent_issue = str(parent.issue_id)
        now = self.deps.now()
        await self.deps.store.add_pending(self.target, node_id=node, kind=item.kind, requested_at=now)
        if node not in self.ids:
            await self._reload()
        values = rules.canonical(item)
        made, refused = await self._make(node, item.kind, values, parent_issue)
        await self._store(
            node,
            issue_id=uuid.UUID(made.id),
            identifier=made.identifier,
            error_code=ESTIMATE_REFUSED if refused else None,
            synced_at=now,
        )
        self.fresh.add(node)
        self.counts.created += 1
        await self._link(node, made.id)
        await self._store(node, content={**values, "blocked_by": []}, content_hash=digest(values), synced_at=now)

    async def _make(
        self, node: str, kind: str, values: Mapping[str, Any], parent_issue: str | None
    ) -> tuple[Issue, bool]:
        """Create the issue under its client id; an issue already there under that id (a create of an earlier pass
        that crashed before its row was written) is adopted. ``True``: the estimate was refused and left out."""

        rendered = rules.render(values, node)
        issue_uuid = node_uuid(self.pid, KIND_STORY if kind == "story" else KIND_TASK, node)
        milestone = await self.scaffold.milestone(rendered.release) if kind == "story" and rendered.release else None
        new = NewIssue(
            issue_id=str(issue_uuid),
            team_id=self.target.team_id,
            title=rendered.title,
            description=rendered.description,
            parent_id=parent_issue,
            project_id=self.target.linear_project_id,
            milestone_id=milestone,
            label_ids=tuple(await self.scaffold.label_ids(rendered.label_names)),
            estimate=rendered.estimate,
        )
        refused = False
        while True:
            try:
                return await self.gateway.create_issue(new), refused
            except LinearAlreadyExistsError:
                return await self.gateway.issue(new.issue_id), refused
            except LinearInvalidError as error:
                if error.field != "estimate" or new.estimate is None:
                    raise
                new, refused = dataclasses.replace(new, estimate=None), True
                self.counts.estimate_refused += 1

    async def _link(self, node: str, issue_id: str) -> None:
        """The "Open in Graphico" link of an issue (the same url again only renames it)."""

        if self.deps.public_url:
            url = f"{self.deps.public_url.rstrip('/')}/projects/{self.project_id}?node={node}"
            await self.gateway.attach_link(issue_id, url, rules.LINK_TITLE)

    # Updating --------------------------------------------------------------------------------------------------------

    async def _reconcile(self, changed: Sequence[str]) -> None:
        events = (
            [(node, rules.CHANGED) for node in changed]
            + [(node, rules.REVIVED) for node in self.desired.revived]
            + [(node, rules.REMOVED) for node in self.desired.removed]
        )
        for node, event in events:
            if node not in self.failed and self._has_issue(node):
                await self._guard(node, self._apply(node, event))
            self._tick()

    async def _apply(self, node: str, event: str) -> None:
        row, item = self.ids[node], self.desired.items.get(node)
        issue = await self.gateway.issue(str(row.issue_id))
        editable = issue.state.type in EDITABLE_STATES
        now = self.deps.now()
        if item is None:  # leaves the plan: the removal label and a comment, whatever the state
            await self._set_labels(issue, add_removed=True)
            await self.gateway.create_comment(str(row.issue_id), rules.comment(node, event))
            self.counts.commented += 1
            self.counts.removed += 1
            await self._store(node, removed_at=now, error_code=None, synced_at=now)
            return
        new = rules.canonical(item)
        refused = False
        if editable:
            refused = await self._overwrite(issue, node, new, revived=event == rules.REVIVED)
            self.counts.updated += 1
        elif event == rules.REVIVED:
            await self._set_labels(issue, add_removed=False)
        items = rules.change_items(row.content, new)
        if event == rules.REVIVED or (items and not editable):
            await self._comment(row, node, event, items if not editable else [], issue)
        stored = {**new, "blocked_by": list((row.content or {}).get("blocked_by") or [])}
        await self._store(
            node,
            content=stored,
            content_hash=digest(new),
            removed_at=None if event == rules.REVIVED else row.removed_at,
            error_code=ESTIMATE_REFUSED if refused else None,
            synced_at=now,
        )

    async def _comment(self, row: LinearIssue, node: str, event: str, items: Sequence[str], issue: Issue) -> None:
        body = rules.comment(node, event, items, issue.state.name)
        key = f"{node}:{row.content_hash}:{digest({'items': list(items)})}" if event == rules.CHANGED else None
        comment_id = None if key is None else str(node_uuid(self.pid, KIND_COMMENT, key))
        try:
            await self.gateway.create_comment(str(row.issue_id), body, comment_id=comment_id)
        except LinearAlreadyExistsError:
            return  # this very change was commented before (a pass that crashed before its row was written)
        self.counts.commented += 1

    async def _overwrite(self, issue: Issue, node: str, new: Mapping[str, Any], *, revived: bool) -> bool:
        """Write the plan's fields into an issue that has not started; ``True`` when the estimate was refused."""

        row = self.ids[node]
        rendered = rules.render(new, node)
        old = row.content or {}
        fields: dict[str, Any] = {
            "title": rendered.title,
            "description": rendered.description,
            "estimate": rendered.estimate,
            "label_ids": await self._label_ids(issue, rendered.label_names, drop_removed=revived),
        }
        if old.get("release") != new.get("release") and new["kind"] == "story":
            fields["milestone_id"] = await self.scaffold.milestone(rendered.release) if rendered.release else None
            fields["project_id"] = self.target.linear_project_id
        if new.get("parent") and old.get("parent") != new["parent"] and self._has_issue(new["parent"]):
            fields["parent_id"] = str(self.ids[new["parent"]].issue_id)
        refused = False
        try:
            await self.gateway.update_issue(issue.id, **fields)
        except LinearInvalidError as error:
            if error.field != "estimate" or rendered.estimate is None:
                raise
            await self.gateway.update_issue(issue.id, **{**fields, "estimate": None})
            refused = True
            self.counts.estimate_refused += 1
        if not old:  # an issue made by a pass that stopped before its content was stored: the link may be missing
            await self._link(node, issue.id)
        return refused

    async def _label_ids(self, issue: Issue, names: Sequence[str], *, drop_removed: bool = False) -> list[str]:
        """The labels an overwrite sets: what a person added stays, the owned and area labels are the plan's."""

        keep = [label.id for label in issue.labels if not await self.scaffold.is_owned(label)]
        wanted = await self.scaffold.label_ids(names)
        removed = [] if drop_removed else [label.id for label in issue.labels if label.name == rules.REMOVED_LABEL]
        return [*keep, *wanted, *removed]

    async def _set_labels(self, issue: Issue, *, add_removed: bool) -> None:
        """Put the removal label on an issue that left the plan, or take it off one that came back."""

        current = [label.id for label in issue.labels]
        removed_id = await self.scaffold.removed_label_id()
        wanted = [*current, removed_id] if add_removed else [x for x in current if x != removed_id]
        if set(wanted) != set(current):
            await self.gateway.update_issue(issue.id, label_ids=list(dict.fromkeys(wanted)))

    # Relations -------------------------------------------------------------------------------------------------------

    def _wanted_blockers(self, node: str) -> list[str]:
        item = self.desired.items.get(node)
        if item is None:
            return []
        return [
            blocker for blocker in item.blocked_by if self._has_issue(blocker) and self.ids[blocker].removed_at is None
        ]

    def _related(self) -> list[str]:
        """The nodes that have an issue and a blocker issue that is not linked yet (no Linear call)."""

        found: list[str] = []
        for node in self.desired.items:
            row = self.ids.get(node)
            if row is None or row.content is None or not self._has_issue(node) or node in self.failed:
                continue
            if set(self._wanted_blockers(node)) - set(row.content.get("blocked_by") or []):
                found.append(node)
        return found

    async def _relate(self, node: str) -> None:
        """Add ``blocker blocks node`` for each blocker not linked yet; the id of a relation is a UUIDv5 of the pair,
        so one a crashed pass made is adopted."""

        row = self.ids[node]
        linked = set(row.content.get("blocked_by") or [])
        for blocker in sorted(set(self._wanted_blockers(node)) - linked):
            relation_id = node_uuid(self.pid, KIND_RELATION, f"{blocker}>{node}")
            try:
                await self.gateway.create_relation(
                    str(relation_id), str(self.ids[blocker].issue_id), str(row.issue_id), "blocks"
                )
                self.counts.relations_added += 1
            except LinearAlreadyExistsError:
                pass
            except LinearNotFoundError:
                await self._missing_issues(node, blocker)
                return
            linked.add(blocker)
            await self._store(node, content={**row.content, "blocked_by": sorted(linked)})

    async def _missing_issues(self, node: str, blocker: str) -> None:
        """A relation was refused as not found: find which of the two issues a person deleted."""

        gone = 0
        for name in (node, blocker):
            try:
                await self.gateway.issue(str(self.ids[name].issue_id))
            except LinearNotFoundError:
                await self._gone(name)
                gone += 1
        if not gone:
            await self._fail(node, run_code(LinearCode.NOT_FOUND))
