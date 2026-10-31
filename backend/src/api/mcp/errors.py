"""Tool error codes, their fixed messages and the sanitizing mapper (FR-MCP-TOOL-08, tools.md Error codes).

A tool answers with a code and a message from ``MESSAGES``; exception text never leaves the registry's
wrapper, because driver and validation text can hold feature text or identifiers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

from pydantic import BaseModel

from db.session import is_database_unreachable
from graph.core import InfrastructureUnavailableError

ErrorCode = Literal[
    "project_not_found", "invalid_cursor", "graph_unavailable", "database_unavailable", "internal_error"
]

PROJECT_NOT_FOUND: Final = "project_not_found"
INVALID_CURSOR: Final = "invalid_cursor"
GRAPH_UNAVAILABLE: Final = "graph_unavailable"
DATABASE_UNAVAILABLE: Final = "database_unavailable"
INTERNAL_ERROR: Final = "internal_error"

MESSAGES: Final[Mapping[ErrorCode, str]] = {
    PROJECT_NOT_FOUND: "The project does not exist or you are not a member of it.",
    INVALID_CURSOR: "The cursor is invalid. Start again without a cursor.",
    GRAPH_UNAVAILABLE: "The specification graph is unavailable. Try again later.",
    DATABASE_UNAVAILABLE: "The database is unavailable. Try again later.",
    INTERNAL_ERROR: "Something went wrong. Try again later.",
}


class McpToolError(Exception):
    """A tool failure with its code; ``error_type`` is the class name of the mapped exception (audit only)."""

    def __init__(self, code: ErrorCode, error_type: str | None = None) -> None:
        super().__init__(code)
        self.code: ErrorCode = code
        self.error_type = error_type or type(self).__name__


class ToolErrorDetail(BaseModel):
    code: ErrorCode
    message: str


class ToolErrorBody(BaseModel):
    """``{"error": {"code", "message"}}``: structured content and text of a tool error."""

    error: ToolErrorDetail


def error_body(code: ErrorCode) -> ToolErrorBody:
    return ToolErrorBody(error=ToolErrorDetail(code=code, message=MESSAGES[code]))


def to_tool_error(error: Exception) -> McpToolError:
    """The one place an exception becomes a code; the order is fixed.

    A graph outage is checked before ``is_database_unreachable``, which counts any ``OSError`` as a database
    outage. ``ConfigurationError``, ``ConformanceError`` and every unknown failure are bugs: ``internal_error``.
    """

    if isinstance(error, McpToolError):
        return error
    name = type(error).__name__
    if isinstance(error, InfrastructureUnavailableError):
        return McpToolError(GRAPH_UNAVAILABLE, name)
    if is_database_unreachable(error):
        return McpToolError(DATABASE_UNAVAILABLE, name)
    return McpToolError(INTERNAL_ERROR, name)
