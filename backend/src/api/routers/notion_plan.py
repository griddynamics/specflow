"""The Notion plan destination of a project (docs/notion/plan.md "Routes"): the target and the access check. Stream E1
owns this file.

Any member reads, saves and checks (D6). The export acts as the member the project's Notion is connected by (R1, ``PUT
/projects/{pid}/integrations/notion``): every call here runs as them, and ``409 notion_not_connected`` says there is
none. ``503 notion_unavailable`` when the Notion integration is off. ``PUT`` never waits for a pass: the runner is
told after the commit. No Notion call is made inside a transaction. Events carry ids and codes only.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.notion import notion_unavailable
from api.notion_plan.runner import NotionPlanRunner
from api.runtime import Runtime
from api.schemas import (
    NotionPlanAccessFailed,
    NotionPlanAccessIn,
    NotionPlanAccessOk,
    NotionPlanAccessResult,
    NotionPlanIn,
    NotionPlanOut,
)
from db import notion_plan as plan_db

router = APIRouter(prefix="/projects/{pid}/notion-plan", tags=["notion-plan"])


def _runner(runtime: Runtime) -> NotionPlanRunner:
    runner: NotionPlanRunner | None = runtime.notion_plan
    if runner is None:
        raise notion_unavailable()
    return runner


async def _out(runtime: Runtime, pid: uuid.UUID, user_id: uuid.UUID) -> NotionPlanOut:
    """The destination of the project; E1 fills ``connected_by_*`` from ``acting_member`` and the connection."""

    del user_id  # E1: ``connected_by_me`` is whether the bound member is this user
    runner: NotionPlanRunner | None = runtime.notion_plan
    async with runtime.transaction() as session:
        target = await plan_db.get_target(session, pid) if runner is not None else None
    if runner is None or target is None:
        return NotionPlanOut(
            available=runner is not None,
            parent_page_id=None,
            parent_page_title=None,
            parent_page_url=None,
            database_url=None,
            enabled=False,
            sync=None,
            connected_by_name=None,
            connected_by_me=False,
        )
    return NotionPlanOut(
        available=True,
        parent_page_id=target.parent_page_id,
        parent_page_title=target.parent_page_title,
        parent_page_url=target.parent_page_url,
        database_url=target.database_url,
        enabled=target.enabled,
        sync=runner.status_from(target),
        connected_by_name=None,  # E1: the workspace name of the bound member's connection
        connected_by_me=False,  # E1: the bound member is the caller
    )


@router.get("", response_model=NotionPlanOut)
async def get_notion_plan(runtime: RuntimeDep, user: UserDep, access: MemberDep) -> NotionPlanOut:
    return await _out(runtime, access.project.id, user.id)


@router.put("", response_model=NotionPlanOut)
async def put_notion_plan(body: NotionPlanIn, runtime: RuntimeDep, user: UserDep, access: MemberDep) -> NotionPlanOut:
    """Resolve the parent page link as the bound member (``api.notion_plan.access.resolve``), save the target
    (``db.notion_plan.save_target``) and tell the runner (``after_settings``) after the commit."""

    _runner(runtime)
    del body, user, access
    raise NotImplementedError("E1 fills put_notion_plan")


@router.post("/access-check", response_model=NotionPlanAccessResult)
async def check_notion_plan_access(
    body: NotionPlanAccessIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> NotionPlanAccessOk | NotionPlanAccessFailed:
    """Read the parent page as the bound member (``api.notion_plan.access.check_access``)."""

    _runner(runtime)
    del body, user, access
    raise NotImplementedError("E1 fills check_notion_plan_access")
