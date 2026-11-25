"""One Jira sync pass of a project: ``sync_once`` follows docs/jira-sync/sync.md#pass-order.

The pass writes map rows only (the runner records the start and the end). Jira calls never run inside a transaction;
every row write opens its own and first checks that the target is unchanged (FR-JIRA-RUN-08).
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final, Literal

from api.contained import Contained
from api.exports import rank
from api.exports.digest import digest
from api.exports.status import FAILED, PARTIAL, SKIPPED, SUCCEEDED
from api.jira import content as rules
from api.jira.desired import EPIC, Desired, Item, Row, SpecRef, build, content, is_deleted, spec_ids
from db import jira as jira_db
from db.models import JiraIssue
from graph.plan.reader import LoadedPlan
from jira import adf
from jira.gateway import IssueState, JiraError, JiraGateway, JiraProject
from jira.reasons import Reason
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger(__name__)

TODO_CATEGORY: Final = "new"
RECOVERY_MARGIN_MINUTES: Final = 5
SECONDS_PER_MINUTE: Final = 60
SEARCH_INDEX_LAG_SECONDS: Final = 60  # Jira's search lists a new issue only after this long (seen live: seconds)
REMOTE_TITLE: Final = "Specflow plan"
NOT_CONFIGURED, NO_PLAN, PLAN_APPLYING = "jira_not_configured", "no_plan", "plan_applying"
TARGET_CHANGED, UNEXPECTED_ERROR = "target_changed", "unexpected_error"
_RUN_REASONS: Final = frozenset(
    {
        Reason.AUTH_FAILED,
        Reason.FORBIDDEN,
        Reason.NOT_FOUND,
        Reason.UNREACHABLE,
        Reason.UNEXPECTED,
        Reason.NOT_CONNECTED,
        Reason.RECONNECT_REQUIRED,
    }
)
_DESCRIPTION_INPUTS: Final = ("v", "text", "release", "phase", "done_when", "specs", "task_id")
_KIND_TYPE: Final = {"epic": "epic_type_id", "story": "story_type_id", "subtask": "subtask_type_id"}


@dataclass
class SyncCounts:
    created: int = 0
    updated: int = 0
    commented: int = 0
    removed: int = 0
    links_added: int = 0
    links_removed: int = 0
    ranked: int = 0
    unchanged: int = 0
    failed: int = 0


@dataclass(frozen=True)
class SyncResult:
    status: str
    error_code: str | None
    counts: SyncCounts


@dataclass(frozen=True)
class SyncProgress:
    """Where a running pass is: ``done`` of ``total`` units of ``step`` ("issues", "links" or "order")."""

    step: Literal["issues", "links", "order"]
    done: int
    total: int


@dataclass(frozen=True)
class SyncDeps:
    """What a pass needs from its host; ``spec_names`` maps each asked spec id (an alias too) to its current node."""

    gateway: JiraGateway
    transaction: Callable[[], AbstractAsyncContextManager[Any]]
    load_plan: Callable[[str], Awaitable[LoadedPlan]]
    spec_names: Callable[[str, Sequence[str]], Awaitable[Mapping[str, SpecRef]]]
    public_url: str | None
    now: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    report: Callable[[str, SyncProgress], None] | None = None  # (project id, progress), called as the pass advances


class TargetChangedError(Exception):
    """The owner changed the target during the pass: stop, write nothing more."""


@dataclass
class _Diff:
    creates: list[str]
    changed: list[str]
    moves: dict[str, list[rank.Move]]
    orders: dict[str, tuple[list[str], list[str]]]  # per scope with moves: (previous, wanted)


def _row(issue: JiraIssue) -> Row:
    return Row(
        issue.node_id,
        issue.kind,
        issue.jira_key,
        issue.requested_at,
        issue.content,
        issue.content_hash,
        issue.position,
        issue.removed_at,
        issue.error_code,
        issue.synced_at,
    )


def _run_code(reason: Reason) -> str:
    return (reason if reason in _RUN_REASONS else Reason.UNEXPECTED).value


async def sync_once(project_id: str, deps: SyncDeps) -> SyncResult:
    """Run one pass; the result holds a status and a code, never text."""

    run = _Pass(project_id, deps)
    status: str = FAILED
    code: str | None = UNEXPECTED_ERROR
    with Contained() as step:
        try:
            status, code = await run.execute()
        except TargetChangedError:
            status, code = SKIPPED, TARGET_CHANGED
        except JiraError as error:
            status, code = FAILED, _run_code(error.reason)
    if step.failed:
        log_event(logger, WARNING, "api.jira.sync.failed", project_id=project_id, error_type=step.error_type)
    return SyncResult(status, code, run.counts)


class _Pass:
    def __init__(self, project_id: str, deps: SyncDeps) -> None:
        self.project_id = project_id
        self.pid = uuid.UUID(project_id)
        self.deps = deps
        self.gateway = deps.gateway
        self.counts = SyncCounts()
        self.rows: dict[str, Row] = {}
        self.jira_project = ""
        self.epic_key: str | None = None
        self.project_name = ""
        self.plan: LoadedPlan
        self.names: Mapping[str, SpecRef] = {}
        self.desired: Desired
        self.states: dict[str, IssueState | None] = {}
        self.fresh: set[str] = set()
        self.failed: set[str] = set()
        self.link_queue: list[str] = []
        self.died = False
        self.key_cache: dict[str, str] | None = None
        self.issue_types: JiraProject | None = None
        self.points_looked = False
        self.points: str | None = None
        self.epic_checked = False
        self.step: Literal["issues", "links", "order"] = "issues"
        self.done = 0
        self.total = 0

    def _begin(self, step: Literal["issues", "links", "order"], total: int, *, done: int = 0) -> None:
        self.step, self.done, self.total = step, done, total
        self._report()

    def _tick(self) -> None:
        self.done = min(self.done + 1, self.total)
        self._report()

    def _report(self) -> None:
        if self.deps.report is not None:
            self.deps.report(self.project_id, SyncProgress(self.step, self.done, self.total))

    async def execute(self) -> tuple[str, str | None]:
        async with self.deps.transaction() as session:
            target = await jira_db.get_target(session, self.pid)
            issues = await jira_db.list_issues(session, self.pid)
        if target is None or not target.enabled:
            return SKIPPED, NOT_CONFIGURED
        self.jira_project, self.epic_key, self.project_name = target.jira_project, target.epic_key, target.project_name
        self.rows = {issue.node_id: _row(issue) for issue in issues}
        self.plan = await self.deps.load_plan(self.project_id)
        if self.plan.current.plan is None:
            return SKIPPED, NO_PLAN
        if self.plan.applying:
            return SKIPPED, PLAN_APPLYING
        ids = spec_ids(self.plan)
        self.names = await self.deps.spec_names(self.project_id, ids) if ids else {}
        self.desired = self._build()
        if self.epic_key is None and EPIC in self.desired.deleted:
            raise JiraError(Reason.NOT_FOUND, http_status=404)  # the created Epic is gone: the owner sets an Epic key
        diff = self._diff(planning=True)
        revived = len(self.desired.revived)
        self.counts.unchanged = len(self.desired.items) - len(diff.creates) - len(diff.changed) - revived
        if not (diff.creates or diff.changed or self.desired.removed or revived or diff.moves):
            return SUCCEEDED, None
        await self._recover()
        again = self._diff(planning=True)
        events = len(again.changed) + len(self.desired.revived) + len(self.desired.removed)
        self._begin("issues", len(again.creates) + events)
        await self._create_all()
        self.desired = self._build()
        diff = self._diff(planning=False)
        await self._reconcile(diff)
        await self._link_all()
        await self._rank_all(diff)
        return (PARTIAL if self.counts.failed else SUCCEEDED), None

    def _build(self) -> Desired:
        return build(
            self.plan, project_name=self.project_name, epic_key=self.epic_key, rows=self.rows, names=self.names
        )

    def _keys(self) -> dict[str, str]:
        if self.key_cache is None:
            self.key_cache = {n: r.jira_key for n, r in self.rows.items() if r.jira_key and not is_deleted(r)}
        return self.key_cache

    def _content(self, item: Item, *, linked: bool = True) -> dict[str, Any]:
        return content(item, self._keys(), self.epic_key, linked=linked)

    def _dead(self, node: str) -> bool:
        return node in self.rows and is_deleted(self.rows[node])

    def _diff(self, *, planning: bool) -> _Diff:
        desired, keys = self.desired, self._keys()
        creates = [node for node in desired.items if node not in keys]
        changed = [
            node
            for node, item in desired.items.items()
            if node in keys
            and node not in self.fresh
            and node not in desired.revived
            and digest(self._content(item)) != self.rows[node].content_hash
        ]
        present = set(keys) | (set(creates) if planning else set())
        moves: dict[str, list[rank.Move]] = {}
        orders: dict[str, tuple[list[str], list[str]]] = {}
        for scope, order in desired.orders.items():
            wanted = [node for node in order if node in present]
            positioned = [
                node
                for node in wanted
                if node in keys and node not in self.fresh and self.rows[node].position is not None
            ]
            positioned.sort(key=lambda node: (self.rows[node].position or 0, node))
            if found := rank.plan_moves(positioned, wanted):
                moves[scope] = found
                orders[scope] = (positioned, wanted)
        return _Diff(creates, changed, moves, orders)

    async def _write(self, write: Callable[..., Awaitable[None]], *args: Any, **kwargs: Any) -> None:
        async with self.deps.transaction() as session:
            unchanged = await jira_db.target_unchanged(
                session, self.pid, jira_project=self.jira_project, epic_key=self.epic_key
            )
            if not unchanged:
                raise TargetChangedError
            await write(session, self.pid, *args, **kwargs)

    async def _store(self, node: str, **columns: Any) -> None:
        await self._write(jira_db.update_issue, node, **columns)
        self.rows[node] = replace(self.rows[node], **columns)
        self.key_cache = None

    async def _fail(self, node: str, reason: Reason) -> None:
        if node in self.failed:
            return
        self.failed.add(node)
        self.counts.failed += 1
        log_event(
            logger,
            WARNING,
            "api.jira.sync.item_failed",
            project_id=self.project_id,
            node_id=node,
            jira_key=self.rows[node].jira_key,
            error_code=reason.value,
        )
        await self._store(node, error_code=reason.value)

    async def _deleted(self, node: str) -> None:
        row = self.rows.get(node)
        if row is None or is_deleted(row):
            return
        now = self.deps.now()
        if row.removed_at is None:
            self.counts.removed += 1
        self.died = True
        await self._store(node, removed_at=row.removed_at or now, error_code=Reason.NOT_FOUND.value, synced_at=now)

    async def _guard(self, node: str, call: Awaitable[None]) -> None:
        """Item errors end the item, not the pass: 400 fails it, 404 means a person deleted the issue or its parent."""

        try:
            await call
        except JiraError as error:
            row = self.rows.get(node)
            culprit = node if row and row.jira_key else self.desired.items[node].parent or node
            if error.reason is Reason.INVALID_FIELD:
                await self._fail(node, error.reason)
            elif error.reason is Reason.NOT_FOUND and culprit != EPIC:
                await self._deleted(culprit)
            else:
                raise

    async def _check_epic(self) -> None:
        """Jira answers 400, not 404, for a Story whose Epic is gone: read the Epic once before a Story is created or
        moved to it. An Epic made in this pass needs no read."""

        key = self.epic_key or self._keys().get(EPIC)
        if self.epic_checked or key is None or EPIC in self.fresh:
            return
        self.epic_checked = True
        if (await self.gateway.issue_states([key]))[key] is None:
            raise JiraError(Reason.NOT_FOUND, http_status=404)

    async def _points_field(self) -> str | None:
        if not self.points_looked:
            self.points, self.points_looked = await self.gateway.story_points_field(), True
        return self.points

    def _description(self, item: Item, body: Mapping[str, Any]) -> dict[str, Any]:
        if item.kind == "story":
            return adf.description_doc("story", body, story_id=item.node_id)
        return adf.description_doc("subtask", body)

    async def _recover(self) -> None:
        refused = Reason.INVALID_FIELD.value  # a 400 proves Jira created nothing: no need to wait for or search it
        pending = {
            node: row
            for node, row in self.rows.items()
            if row.jira_key is None and node in self.desired.items and row.error_code != refused
        }
        if not pending:
            return
        youngest = max(row.requested_at for row in pending.values())
        lag = SEARCH_INDEX_LAG_SECONDS - (self.deps.now() - youngest).total_seconds()
        if lag > 0:  # a create that crashed may not be searchable yet: a duplicate is worse than a wait
            await self.deps.sleep(lag)
        oldest = min(row.requested_at for row in pending.values())
        age = max(0.0, (self.deps.now() - oldest).total_seconds())
        minutes = math.ceil(age / SECONDS_PER_MINUTE) + RECOVERY_MARGIN_MINUTES
        found = await self.gateway.find_created(self.jira_project, project_id=self.project_id, minutes=minutes)
        for node in sorted(pending):
            if node in found:
                await self._store(node, jira_key=found[node], error_code=None)
                log_event(
                    logger,
                    INFO,
                    "api.jira.sync.recovered",
                    project_id=self.project_id,
                    node_id=node,
                    jira_key=found[node],
                )

    async def _create_all(self) -> None:
        for node, item in self.desired.items.items():
            if node in self._keys():
                continue
            if item.kind != "epic" and self._content(item)["parent"] is None:
                self._tick()
                continue
            if self.issue_types is None:
                self.issue_types = await self.gateway.project(self.jira_project)
                await self._points_field()
            if item.kind == "story":
                await self._check_epic()
            await self._guard(node, self._create(item))
            self._tick()

    async def _create(self, item: Item) -> None:
        type_id = getattr(self.issue_types, _KIND_TYPE[item.kind])
        if type_id is None:
            raise JiraError(Reason.ISSUE_TYPES_MISSING)
        node, now = item.node_id, self.deps.now()
        await self._write(jira_db.add_pending, node_id=node, kind=item.kind, requested_at=now)
        row = self.rows.get(node)
        self.rows[node] = (
            replace(row, requested_at=now, error_code=None)
            if row
            else Row(node, item.kind, None, now, None, None, None, None, None, None)
        )
        body = self._content(item, linked=False)
        fields: dict[str, Any] = {
            "project": {"key": self.jira_project},
            "issuetype": {"id": type_id},
            "summary": body["summary"],
            "labels": body["labels"],
        }
        if item.kind != "epic":
            fields["parent"] = {"key": body["parent"]}
            fields["description"] = self._description(item, body)
        points = await self._points_field()
        if points and body["points"] is not None:
            fields[points] = body["points"]
        key = await self.gateway.create_issue(fields, project_id=self.project_id, node_id=node)
        await self._store(node, jira_key=key, error_code=None)
        self.fresh.add(node)
        self.counts.created += 1
        await self._remote_link(node, key)
        order = self.desired.orders.get(item.parent or "", ())
        position = order.index(node) if node in order else None
        await self._store(node, content=body, content_hash=digest(body), position=position, synced_at=now)
        if item.blocked_by:
            self.link_queue.append(node)

    async def _remote_link(self, node: str, key: str) -> None:
        if self.deps.public_url:
            await self.gateway.upsert_remote_link(
                key,
                global_id=f"specflow:{self.project_id}:{node}",
                url=f"{self.deps.public_url.rstrip('/')}/projects/{self.project_id}/plan",
                title=REMOTE_TITLE,
            )

    async def _reconcile(self, diff: _Diff) -> None:
        desired = self.desired
        moved = {node for moves in diff.moves.values() for move in moves for node in move.keys}
        events = (
            [(node, rules.CHANGED) for node in diff.changed]
            + [(node, rules.REVIVED) for node in desired.revived]
            + [(node, rules.REMOVED) for node in desired.removed]
        )
        events = [(node, event) for node, event in events if node not in self.failed]
        asked = dict.fromkeys([node for node, _ in events] + sorted(moved))
        nodes = [
            node for node in asked if node not in self.fresh and node not in self.failed and self.rows[node].jira_key
        ]
        if self.done + len(events) != self.total:  # the creates changed what is to update: the total is final now
            self._begin("issues", self.done + len(events), done=self.done)
        keys = [str(self.rows[node].jira_key) for node in nodes]
        found = await self.gateway.issue_states(keys) if keys else {}
        self.states = {node: found[key] for node, key in zip(nodes, keys, strict=True)}
        for node, state in self.states.items():
            if state is None:
                if node == EPIC and node in desired.items:
                    raise JiraError(Reason.NOT_FOUND, http_status=404)
                await self._deleted(node)
        for node, event in events:
            state = self.states[node]
            if state is None or self._dead(node):
                self._tick()
                continue
            if event != rules.REMOVED and state.category == TODO_CATEGORY:
                await self._points_field()
                if self._moves_story(node):
                    await self._check_epic()
            await self._guard(node, self._apply(node, event, state))
            self._tick()

    def _moves_story(self, node: str) -> bool:
        item = self.desired.items.get(node)
        return (
            item is not None
            and item.kind == "story"
            and self._content(item)["parent"] != (self.rows[node].content or {}).get("parent")
        )

    async def _edit_args(
        self, item: Item, old: Mapping[str, Any] | None, new: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        before = old or {}
        fields: dict[str, Any] = {}
        if new["summary"] != before.get("summary"):
            fields["summary"] = new["summary"]
        if item.kind != "epic":
            if old is None or any(new[name] != before.get(name) for name in _DESCRIPTION_INPUTS):
                fields["description"] = self._description(item, new)
            if new["parent"] != before.get("parent"):
                fields["parent"] = {"key": new["parent"]}
        points = await self._points_field()
        if points and new["points"] != before.get("points"):
            fields[points] = new["points"]
        was = before.get("labels") or []
        return fields, [name for name in new["labels"] if name not in was], [n for n in was if n not in new["labels"]]

    async def _overwrite(self, node: str, event: str, item: Item | None, new: dict[str, Any]) -> dict[str, Any]:
        """Edit a To Do issue (the owned fields, or just the removal label); return the content to store."""

        row = self.rows[node]
        fields: dict[str, Any] = {}
        add: list[str] = [rules.REMOVED_LABEL] if item is None else []
        remove: list[str] = []
        if item is not None:
            fields, add, remove = await self._edit_args(item, row.content, new)
        if event == rules.REVIVED:
            remove = [*remove, rules.REMOVED_LABEL]
        if fields or add or remove:
            await self.gateway.edit_issue(str(row.jira_key), fields=fields, add_labels=add, remove_labels=remove)
        if item is None:
            return new
        self.counts.updated += 1
        old_blocked = list((row.content or {}).get("blocked_by") or [])
        if set(new["blocked_by"]) == set(old_blocked):
            return new
        self.link_queue.append(node)
        return {**new, "blocked_by": old_blocked}

    async def _apply(self, node: str, event: str, state: IssueState) -> None:
        row, item = self.rows[node], self.desired.items.get(node)
        key, now = str(row.jira_key), self.deps.now()
        todo = state.category == TODO_CATEGORY
        new = self._content(item) if item else {}
        stored = await self._overwrite(node, event, item, new) if todo else new
        items = rules.changes(row.content, new, self._keys()) if item else []
        if item is None or event == rules.REVIVED or (items and not todo):
            lead = rules.lead(node, event, state.status)
            await self.gateway.add_comment(key, adf.comment_doc(lead, items))
            self.counts.commented += 1
        if row.content is None and item is not None:
            await self._remote_link(node, key)
        columns: dict[str, Any] = {"synced_at": now, "error_code": None}
        if item is not None:
            columns |= {"content": stored, "content_hash": digest(stored)}
            columns["removed_at"] = None if event == rules.REVIVED else row.removed_at
        else:
            columns["removed_at"] = now
            self.counts.removed += 1
        await self._store(node, **columns)

    async def _link_all(self) -> None:
        queue = list(dict.fromkeys(self.link_queue))
        self._begin("links", len(queue))
        for node in queue:
            if node not in self.failed and not self._dead(node):
                await self._guard(node, self._link(node))
            self._tick()

    async def _link(self, node: str) -> None:
        row, item = self.rows[node], self.desired.items[node]
        key = str(row.jira_key)
        state = self.states.get(node) or IssueState(TODO_CATEGORY, "")
        old = list((row.content or {}).get("blocked_by") or [])
        wanted = self._content(item)["blocked_by"]
        for prerequisite in wanted:
            prerequisite_key = self._keys()[prerequisite]
            if prerequisite in old or prerequisite_key in state.blocked_by:
                continue
            if not await self._add_link(node, prerequisite, prerequisite_key, key):
                return
        for prerequisite in old:
            link_id = state.blocked_by.get(self._keys().get(prerequisite, ""))
            if prerequisite not in wanted and link_id:
                await self._delete_link(link_id)
        body = self._content(item)
        await self._store(node, content=body, content_hash=digest(body), synced_at=self.deps.now())

    async def _delete_link(self, link_id: str) -> None:
        try:
            await self.gateway.delete_link(link_id)
        except JiraError as error:
            if error.reason is not Reason.NOT_FOUND:
                raise
            return
        self.counts.links_removed += 1

    async def _add_link(self, node: str, prerequisite: str, prerequisite_key: str, key: str) -> bool:
        try:
            await self.gateway.add_blocks_link(prerequisite=prerequisite_key, waiter=key)
        except JiraError as error:
            if error.reason is not Reason.NOT_FOUND:
                raise
            found = await self.gateway.issue_states([prerequisite_key, key])
            gone = [
                name for name, issue_key in ((prerequisite, prerequisite_key), (node, key)) if found[issue_key] is None
            ]
            for name in gone:
                await self._deleted(name)
            if not gone:
                await self._fail(node, Reason.UNEXPECTED)
            return bool(gone) and node not in gone
        self.counts.links_added += 1
        return True

    async def _rank_all(self, diff: _Diff) -> None:
        if self.died:
            return
        self._begin("order", len(diff.orders))
        not_todo = {node for node, state in self.states.items() if state is None or state.category != TODO_CATEGORY}
        keys = self._keys()
        failed_scopes: set[str] = set()
        real: dict[str, list[str]] = {}
        for scope, (previous, wanted) in diff.orders.items():
            applied = [*previous, *(node for node in wanted if node not in previous)]
            todo = [node for node in wanted if node not in not_todo]
            for move in rank.plan_moves([node for node in previous if node not in not_todo], todo):
                try:
                    await self.gateway.rank(
                        [keys[node] for node in move.keys],
                        before=keys[move.before] if move.before else None,
                        after=keys[move.after] if move.after else None,
                    )
                except JiraError as error:
                    if error.reason not in (Reason.INVALID_FIELD, Reason.NOT_FOUND):
                        raise
                    reason = error.reason if error.reason is Reason.INVALID_FIELD else Reason.UNEXPECTED
                    failed_scopes.add(scope)
                    await self._fail(move.keys[0], reason)
                    break
                applied = rank.apply_moves(applied, [move])
                self.counts.ranked += len(move.keys)
                for node in move.keys:
                    if self.rows[node].error_code:
                        await self._store(node, error_code=None)
            real[scope] = applied
            self._tick()
        positions = {
            node: index
            for scope, order in (self.desired.orders | real).items()
            if scope not in failed_scopes
            for index, node in enumerate(order)
            if node in keys and self.rows[node].position != index
        }
        if positions:
            await self._write(jira_db.set_positions, positions)
            for node, index in positions.items():
                self.rows[node] = replace(self.rows[node], position=index)
