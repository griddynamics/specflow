"""The error envelope ``{"error": {code, message, details}}`` (SPECS section 9).

Messages are fixed sentences chosen here; validation details carry only the
field location and the error type, never the submitted value.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from agent_contracts import AGENT_PREFIX, USER_ID_HEADER
from auth.errors import AuthError
from logger.logger import ERROR, INFO, get_logger, log_event

logger = get_logger("api.errors")

CLIENT_CLOSED_REQUEST: Final = 499
_HTTP_CODES: Final = {404: "not_found", 405: "method_not_allowed"}


class ApiError(Exception):
    """A deliberate API failure: HTTP ``status``, public ``code`` and fixed ``message``."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.headers = headers or {}


class ApiInvariantError(RuntimeError):
    """An internal invariant of the API does not hold (a bug or a setting the role needs is missing)."""


def required[V](value: V | None, what: str) -> V:
    """``value`` unless it is ``None``, which is an ``ApiInvariantError`` naming ``what``."""

    if value is None:
        raise ApiInvariantError(what)
    return value


def project_not_found() -> ApiError:
    return ApiError(404, "project_not_found", "The project does not exist.")


def document_not_found() -> ApiError:
    return ApiError(404, "document_not_found", "The document does not exist.")


def source_not_found() -> ApiError:
    """The one code that means "this source is gone" (INT-SRC-0003, FR-SRC-0019.AC3)."""

    return ApiError(404, "source_not_found", "The source does not exist.")


def batch_ended() -> ApiError:
    return ApiError(409, "batch_ended", "The upload batch is closed.")


def removal_unavailable() -> ApiError:
    return ApiError(503, "removal_unavailable", "The source could not be removed. Try again.")


def database_unavailable() -> ApiError:
    return ApiError(503, "database_unavailable", "The database is unavailable. Try again later.")


def model_not_allowed() -> ApiError:
    return ApiError(422, "model_not_allowed", "The requested model is not allowed.")


def agent_unavailable() -> ApiError:
    return ApiError(503, "agent_unavailable", "The agent routes are not configured.")


def invalid_cursor() -> ApiError:
    return ApiError(400, "invalid_cursor", "The cursor is invalid.")


def no_plan() -> ApiError:
    return ApiError(404, "no_plan", "The project has no implementation plan yet.")


def task_not_found() -> ApiError:
    return ApiError(404, "task_not_found", "The task is not in the current plan of the project.")


def feature_not_found() -> ApiError:
    return ApiError(404, "feature_not_found", "The feature is not in the specification graph of the project.")


def story_not_found() -> ApiError:
    return ApiError(404, "story_not_found", "The story is not in the current plan of the project.")


def node_not_found(details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(
        404, "node_not_found", "The node is not in the specification graph of the project.", details=details
    )


def invalid_arguments(
    argument: str | None = None, *, allowed: Sequence[str] | None = None, next_tool: str | None = None
) -> ApiError:
    """``argument`` names the argument that broke the rule (``details.argument``, never its value), ``allowed`` the
    values it takes and ``next_tool`` the tool to call instead (``details.next``)."""

    found = {"argument": argument, "allowed": list(allowed) if allowed is not None else None, "next": next_tool}
    details = {name: value for name, value in found.items() if value is not None} or None
    return ApiError(400, "invalid_arguments", "The arguments do not fit together.", details=details)


def invalid_commit() -> ApiError:
    return ApiError(
        400,
        "invalid_commit",
        "The merge commit must be 40 or 64 hexadecimal characters.",
        details={"argument": "merge_commit"},
    )


def finding_limit() -> ApiError:
    return ApiError(409, "finding_limit", "The caller has too many open findings on the story.")


def area_not_found() -> ApiError:
    return ApiError(404, "area_not_found", "The area is not in the current plan of the project.")


def story_claimed(details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, "story_claimed", "Another member holds this story.", details=details)


def not_available(details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, "not_available", "The story cannot be claimed now.", details=details)


def partly_claimed(details: dict[str, Any] | None = None) -> ApiError:
    return ApiError(409, "partly_claimed", "A task of this story is claimed on its own.", details=details)


def plan_applying() -> ApiError:
    return ApiError(409, "plan_applying", "The plan is being updated.")


def claim_mismatch() -> ApiError:
    return ApiError(409, "claim_mismatch", "The claim token does not match the claim.")


def unknown_relation() -> ApiError:
    return ApiError(400, "unknown_relation", "A relation is not in the ontology of the project.")


def graph_unavailable() -> ApiError:
    return ApiError(503, "graph_unavailable", "The specification graph is unavailable. Try again later.")


def envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


AGENT_USER_STATE_KEY: Final = "agent_user_id"


def _respond(
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None,
    headers: dict[str, str],
    *,
    request: Request,
) -> JSONResponse:
    """The envelope; an agent route that has provisioned its user (``AGENT_USER_STATE_KEY``) names it on errors too."""

    user_id = request.scope.get("state", {}).get(AGENT_USER_STATE_KEY)
    if user_id is not None:
        headers = {**headers, USER_ID_HEADER: user_id}
    return JSONResponse(envelope(code, message, details), status_code=status, headers=headers)


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(ApiError, exc)
    return _respond(error.status, error.code, error.message, error.details, error.headers, request=request)


async def _auth_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(AuthError, exc)
    return _respond(error.status, error.code, error.message, None, error.headers, request=request)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(RequestValidationError, exc)
    fields = [{"loc": [str(part) for part in item["loc"]], "type": item["type"]} for item in error.errors()]
    return _respond(422, "validation_failed", "The request is invalid.", {"errors": fields}, {}, request=request)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(StarletteHTTPException, exc)
    code = _HTTP_CODES.get(error.status_code, "http_error")
    headers = {str(k): str(v) for k, v in (error.headers or {}).items()}
    return _respond(error.status_code, code, "The request cannot be served.", None, headers, request=request)


async def _disconnect(request: Request, _: Exception) -> Response:
    log_event(logger, INFO, "api.request.disconnected", path=request.url.path)
    return Response(status_code=CLIENT_CLOSED_REQUEST)


async def _unhandled(request: Request, error: Exception) -> JSONResponse:
    # An agent route logs the class only, as the MCP tools did: exception text can hold feature text.
    log_event(
        logger,
        ERROR,
        "api.request.failed",
        method=request.method,
        path=request.url.path,
        error_type=type(error).__name__,
        exc_info=not request.url.path.startswith(f"{AGENT_PREFIX}/"),
    )
    return _respond(500, "internal_error", "An internal error occurred.", None, {}, request=request)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(AuthError, _auth_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(ClientDisconnect, _disconnect)
    app.add_exception_handler(Exception, _unhandled)
