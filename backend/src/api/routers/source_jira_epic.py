"""The Jira epic source (docs/sources/importers.md): check an epic key or link, then import the epic and its issues.

Any member may do both (D6). The import reads as the member the project's Atlassian is connected by (R1) and runs in
this process (``runtime.imports``, AC5). The summary of the epic is read from Jira and lives in the source row and its
documents only; events carry ids, the kind and counts.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.errors import ApiError
from api.imports.atlassian import AtlassianAccess, check_error, jira_reader, project_access
from api.imports.jira_epic import EpicInspection, JiraEpicImport, JiraEpicImporter, inspect_epic
from api.project_integrations import atlassian_site_mismatch
from api.schemas import BatchSourceOut, JiraEpicCheckOut, JiraEpicChildOut, JiraEpicIn
from api.source_imports import StartedImport, start_import
from jira.epic_links import EpicLinkError, parse_epic
from jira.gateway import JiraError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.source_jira_epic")
router = APIRouter(prefix="/projects/{pid}/sources/jira-epic", tags=["sources"])
KIND = "jira_epic"


def jira_epic_link_invalid() -> ApiError:
    return ApiError(422, "jira_epic_link_invalid", "Enter an issue key such as ABC-12, or a link to the epic.")


def jira_epic_not_an_epic() -> ApiError:
    return ApiError(422, "jira_epic_not_an_epic", "The issue is not an epic.")


def jira_epic_not_found() -> ApiError:
    return ApiError(409, "jira_epic_not_found", "The epic does not exist or is not visible to the member.")


def jira_epic_forbidden() -> ApiError:
    return ApiError(409, "jira_epic_forbidden", "The member who connected Atlassian cannot read this epic.")


def _key(value: str, access: AtlassianAccess) -> str:
    try:
        return parse_epic(value, access.site_host)
    except EpicLinkError as error:
        raise (atlassian_site_mismatch() if error.site_mismatch else jira_epic_link_invalid()) from None


async def _inspected(access: AtlassianAccess, key: str) -> EpicInspection:
    try:
        async with jira_reader(access) as jira:
            found = await inspect_epic(jira, key)
    except JiraError as error:
        raise check_error(error, not_found=jira_epic_not_found(), forbidden=jira_epic_forbidden()) from None
    if not found.is_epic:
        raise jira_epic_not_an_epic()
    return found


@router.post("/check", response_model=JiraEpicCheckOut)
async def check_jira_epic(body: JiraEpicIn, runtime: RuntimeDep, access: MemberDep) -> JiraEpicCheckOut:
    atlassian = await project_access(runtime, access.project.id)
    found = await _inspected(atlassian, _key(body.key_or_link, atlassian))
    return JiraEpicCheckOut(
        key=found.key,
        summary=found.summary,
        issues=found.issues,
        children=[JiraEpicChildOut(**asdict(child)) for child in found.children],
        capped=found.capped,
    )


@router.post("", status_code=201, response_model=BatchSourceOut)
async def add_jira_epic_source(
    body: JiraEpicIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> BatchSourceOut:
    pid = access.project.id
    atlassian = await project_access(runtime, pid)
    found = await _inspected(atlassian, _key(body.key_or_link, atlassian))
    importer = JiraEpicImporter(runtime.jira.connections, JiraEpicImport(found.key, frozenset(body.exclude)))
    created = await start_import(
        runtime,
        pid,
        user.id,
        started=StartedImport(KIND, importer, atlassian.user_id),
        title=f"{found.key}: {found.summary}".rstrip(": "),
        link=f"https://{atlassian.site_host}/browse/{found.key}",
    )
    log_event(
        logger,
        INFO,
        "api.imports.added",
        user_id=str(user.id),
        project_id=str(pid),
        kind=KIND,
        excluded=len(set(body.exclude)),
    )
    return created
