"""Tool error codes, their fixed messages and ``from_api`` (FR-MCP-TOOL-08, tools.md Error codes).

A tool answers with a code and a message from ``MESSAGES``; API message text and exception text never leave the
registry's wrapper, because they can hold feature text or identifiers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import Any, Final, cast, get_args

from pydantic import BaseModel, ValidationError, model_serializer

from agent_contracts import (
    AREA_NOT_FOUND,
    CLAIM_MISMATCH,
    DATABASE_UNAVAILABLE,
    FEATURE_NOT_FOUND,
    FINDING_LIMIT,
    GRAPH_UNAVAILABLE,
    INTERNAL_ERROR,
    INVALID_ARGUMENTS,
    INVALID_COMMIT,
    INVALID_CURSOR,
    NO_PLAN,
    NODE_NOT_FOUND,
    NOT_AVAILABLE,
    PARTLY_CLAIMED,
    PLAN_APPLYING,
    PROJECT_NOT_FOUND,
    STORY_CLAIMED,
    STORY_NOT_FOUND,
    TASK_NOT_FOUND,
    UNKNOWN_RELATION,
    ErrorCode,
    ErrorDetails,
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
    AREA_NOT_FOUND: "The area is not in the current plan of the project. List the areas to find it.",
    STORY_NOT_FOUND: "The story is not in the current plan of the project. List the stories to find it.",
    NODE_NOT_FOUND: "The node is not in the specification graph of the project. Search the specification to find it.",
    STORY_CLAIMED: "Another member holds this story. Pick another story or talk to the holder.",
    NOT_AVAILABLE: "The story cannot be claimed now. Its details name the state, the stories it waits on and findings.",
    PARTLY_CLAIMED: "A task of this story is claimed on its own. Try again when it is released or completed.",
    PLAN_APPLYING: "The plan is being updated. Try again in a minute.",
    CLAIM_MISMATCH: "The claim token does not match your claim. Call claim_story for the story to get your token.",
    INVALID_COMMIT: "The merge commit must be the full 40 or 64 character hexadecimal hash.",
    INVALID_ARGUMENTS: "The arguments do not fit together. Check the tool description and try again.",
    UNKNOWN_RELATION: "A relation is not in the ontology of the project. Leave relations out to use them all.",
    FINDING_LIMIT: "You have too many open findings here. Ask a person to resolve some first.",
}


class McpToolError(Exception):
    """A tool failure with its code; ``error_type`` is the class name of the mapped exception (audit only);
    ``details`` the validated details of the API's envelope, when it sent any."""

    def __init__(self, code: ErrorCode, error_type: str | None = None, details: ErrorDetails | None = None) -> None:
        super().__init__(code)
        self.code: ErrorCode = code
        self.error_type = error_type or type(self).__name__
        self.details = details


class ToolErrorDetail(BaseModel):
    """``code`` and ``message``, and ``details`` only when there are some: an error without details serialises as it
    always did. Inside ``details`` the fields that are not set are left out; a holder keeps its ``name: null``."""

    code: ErrorCode
    message: str
    details: ErrorDetails | None = None

    @model_serializer(mode="wrap")
    def _without_empty_details(self, handler: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        data = handler(self)
        found = data.pop("details", None)
        if found is not None:
            data["details"] = {name: value for name, value in found.items() if value is not None}
        return data


class ToolErrorBody(BaseModel):
    """``{"error": {"code", "message"}}``: structured content and text of a tool error."""

    error: ToolErrorDetail


def error_body(code: ErrorCode, details: ErrorDetails | None = None) -> ToolErrorBody:
    return ToolErrorBody(error=ToolErrorDetail(code=code, message=MESSAGES[code], details=details))


API_AUTH_REJECTED: Final = "ApiAuthRejected"
API_AGENT_UNAVAILABLE: Final = "ApiAgentUnavailable"
_AGENT_UNAVAILABLE_CODE: Final = "agent_unavailable"
_ERROR_CODES: Final[frozenset[str]] = frozenset(get_args(ErrorCode))


class ApiErrorDetail(BaseModel):
    code: str
    details: dict[str, Any] | None = None


class ApiErrorBody(BaseModel):
    """The ``code`` and ``details`` of the API's ``{"error": {code, message, details}}`` envelope; its message text is
    never read."""

    error: ApiErrorDetail


def _error(body: object) -> ApiErrorDetail | None:
    try:
        return ApiErrorBody.model_validate(body).error
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
    found = _error(result.body)
    code = found.code if found is not None else None
    if status == HTTPStatus.SERVICE_UNAVAILABLE and code == _AGENT_UNAVAILABLE_CODE:
        raise McpToolError(INTERNAL_ERROR, API_AGENT_UNAVAILABLE)
    if status >= HTTPStatus.BAD_REQUEST and found is not None and code in _ERROR_CODES:
        raise McpToolError(cast(ErrorCode, code), f"ApiError:{code}", _details(found))
    raise McpToolError(INTERNAL_ERROR, API_PROTOCOL_ERROR)


def _details(found: ApiErrorDetail) -> ErrorDetails | None:
    """The envelope's ``details`` validated like a success body (invalid ones are a protocol error); none when the
    API sent none (an empty object counts as none)."""

    if not found.details:
        return None
    try:
        return ErrorDetails.model_validate(found.details)
    except ValidationError:
        raise McpToolError(INTERNAL_ERROR, API_PROTOCOL_ERROR) from None
