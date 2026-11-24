"""What the Atlassian importers share (docs/sources/importers.md): the member the project's Atlassian is connected by,
a reader as that member, and the closed outcomes of a refused read.

The acting account is the member bound to the project (R1), never the caller; the import runs in this process with that
member's tokens (``AtlassianConnections``). Fixed messages only: nothing Atlassian says reaches an error or a log.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urlsplit

from api.errors import ApiError
from api.imports.model import ImportFailedError
from api.project_integrations import (
    acting_member,
    atlassian_not_connected,
    atlassian_reconnect_required,
    atlassian_unreachable,
)
from api.routers.atlassian import atlassian_oauth_unavailable
from api.runtime import Runtime
from db.integrations import ATLASSIAN
from jira.confluence import ConfluenceClient
from jira.gateway import JiraError
from jira.http import TokenSource
from jira.issues import JiraIssueReader
from jira.reader import AtlassianReader, confluence_url, jira_url
from jira.reasons import Reason

NO_ACCESS: Final = "no_access"  # the closed reason of an item or a source the member cannot read
READ_STEP: Final = "read"
SKIPPABLE: Final = frozenset({Reason.NOT_FOUND, Reason.FORBIDDEN})  # an item the member cannot see: skipped, counted
_NO_ACCESS: Final = frozenset({Reason.NOT_FOUND, Reason.FORBIDDEN, Reason.AUTH_FAILED, Reason.RECONNECT_REQUIRED})
_RECONNECT: Final = frozenset({Reason.AUTH_FAILED, Reason.RECONNECT_REQUIRED, Reason.NOT_CONNECTED})


class ConnectionView(Protocol):
    status: str
    cloud_id: str


class Connections(Protocol):
    """The part of ``AtlassianConnections`` the importers use."""

    @property
    def site_url(self) -> str: ...

    async def get(self, user_id: uuid.UUID) -> ConnectionView | None: ...

    def token_source(self, user_id: uuid.UUID) -> TokenSource: ...


@dataclass(frozen=True)
class AtlassianAccess:
    """The member a project's Atlassian acts as and what a reader of theirs needs."""

    user_id: uuid.UUID
    cloud_id: str
    tokens: TokenSource
    site_host: str


def reader_for(base_url: str, tokens: TokenSource) -> AtlassianReader:
    """A reader of ``base_url`` as one user (the one place that builds it, so a test can put a transport behind it)."""

    return AtlassianReader(base_url, tokens)


def site_host(connections: Connections) -> str:
    return (urlsplit(connections.site_url).hostname or "").lower()


async def access_of(connections: Connections, user_id: uuid.UUID) -> AtlassianAccess | None:
    """The access of ``user_id``; ``None`` without an active connection."""

    view = await connections.get(user_id)
    if view is None or view.status != "active":
        return None
    return AtlassianAccess(user_id, view.cloud_id, connections.token_source(user_id), site_host(connections))


async def project_access(runtime: Runtime, project_id: uuid.UUID) -> AtlassianAccess:
    """The access of the member the project's Atlassian is connected by; ``ApiError`` when there is none to use."""

    jira = runtime.jira
    if jira is None or jira.connections is None:
        raise atlassian_oauth_unavailable()
    member = await acting_member(runtime, project_id, ATLASSIAN)
    if member is None:
        raise atlassian_not_connected()
    view = await jira.connections.get(member)
    if view is None:
        raise atlassian_not_connected()
    if view.status != "active":
        raise atlassian_reconnect_required()
    return AtlassianAccess(member, view.cloud_id, jira.connections.token_source(member), site_host(jira.connections))


@asynccontextmanager
async def confluence_client(access: AtlassianAccess) -> AsyncIterator[ConfluenceClient]:
    reader = reader_for(confluence_url(access.cloud_id), access.tokens)
    try:
        yield ConfluenceClient(reader)
    finally:
        await reader.aclose()


@asynccontextmanager
async def jira_reader(access: AtlassianAccess) -> AsyncIterator[JiraIssueReader]:
    reader = reader_for(jira_url(access.cloud_id), access.tokens)
    try:
        yield JiraIssueReader(reader)
    finally:
        await reader.aclose()


def check_error(error: JiraError, *, not_found: ApiError, forbidden: ApiError) -> ApiError:
    """The API answer to a refused read of a check route."""

    if error.reason is Reason.NOT_FOUND:
        return not_found
    if error.reason is Reason.FORBIDDEN:
        return forbidden
    if error.reason in _RECONNECT:
        return atlassian_reconnect_required()
    return atlassian_unreachable()


def import_failure(error: JiraError) -> ImportFailedError:
    """The closed ``connections.failure_reason`` of an import a read stopped: no access, else unknown."""

    return ImportFailedError("no_access" if error.reason in _NO_ACCESS else "unknown_error")
