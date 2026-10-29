"""Wire shapes of the users capability."""

from __future__ import annotations

from pydantic import BaseModel

from shop.enums import UserRole


class LoginIn(BaseModel):
    user_name: str
    password: str


class SessionOut(BaseModel):
    user_id: str


class UserOut(BaseModel):
    id: str
    role: UserRole
