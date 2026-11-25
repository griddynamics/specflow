"""One GitHub Projects pass of a project (docs/github/projects.md "Pass"): the plan's stories become issues of the
target repository with an item on the organization project, its tasks become sub-issues, its areas labels and its
story order the project's item order. The rules are the Jira sync's (docs/jira-sync/sync.md): an open issue whose Status
is not started takes the plan's fields, any other issue gets one comment listing the changes, an item that leaves the
plan is labelled and commented, never deleted or closed.

The pass writes map rows only (the runner records the start and the end). GitHub calls never run inside a transaction;
every row write checks that the destination is unchanged. A crash between an issue's create and its row can create that
issue twice after the restart (the same window as the Jira sync's create).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from api.contained import Contained
from api.exports import rank
from api.exports.digest import digest
from api.exports.status import FAILED, PARTIAL, SKIPPED, SUCCEEDED
from api.github_projects.gateway import ProjectsGateway
from api.github_projects.store import ProjectsStore, TargetChangedError
from api.jira.desired import EPIC, Desired, Item, Row, SpecRef, build, content, is_deleted, spec_ids
from db.github_projects import ProjectTargetView
from db.models import GitHubProjectItem
from github.codes import GitHubCode, GitHubError
from github.projects import IssueContent
from graph.plan.reader import LoadedPlan
from jira.reasons import Reason
from logger.logger import WARNING, get_logger, log_event

logger = get_logger(__name__)

NOT_CONFIGURED: Final = "github_projects_not_configured"
NO_PLAN, PLAN_APPLYING = "no_plan", "plan_applying"
TARGET_CHANGED, UNEXPECTED_ERROR = "target_changed", "unexpected_error"
REMOVED_LABEL: Final = "graphico-removed"
_STORY_PARENT: Final = "-"  # the Epic placeholder of the shared builder: a story has no parent issue
_RUN_CODES: Final = frozenset(
    {
        GitHubCode.NOT_CONNECTED,
        GitHubCode.RECONNECT_REQUIRED,
        GitHubCode.UNREACHABLE,
        GitHubCode.AUTH_FAILED,
        GitHubCode.REPO_NOT_ACCESSIBLE,
        GitHubCode.PERMISSIONS_MISSING,
        GitHubCode.PROJECT_NOT_FOUND,
    }
)
_FIELDS: Final = (
    ("summary", "Title"),
    ("text", "Description"),
    ("done_when", "Done when"),
    ("release", "Release"),
    ("phase", "Phase"),
    ("specs", "Implements"),
    ("labels", "Labels"),
)


@dataclass
class PushCounts:
    created: int = 0
    updated: int = 0
    commented: int = 0
    removed: int = 0
    ranked: int = 0
    unchanged: int = 0
    failed: int = 0


@dataclass(frozen=True)
class PushResult:
    status: str
    error_code: str | None
    counts: PushCounts


@dataclass(frozen=True)
class PushDeps:
    """What a pass needs from its host; ``gateway_for`` is the gateway of the member the project's GitHub is
    connected by NOW, read for every pass (a refusal is a ``GitHubError`` and ends the pass ``failed`` with no GitHub
    call)."""

    store: ProjectsStore
    gateway_for: Callable[[uuid.UUID], Awaitable[ProjectsGateway]]
    load_plan: Callable[[str], Awaitable[LoadedPlan]]
    spec_names: Callable[[str, Sequence[str]], Awaitable[Mapping[str, SpecRef]]]
    now: Callable[[], datetime]


def _row(item: GitHubProjectItem) -> Row:
    gone = item.error_code == GitHubCode.ISSUE_GONE.value  # the shared builder knows one "deleted" code
    return Row(
        item.node_id,
        item.kind,
        None if item.issue_number is None else str(item.issue_number),
        item.requested_at,
        item.content,
        item.content_hash,
        item.position,
        item.removed_at,
        Reason.NOT_FOUND.value if gone else item.error_code,
        item.synced_at,
    )


def issue_content(values: Mapping[str, Any], node_id: str) -> IssueContent:
    """The title, Markdown body and labels an issue holds for a canonical ``content``."""

    lines: list[str] = []
    if values.get("text"):
        lines += [str(values["text"]), ""]
    for key, label in (("release", "Release"), ("phase", "Phase")):
        if values.get(key):
            lines.append(f"**{label}:** {values[key]}")
    done = [str(entry) for entry in values.get("done_when") or []]
    if done:
        lines += ["", "**Done when**", *(f"- [ ] {entry}" for entry in done)]
    specs = [f"{spec['name']} ({spec['id']})" for spec in values.get("specs") or []]
    if specs:
        lines += ["", "**Implements:** " + ", ".join(specs)]
    lines += ["", f"_Plan item `{node_id}`, kept up to date by Graphico._"]
    return IssueContent(str(values["summary"]), "\n".join(lines).strip(), list(values.get("labels") or []))


def changes(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> list[str]:
    """The comment lines for ``new`` against the stored ``old``: the names of the fields that differ."""

    before = old or {}
    found = [label for key, label in _FIELDS if before.get(key) != new.get(key)]
    return [f"{label} changed." for label in found]


def _run_code(code: GitHubCode) -> str:
    return (code if code in _RUN_CODES else GitHubCode.REFUSED).value


async def push_once(project_id: str, deps: PushDeps) -> PushResult:
    """Run one pass; the result holds a status and a code, never text."""

    run = _Pass(project_id, deps)
    status: str = FAILED
    code: str | None = UNEXPECTED_ERROR
    with Contained() as step:
        try:
            status, code = await run.execute()
        except TargetChangedError:
            status, code = SKIPPED, TARGET_CHANGED
        except GitHubError as error:
            status, code = FAILED, _run_code(error.code)
    if step.failed:
        log_event(logger, WARNING, "api.github_projects.sync.failed", project_id=project_id, error_type=step.error_type)
    return PushResult(status, code, run.counts)


class _Pass:
    def __init__(self, project_id: str, deps: PushDeps) -> None:
        self.project_id = project_id
        self.pid = uuid.UUID(project_id)
        self.deps = deps
        self.counts = PushCounts()
        self.rows: dict[str, Row] = {}
        self.ids: dict[str, GitHubProjectItem] = {}
        self.target: ProjectTargetView
        self.gateway: ProjectsGateway
        self.plan: LoadedPlan
        self.names: Mapping[str, SpecRef] = {}
        self.desired: Desired
        self.fresh: set[str] = set()
        self.failed: set[str] = set()

    # The pass --------------------------------------------------------------------------------------------------------

    async def execute(self) -> tuple[str, str | None]:
        target = await self.deps.store.get_target(self.pid)
        if target is None or not target.enabled:
            return SKIPPED, NOT_CONFIGURED
        self.target = target
        await self._reload()
        self.plan = await self.deps.load_plan(self.project_id)
        if self.plan.current.plan is None:
            return SKIPPED, NO_PLAN
        if self.plan.applying:
            return SKIPPED, PLAN_APPLYING
        ids = spec_ids(self.plan)
        self.names = await self.deps.spec_names(self.project_id, ids) if ids else {}
        self.desired = self._build()
        revived = len(self.desired.revived)
        creates, changed = self._diff()
        self.counts.unchanged = len(self.desired.items) - len(creates) - len(changed) - revived
        orders = self._wanted_order()
        if not (creates or changed or self.desired.removed or revived or self._missing_items() or self._moves(orders)):
            return SUCCEEDED, None
        self.gateway = await self.deps.gateway_for(self.pid)
        for node in creates:
            await self._guard(node, self._create(node))
        self.desired = self._build()
        creates, changed = self._diff()
        await self._update_all(changed)
        await self._leave_and_return()
        for node in self._missing_items():
            await self._guard(node, self._attach(node))
        await self._rank()
        return (PARTIAL if self.counts.failed else SUCCEEDED), None

    async def _reload(self) -> None:
        self.ids = {item.node_id: item for item in await self.deps.store.list_items(self.pid)}
        self.rows = {node: _row(item) for node, item in self.ids.items()}

    def _build(self) -> Desired:
        return build(self.plan, project_name="", epic_key=_STORY_PARENT, rows=self.rows, names=self.names)

    def _keys(self) -> dict[str, str]:
        return {node: row.jira_key for node, row in self.rows.items() if row.jira_key and not is_deleted(row)}

    def _content(self, item: Item) -> dict[str, Any]:
        return content(item, self._keys(), _STORY_PARENT, linked=False)

    def _diff(self) -> tuple[list[str], list[str]]:
        keys = self._keys()
        creates = [node for node in self.desired.items if node not in keys]
        changed = [
            node
            for node, item in self.desired.items.items()
            if node in keys
            and node not in self.fresh
            and node not in self.desired.revived
            and digest(self._content(item)) != self.rows[node].content_hash
        ]
        return creates, changed

    # Rows ------------------------------------------------------------------------------------------------------------

    async def _store(self, node: str, **columns: Any) -> None:
        await self.deps.store.update_item(self.target, node, **columns)
        for name, value in columns.items():
            setattr(self.ids[node], name, value)
        self.rows[node] = _row(self.ids[node])

    async def _fail(self, node: str, code: GitHubCode) -> None:
        if node in self.failed:
            return
        self.failed.add(node)
        self.counts.failed += 1
        log_event(
            logger,
            WARNING,
            "api.github_projects.sync.item_failed",
            project_id=self.project_id,
            node_id=node,
            error_code=code.value,
        )
        await self._store(node, error_code=code.value)

    async def _gone(self, node: str) -> None:
        """A person deleted or moved the issue: it is never made again."""

        row = self.rows.get(node)
        if row is None or is_deleted(row):
            return
        now = self.deps.now()
        if row.removed_at is None:
            self.counts.removed += 1
        await self._store(node, removed_at=row.removed_at or now, error_code=GitHubCode.ISSUE_GONE.value, synced_at=now)

    async def _guard(self, node: str, call: Awaitable[None]) -> None:
        """Item errors end the item, not the pass: GitHub's 422 fails it, a 404/410 on its issue means a person removed
        it."""

        try:
            await call
        except GitHubError as error:
            if error.code is GitHubCode.ITEM_INVALID:
                await self._fail(node, error.code)
            elif error.code is GitHubCode.ISSUE_GONE:
                await self._gone(node)
            else:
                raise

    # Steps -----------------------------------------------------------------------------------------------------------

    async def _create(self, node: str) -> None:
        item = self.desired.items[node]
        parent = self.rows.get(item.parent or "") if item.kind == "subtask" else None
        if item.kind == "subtask" and (parent is None or parent.jira_key is None or item.parent in self.failed):
            return  # its story has no issue (refused or gone): the task waits for the next pass
        now = self.deps.now()
        await self.deps.store.add_pending(self.target, node_id=node, kind=item.kind, requested_at=now)
        if node not in self.ids:
            await self._reload()
        values = self._content(item)
        ref = await self.gateway.create_issue(self.target.repository_name, issue_content(values, node))
        await self._store(
            node,
            issue_number=ref.number,
            issue_id=ref.issue_id,
            issue_node_id=ref.node_id,
            content=values,
            content_hash=digest(values),
            error_code=None,
            synced_at=now,
        )
        self.fresh.add(node)
        self.counts.created += 1
        await self._attach(node)

    async def _attach(self, node: str) -> None:
        """Link a made issue: a story gets its item on the project, a task becomes a sub-issue of its story's issue. A
        task's ``item_id`` holds the GraphQL id of the issue it is linked under, so a link a pass did not finish is
        made again."""

        item, row = self.desired.items.get(node), self.ids[node]
        if row.kind == "story":
            item_id = await self.gateway.add_item(self.target.project_node_id, str(row.issue_node_id))
            await self._store(node, item_id=item_id)
        elif item is not None and item.parent in self.ids and row.issue_id is not None:
            parent = self.ids[item.parent]
            if parent.issue_number is not None:
                await self.gateway.add_sub_issue(self.target.repository_name, parent.issue_number, row.issue_id)
                await self._store(node, item_id=parent.issue_node_id)

    def _missing_items(self) -> list[str]:
        """Issues an earlier pass stopped before linking: a story without its item on the project, a task whose
        story has an issue but which is not yet its sub-issue."""

        return [node for node, row in self.ids.items() if self._unlinked(node, row)]

    def _unlinked(self, node: str, row: GitHubProjectItem) -> bool:
        if row.issue_number is None or row.item_id is not None or row.removed_at is not None:
            return False
        if row.kind == "story":
            return True
        item = self.desired.items.get(node)
        parent = self.ids.get(item.parent or "") if item is not None else None
        return parent is not None and parent.issue_number is not None

    async def _states(self, nodes: Sequence[str]) -> dict[str, Any]:
        """The state of each node's issue; a story's Status is read from its own item on the project (a task has none:
        its ``item_id`` marks the link, not an item)."""

        rows = {node: self.ids[node] for node in nodes if self.ids[node].issue_node_id}
        refs = [(str(row.issue_node_id), row.item_id if row.kind == "story" else None) for row in rows.values()]
        if not refs:
            return {}
        found = await self.gateway.issue_states(refs)
        return {node: found.get(str(row.issue_node_id)) for node, row in rows.items()}

    async def _update_all(self, changed: Sequence[str]) -> None:
        batch: dict[str, Any] | None
        try:
            batch = await self._states(changed)
        except GitHubError as error:
            if error.code not in (GitHubCode.ISSUE_GONE, GitHubCode.ITEM_INVALID):
                raise
            batch = None  # one issue spoils the batch: ask per item, each one guarded
        for node in changed:
            await self._guard(node, self._update(node, batch))

    async def _update(self, node: str, batch: dict[str, Any] | None) -> None:
        state = (await self._states([node])).get(node) if batch is None else batch.get(node)
        if state is None:
            await self._gone(node)  # GitHub no longer finds the issue
            return
        row, number, repo = self.ids[node], self.ids[node].issue_number, self.target.repository_name
        values = self._content(self.desired.items[node])
        written = issue_content(values, node)
        if state.not_started:
            await self.gateway.edit_issue(repo, int(number or 0), written)
            await self.gateway.add_labels(repo, int(number or 0), written.labels)  # added, never replaced
            self.counts.updated += 1
        else:
            lines = changes(row.content, values) or ["The plan changed."]
            await self.gateway.comment(
                repo, int(number or 0), "Graphico plan update:\n" + "\n".join(f"- {x}" for x in lines)
            )
            self.counts.commented += 1
        await self._store(node, content=values, content_hash=digest(values), error_code=None, synced_at=self.deps.now())

    async def _leave_and_return(self) -> None:
        repo = self.target.repository_name
        for node in self.desired.removed:
            await self._guard(node, self._leave(node, repo))
        for node in self.desired.revived:
            await self._guard(node, self._return(node, repo))

    async def _leave(self, node: str, repo: str) -> None:
        number = int(self.ids[node].issue_number or 0)
        await self.gateway.add_labels(repo, number, [REMOVED_LABEL])
        await self.gateway.comment(repo, number, "This item was removed from the Graphico plan.")
        self.counts.removed += 1
        await self._store(node, removed_at=self.deps.now(), error_code=None, synced_at=self.deps.now())

    async def _return(self, node: str, repo: str) -> None:
        number = int(self.ids[node].issue_number or 0)
        await self.gateway.remove_label(repo, number, REMOVED_LABEL)
        await self.gateway.comment(repo, number, "This item is back in the Graphico plan.")
        self.counts.updated += 1
        await self._store(node, removed_at=None, error_code=None, synced_at=self.deps.now())

    # Order -----------------------------------------------------------------------------------------------------------

    def _wanted_order(self) -> list[str]:
        """The stories of the plan in plan order."""

        return list(self.desired.orders.get(EPIC, ()))

    def _moves(self, wanted: Sequence[str]) -> list[rank.Move]:
        positioned = sorted(
            (
                node
                for node in wanted
                if node in self.ids
                and self.ids[node].item_id
                and node not in self.fresh
                and self.ids[node].position is not None
            ),
            key=lambda node: (self.ids[node].position or 0, node),
        )
        return rank.plan_moves(positioned, [node for node in wanted if node in positioned or node not in self.ids])

    async def _rank(self) -> None:
        wanted = [node for node in self._wanted_order() if node in self.ids and self.ids[node].item_id]
        positioned = sorted(
            (node for node in wanted if node not in self.fresh and self.ids[node].position is not None),
            key=lambda node: (self.ids[node].position or 0, node),
        )
        applied = [*positioned, *(node for node in wanted if node not in positioned)]
        project = self.target.project_node_id
        for move in rank.plan_moves(positioned, wanted):
            applied = rank.apply_moves(applied, [move])
            for key in move.keys:
                at = applied.index(key)
                after = self.ids[applied[at - 1]].item_id if at else None
                await self._guard(key, self.gateway.move_item(project, str(self.ids[key].item_id), after))
            self.counts.ranked += len(move.keys)
        for index, node in enumerate(wanted):
            if self.ids[node].position != index:
                await self._store(node, position=index)
