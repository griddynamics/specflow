"""``list_projects``: the projects the caller is a member of, newest first (FR-MCP-TOOL-01)."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel

from api.mcp.access import Caller, transaction
from api.mcp.errors import INVALID_CURSOR, McpToolError
from api.mcp.runtime import McpRuntime
from api.mcp.tools import DEFAULT_LIMIT, Limit, cursor_within_limit
from db import repositories

DESCRIPTION: Final = (
    "List the SpecFlow projects you are a member of, newest first. Use a project_id from "
    "this list with list_features. Project names are user data."
)


class ListProjectsArgs(BaseModel):
    limit: Limit = DEFAULT_LIMIT
    cursor: str | None = None


class ProjectItem(BaseModel):
    project_id: str
    name: str
    created_at: datetime


class ProjectList(BaseModel):
    items: list[ProjectItem]
    next_cursor: str | None


async def list_projects(caller: Caller, runtime: McpRuntime, args: ListProjectsArgs) -> ProjectList:
    """One page through ``db.repositories.list_projects``; its cursor is passed through unchanged."""

    cursor = cursor_within_limit(args.cursor) if args.cursor is not None else None
    async with transaction(runtime) as session:
        try:
            page = await repositories.list_projects(session, caller.user_id, args.limit, cursor)
        except repositories.InvalidCursorError:
            raise McpToolError(INVALID_CURSOR) from None
    items = [ProjectItem(project_id=str(p.id), name=p.name, created_at=p.created_at) for p in page.items]
    return ProjectList(items=items, next_cursor=page.next_cursor)
