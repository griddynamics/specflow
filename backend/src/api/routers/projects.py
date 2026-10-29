"""Projects: create, list, get. Non-members always see 404 (AC-7.4)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from api.deps import RuntimeDep, UserDep
from api.errors import ApiError, project_not_found
from api.schemas import Page, ProjectCreate, ProjectOut
from db import repositories as repo
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.projects")
router = APIRouter(tags=["projects"])

LimitQuery = Annotated[int, Query(ge=1, le=100)]


def invalid_cursor() -> ApiError:
    return ApiError(
        422,
        "validation_failed",
        "The request is invalid.",
        details={"errors": [{"loc": ["query", "cursor"], "type": "invalid_cursor"}]},
    )


@router.post("/projects", status_code=201, response_model=ProjectOut)
async def create_project(body: ProjectCreate, runtime: RuntimeDep, user: UserDep) -> ProjectOut:
    async with runtime.transaction() as session:
        project = await repo.create_project(session, user.id, body.name)
    log_event(
        logger,
        INFO,
        "api.project.created",
        user_id=str(user.id),
        project_id=str(project.id),
    )
    return ProjectOut.model_validate(project, from_attributes=True)


@router.get("/projects", response_model=Page[ProjectOut])
async def list_projects(
    runtime: RuntimeDep,
    user: UserDep,
    limit: LimitQuery = 50,
    cursor: str | None = None,
) -> Page[ProjectOut]:
    try:
        async with runtime.transaction() as session:
            page = await repo.list_projects(session, user.id, limit, cursor)
    except repo.InvalidCursorError:
        raise invalid_cursor() from None
    return Page[ProjectOut](
        items=[ProjectOut.model_validate(p, from_attributes=True) for p in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/projects/{pid}", response_model=ProjectOut)
async def get_project(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> ProjectOut:
    async with runtime.transaction() as session:
        project = await repo.get_member_project(session, user.id, pid)
    if project is None:
        raise project_not_found()
    return ProjectOut.model_validate(project, from_attributes=True)
