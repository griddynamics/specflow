"""Routes of the users capability."""

from __future__ import annotations

from fastapi import APIRouter, Response

from shop.platform.auth import SESSION_COOKIE, CurrentUserDep
from shop.users import service
from shop.users.schemas import LoginIn, SessionOut, UserOut

router = APIRouter(tags=["users"])


@router.post("/auth/login", response_model=SessionOut)
async def login_route(body: LoginIn, response: Response) -> SessionOut:
    session, token = service.login(body.user_name, body.password)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict")
    return session


@router.get("/users/me", response_model=UserOut)
async def read_me(user: CurrentUserDep) -> UserOut:
    return service.describe(user)
