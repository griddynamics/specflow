"""The Confluence link source (docs/sources/importers.md): check a page or space link, then import it.

Any member may do both (D6). The import reads as the member the project's Atlassian is connected by (R1) and runs in
this process (``runtime.imports``, AC5). The title of the source is read from Confluence and lives in the source row
and its documents only; events carry ids, the kind and counts.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.errors import ApiError, source_not_found
from api.imports import ImportJob
from api.imports.atlassian import AtlassianAccess, check_error, confluence_client, project_access
from api.imports.confluence import ConfluenceImport, ConfluenceImporter, Inspection, inspect
from api.project_integrations import atlassian_site_mismatch
from api.runtime import Runtime
from api.schemas import BatchSourceOut, ConfluenceCheckOut, ConfluenceLinkIn
from api.source_views import summary_out
from db import source_files as files
from jira.confluence_links import LinkCode, LinkError, PageTarget, SpaceTarget, parse_link
from jira.gateway import JiraError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.source_confluence")
router = APIRouter(prefix="/projects/{pid}/sources/confluence", tags=["sources"])
KIND = "confluence"


def confluence_link_invalid() -> ApiError:
    return ApiError(422, "confluence_link_invalid", "The link is not a Confluence page or space link.")


def confluence_short_link_unsupported() -> ApiError:
    return ApiError(
        422, "confluence_short_link_unsupported", "Short links are not supported. Paste the full page address."
    )


def confluence_not_found() -> ApiError:
    return ApiError(409, "confluence_not_found", "The page or space does not exist or is not visible to the member.")


def confluence_forbidden() -> ApiError:
    return ApiError(409, "confluence_forbidden", "The member who connected Atlassian cannot read this page or space.")


def _target(link: str, access: AtlassianAccess) -> PageTarget | SpaceTarget:
    try:
        return parse_link(link, access.site_host)
    except LinkError as error:
        if error.code is LinkCode.SITE_MISMATCH:
            raise atlassian_site_mismatch() from None
        raise (
            confluence_short_link_unsupported() if error.code is LinkCode.SHORT else confluence_link_invalid()
        ) from None


async def _inspected(access: AtlassianAccess, source: ConfluenceImport) -> Inspection:
    try:
        async with confluence_client(access) as confluence:
            return await inspect(confluence, source, access.site_host)
    except JiraError as error:
        raise check_error(error, not_found=confluence_not_found(), forbidden=confluence_forbidden()) from None


@router.post("/check", response_model=ConfluenceCheckOut)
async def check_confluence_link(body: ConfluenceLinkIn, runtime: RuntimeDep, access: MemberDep) -> ConfluenceCheckOut:
    atlassian = await project_access(runtime, access.project.id)
    source = ConfluenceImport(_target(body.link, atlassian), body.follow)
    found = await _inspected(atlassian, source)
    return ConfluenceCheckOut(
        kind="page" if found.kind == "page" else "space",
        title=found.title,
        pages=found.pages,
        capped=found.capped,
        linked_issues=found.linked_issues,
    )


@router.post("", status_code=201, response_model=BatchSourceOut)
async def add_confluence_source(
    body: ConfluenceLinkIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> BatchSourceOut:
    pid = access.project.id
    atlassian = await project_access(runtime, pid)
    source = ConfluenceImport(_target(body.link, atlassian), body.follow)
    found = await _inspected(atlassian, source)
    created = await start_import(
        runtime, pid, user.id, atlassian=atlassian, source=source, title=found.title, link=body.link
    )
    log_event(
        logger, INFO, "api.imports.added", user_id=str(user.id), project_id=str(pid), kind=KIND, follow=body.follow
    )
    return created


async def start_import(
    runtime: Runtime,
    pid: uuid.UUID,
    user_id: uuid.UUID,
    *,
    atlassian: AtlassianAccess,
    source: ConfluenceImport,
    title: str,
    link: str,
) -> BatchSourceOut:
    """Create the source and start its import as the bound member; the source as the Sources status shows it."""

    source_id = await runtime.imports.add_source(pid, user_id, kind=KIND, name=title, link=link.strip())
    importer = ConfluenceImporter(runtime.jira.connections, source)
    runtime.imports.start(ImportJob(pid, source_id, atlassian.user_id, KIND), importer)
    async with runtime.transaction() as session:
        summary = await files.source_summary(session, pid, source_id)
    if summary is None:  # removed between the create and the read
        raise source_not_found()
    return BatchSourceOut(source=summary_out(summary))
