"""One Notion plan pass of a project (docs/notion/plan.md "Pass").

The plan's stories and tasks become rows of one database "Graphico plan - <project>" created under the target's parent
page on the first pass (N11); the desired set is ``api.jira.desired.build`` with a placeholder epic (N7). A row's
owned fields are overwritten only while its Status is in the ``to_do`` group, otherwise one comment lists the changes;
a row that leaves the plan gets ``Removed`` checked and a comment, never a delete (N8). A pass spends at most
``REQUEST_BUDGET`` requests on rows (a create costs 1, an update, a removal and a comment about 3); when rows are left,
or Notion's rate limit stops a pass that already wrote something, it calls ``start(project_id, "plan_run")`` on its
runner before it ends so one more pass follows (N9, no change to ``api.exports.runner``). A pass that wrote nothing
never restarts: rows Notion refuses or a person deleted do not count as written. Notion calls never run inside a
transaction; every row write checks that the destination is unchanged. A crash between a create and its row is repaired
by a query on ``Graphico ID``, and only when the map holds a pending row of the node (a create that never got its
page id).

Probed live (2026-11-25, API 2026-03-11): the API creates a ``status`` property (``{"status": {}}``) with the options
Not started, In progress, Done in the groups To-do, In progress, Complete and the default option Not started, so no
``select`` fallback is needed. A self-relation added with ``dual_property: {}`` comes back with the synced side named
"Related to <database> (<name>)" and its ``synced_property_id``; the pass renames that side to Children and Blocks.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final

from api.contained import Contained
from api.exports.digest import digest
from api.exports.status import FAILED, PARTIAL, SKIPPED, SUCCEEDED
from api.jira import content as content_rules
from api.jira.desired import Desired, Item, Row, SpecRef, build, content, is_deleted, spec_ids
from api.notion_plan.store import PlanStore, TargetChangedError
from db.models import NotionPlanItem
from db.notion_plan import PlanTargetView
from graph.plan.reader import LoadedPlan
from jira.reasons import Reason
from logger.logger import WARNING, get_logger, log_event
from notion.gateway import NotionGateway
from notion.model import NotionDataSource
from notion.reasons import NotionCode, NotionError

logger = get_logger(__name__)

NOT_CONFIGURED: Final = "notion_plan_not_configured"
NO_PLAN, PLAN_APPLYING = "no_plan", "plan_applying"
TARGET_CHANGED, UNEXPECTED_ERROR = "target_changed", "unexpected_error"
# Requests the row steps of one pass may spend (the limiter's 150 a minute plus the 60 s a pass may wait fit about
# 300); the rest follow in a new pass.
REQUEST_BUDGET: Final = 240
CREATE_COST, UPDATE_COST, FLAG_COST, REFUSED_COST = 1, 3, 3, 1  # requests of a create, an update, a removal, a refusal
_NO_PARENT: Final = "-"  # the epic placeholder of the shared builder: a story has no parent row
_REMOVED_LINE: Final = "This item was removed from the Graphico plan."
_RETURNED_LINE: Final = "This item is back in the Graphico plan."
_RUN_CODES: Final = frozenset(
    {
        NotionCode.NOT_CONNECTED,
        NotionCode.RECONNECT_REQUIRED,
        NotionCode.UNREACHABLE,
        NotionCode.AUTH_FAILED,
        NotionCode.NOT_SHARED,
        NotionCode.NOT_A_PAGE,
        NotionCode.PERMISSIONS_MISSING,
        NotionCode.RATE_LIMITED,
        NotionCode.DATABASE_GONE,
        NotionCode.ITEM_INVALID,
    }
)
# The schema of the plan database (N7). ``Parent``/``Blocked by`` are added after the create; their synced sides are
# renamed to the second name.
DATABASE_PROPERTIES: Final[dict[str, Any]] = {
    "Name": {"title": {}},
    "Type": {"select": {"options": [{"name": "Story"}, {"name": "Task"}]}},
    "Status": {"status": {}},
    "Points": {"number": {"format": "number"}},
    "Work area": {"multi_select": {}},
    "Graphico ID": {"rich_text": {}},
    "Removed": {"checkbox": {}},
}
RELATIONS: Final = (("Parent", "Children"), ("Blocked by", "Blocks"))
_FIELDS: Final = (
    ("summary", "Name"),
    ("text", "Description"),
    ("done_when", "Done when"),
    ("release", "Release"),
    ("phase", "Phase"),
    ("specs", "Implements"),
    ("points", "Points"),
    ("labels", "Work area"),
    ("parent", "Parent"),
    ("blocked_by", "Blocked by"),
)


@dataclass
class PushCounts:
    created: int = 0
    updated: int = 0
    commented: int = 0
    removed: int = 0
    unchanged: int = 0
    failed: int = 0
    deferred: int = 0  # rows left for the next pass


@dataclass(frozen=True)
class PushResult:
    status: str
    error_code: str | None
    counts: PushCounts


@dataclass(frozen=True)
class PushDeps:
    """What a pass needs from its host; ``gateway_for`` is the gateway of the member the project's Notion is
    connected by NOW, read for every pass (a refusal is a ``NotionError`` and ends the pass ``failed`` with no Notion
    call); ``restart`` asks the runner for one more pass (``NotionPlanRunner.start`` with ``plan_run``)."""

    store: PlanStore
    gateway_for: Callable[[uuid.UUID], Awaitable[NotionGateway]]
    load_plan: Callable[[str], Awaitable[LoadedPlan]]
    spec_names: Callable[[str, Sequence[str]], Awaitable[Mapping[str, SpecRef]]]
    restart: Callable[[str], Awaitable[None]]
    now: Callable[[], datetime]


def _row(item: NotionPlanItem) -> Row:
    """A map row as the shared builder reads it: the page id plays the Jira key, a row a person deleted reads as the
    builder's one "deleted" code, a task is a ``subtask``."""

    gone = item.error_code == NotionCode.PAGE_GONE.value
    return Row(
        item.node_id,
        "story" if item.kind == "story" else "subtask",
        item.page_id,
        item.requested_at,
        item.content,
        item.content_hash,
        None,
        item.removed_at,
        Reason.NOT_FOUND.value if gone else item.error_code,
        item.synced_at,
    )


