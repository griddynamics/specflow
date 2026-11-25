"""A Notion that behaves like Notion where the plan pass depends on it, an in-memory ``PlanStore`` and a harness for
the Notion plan pass (no tests in here). The plans are the Jira sync tests' plans; the gateway is
``FakeNotionGateway`` with the behaviours the fixtures lack: a two-way relation grows its synced side, a row made in a
database with a ``Status`` property starts in the default option and a written text part reads back with
``plain_text``."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from api.jira.desired import SpecRef
from api.notion_plan.store import TargetChangedError
from api.notion_plan.sync import PushDeps, PushResult, push_once
from db.models import NotionPlanItem
from db.notion_plan import PlanTargetView
from graph.plan.reader import LoadedPlan
from jira_sync_support import T0, basic_plan
from notion.fake import ROOT_PAGE_ID, FakeNotionGateway, normal
from notion.model import NotionDataSource, NotionPage
from notion.reasons import NotionError

PARENT_ID = ROOT_PAGE_ID
PARENT_URL = "https://www.notion.so/Specflow-Notion-Lab-423ed33606458"
NOT_STARTED, IN_PROGRESS, DONE = "opt-not-started", "opt-in-progress", "opt-done"
STATUS_SCHEMA = {
    "type": "status",
    "status": {
        "options": [
            {"id": NOT_STARTED, "name": "Not started"},
            {"id": IN_PROGRESS, "name": "In progress"},
            {"id": DONE, "name": "Done"},
        ],
        "groups": [
            {"id": "g1", "name": "To-do", "option_ids": [NOT_STARTED]},
            {"id": "g2", "name": "In progress", "option_ids": [IN_PROGRESS]},
            {"id": "g3", "name": "Complete", "option_ids": [DONE]},
        ],
    },
}
OPTIONS = {NOT_STARTED: "Not started", IN_PROGRESS: "In progress", DONE: "Done"}


def _as_read(properties: Mapping[str, Any]) -> dict[str, Any]:
    """Notion answers every text part with ``plain_text``; what was written has only ``text.content``."""

    def part(value: dict[str, Any]) -> dict[str, Any]:
        return {**value, "plain_text": (value.get("text") or {}).get("content", "")}

    found: dict[str, Any] = {}
    for name, value in properties.items():
        found[name] = {
            key: [part(p) for p in inner] if key in ("title", "rich_text") else inner for key, inner in value.items()
        }
    return found


class PlanNotion(FakeNotionGateway):
    """``FakeNotionGateway`` plus Notion's relation and ``Status`` behaviour; ``synced`` lists the sides it added."""

    def __init__(self) -> None:
        super().__init__()
        self.synced: list[str] = []

    async def create_database(self, parent_page_id: str, title: str, properties: Mapping[str, Any]) -> Any:
        database = await super().create_database(parent_page_id, title, properties)
        source = self.data_sources[normal(database.data_sources[0].id)]
        if "Status" in source.properties:  # Notion fills the options and the groups of a new status property
            self.data_sources[source.id] = replace(
                source, properties={**source.properties, "Status": {"id": "Status", "name": "Status", **STATUS_SCHEMA}}
            )
        return database

    async def update_data_source(self, data_source_id: str, properties: Mapping[str, Any]) -> NotionDataSource:
        """A ``dual_property`` relation adds its synced side ``synced:<name>`` and answers its id; a rename by id (a
        property key that is not a name of the schema) renames that property."""

        source_id = normal(data_source_id)
        added: dict[str, Any] = {}
        renames: dict[str, str] = {}
        for name, value in properties.items():
            relation = value.get("relation")
            if relation is not None:
                synced = f"synced:{name}"
                added[name] = {**value, "relation": {**relation, "dual_property": {"synced_property_id": synced}}}
                added[synced] = {"type": "relation", "relation": {"dual_property": {"synced_property_id": name}}}
                self.synced.append(synced)
            elif name.startswith("synced:") and "name" in value:
                renames[name] = str(value["name"])
        result = await super().update_data_source(data_source_id, added)
        self.calls.pop()  # one call of the pass is one entry: the super call only builds the schema
        self._enter("update_data_source", data_source_id, properties)
        schema = dict(result.properties)
        for old, new in renames.items():
            schema[new] = {**schema.pop(old), "name": new}
        updated = replace(result, properties=schema)
        self.data_sources[source_id] = updated
        return updated

    async def create_page(self, data_source_id: str, *, properties: Mapping[str, Any], markdown: str = "") -> Any:
        page = await super().create_page(data_source_id, properties=_as_read(properties), markdown=markdown)
        source = self.data_sources[normal(data_source_id)]
        if "Status" in source.properties:
            status = {"id": "Status", "type": "status", "status": {"id": NOT_STARTED, "name": "Not started"}}
            page = replace(page, properties={**page.properties, "Status": status})
            self.pages[page.id] = page
        return page

    async def update_page(self, page_id: str, *, properties: Mapping[str, Any]) -> NotionPage:
        return await super().update_page(page_id, properties=_as_read(properties))

    # Test controls -----------------------------------------------------------------------------------------------

    def source(self) -> NotionDataSource:
        """The one data source the pass made."""

        return next(source for key, source in self.data_sources.items() if key.startswith("fa4e0000"))

    def row(self, node_id: str) -> NotionPage:
        """The row whose ``Graphico ID`` is ``node_id``."""

        for page_id in self.rows[self.source().id]:
            page = self.pages[normal(page_id)]
            text = "".join(p.get("plain_text", "") for p in page.properties["Graphico ID"].get("rich_text") or [])
            if text == node_id:
                return page
        raise KeyError(node_id)

    def set_status(self, node_id: str, option: str) -> None:
        page = self.row(node_id)
        status = {"id": "Status", "type": "status", "status": {"id": option, "name": OPTIONS[option]}}
        self.pages[page.id] = replace(page, properties={**page.properties, "Status": status})

    def trash(self, node_id: str) -> None:
        page = self.row(node_id)
        self.pages[page.id] = replace(page, in_trash=True)

    def writes(self) -> list[str]:
        reads = {"page", "database", "data_source", "page_markdown", "query_data_source"}
        return [call.method for call in self.calls if call.method not in reads]

    def row_text(self, node_id: str, name: str) -> str:
        """The plain text of a rich-text, title or select property of a row."""

        prop = self.row(node_id).properties[name]
        if "select" in prop:
            return str(prop["select"]["name"])
        parts = prop.get("title") or prop.get("rich_text") or []
        return "".join(str((p.get("text") or {}).get("content", p.get("plain_text", ""))) for p in parts)

    def relation(self, node_id: str, name: str) -> list[str]:
        return [str(r["id"]) for r in self.row(node_id).properties[name]["relation"]]


