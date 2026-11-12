"""``from_api``, the tool error mapper, and its fixed messages (FR-MCP-TOOL-08; tools.md Error codes).

Pure unit tests: API results are built in the test, nothing is replaced.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import BaseModel

from mcp_server.api_client import ApiResult
from mcp_server.errors import (
    DATABASE_UNAVAILABLE,
    GRAPH_UNAVAILABLE,
    INTERNAL_ERROR,
    INVALID_CURSOR,
    MESSAGES,
    PROJECT_NOT_FOUND,
    McpToolError,
    error_body,
    from_api,
)
from mcp_support import BACKEND

TOOLS_DOC = BACKEND.parent / "docs" / "mcp-server" / "tools.md"
LEAK = "LEAK-feature-text-or-host"


USER = "11111111-1111-4111-8111-111111111111"


class Page(BaseModel):
    items: list[int]


def reply(status: int | None, body: object = None, *, user_id: str | None = USER, failure: str | None = None):
    return ApiResult(status=status, body=body, user_id=user_id, failure=failure)


def envelope(code: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": LEAK, "details": {}}}


def test_fr_mcp_tool_08_a_2xx_with_a_valid_body_and_the_user_header_is_the_model() -> None:
    assert from_api(reply(200, {"items": [1, 2]}), Page) == Page(items=[1, 2])


MAPPED: list[tuple[ApiResult, str, str]] = [
    (reply(200, {"items": [1]}, user_id=None), INTERNAL_ERROR, "ApiProtocolError"),
    (reply(200, {"items": "x" + LEAK}), INTERNAL_ERROR, "ApiProtocolError"),
    (reply(200, None), INTERNAL_ERROR, "ApiProtocolError"),
    (reply(404, envelope("project_not_found")), PROJECT_NOT_FOUND, "ApiError:project_not_found"),
    (reply(400, envelope("invalid_cursor")), INVALID_CURSOR, "ApiError:invalid_cursor"),
    (reply(503, envelope("graph_unavailable")), GRAPH_UNAVAILABLE, "ApiError:graph_unavailable"),
    (reply(503, envelope("database_unavailable")), DATABASE_UNAVAILABLE, "ApiError:database_unavailable"),
    (reply(500, envelope("internal_error")), INTERNAL_ERROR, "ApiError:internal_error"),
    (reply(401, envelope("invalid_token")), INTERNAL_ERROR, "ApiAuthRejected"),
    (reply(403, envelope("project_not_found")), INTERNAL_ERROR, "ApiAuthRejected"),
    (reply(503, envelope("agent_unavailable")), INTERNAL_ERROR, "ApiAgentUnavailable"),
    (reply(404, envelope("agent_unavailable")), INTERNAL_ERROR, "ApiProtocolError"),
    (reply(422, envelope("validation_failed")), INTERNAL_ERROR, "ApiProtocolError"),
    (reply(404, envelope("a_new_code")), INTERNAL_ERROR, "ApiProtocolError"),
    (reply(502, None), INTERNAL_ERROR, "ApiProtocolError"),  # not JSON
    (reply(500, {"detail": LEAK}), INTERNAL_ERROR, "ApiProtocolError"),
    (reply(302, envelope("project_not_found")), INTERNAL_ERROR, "ApiProtocolError"),
    (reply(None, failure="ApiUnreachable", user_id=None), INTERNAL_ERROR, "ApiUnreachable"),
    (reply(None, failure="ApiTimeout", user_id=None), INTERNAL_ERROR, "ApiTimeout"),
]


@pytest.mark.parametrize(("result", "code", "error_type"), MAPPED, ids=[m[2] + f"-{m[0].status}" for m in MAPPED])
def test_fr_mcp_tool_08_each_api_result_maps_to_its_code_and_error_type_and_no_api_text(
    result: ApiResult, code: str, error_type: str
) -> None:
    """FR-MCP-TOOL-08: the ``from_api`` table of architecture.md, Errors; the API's message text never leaves."""

    with pytest.raises(McpToolError) as raised:
        from_api(result, Page)

    mapped = raised.value
    assert (mapped.code, mapped.error_type) == (code, error_type)
    body = error_body(mapped.code)
    assert body.model_dump(mode="json") == {"error": {"code": code, "message": MESSAGES[mapped.code]}}
    assert LEAK not in body.model_dump_json() and LEAK not in str(mapped) and LEAK not in mapped.error_type


def test_fr_mcp_tool_08_a_tool_error_keeps_its_own_class_name_as_audit_type() -> None:
    own = McpToolError(PROJECT_NOT_FOUND)

    assert (own.code, own.error_type) == (PROJECT_NOT_FOUND, "McpToolError")


def test_fr_mcp_tool_08_the_messages_equal_the_tools_md_error_codes_table() -> None:
    """FR-MCP-TOOL-08: ``MESSAGES`` is word for word the doc's Error codes table (parsed from tools.md)."""

    text = TOOLS_DOC.read_text(encoding="utf-8")
    table = text.split("## Error codes\n", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `(\w+)` \|.*\| (.+?) \|$", table, re.MULTILINE)
    assert dict(rows) == dict(MESSAGES)
    assert len(rows) == 20


def test_fr_mcp_tool_08_no_message_carries_variable_text() -> None:
    """FR-MCP-TOOL-08: the messages are plain sentences: no placeholder, id or brace to fill."""

    for message in MESSAGES.values():
        assert re.fullmatch(r"[A-Z][A-Za-z0-9_ ,.]+", message), message
