"""Tool handlers: plain async functions with no SDK import (architecture.md, Tool registry)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from agent_contracts import INVALID_CURSOR, MAX_CURSOR_CHARS
from api.mcp.errors import McpToolError


class Listing(Protocol):
    """What the audit reads of a result: the item count."""

    @property
    def items(self) -> Sequence[object]: ...


def cursor_within_limit(cursor: str) -> str:
    """``cursor`` unchanged, or ``invalid_cursor`` when it is over ``MAX_CURSOR_CHARS`` (FR-MCP-TOOL-07)."""

    if len(cursor) > MAX_CURSOR_CHARS:
        raise McpToolError(INVALID_CURSOR)
    return cursor
