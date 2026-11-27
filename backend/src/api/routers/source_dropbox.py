"""The Dropbox link source (docs/dropbox/source.md, docs/sources/importers.md): check a Dropbox folder or file link,
then import it without the files the member unchecked.

Any member may do both (D6). The import reads as the member the project's Dropbox is connected by (R1) and runs in this
process (``runtime.imports``, AC5). The name of the folder or file is read from Dropbox and lives in the source row and
its documents only; events carry ids, the kind and counts, never a link, a path, a name or a token. The pasted shared
link is never stored (DB8): ``connections.link`` is the ``/home`` address of the resolved target.

W0 wrote the routes thin over the ``api.imports.dropbox`` stubs; stream S2 owns this file and
``tests/test_api_source_dropbox.py``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.dropbox import dropbox_connections, dropbox_error
from api.imports.dropbox import DropboxImport, DropboxImporter, Inspection, Resolved, inspect, resolve
from api.project_integrations import integration_workspace_mismatch
from api.runtime import Runtime
from api.schemas import BatchSourceOut, DropboxCheckOut, DropboxFileOut, DropboxLinkIn
from api.source_imports import StartedImport, start_import
from db import integrations as integrations_db
from db.integrations import DROPBOX
from dropbox.codes import DropboxCode, DropboxError
from dropbox.connections import DropboxConnections
from dropbox.links import canonical_link, parse_link
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.source_dropbox")
router = APIRouter(prefix="/projects/{pid}/sources/dropbox", tags=["sources"])
KIND = "dropbox"


async def _bound(runtime: Runtime, project_id: uuid.UUID) -> tuple[DropboxConnections, uuid.UUID]:
    """The Dropbox service and the member the project's Dropbox is connected by (R1); ``ApiError`` when there is none
    to use. A member who reconnected another Dropbox account (another workspace than the bound one, D2) is refused
    ``integration_workspace_mismatch``."""

    connections = dropbox_connections(runtime)
    async with runtime.transaction() as session:
        bound = await integrations_db.get_integration(session, project_id, DROPBOX)
    member = None if bound is None else bound.user_id
    view = None if member is None else await connections.get(member)
    if bound is None or member is None or view is None:
        raise dropbox_error(DropboxError(DropboxCode.NOT_CONNECTED))
    if view.status != "active":
        raise dropbox_error(DropboxError(DropboxCode.RECONNECT_REQUIRED))
    if view.workspace_id != bound.workspace_id:
        raise integration_workspace_mismatch()
    return connections, member


def _source(body: DropboxLinkIn) -> DropboxImport:
    try:
        return DropboxImport(parse_link(body.link), frozenset(body.exclude))
    except DropboxError as error:
        raise dropbox_error(error) from None


async def _inspected(connections: DropboxConnections, member: uuid.UUID, source: DropboxImport) -> Inspection:
    try:
        return await inspect(connections, member, source)
    except DropboxError as error:
        raise dropbox_error(error) from None


async def _resolved(connections: DropboxConnections, member: uuid.UUID, source: DropboxImport) -> Resolved:
    try:
        return await resolve(connections, member, source)
    except DropboxError as error:
        raise dropbox_error(error) from None


@router.post("/check", response_model=DropboxCheckOut)
async def check_dropbox_link(body: DropboxLinkIn, runtime: RuntimeDep, access: MemberDep) -> DropboxCheckOut:
    """What the link would import: the folder or file, its importable files (to uncheck), what is skipped, the caps."""

    connections, member = await _bound(runtime, access.project.id)
    found = await _inspected(connections, member, _source(body))
    return DropboxCheckOut(
        kind=found.kind,
        name=found.name,
        files=[DropboxFileOut(id=f.id, name=f.name, path=f.path, category=f.category) for f in found.files],
        files_capped=found.files_capped,
        skipped=found.skipped,
        folders=found.folders,
        bytes=found.bytes,
        capped=found.capped,
    )


@router.post("", status_code=201, response_model=BatchSourceOut)
async def add_dropbox_source(
    body: DropboxLinkIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> BatchSourceOut:
    """Create the source for the caller and start its import as the member the project's Dropbox is connected by."""

    pid = access.project.id
    connections, member = await _bound(runtime, pid)
    source = _source(body)
    found = await _resolved(connections, member, source)
    created = await start_import(
        runtime,
        pid,
        user.id,
        started=StartedImport(KIND, DropboxImporter(connections, source), member),
        title=found.name,
        link=canonical_link(found.path_display),  # never the pasted shared link (DB8)
    )
    log_event(logger, INFO, "api.imports.added", user_id=str(user.id), project_id=str(pid), kind=KIND)
    return created