class FakeStore:
    """``PlanStore`` over a dict; ``changed`` makes the next write raise ``TargetChangedError``."""

    def __init__(self, target: PlanTargetView | None) -> None:
        self.target = target
        self.items: dict[str, NotionPlanItem] = {}
        self.changed = False
        self.saved: list[dict[str, str]] = []
        self.resets = 0

    async def get_target(self, project_id: uuid.UUID) -> PlanTargetView | None:
        return self.target if self.target is not None and self.target.project_id == project_id else None

    async def list_items(self, project_id: uuid.UUID) -> list[NotionPlanItem]:
        found = [item for item in self.items.values() if item.project_id == project_id]
        return sorted(found, key=lambda item: item.node_id)

    async def save_database(
        self, target: PlanTargetView, *, database_id: str, data_source_id: str, database_url: str
    ) -> None:
        if self.changed or self.target is None or target.project_id != self.target.project_id:
            raise TargetChangedError
        self.saved.append({"database_id": database_id, "data_source_id": data_source_id})
        self.target = replace(
            self.target, database_id=database_id, data_source_id=data_source_id, database_url=database_url
        )

    async def reset_database(self, target: PlanTargetView) -> None:
        if self.changed or self.target is None or target.project_id != self.target.project_id:
            raise TargetChangedError
        self.resets += 1
        self.items.clear()
        self.target = replace(self.target, database_id=None, data_source_id=None, database_url=None)

    async def add_pending(self, target: PlanTargetView, *, node_id: str, kind: str, requested_at: datetime) -> None:
        if self.changed:
            raise TargetChangedError
        if node_id not in self.items:
            self.items[node_id] = NotionPlanItem(
                project_id=target.project_id, node_id=node_id, kind=kind, requested_at=requested_at
            )

    async def update_item(self, target: PlanTargetView, node_id: str, **columns: Any) -> None:
        if self.changed or self.target is None or target.project_id != self.target.project_id:
            raise TargetChangedError
        for name, value in columns.items():
            setattr(self.items[node_id], name, value)


def target_view(project_id: uuid.UUID, *, enabled: bool = True, database: bool = False) -> PlanTargetView:
    return PlanTargetView(
        project_id=project_id,
        project_name="Shop",
        parent_page_id=PARENT_ID,
        parent_page_title="Specflow Notion Lab",
        parent_page_url=PARENT_URL,
        database_id="db-1" if database else None,
        data_source_id="ds-1" if database else None,
        database_url="https://www.notion.so/db1" if database else None,
        enabled=enabled,
        updated_at=T0,
        last_status=None,
        last_trigger=None,
        last_started_at=None,
        last_finished_at=None,
        last_error_code=None,
        last_counts=None,
    )


class Harness:
    """A project with an enabled target, the fake Notion and the fake rows; ``push`` runs one pass."""

    def __init__(self, plan: LoadedPlan | None = None, *, enabled: bool = True) -> None:
        self.project_id = uuid.uuid4()
        self.plan = plan or basic_plan()
        self.gateway = PlanNotion()
        self.store = FakeStore(target_view(self.project_id, enabled=enabled))
        self.gateway_error: NotionError | None = None
        self.restarts: list[str] = []
        self.gateway_asked = 0

    async def _gateway_for(self, _project_id: uuid.UUID) -> PlanNotion:
        self.gateway_asked += 1
        if self.gateway_error is not None:
            raise self.gateway_error
        return self.gateway

    async def _load_plan(self, _project_id: str) -> LoadedPlan:
        return self.plan

    async def _spec_names(self, _project_id: str, _ids: Sequence[str]) -> dict[str, SpecRef]:
        return {}

    async def _restart(self, project_id: str) -> None:
        self.restarts.append(project_id)

    def deps(self) -> PushDeps:
        return PushDeps(
            store=self.store,
            gateway_for=self._gateway_for,
            load_plan=self._load_plan,
            spec_names=self._spec_names,
            restart=self._restart,
            now=lambda: datetime.now(UTC),
        )

    async def push(self) -> PushResult:
        return await push_once(str(self.project_id), self.deps())

    def page_of(self, node_id: str) -> str:
        return str(self.store.items[node_id].page_id)
