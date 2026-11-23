"""``SlackLinks``: which SpecFlow user a Slack user is (docs/slack/gap-answering.md "Parts").

A Slack user gets a one-time nonce in Slack; the web app sends it back from the person's signed-in session, so the
link is made by SpecFlow's own sign-in, never by matching e-mail. A nonce is bound to the Slack team and user, used
once and valid for ``NONCE_TTL``. Nonces live in this process (the API runs as one replica); a restart drops them.

Spike storage: the links are a JSON file in the graph state folder (``GRAPH_STATE_DIR``), not a table, so the spike
adds no migration to a database. Phase 1b moves them to the table ``slack_user_link``.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

NONCE_TTL: Final = timedelta(minutes=10)
LINKS_FILE: Final = "slack-links.json"


@dataclass(frozen=True)
class SlackUser:
    team_id: str
    user_id: str

    @property
    def key(self) -> str:
        return f"{self.team_id}:{self.user_id}"


@dataclass(frozen=True)
class _Nonce:
    slack: SlackUser
    expires_at: datetime


class LinkRefusedError(Exception):
    """The nonce is unknown, used or expired."""


def _now() -> datetime:
    return datetime.now(UTC)


class SlackLinks:
    """The links of Slack users to SpecFlow users and the nonces that make them."""

    def __init__(self, folder: Path, *, clock: Callable[[], datetime] = _now) -> None:
        self._path = folder / LINKS_FILE
        self._clock = clock
        self._nonces: dict[str, _Nonce] = {}
        self._lock = asyncio.Lock()

    def nonce_for(self, slack: SlackUser) -> str:
        """A new one-time nonce for ``slack``; expired nonces are dropped."""

        now = self._clock()
        self._nonces = {key: value for key, value in self._nonces.items() if value.expires_at > now}
        nonce = secrets.token_urlsafe(24)
        self._nonces[nonce] = _Nonce(slack, now + NONCE_TTL)
        return nonce

    async def link(self, nonce: str, user_id: uuid.UUID) -> SlackUser:
        """Link the nonce's Slack user to ``user_id`` (replacing an older link of that Slack user) and spend the
        nonce. ``LinkRefusedError`` for an unknown, used or expired nonce."""

        found = self._nonces.pop(nonce, None)
        if found is None or found.expires_at <= self._clock():
            raise LinkRefusedError
        async with self._lock:
            links = await asyncio.to_thread(self._read)
            links[found.slack.key] = str(user_id)
            await asyncio.to_thread(self._write, links)
        return found.slack

    async def user_of(self, slack: SlackUser) -> uuid.UUID | None:
        """The linked SpecFlow user of ``slack``; ``None`` when not linked."""

        async with self._lock:
            links = await asyncio.to_thread(self._read)
        value = links.get(slack.key)
        return None if value is None else uuid.UUID(value)

    def _read(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        data = json.loads(self._path.read_text(encoding="utf-8"))
        return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}

    def _write(self, links: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(links, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self._path)
