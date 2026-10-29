"""The users capability's entry module: other capabilities import only this."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from shop.config import get_settings
from shop.enums import UserRole
from shop.errors import UnauthenticatedError
from shop.platform.auth import CurrentUser, open_session
from shop.users.schemas import SessionOut, UserOut


@dataclass
class _StoredUser:
    id: str
    password_digest: str
    role: UserRole


_USERS: dict[str, _StoredUser] = {}


def _digest(password: str) -> str:
    key = get_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, password.encode(), hashlib.sha256).hexdigest()


def login(user_name: str, password: str) -> tuple[SessionOut, str]:
    stored = _USERS.get(user_name)
    if stored is None or not hmac.compare_digest(stored.password_digest, _digest(password)):
        raise UnauthenticatedError
    token = secrets.token_hex(32)
    open_session(token, CurrentUser(id=stored.id, role=stored.role))
    return SessionOut(user_id=stored.id), token


def describe(user: CurrentUser) -> UserOut:
    return UserOut(id=user.id, role=user.role)
