"""FastAPI dependencies: the runtime and the authenticated user."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Request, Response

from agent_contracts import USER_ID_HEADER
from api.errors import AGENT_USER_STATE_KEY, agent_unavailable, project_not_found
from api.runtime import Runtime
from auth.claims import Principal
from auth.dependencies import AGENT_VERIFIER_STATE_ATTR, current_agent_principal, current_principal
from db import repositories as repo
from db.models import Project, User


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


async def current_user(runtime: RuntimeDep, principal: Annotated[Principal, Depends(current_principal)]) -> User:
    """Authenticate, then provision or refresh the user row just in time (AC-7.2)."""

    async with runtime.transaction() as session:
        return await repo.upsert_user(session, principal)


UserDep = Annotated[User, Depends(current_user)]


async def agent_principal(request: Request) -> Principal:
    """The MCP-policy principal of an agent route; ``503 agent_unavailable`` when the api has no MCP settings."""

    if getattr(request.app.state, AGENT_VERIFIER_STATE_ATTR, None) is None:
        raise agent_unavailable()
    return await current_agent_principal(request)


async def current_agent_user(
    request: Request,
    response: Response,
    runtime: RuntimeDep,
    principal: Annotated[Principal, Depends(agent_principal)],
) -> User:
    """Verify (MCP policy), then provision as ``current_user`` does; every response after this names the user.

    The header goes on a success through ``response`` and on an error through the request state, which the error
    handlers read (``api.errors``).
    """

    async with runtime.transaction() as session:
        user = await repo.upsert_user(session, principal)
    setattr(request.state, AGENT_USER_STATE_KEY, str(user.id))
    response.headers[USER_ID_HEADER] = str(user.id)
    return user


AgentUserDep = Annotated[User, Depends(current_agent_user)]


async def agent_project(project_id: str, runtime: RuntimeDep, user: AgentUserDep) -> Project:
    """The project of the caller's ``project_id`` when the user is a member; membership comes before any read.

    ``project_id`` is caller text: a malformed one is ``project_not_found`` with no query and is never logged.
    """

    try:
        parsed = uuid.UUID(project_id)
    except ValueError:
        raise project_not_found() from None
    async with runtime.transaction() as session:
        project = await repo.get_member_project(session, user.id, parsed)
    if project is None:
        raise project_not_found()
    return project


AgentProjectDep = Annotated[Project, Depends(agent_project)]
