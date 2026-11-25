"""The Linear plan destination of a project: the target, the teams and projects the acting member can use, and the
access check.

Any member reads, saves and checks (D6). The export acts as the member the project's Linear is connected by (rule R1,
``PUT /projects/{pid}/integrations/linear``): every call here runs as them, and ``409 linear_not_connected`` says there
is none. ``503 linear_unavailable`` when the Linear integration is off. ``PUT`` never waits for a pass: the runner is
told after the commit. No Linear call is made inside a transaction. Events carry ids and codes only."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import APIRouter

from api.deps import MemberDep, RuntimeDep, UserDep
from api.errors import ApiError
from api.linear.errors import linear_error, linear_not_connected, linear_unavailable
from api.linear_plan.access import AccessRefusedError, check_access, resolve
from api.linear_plan.models import (
    LinearPlanAccessFailed,
    LinearPlanAccessIn,
    LinearPlanAccessOk,
    LinearPlanAccessResult,
    LinearPlanIn,
    LinearPlanOut,
    LinearProjectChoice,
    LinearProjectsOut,
    LinearTeamChoice,
    LinearTeamsOut,
)
from api.linear_plan.runner import LinearPlanRunner
from api.project_integrations import acting_member
from api.runtime import Runtime
from db import linear_plan as plan_db
from db.integrations import LINEAR
from db.linear_plan import ProjectChoice
from linear.errors import LinearAlreadyExistsError, LinearError, LinearInvalidError
from linear.gateway import LinearGateway
from linear.ids import KIND_PROJECT, node_uuid
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.linear_plan")
router = APIRouter(prefix="/projects/{pid}/linear-plan", tags=["linear-plan"])

_REFUSALS = {
    "linear_team_not_found": (
        404,
        "linear_team_not_found",
        "The Linear team does not exist or your account cannot see it.",
    ),
    "linear_project_not_found": (
        404,
        "linear_project_not_found",
        "The Linear project does not exist or your account cannot see it.",
    ),
}


def plan_runner(runtime: Runtime) -> LinearPlanRunner:
    runner: LinearPlanRunner | None = runtime.linear_plan
    if runner is None:
        raise linear_unavailable()
    return runner


async def _acting(runtime: Runtime, pid: uuid.UUID) -> uuid.UUID:
    member = await acting_member(runtime, pid, LINEAR)
    if member is None:
        raise linear_not_connected()
    return member


@asynccontextmanager
async def _gateway(runtime: Runtime, pid: uuid.UUID) -> AsyncIterator[LinearGateway]:
    """The gateway of the member the project's Linear is bound to, for one request; a Linear failure is an API error."""

    runner = plan_runner(runtime)
    acting = await _acting(runtime, pid)
    try:
        async with runner.gateway_of(acting) as gateway:
            yield gateway
    except LinearError as error:
        raise linear_error(error) from None


def _refused(refused: AccessRefusedError) -> ApiError:
    """The API error of a refused destination: the unknown team or project has its own code, the rest are Linear's."""

    if refused.reason in _REFUSALS:
        status, code, message = _REFUSALS[refused.reason]
        return ApiError(status, code, message)
    return linear_error(refused.cause or LinearInvalidError())


async def _out(runtime: Runtime, pid: uuid.UUID, user_id: uuid.UUID) -> LinearPlanOut:
    runner: LinearPlanRunner | None = runtime.linear_plan
    async with runtime.transaction() as session:
        target = await plan_db.get_target(session, pid) if runner is not None else None
    if runner is None or target is None:
        return LinearPlanOut(
            available=runner is not None,
            team_id=None,
            team_key=None,
            project_id=None,
            project_name=None,
            project_url=None,
            created_by_graphico=False,
            enabled=False,
            sync=None,
            workspace=None,
            connected_by_me=False,
        )
    bound = await acting_member(runtime, pid, LINEAR)  # who acts now, not who saved
    connection = None if bound is None or runtime.linear is None else await runtime.linear.get(bound)
    return LinearPlanOut(
        available=True,
        team_id=target.team_id,
        team_key=target.team_key,
        project_id=target.linear_project_id,
        project_name=target.linear_project_name,
        project_url=target.linear_project_url,
        created_by_graphico=target.created_by_graphico,
        enabled=target.enabled,
        sync=runner.status_from(target),
        workspace=None if connection is None else connection.organization_name,
        connected_by_me=bound == user_id,
    )


