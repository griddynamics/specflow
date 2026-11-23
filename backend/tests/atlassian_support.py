"""Shared fakes of the Atlassian connection tests (no tests in here): ``db.atlassian`` over a dict, a scripted
``AtlassianOAuthClient`` and the oauth settings. Every value is a fake; ``test_api_atlassian_connections`` keeps the
fake's signatures equal to the repository's."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from db import atlassian as atlassian_module
from db.models import AtlassianConnection
from jira.oauth import AtlassianAccount, OAuthCode, OAuthError, TokenGrant
from settings import AtlassianOAuthSettings

SITE = "https://example.atlassian.net"
CLOUD = "cloud-0001"
CLIENT_CANARY = "client-secret-canary-71c2"  # the client secret of the oauth settings
ACCOUNT_ID = "account-id-canary-5e0a"
ACCOUNT_NAME = "Name Canary Ada"
TOKEN_KEY = Fernet.generate_key().decode()
T0 = datetime(2026, 1, 1, tzinfo=UTC)

PUBLIC_FUNCTIONS = (
    "get_connection",
    "lock_connection",
    "upsert_connection",
    "rotate_refresh_token",
    "mark_expired",
    "delete_connection",
)


def oauth_env() -> dict[str, str]:
    return {
        "ATLASSIAN_SITE_URL": SITE,
        "ATLASSIAN_OAUTH_CLIENT_ID": "client-id-1",
        "ATLASSIAN_OAUTH_CLIENT_SECRET": CLIENT_CANARY,
        "ATLASSIAN_TOKEN_KEY": TOKEN_KEY,
        "PUBLIC_URL": "http://localhost:3001",
    }


def oauth_settings() -> AtlassianOAuthSettings:
    return AtlassianOAuthSettings(**oauth_env())


class FakeAtlassianDb:
    """The ``db.atlassian`` functions over a dict. ``events`` records every write and each commit of
    ``transaction()`` (a failed block records ``rollback`` and keeps nothing it wrote)."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, AtlassianConnection] = {}
        self.events: list[tuple[str, Any]] = []
        self.locked: list[uuid.UUID] = []
        self.sessions: list[AsyncSession] = []  # the session of every repository call

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in PUBLIC_FUNCTIONS:
            monkeypatch.setattr(atlassian_module, name, getattr(self, name))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        before = {user: _values(row) for user, row in self.rows.items()}
        try:
            yield None
        except BaseException:
            self.rows = {user: AtlassianConnection(**values) for user, values in before.items()}
            self.events.append(("rollback", None))
            raise
        self.events.append(("commit", None))

    def add(self, user_id: uuid.UUID, *, refresh: str = "refresh-0", status: str = "active", **over: Any) -> None:
        cipher = Fernet(TOKEN_KEY.encode())
        values: dict[str, Any] = {
            "user_id": user_id,
            "cloud_id": CLOUD,
            "account_id": ACCOUNT_ID,
            "account_name": ACCOUNT_NAME,
            "scopes": "read:jira-work",
            "refresh_token": cipher.encrypt(refresh.encode()),
            "status": status,
            "connected_at": T0,
            "last_used_at": T0,
            "expired_at": None,
        }
        self.rows[user_id] = AtlassianConnection(**(values | over))

    def refresh_token(self, user_id: uuid.UUID) -> str:
        return Fernet(TOKEN_KEY.encode()).decrypt(self.rows[user_id].refresh_token).decode()

    async def get_connection(self, session: AsyncSession, user_id: uuid.UUID) -> AtlassianConnection | None:
        self.sessions.append(session)
        return self.rows.get(user_id)

    async def lock_connection(self, session: AsyncSession, user_id: uuid.UUID) -> AtlassianConnection | None:
        self.sessions.append(session)
        self.locked.append(user_id)
        return self.rows.get(user_id)

    async def upsert_connection(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        cloud_id: str,
        account_id: str,
        account_name: str,
        scopes: str,
        refresh_token: bytes,
        now: datetime,
    ) -> None:
        self.sessions.append(session)
        self.events.append(("upsert", user_id))
        self.rows[user_id] = AtlassianConnection(
            user_id=user_id,
            cloud_id=cloud_id,
            account_id=account_id,
            account_name=account_name,
            scopes=scopes,
            refresh_token=refresh_token,
            status="active",
            connected_at=now,
            last_used_at=now,
            expired_at=None,
        )

    async def rotate_refresh_token(
        self, session: AsyncSession, user_id: uuid.UUID, *, refresh_token: bytes, now: datetime
    ) -> None:
        self.sessions.append(session)
        self.events.append(("rotate", user_id))
        if (row := self.rows.get(user_id)) is not None:
            row.refresh_token, row.last_used_at = refresh_token, now

    async def mark_expired(self, session: AsyncSession, user_id: uuid.UUID, *, now: datetime) -> None:
        self.sessions.append(session)
        self.events.append(("expire", user_id))
        if (row := self.rows.get(user_id)) is not None:
            row.status, row.expired_at = "expired", now

    async def delete_connection(self, session: AsyncSession, user_id: uuid.UUID) -> bool:
        self.sessions.append(session)
        self.events.append(("delete", user_id))
        return self.rows.pop(user_id, None) is not None


def _values(row: AtlassianConnection) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in AtlassianConnection.__table__.columns}


class FakeOAuthClient:
    """A scripted ``AtlassianOAuthClient``: each refresh rotates ``refresh-N`` to ``refresh-N+1`` and answers
    ``access-N+1``; ``refuse`` makes the next refreshes fail with that code; ``gate`` holds a refresh."""

    def __init__(self, events: list[tuple[str, Any]] | None = None) -> None:
        self.events = events if events is not None else []
        self.refreshes: list[str] = []
        self.exchanges: list[str] = []
        self.refuse: OAuthCode | None = None
        self.exchange_error: OAuthCode | None = None
        self.site_error: OAuthCode | None = None
        self.gate: asyncio.Event | None = None
        self.expires_in = 3600
        self.closed = False

    def authorize_url(self, state: str) -> str:
        return f"https://auth.atlassian.com/authorize?state={state}"

    async def exchange(self, code: str) -> TokenGrant:
        self.exchanges.append(code)
        if self.exchange_error is not None:
            raise OAuthError(self.exchange_error)
        return TokenGrant(SecretStr("access-first"), SecretStr("refresh-first"), self.expires_in, "read:jira-work")

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant:
        used = refresh_token.get_secret_value()
        self.refreshes.append(used)
        if self.gate is not None:
            await self.gate.wait()
        if self.refuse is not None:
            raise OAuthError(self.refuse)
        number = len(self.refreshes)
        self.events.append(("token_call", used))
        return TokenGrant(SecretStr(f"access-{number}"), SecretStr(f"refresh-{number}"), self.expires_in, "s")

    async def cloud_id(self, access_token: SecretStr, site_url: str) -> str:
        del access_token, site_url
        if self.site_error is not None:
            raise OAuthError(self.site_error)
        return CLOUD

    async def account(self, access_token: SecretStr, cloud_id: str) -> AtlassianAccount:
        del access_token, cloud_id
        return AtlassianAccount(account_id=ACCOUNT_ID, name=ACCOUNT_NAME)

    async def aclose(self) -> None:
        self.closed = True
