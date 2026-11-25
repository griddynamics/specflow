"""The Notion plan destination of a project (docs/notion/plan.md "Routes"): the target and the access check. Stream E1
owns this file.

Any member reads, saves and checks (D6). The export acts as the member the project's Notion is connected by (R1, ``PUT
/projects/{pid}/integrations/notion``): every call here runs as them, and ``409 notion_not_connected`` says there is
none. ``503 notion_unavailable`` when the Notion integration is off. ``PUT`` never waits for a pass: the runner is
told after the commit. No Notion call is made inside a transaction. Events carry ids and codes only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.notion import notion_error, notion_unavailable
from api.notion_plan.access import AccessRefusedError, ResolvedParent, check_access, reason_of, resolve
from api.notion_plan.runner import NotionPlanRunner
from api.project_integrations import acting_member
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
from db.integrations import NOTION
from db.notion_plan import ParentChoice
from logger.logger import INFO, get_logger, log_event
from notion.gateway import NotionGateway
from notion.reasons import NotionCode, NotionError

logger = get_logger("api.routers.notion_plan")
router = APIRouter(prefix="/projects/{pid}/notion-plan", tags=["notion-plan"])


def _runner(runtime: Runtime) -> NotionPlanRunner:
    runner: NotionPlanRunner | None = runtime.notion_plan
    if runner is None:
        raise notion_unavailable()
    return runner


async def _gateway(runtime: Runtime, pid: uuid.UUID) -> NotionGateway:
    """The gateway of the member the project's Notion is bound to; a ``NotionError`` says why there is none."""

    member = await acting_member(runtime, pid, NOTION)
    if member is None:
        raise NotionError(NotionCode.NOT_CONNECTED)
    return await _runner(runtime).gateway_of(member)


async def _out(runtime: Runtime, pid: uuid.UUID, user_id: uuid.UUID) -> NotionPlanOut:
    """The destination of the project; ``connected_by_*`` is the bound member's workspace (who acts now, not who
    saved the destination)."""

    runner: NotionPlanRunner | None = runtime.notion_plan
    async with runtime.transaction() as session:
        target = await plan_db.get_target(session, pid) if runner is not None else None
    bound = await acting_member(runtime, pid, NOTION) if target is not None else None
    connection = None
    if bound is not None and runtime.notion is not None:
        connection = await runtime.notion.get(bound)
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
        connected_by_name=None if connection is None else connection.workspace_name,
        connected_by_me=bound == user_id,
    )


@router.get("", response_model=NotionPlanOut)
async def get_notion_plan(runtime: RuntimeDep, user: UserDep, access: MemberDep) -> NotionPlanOut:
    return await _out(runtime, access.project.id, user.id)


@router.put("", response_model=NotionPlanOut)
async def put_notion_plan(body: NotionPlanIn, runtime: RuntimeDep, user: UserDep, access: MemberDep) -> NotionPlanOut:
    """Resolve the parent page link as the bound member (``api.notion_plan.access.resolve``), save the target
    (``db.notion_plan.save_target``) and tell the runner (``after_settings``) after the commit."""

    runner = _runner(runtime)
    pid = access.project.id
    try:
        parent = await _resolved(runtime, pid, body.link)
    except AccessRefusedError as refused:
        raise notion_error(NotionError(NotionCode(refused.reason))) from None
    except NotionError as error:
        raise notion_error(error) from None
    async with runtime.transaction() as session:
        saved = await plan_db.save_target(
            session,
            pid,
            parent=ParentChoice(parent.page_id, parent.title, parent.url),
            enabled=body.enabled,
            now=datetime.now(UTC),
        )
    log_event(
        logger,
        INFO,
        "api.notion_plan.saved",
        user_id=str(user.id),
        project_id=str(pid),
        enabled=body.enabled,
        destination_changed=saved.destination_changed,
    )
    await runner.after_settings(str(pid), saved)  # after the commit; it never raises and never waits for the pass
    return await _out(runtime, pid, user.id)


async def _resolved(runtime: Runtime, pid: uuid.UUID, link: str) -> ResolvedParent:
    return await resolve(await _gateway(runtime, pid), link)


@router.post("/access-check", response_model=NotionPlanAccessResult)
async def check_notion_plan_access(
    body: NotionPlanAccessIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> NotionPlanAccessOk | NotionPlanAccessFailed:
    """Read the parent page as the bound member (``api.notion_plan.access.check_access``). A project with no binding
    or an expired connection is a ``failed`` answer, not an error: the form shows it in words."""

    _runner(runtime)
    pid = access.project.id
    try:
        result = await check_access(await _gateway(runtime, pid), body.link)
    except NotionError as error:
        result = NotionPlanAccessFailed(reason=reason_of(error))
    log_event(
        logger,
        INFO,
        "api.notion_plan.access_checked",
        user_id=str(user.id),
        project_id=str(pid),
        result=result.result,
        reason=result.reason if isinstance(result, NotionPlanAccessFailed) else None,
    )
    return result