@router.get("", response_model=LinearPlanOut)
async def get_linear_plan(runtime: RuntimeDep, user: UserDep, access: MemberDep) -> LinearPlanOut:
    return await _out(runtime, access.project.id, user.id)


@router.get("/teams", response_model=LinearTeamsOut)
async def list_linear_teams(runtime: RuntimeDep, access: MemberDep) -> LinearTeamsOut:
    """The teams the acting member sees in their Linear workspace."""

    async with _gateway(runtime, access.project.id) as gateway:
        viewer = await gateway.viewer()
        teams = await gateway.teams()
    return LinearTeamsOut(
        workspace=viewer.organization.name,
        teams=[LinearTeamChoice(id=team.id, key=team.key, name=team.name) for team in teams],
    )


@router.get("/projects", response_model=LinearProjectsOut)
async def list_linear_projects(team_id: str, runtime: RuntimeDep, access: MemberDep) -> LinearProjectsOut:
    """The projects of a team the acting member sees (not archived)."""

    async with _gateway(runtime, access.project.id) as gateway:
        found = await gateway.projects(team_id)
    return LinearProjectsOut(projects=[LinearProjectChoice(id=p.id, name=p.name, url=p.url) for p in found])


@router.put("", response_model=LinearPlanOut)
async def put_linear_plan(body: LinearPlanIn, runtime: RuntimeDep, user: UserDep, access: MemberDep) -> LinearPlanOut:
    runner = plan_runner(runtime)
    pid = access.project.id
    async with _gateway(runtime, pid) as gateway:
        try:
            found = await resolve(gateway, body.team_id, body.project_id)
            project, created = found.project, False
            if project is None:  # Graphico's own project; its id is a UUIDv5, so a retry adopts it
                made_id = str(node_uuid(pid, KIND_PROJECT, "-"))
                created = True
                try:
                    project = await gateway.create_project(made_id, found.team.id, access.project.name)
                except LinearAlreadyExistsError:
                    project = await gateway.project(made_id)
        except AccessRefusedError as refused:
            raise _refused(refused) from None
    async with runtime.transaction() as session:
        saved = await plan_db.save_target(
            session,
            pid,
            team_id=found.team.id,
            team_key=found.team.key,
            project=ProjectChoice(project.id, project.name, project.url, created),
            enabled=body.enabled,
            now=datetime.now(UTC),
        )
    log_event(
        logger,
        INFO,
        "api.linear_plan.saved",
        user_id=str(user.id),
        project_id=str(pid),
        enabled=body.enabled,
        destination_changed=saved.destination_changed,
    )
    await runner.after_settings(str(pid), saved)  # after the commit; it never raises and never waits for the pass
    return await _out(runtime, pid, user.id)


@router.post("/access-check", response_model=LinearPlanAccessResult)
async def check_linear_plan_access(
    body: LinearPlanAccessIn, runtime: RuntimeDep, user: UserDep, access: MemberDep
) -> LinearPlanAccessOk | LinearPlanAccessFailed:
    pid = access.project.id
    async with _gateway(runtime, pid) as gateway:
        result = await check_access(gateway, body.team_id, body.project_id)
    log_event(
        logger,
        INFO,
        "api.linear_plan.access_checked",
        user_id=str(user.id),
        project_id=str(pid),
        result=result.result,
        reason=result.reason if isinstance(result, LinearPlanAccessFailed) else None,
    )
    return result
