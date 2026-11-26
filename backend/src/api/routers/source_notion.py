"""The Notion link source (docs/notion/source.md "Routes"): check a page or database link, then import it. Stream D1
owns this file.

Any member may do both (D6). The import reads as the member the project's Notion is connected by (R1) and runs in this
process (``runtime.imports``). The title of the source is read from Notion and lives in the source row and its documents
only; events carry ids, the kind and counts. ``503 notion_unavailable`` when the integration is off.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.imports.notion import NotionImport, NotionImporter, inspect, open_link
from api.notion import notion_connections, notion_error
from api.runtime import Runtime
from api.schemas import BatchSourceOut, NotionCheckOut, NotionLinkIn, NotionSharedItem, NotionSharedOut
from api.source_imports import StartedImport, start_import
from db import integrations as integrations_db
from db.integrations import NOTION
from logger.logger import INFO, get_logger, log_event
from notion.connections import NotionConnections
from notion.gateway import NotionGateway
from notion.links import parse_link
from notion.reasons import NotionCode, NotionError
from notion.shared import shared_objects

logger = get_logger("api.routers.source_notion")
router = APIRouter(prefix="/projects/{pid}/sources/notion", tags=["sources"])
KIND = "notion"


async def _reader(
    runtime: Runtime, project_id: uuid.UUID, link: str
) -> tuple[NotionConnections, uuid.UUID, NotionImport, NotionGateway]:
    """The link parsed and the gateway of the member the project's Notion is bound to; every refusal is an
    ``ApiError`` of ``api.notion``."""

    try:
        source = NotionImport(parse_link(link))
    except NotionError as error:
        raise notion_error(error) from None
    connections, member, gateway = await _bound(runtime, project_id)
    return connections, member, source, gateway


async def _bound(runtime: Runtime, project_id: uuid.UUID) -> tuple[NotionConnections, uuid.UUID, NotionGateway]:
    """The gateway of the member the project's Notion is bound to; every refusal is an ``ApiError`` of
    ``api.notion``."""

    connections = notion_connections(runtime)
    try:
        async with runtime.transaction() as session:
            bound = await integrations_db.get_integration(session, project_id, NOTION)
        if bound is not None and bound.user_id is not None:
            member = bound.user_id
            mine = await connections.get(member)
            if mine is None or mine.workspace_id == bound.workspace_id:
                return connections, member, await connections.gateway(member)
            # the member reconnected into another Notion workspace: the binding's pages are not theirs any more
            raise notion_error(NotionError(NotionCode.RECONNECT_REQUIRED))
    except NotionError as error:
        raise notion_error(error) from None
    raise notion_error(NotionError(NotionCode.NOT_CONNECTED))


@router.get("/shared", response_model=NotionSharedOut)
async def list_shared_notion(runtime: RuntimeDep, access: MemberDep) -> NotionSharedOut:
    """The pages and databases the member the project's Notion is bound to shared with Graphico, for the pickers of
    the source and plan forms (``notion.shared``); ``409 notion_not_connected``, ``409 notion_reconnect_required``."""

    *_, gateway = await _bound(runtime, access.project.id)
    try:
        found = await shared_objects(gateway)
    except NotionError as error:
        raise notion_error(error) from None
    items = [
        NotionSharedItem(kind=item.kind, title=item.title, link=item.link, parent_title=item.parent_title)
        for item in found.items
    ]
    return NotionSharedOut(items=items, capped=found.capped)


@router.post("/check", response_model=NotionCheckOut)
async def check_notion_link(body: NotionLinkIn, runtime: RuntimeDep, access: MemberDep) -> NotionCheckOut:
    """What the link would import, read as the member the project's Notion is bound to; ``422 notion_link_invalid``,
    ``409 notion_not_shared``, ``409 notion_not_connected``, ``409 notion_reconnect_required``."""

    *_, source, gateway = await _reader(runtime, access.project.id, body.link)
    try:
        found = await inspect(gateway, source)
    except NotionError as error:
        raise notion_error(error) from None
    return NotionCheckOut(kind=found.kind, title=found.title, pages=found.pages, capped=found.capped)


@router.post("", status_code=201, response_model=BatchSourceOut)
async def add_notion_source(
    body: NotionLinkIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> BatchSourceOut:
    """Check the link as above, create the source for the caller and start the import (``start_import`` of
    ``api.source_imports`` with ``NotionImporter``, reading as the member the project is bound to)."""

    pid = access.project.id
    connections, member, source, gateway = await _reader(runtime, pid, body.link)
    try:
        _kind, title = await open_link(gateway, source)  # the import walks the tree, once
    except NotionError as error:
        raise notion_error(error) from None
    created = await start_import(
        runtime,
        pid,
        user.id,
        started=StartedImport(KIND, NotionImporter(connections, source), member),
        title=title or "Untitled",
        link=body.link,
    )
    log_event(logger, INFO, "api.imports.added", user_id=str(user.id), project_id=str(pid), kind=KIND)
    return created
