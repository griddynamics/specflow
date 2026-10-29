"""``GET /me``: the caller, provisioned just in time by the ``current_user`` dependency."""

from __future__ import annotations

from fastapi import APIRouter

from api.deps import UserDep
from api.schemas import MeOut

router = APIRouter(tags=["me"])


@router.get("/me", response_model=MeOut)
async def me(user: UserDep) -> MeOut:
    return MeOut(
        id=user.id,
        actor_id=user.actor_id,
        email=user.email,
        display_name=user.display_name,
    )
