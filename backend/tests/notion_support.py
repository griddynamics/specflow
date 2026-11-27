"""Shared fakes of the Notion tests (no tests in here): the fixture-backed ``FakeNotionGateway`` as the member's
gateway, ``FakeNotionConnections`` as ``runtime.notion`` and ``Connections`` of the importer, and the recording writer
and import rig of the Atlassian importer tests (the same ``ImportWriter`` boundary). Every id and text comes from
``notion/fixtures``: e-mails are ``person@example.com``, people ``Sample Author``."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from api.imports import ImportJob
from atlassian_import_support import SOURCE_ID, FakeImports, RecordingWriter, as_writer, source_card
from db import source_files
from integrations_db_fakes import FakeIntegrationsDb
from notion.connections import ConnectionView
from notion.fake import FakeNotionGateway
from notion.reasons import NotionCode, NotionError

__all__ = [
    "ACTING",
    "NOW",
    "SOURCE_ID",
    "FakeImports",
    "FakeNotionConnections",
    "RecordingWriter",
    "as_writer",
    "notion_job",
    "wire_notion_rig",
]

ACTING = uuid.UUID(int=77)
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class FakeNotionConnections:
    """``NotionConnections`` for the routes and the importer: users with an ``active`` or ``expired`` connection, and
    the one ``FakeNotionGateway`` every member reads and writes through (``gateway``)."""

    def __init__(self, gateway: FakeNotionGateway | None = None) -> None:
        self.fake = gateway or FakeNotionGateway()
        self.users: dict[uuid.UUID, str] = {ACTING: "active"}  # user id -> status
        self.workspace = "Sample Workspace"
        self.workspace_id = "ws-1"  # the workspace the binding of the rig names
        self.gateways_asked: list[uuid.UUID] = []

    def expire(self, user_id: uuid.UUID) -> None:
        self.users[user_id] = "expired"

    async def get(self, user_id: uuid.UUID) -> ConnectionView | None:
        status = self.users.get(user_id)
        if status is None:
            return None
        return ConnectionView(
            "expired" if status == "expired" else "active", self.workspace_id, self.workspace, NOW, NOW
        )

    async def gateway(self, user_id: uuid.UUID) -> FakeNotionGateway:
        self.gateways_asked.append(user_id)
        status = self.users.get(user_id)
        if status is None:
            raise NotionError(NotionCode.NOT_CONNECTED)
        if status == "expired":
            raise NotionError(NotionCode.RECONNECT_REQUIRED)
        return self.fake

    async def aclose(self) -> None:
        return None


def notion_job(project: uuid.UUID, *, user: uuid.UUID = ACTING, source: uuid.UUID = SOURCE_ID) -> ImportJob:
    return ImportJob(project, source, user, "notion")


async def wire_notion_rig(served: Any, monkeypatch: pytest.MonkeyPatch, *, alpha: uuid.UUID) -> SimpleNamespace:
    """The REST app of ``serve_agent`` with a Notion bound to ``ACTING`` for project ``Alpha``,
    ``FakeNotionConnections`` as ``runtime.notion``, ``FakeImports`` as ``runtime.imports`` and the card of the added
    source. Returns the rig the route tests share."""

    runtime = served.runtime
    connections, integrations, imports = FakeNotionConnections(), FakeIntegrationsDb(), FakeImports()
    integrations.install(monkeypatch)
    integrations.put(alpha, "notion", ACTING, workspace_id=connections.workspace_id)
    runtime.notion = connections
    runtime.imports = imports

    async def source_summary(_session: Any, _project: uuid.UUID, source_id: uuid.UUID) -> Any:
        added = imports.added[-1] if imports.added else {"name": "", "link": ""}
        card = source_card("notion", served.users["alice"].id, added["name"], added["link"])
        return card if source_id == SOURCE_ID else None

    monkeypatch.setattr(source_files, "source_summary", source_summary)
    return SimpleNamespace(
        http=served.client, runtime=runtime, connections=connections, fake=connections.fake,
        integrations=integrations, imports=imports, alpha=alpha,
    )  # fmt: skip
