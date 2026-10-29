"""The session-cookie dependency every protected route declares."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from shop.enums import UserRole
from shop.errors import UnauthenticatedError

SESSION_COOKIE = "session"


@dataclass(frozen=True)
class CurrentUser:
    id: str
    role: UserRole


_SESSIONS: dict[str, CurrentUser] = {}


def open_session(token: str, user: CurrentUser) -> None:
    _SESSIONS[token] = user


async def current_user(request: Request) -> CurrentUser:
    token = request.cookies.get(SESSION_COOKIE)
    user = _SESSIONS.get(token) if token else None
    if user is None:
        raise UnauthenticatedError
    return user


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]
