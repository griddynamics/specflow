"""User provisioning and the one membership function (access-control.md, FR-MCP-ACC-01 to ACC-05).

Neo4j has no per-user permissions: ``member_project`` is the only guard in front of the graph. Both
steps run in their own short transaction, so the user row stays when the tool then denies the project.
"""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from api.mcp.errors import PROJECT_NOT_FOUND, McpToolError
from api.mcp.runtime import McpRuntime
from auth.claims import Principal
from db import repositories
from db import session as db_session
from db.models import Project


@dataclass(frozen=True)
class Caller:
    """The token's ``Principal`` and the id of the user row provisioning returned."""

    principal: Principal
    user_id: uuid.UUID


def transaction(runtime: McpRuntime) -> AbstractAsyncContextManager[Any]:
    """One session and one transaction, committed on success; a new session per call (FR-MCP-ACC-04)."""

    return db_session.transaction(runtime.session_factory)


def parse_project_id(value: str) -> uuid.UUID | None:
    """The UUID the argument spells, else ``None`` (malformed ids are denied without a query)."""

    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def provision(runtime: McpRuntime, principal: Principal) -> uuid.UUID:
    """``upsert_user`` committed in its own transaction (FR-MCP-ACC-05); returns the user id."""

    async with transaction(runtime) as session:
        user = await repositories.upsert_user(session, principal)
        return user.id


async def member_project(runtime: McpRuntime, user_id: uuid.UUID, project_id: str) -> Project:
    """The project when the user is a member (FR-MCP-ACC-02, ACC-03).

    A malformed, unknown or foreign id raises the same ``project_not_found``; a malformed one runs no query.
    """

    parsed = parse_project_id(project_id)
    if parsed is None:
        raise McpToolError(PROJECT_NOT_FOUND)
    async with transaction(runtime) as session:
        project = await repositories.get_member_project(session, user_id, parsed)
    if project is None:
        raise McpToolError(PROJECT_NOT_FOUND)
    return project
