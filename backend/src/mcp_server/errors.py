"""Tool error codes, their fixed messages and ``from_api`` (FR-MCP-TOOL-08, tools.md Error codes).

A tool answers with a code and a message from ``MESSAGES``; API message text and exception text never leave the
registry's wrapper, because they can hold feature text or identifiers.
"""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Final, cast, get_args

from pydantic import BaseModel, ValidationError

from agent_contracts import (
    DATABASE_UNAVAILABLE,
    FEATURE_NOT_FOUND,
    GRAPH_UNAVAILABLE,
    INTERNAL_ERROR,
    INVALID_CURSOR,
    NO_PLAN,
    PROJECT_NOT_FOUND,
    TASK_NOT_FOUND,
    ErrorCode,
)
from mcp_server.api_client import API_PROTOCOL_ERROR, ApiResult

MESSAGES: Final[Mapping[ErrorCode, str]] = {
    PROJECT_NOT_FOUND: "The project does not exist or you are not a member of it.",
    INVALID_CURSOR: "The cursor is invalid. Start again without a cursor.",
    NO_PLAN: "The project has no implementation plan yet. Create one on the Plan page of the web app.",
    TASK_NOT_FOUND: "The task is not in the current plan of the project. List the tasks to find it.",
    FEATURE_NOT_FOUND: "The feature is not in the specification graph of the project. List the features to find it.",
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


API_AUTH_REJECTED: Final = "ApiAuthRejected"
API_AGENT_UNAVAILABLE: Final = "ApiAgentUnavailable"
_AGENT_UNAVAILABLE_CODE: Final = "agent_unavailable"
_ERROR_CODES: Final[frozenset[str]] = frozenset(get_args(ErrorCode))


class ApiErrorDetail(BaseModel):
    code: str


class ApiErrorBody(BaseModel):
    """The ``code`` of the API's ``{"error": {code, message, details}}`` envelope; its text is never read."""

    error: ApiErrorDetail


def _error_code(body: object) -> str | None:
    try:
        return ApiErrorBody.model_validate(body).error.code
    except ValidationError:
        return None


def from_api[M: BaseModel](result: ApiResult, output: type[M]) -> M:
    """The tool result of one API call: the body validated into ``output``, else the ``McpToolError`` (the table
    of architecture.md, Errors). The API's message text never reaches the error."""

    if result.failure is not None:
        raise McpToolError(INTERNAL_ERROR, result.failure)
    status = result.status or 0
    if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
        if result.user_id is None:  # the API answers every success with the provisioned user
            raise McpToolError(INTERNAL_ERROR, API_PROTOCOL_ERROR)
        try:
            return output.model_validate(result.body)
        except ValidationError:
            raise McpToolError(INTERNAL_ERROR, API_PROTOCOL_ERROR) from None
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        raise McpToolError(INTERNAL_ERROR, API_AUTH_REJECTED)
    code = _error_code(result.body)
    if status == HTTPStatus.SERVICE_UNAVAILABLE and code == _AGENT_UNAVAILABLE_CODE:
        raise McpToolError(INTERNAL_ERROR, API_AGENT_UNAVAILABLE)
    if status >= HTTPStatus.BAD_REQUEST and code in _ERROR_CODES:
        raise McpToolError(cast(ErrorCode, code), f"ApiError:{code}")
    raise McpToolError(INTERNAL_ERROR, API_PROTOCOL_ERROR)
