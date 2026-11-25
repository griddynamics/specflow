"""The Linear source (docs/sources/importers.md): check a Linear project or issue link (or an identifier such as
``GRA-12``), then import the project's issues and documents, or the issue and its sub-issues.

Any member may do both (D6). The import reads as the member the project's Linear is connected by (R1) and runs in this
process (``runtime.imports``). Names and titles are read from Linear and live in the source row and its documents only;
events carry ids, the kind and counts. Adding the same link again is a new source (D7: the import is a snapshot).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from api.deps import MemberDep, RuntimeDep, UserDep
from api.errors import ApiError
from api.imports.linear_access import project_access
from api.imports.linear_project import (
    MAX_LINEAR_ISSUES,
    LinearImport,
    LinearProjectImporter,
    inspect_link,
    resolve_target,
)
from api.linear.errors import linear_error
from api.schemas import LINK_MAX, BatchSourceOut, IssueKey
from api.source_imports import StartedImport, start_import
from linear.errors import LinearError, LinearInvalidError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.source_linear")
router = APIRouter(prefix="/projects/{pid}/sources/linear", tags=["sources"])
KIND = "linear_project"
EXCLUDE_MAX = MAX_LINEAR_ISSUES  # issues not to import: at most the issues an import takes


class LinearSourceIn(BaseModel):
    """A Linear project or issue by its link, or an issue by its identifier (``GRA-12``)."""

    model_config = ConfigDict(extra="forbid")

    link: str = Field(max_length=LINK_MAX)
    # issues under the target not to import (the check ignores it)
    exclude: list[IssueKey] = Field(default_factory=list, max_length=EXCLUDE_MAX)


class LinearItemOut(BaseModel):
    """One issue an import would take: identifier, title and state name (shown, not stored here), parent identifier."""

    identifier: str
    title: str
    state: str
    parent: str | None


class LinearCheckOut(BaseModel):
    """What an import would take: the kind and name of the target, how many issues it has for the member (at most the
    cap is imported, listed in order) and how many project documents."""

    kind: Literal["project", "issue"]
    name: str
    issues: int
    items: list[LinearItemOut]
    capped: bool  # more issues than the cap
    documents: int


def linear_link_invalid() -> ApiError:
    return ApiError(
        422, "linear_link_invalid", "Enter a Linear project or issue link, or an issue identifier such as ENG-12."
    )


def _refused(error: LinearError) -> ApiError:
    """A pasted value Linear cannot read as a link is ``linear_link_invalid``; any other refusal is its closed code."""

    return linear_link_invalid() if isinstance(error, LinearInvalidError) else linear_error(error)


@router.post("/check", response_model=LinearCheckOut)
async def check_linear_source(body: LinearSourceIn, runtime: RuntimeDep, access: MemberDep) -> LinearCheckOut:
    linear = await project_access(runtime, access.project.id)
    try:
        async with linear.gateway() as gateway:
            found = await inspect_link(gateway, body.link)
    except LinearError as error:
        raise _refused(error) from None
    return LinearCheckOut(
        kind=found.target.kind,
        name=found.target.name,
        issues=found.issues,
        items=[LinearItemOut(**asdict(item)) for item in found.items],
        capped=found.capped,
        documents=found.documents,
    )


@router.post("", status_code=201, response_model=BatchSourceOut)
async def add_linear_source(
    body: LinearSourceIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> BatchSourceOut:
    pid = access.project.id
    linear = await project_access(runtime, pid)
    try:
        async with linear.gateway() as gateway:
            target = await resolve_target(gateway, body.link)
    except LinearError as error:
        raise _refused(error) from None
    importer = LinearProjectImporter(
        linear.connections, LinearImport(target.kind, target.target_id, frozenset(body.exclude))
    )
    created = await start_import(
        runtime,
        pid,
        user.id,
        started=StartedImport(KIND, importer, linear.user_id),
        title=target.name,
        link=target.url,
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
