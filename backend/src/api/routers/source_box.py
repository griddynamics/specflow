"""The Box link source (docs/box/source.md, docs/sources/importers.md): check a folder, file or shared link, then
import it without the files the member unchecked.

Any member may do both (D6). The import reads as the member the project's Box is connected by (R1) and runs in this
process (``runtime.imports``, AC5). The name of the folder or file is read from Box and lives in the source row and its
documents only; events carry ids, the kind and counts, never a link, a name or a token.

Tested in ``tests/test_api_source_box.py``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from api.box import box_connections, box_error
from api.deps import MemberDep, RuntimeDep, UserDep
from api.imports.box import BoxImport, BoxImporter, Inspection, Resolved, inspect, resolve
from api.project_integrations import integration_workspace_mismatch
from api.runtime import Runtime
from api.schemas import BatchSourceOut, BoxCheckOut, BoxFileOut, BoxLinkIn
from api.source_imports import StartedImport, start_import
from box.codes import BoxCode, BoxError
from box.connections import BoxConnections
from box.links import canonical_link, parse_link
from db import integrations as integrations_db
from db.integrations import BOX
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.source_box")
router = APIRouter(prefix="/projects/{pid}/sources/box", tags=["sources"])
KIND = "box"


async def _bound(runtime: Runtime, project_id: uuid.UUID) -> tuple[BoxConnections, uuid.UUID]:
    """The Box service and the member the project's Box is connected by (R1); ``ApiError`` when there is none to use.
    A member who reconnected with another Box account (a workspace other than the one the project was bound to, D2)
    is refused ``integration_workspace_mismatch``: the project never silently reads another account."""

    connections = box_connections(runtime)
    async with runtime.transaction() as session:
        bound = await integrations_db.get_integration(session, project_id, BOX)
    member = None if bound is None else bound.user_id
    view = None if member is None else await connections.get(member)
    if bound is None or member is None or view is None:
        raise box_error(BoxError(BoxCode.NOT_CONNECTED))
    if view.status != "active":
        raise box_error(BoxError(BoxCode.RECONNECT_REQUIRED))
    if view.workspace_id != bound.workspace_id:
        raise integration_workspace_mismatch()
    return connections, member


async def _inspected(connections: BoxConnections, member: uuid.UUID, source: BoxImport) -> Inspection:
    try:
        return await inspect(connections, member, source)
    except BoxError as error:
        raise box_error(error) from None


async def _resolved(connections: BoxConnections, member: uuid.UUID, source: BoxImport) -> Resolved:
    try:
        return await resolve(connections, member, source)
    except BoxError as error:
        raise box_error(error) from None


def _source(body: BoxLinkIn) -> BoxImport:
    try:
        return BoxImport(parse_link(body.link), frozenset(body.exclude))
    except BoxError as error:
        raise box_error(error) from None


@router.post("/check", response_model=BoxCheckOut)
async def check_box_link(body: BoxLinkIn, runtime: RuntimeDep, access: MemberDep) -> BoxCheckOut:
    """What the link would import: the folder or file, its importable files (to uncheck), what is skipped, the caps."""

    connections, member = await _bound(runtime, access.project.id)
    found = await _inspected(connections, member, _source(body))
    return BoxCheckOut(
        kind=found.kind,
        name=found.name,
        files=[BoxFileOut(id=f.id, name=f.name, path=f.path, category=f.category) for f in found.files],
        files_capped=found.files_capped,
        skipped=found.skipped,
        folders=found.folders,
        bytes=found.bytes,
        capped=found.capped,
    )


@router.post("", status_code=201, response_model=BatchSourceOut)
async def add_box_source(body: BoxLinkIn, runtime: RuntimeDep, user: UserDep, access: MemberDep) -> BatchSourceOut:
    """Create the source for the caller and start its import (without the unchecked files) as the member the project's
    Box is connected by."""

    pid = access.project.id
    connections, member = await _bound(runtime, pid)
    source = _source(body)
    found = await _resolved(connections, member, source)
    created = await start_import(
        runtime,
        pid,
        user.id,
        started=StartedImport(KIND, BoxImporter(connections, source), member),
        title=found.name,
        link=canonical_link(found.kind, found.id),  # never the pasted shared link (B8)
    )
    log_event(logger, INFO, "api.imports.added", user_id=str(user.id), project_id=str(pid), kind=KIND)
    return created
