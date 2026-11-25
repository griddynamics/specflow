"""The Notion link source (docs/notion/source.md "Routes"): check a page or database link, then import it. Stream D1
owns this file.

Any member may do both (D6). The import reads as the member the project's Notion is connected by (R1) and runs in this
process (``runtime.imports``). The title of the source is read from Notion and lives in the source row and its documents
only; events carry ids, the kind and counts. ``503 notion_unavailable`` when the integration is off.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.notion import notion_connections
from api.schemas import BatchSourceOut, NotionCheckOut, NotionLinkIn

router = APIRouter(prefix="/projects/{pid}/sources/notion", tags=["sources"])
KIND = "notion"


@router.post("/check", response_model=NotionCheckOut)
async def check_notion_link(body: NotionLinkIn, runtime: RuntimeDep, access: MemberDep) -> NotionCheckOut:
    """What the link would import, read as the member the project's Notion is bound to; ``422 notion_link_invalid``,
    ``409 notion_not_shared``, ``409 notion_not_connected``, ``409 notion_reconnect_required``."""

    notion_connections(runtime)
    del body, access
    raise NotImplementedError("D1 fills check_notion_link")


@router.post("", status_code=201, response_model=BatchSourceOut)
async def add_notion_source(
    body: NotionLinkIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> BatchSourceOut:
    """Check the link as above, create the source for the caller and start the import (``start_import`` of
    ``api.source_imports`` with ``NotionImporter``, reading as ``acting_member(runtime, pid, NOTION)``)."""

    notion_connections(runtime)
    del body, user, access
    raise NotImplementedError("D1 fills add_notion_source")
