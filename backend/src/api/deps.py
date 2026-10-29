"""FastAPI dependencies: the runtime and the authenticated user."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from api.runtime import Runtime
from auth.claims import Principal
from auth.dependencies import current_principal
from db import repositories as repo
from db.models import User


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


async def current_user(runtime: RuntimeDep, principal: Annotated[Principal, Depends(current_principal)]) -> User:
    """Authenticate, then provision or refresh the user row just in time (AC-7.2)."""

    async with runtime.transaction() as session:
        return await repo.upsert_user(session, principal)


UserDep = Annotated[User, Depends(current_user)]