def row_properties(values: Mapping[str, Any], node_id: str, blocked_pages: Sequence[str]) -> dict[str, Any]:
    """The Notion properties an item owns; ``Parent`` and ``Blocked by`` are the full relation lists (so a removed
    link is cleared). ``Status`` is never sent: a new row takes Notion's default (the first To-do option)."""

    parent = values.get("parent")
    areas = [label.removeprefix(content_rules.AREA_PREFIX)[:100] for label in values.get("labels") or []]
    return {
        "Name": {"title": [{"text": {"content": str(values["summary"])}}]},
        "Type": {"select": {"name": "Story" if values.get("task_id") is None else "Task"}},
        "Points": {"number": values.get("points")},
        "Work area": {"multi_select": [{"name": name} for name in areas if name != content_rules.OWNED_LABEL]},
        "Graphico ID": {"rich_text": [{"text": {"content": node_id}}]},
        "Parent": {"relation": [] if parent in (None, _NO_PARENT) else [{"id": parent}]},
        "Blocked by": {"relation": [{"id": page} for page in blocked_pages]},
        "Removed": {"checkbox": False},
    }


_MARKUP: Final = re.compile(r"(\\*)([<>$])")  # a tag, a mention or display math starts with one of these
_MENTION_URL: Final = re.compile(r"user://", re.IGNORECASE)


def _plain(text: object) -> str:
    """Untrusted text for Notion's enhanced Markdown: ``<``, ``>`` and ``$`` are escaped (no tag, mention or math can
    start; backslashes in front of them are doubled so an escape of the author cannot undo ours) and a ``user://``
    address gets a zero-width space, so it never resolves to a person. Other Markdown stays as written."""

    escaped = _MARKUP.sub(lambda found: found[1] * 2 + "\\" + found[2], str(text))
    return _MENTION_URL.sub(lambda found: found[0][:5] + "\u200b" + found[0][5:], escaped)


