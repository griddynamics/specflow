"""Tool handlers: plain async functions with no SDK import (architecture.md, Tool registry)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Final, Protocol

from pydantic import BaseModel, Field

from api.mcp.errors import INVALID_CURSOR, McpToolError

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 100
MAX_CURSOR_CHARS: Final = 512  # FR-MCP-TOOL-07

Limit = Annotated[int, Field(ge=1, le=MAX_LIMIT)]  # the input schema bounds (FR-MCP-TOOL-01, TOOL-03)


class ProjectArguments(BaseModel):
    """The arguments of a project-scoped tool start with the project id, a plain string (FR-MCP-ACC-03)."""

    project_id: str


class Listing(Protocol):
    """What the audit reads of a result: the item count."""

    @property
    def items(self) -> Sequence[object]: ...


def cursor_within_limit(cursor: str) -> str:
    """``cursor`` unchanged, or ``invalid_cursor`` when it is over ``MAX_CURSOR_CHARS`` (FR-MCP-TOOL-07)."""

    if len(cursor) > MAX_CURSOR_CHARS:
        raise McpToolError(INVALID_CURSOR)
    return cursor