def row_body(values: Mapping[str, Any], node_id: str) -> str:
    """The brief as the row's Markdown body; every text of the plan is escaped by ``_plain``."""

    parts: list[str] = [_plain(values["text"])] if values.get("text") else []
    facts = [
        f"**{label}:** {_plain(values[key])}"
        for key, label in (("release", "Release"), ("phase", "Phase"))
        if values[key]
    ]
    if facts:
        parts.append("\n".join(facts))
    done = [f"- [ ] {_plain(entry)}" for entry in values.get("done_when") or []]
    if done:
        parts.append("\n".join(["**Done when**", *done]))
    if values.get("specs"):
        parts.append(
            "**Implements:** " + ", ".join(f"{_plain(s['name'])} ({_plain(s['id'])})" for s in values["specs"])
        )
    parts.append(f"_Plan item `{node_id}`, kept up to date by Graphico._")
    return "\n\n".join(parts)


def changed_fields(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> list[str]:
    """The names of the fields of ``new`` that differ from the stored ``old`` (none stored: every field set)."""

    before = old or {}
    return [label for key, label in _FIELDS if before.get(key) != new.get(key)]


def to_do_options(properties: Mapping[str, Any]) -> frozenset[str]:
    """The ids of the ``Status`` options in the ``to_do`` group (Notion's group named "To-do")."""

    groups = ((properties.get("Status") or {}).get("status") or {}).get("groups") or []
    found: set[str] = set()
    for group in groups:
        if str(group.get("name", "")).casefold().replace(" ", "-") == "to-do":
            found.update(str(option) for option in group.get("option_ids") or [])
    return frozenset(found)


def _run_code(code: NotionCode) -> str:
    return (code if code in _RUN_CODES else NotionCode.AUTH_FAILED).value


async def _settled(run: _Pass) -> tuple[str, str | None]:
    """The status and code of a run: a refusal of Notion or a changed destination is an answer, not an error."""

    try:
        return await run.execute()
    except TargetChangedError:
        return SKIPPED, TARGET_CHANGED
    except NotionError as error:
        return FAILED, _run_code(error.code)


async def push_once(project_id: str, deps: PushDeps) -> PushResult:
    """One pass of the project; the result holds a status and a code, never text."""

    run = _Pass(project_id, deps)
    status: str = FAILED
    code: str | None = UNEXPECTED_ERROR
    with Contained() as step:
        status, code = await _settled(run)
    if step.failed:
        log_event(logger, WARNING, "api.notion_plan.sync.failed", project_id=project_id, error_type=step.error_type)
    return PushResult(status, code, run.counts)


class _Pass:
    def __init__(self, project_id: str, deps: PushDeps) -> None:
        self.project_id = project_id
        self.pid = uuid.UUID(project_id)
        self.deps = deps
        self.counts = PushCounts()
        self.rows: dict[str, Row] = {}
        self.ids: dict[str, NotionPlanItem] = {}
        self.target: PlanTargetView
        self.gateway: NotionGateway
        self.plan: LoadedPlan
        self.names: Mapping[str, SpecRef] = {}
        self.desired: Desired
        self.fresh: set[str] = set()  # made in this pass: their own, still to-do
        self.failed: set[str] = set()
        self.deferred: set[str] = set()
        self.spent = 0  # requests charged to the row budget
        self.limited = False  # Notion's rate limit stopped the row steps after some rows were written
        self._schema: NotionDataSource | None = None

    # The pass --------------------------------------------------------------------------------------------------------

    async def _skip_reason(self) -> str | None:
        """Read the target, the map and the plan; the code that says why there is nothing to do, else ``None``."""

        found = await self.deps.store.get_target(self.pid)
        if found is None or not found.enabled:
            return NOT_CONFIGURED
        self.target = found
        await self._reload()
        self.plan = await self.deps.load_plan(self.project_id)
        if self.plan.current.plan is None:
            return NO_PLAN
        if self.plan.applying:
            return PLAN_APPLYING
        specs = spec_ids(self.plan)
        self.names = await self.deps.spec_names(self.project_id, specs) if specs else {}
        return None

    async def execute(self) -> tuple[str, str | None]:
        if (reason := await self._skip_reason()) is not None:
            return SKIPPED, reason
        target = self.target
        self.desired = self._build()
        creates, changed = self._diff()
        removed, revived = self.desired.removed, self.desired.revived
        self.counts.unchanged = max(len(self.desired.items) - len(creates) - len(changed) - len(revived), 0)
        if target.database_id is not None and not (creates or changed or removed or revived):
            return SUCCEEDED, None
        self.gateway = await self.deps.gateway_for(self.pid)
        await self._database(repair=not any(item.page_id for item in self.ids.values()))
        await self._each(creates, self._create, CREATE_COST)
        self.desired = self._build()
        await self._each(self.desired.removed, self._leave, FLAG_COST)
        await self._each(self.desired.revived, self._return, FLAG_COST)
        self.desired = self._build()
        await self._each(self._diff()[1], self._update, UPDATE_COST)
        self.counts.deferred = len(self.deferred)
        if self.deferred and self._progress():
            await self.deps.restart(self.project_id)
        return (PARTIAL if self.counts.failed else SUCCEEDED), None

    async def _reload(self) -> None:
        self.ids = {item.node_id: item for item in await self.deps.store.list_items(self.pid)}
        self.rows = {node: _row(item) for node, item in self.ids.items()}

    def _build(self) -> Desired:
        return build(self.plan, project_name="", epic_key=_NO_PARENT, rows=self.rows, names=self.names)

    def _keys(self) -> dict[str, str]:
        return {node: row.jira_key for node, row in self.rows.items() if row.jira_key and not is_deleted(row)}

    def _content(self, item: Item) -> dict[str, Any]:
        return content(item, self._keys(), _NO_PARENT)

    def _diff(self) -> tuple[list[str], list[str]]:
        keys = self._keys()
        creates = [node for node in self.desired.items if node not in keys]
        changed = [
            node
            for node, item in self.desired.items.items()
            if node in keys
            and node not in self.desired.revived
            and digest(self._content(item)) != self.rows[node].content_hash
        ]
        return creates, changed

    # Database --------------------------------------------------------------------------------------------------------

    async def _database(self, *, repair: bool) -> None:
        """Make the database and its relations when the target has none; otherwise prove it is still there (a trashed or
        unshared database ends the pass ``notion_database_gone`` before any row is written, and the stored database
        and map are forgotten). ``repair``: no row was ever written, so a crash may have stopped the first pass before
        the relations; they are read and completed."""

        target = self.target
        if target.database_id is not None:
            source_id = await self._live_source(target.database_id)
            if repair:
                await self._relations(source_id, (await self._source()).properties)
            return
        title = f"Graphico plan · {target.project_name}"
        database = await self.gateway.create_database(target.parent_page_id, title, DATABASE_PROPERTIES)
        source_id = database.data_sources[0].id
        await self._remember(database.id, source_id, database.url)
        await self._relations(source_id, None)

    async def _remember(self, database_id: str, source_id: str, url: str) -> None:
        saved = await self.deps.store.save_database(
            self.target, database_id=database_id, data_source_id=source_id, database_url=url
        )
        if not saved:  # the target changed, or a concurrent pass stored its own database first
            log_event(
                logger,
                WARNING,
                "api.notion_plan.sync.database_not_saved",
                project_id=self.project_id,
                database_id=database_id,
            )
            raise TargetChangedError
        self.target = replace(self.target, database_id=database_id, data_source_id=source_id, database_url=url)

    async def _live_source(self, database_id: str) -> str:
        """The data source of the stored database, read from Notion; one the target lacks is resolved here, never made
        again."""

        try:
            database = await self.gateway.database(database_id)
        except NotionError as error:
            if error.code is NotionCode.NOT_SHARED:
                await self._lost()
            raise
        if database.in_trash:
            await self._lost()
        if self.target.data_source_id is not None:
            return self.target.data_source_id
        source_id = database.data_sources[0].id
        await self._remember(database.id, source_id, self.target.database_url or database.url)
        return source_id

    async def _lost(self) -> None:
        """The database is gone: forget it and its rows (a later pass makes a new one) and end this pass."""

        await self.deps.store.reset_database(self.target)
        raise NotionError(NotionCode.DATABASE_GONE)

    async def _relations(self, source_id: str, existing: Mapping[str, Any] | None) -> None:
        """Add the two-way self-relations (they need the data source's own id) and rename the synced sides."""

        for name, synced_name in RELATIONS:
            prop = None if existing is None else existing.get(name)
            if existing is None or prop is None:
                relation = {"relation": {"data_source_id": source_id, "dual_property": {}}}
                prop = (await self.gateway.update_data_source(source_id, {name: relation})).properties.get(name)
            dual = ((prop or {}).get("relation") or {}).get("dual_property") or {}
            synced_id = dual.get("synced_property_id")
            if synced_id and (existing is None or synced_name not in existing):
                await self.gateway.update_data_source(source_id, {str(synced_id): {"name": synced_name}})

    async def _source(self) -> NotionDataSource:
        if self._schema is None:
            self._schema = await self.gateway.data_source(str(self.target.data_source_id))
        return self._schema

    # Rows ------------------------------------------------------------------------------------------------------------

    def _progress(self) -> int:
        """Rows this pass really wrote; refused and vanished rows are not progress that makes a new pass worth it."""

        counts = self.counts
        return counts.created + counts.updated + counts.commented + counts.removed

    async def _each(self, nodes: Sequence[str], action: Callable[[str], Awaitable[bool]], cost: int) -> None:
        """Run ``action`` for each node while the pass has request budget; the nodes left are deferred. An action
        answers false when it wrote nothing (the row waits, or was refused or settled as gone) and costs one request. A
        rate limit after some rows were written ends the row steps and defers the rest; with nothing written it fails
        the pass."""

        for index, node in enumerate(nodes):
            if self.limited or self.spent + cost > REQUEST_BUDGET:
                self.deferred.add(node)
                continue
            try:
                done = await self._guard(node, action(node))
            except NotionError as error:
                if error.code is not NotionCode.RATE_LIMITED or not self._progress():
                    raise
                self.limited = True
                self.deferred.update(nodes[index:])
                continue
            self.spent += cost if done else REFUSED_COST

    async def _guard(self, node: str, call: Awaitable[bool]) -> bool:
        """Item errors end the item, not the pass: Notion's 400 fails it, a trashed row means a person removed it, and a
        row that is not found while its database is there was purged. Only an action that wrote answers true."""

        try:
            return await call
        except NotionError as error:
            if error.code is NotionCode.ITEM_INVALID:
                await self._fail(node, error.code)
            elif error.code is NotionCode.PAGE_GONE:
                await self._gone(node)
            elif error.code is NotionCode.NOT_SHARED:
                item = self.ids.get(node)
                if item is None or item.page_id is None:
                    await self._unshared()  # a create found nothing: the database is what is not there
                    raise
                await self._unshared()
                await self._gone(node)
            else:
                raise
        return False

    async def _unshared(self) -> None:
        """A write found nothing: when the database is in the trash or no longer shared say so (as the pass start does),
        else the database is there and the answer is about the row."""

        try:
            database = await self.gateway.database(str(self.target.database_id))
        except NotionError as error:
            if error.code is NotionCode.NOT_SHARED:
                await self._lost()
            raise
        if database.in_trash:
            await self._lost()

    async def _store(self, node: str, **columns: Any) -> None:
        await self.deps.store.update_item(self.target, node, **columns)
        for name, value in columns.items():
            setattr(self.ids[node], name, value)
        self.rows[node] = _row(self.ids[node])

    async def _fail(self, node: str, code: NotionCode) -> None:
        if node in self.failed:
            return
        self.failed.add(node)
        self.counts.failed += 1
        log_event(
            logger,
            WARNING,
            "api.notion_plan.sync.item_failed",
            project_id=self.project_id,
            node_id=node,
            error_code=code.value,
        )
        await self._store(node, error_code=code.value)

    async def _gone(self, node: str) -> None:
        """A person deleted or archived the row: it is never made again."""

        row = self.rows.get(node)
        if row is None or is_deleted(row):
            return
        now = self.deps.now()
        if row.removed_at is None:
            self.counts.removed += 1
        await self._store(node, removed_at=row.removed_at or now, error_code=NotionCode.PAGE_GONE.value, synced_at=now)

    # Steps -----------------------------------------------------------------------------------------------------------

    async def _create(self, node: str) -> bool:
        item = self.desired.items[node]
        if item.kind == "subtask":
            parent = self.rows.get(item.parent or "")
            if parent is None or parent.jira_key is None or item.parent in self.failed:
                return False  # its story has no row (refused or left for the next pass): the task waits
        pending = node in self.ids and self.ids[node].page_id is None  # an earlier create may have reached Notion
        now = self.deps.now()
        await self.deps.store.add_pending(
            self.target, node_id=node, kind="story" if item.kind == "story" else "task", requested_at=now
        )
        if node not in self.ids:
            await self._reload()
        if pending and (found := await self._find(node)) is not None:
            # The row exists: take it; its content is unknown, so the update step brings it to the plan.
            await self._store(node, page_id=found, content=None, content_hash=None, error_code=None, synced_at=now)
            return False
        values = self._content(item)
        page = await self.gateway.create_page(
            str(self.target.data_source_id),
            properties=row_properties(values, node, self._blocked_pages(values)),
            markdown=row_body(values, node),
        )
        await self._store(
            node, page_id=page.id, content=values, content_hash=digest(values), error_code=None, synced_at=now
        )
        self.fresh.add(node)
        self.counts.created += 1
        return True

    async def _find(self, node: str) -> str | None:
        """The row of ``node`` by its ``Graphico ID`` (the first match wins)."""

        where = {"property": "Graphico ID", "rich_text": {"equals": node}}
        rows = await self.gateway.query_data_source(str(self.target.data_source_id), where=where, page_size=1)
        return rows.rows[0].id if rows.rows else None

    def _blocked_pages(self, values: Mapping[str, Any]) -> list[str]:
        keys = self._keys()
        return [keys[node] for node in values.get("blocked_by") or [] if node in keys]

    async def _leave(self, node: str) -> bool:
        return await self._flag(node, removed=True)

    async def _return(self, node: str) -> bool:
        return await self._flag(node, removed=False)

    async def _flag(self, node: str, *, removed: bool) -> bool:
        page_id = str(self.ids[node].page_id)
        if (await self.gateway.page(page_id)).in_trash:
            await self._gone(node)  # a trashed row is settled once, not failed in every pass
            return False
        await self.gateway.update_page(page_id, properties={"Removed": {"checkbox": removed}})
        await self.gateway.create_comment(page_id, _REMOVED_LINE if removed else _RETURNED_LINE)
        now = self.deps.now()
        if removed:
            self.counts.removed += 1
        else:
            self.counts.updated += 1
        await self._store(node, removed_at=now if removed else None, error_code=None, synced_at=now)
        return True

    async def _is_to_do(self, status: Any) -> bool:
        """Whether a row's Status is in the ``to_do`` group; one that cannot be placed counts as started."""

        option = (status or {}).get("status") or {}
        return option.get("id") in to_do_options((await self._source()).properties)

    async def _update(self, node: str) -> bool:
        page_id, values = str(self.ids[node].page_id), self._content(self.desired.items[node])
        if node in self.fresh:  # made in this pass, only its links to rows made later are missing
            await self._overwrite(node, page_id, values)
            return True
        page = await self.gateway.page(page_id)
        if page.in_trash:
            await self._gone(node)
            return False
        if await self._is_to_do(page.properties.get("Status")):
            await self._overwrite(node, page_id, values)
            self.counts.updated += 1
        else:
            lines = changed_fields(self.ids[node].content, values) or ["The plan"]
            note = "Graphico plan update. The plan changed these fields, left as they are here:\n"
            await self.gateway.create_comment(page_id, note + "\n".join(f"- {line}" for line in lines))
            self.counts.commented += 1
        await self._store(node, content=values, content_hash=digest(values), error_code=None, synced_at=self.deps.now())
        return True

    async def _overwrite(self, node: str, page_id: str, values: Mapping[str, Any]) -> None:
        properties = row_properties(values, node, self._blocked_pages(values))
        await self.gateway.update_page(page_id, properties=properties)
        await self.gateway.replace_markdown(page_id, row_body(values, node))
        if node in self.fresh:
            await self._store(node, content=dict(values), content_hash=digest(values), synced_at=self.deps.now())
