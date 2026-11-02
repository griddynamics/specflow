"""The tool error mapper and its fixed messages (FR-MCP-TOOL-08; tools.md Error codes).

Pure unit tests: exception objects are built in the test, nothing is replaced.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from api.mcp.errors import (
    DATABASE_UNAVAILABLE,
    GRAPH_UNAVAILABLE,
    INTERNAL_ERROR,
    INVALID_CURSOR,
    MESSAGES,
    PROJECT_NOT_FOUND,
    McpToolError,
    error_body,
    to_tool_error,
)
from api.mcp.http import McpInvariantError
from graph.core import ConfigurationError, ConformanceError, InfrastructureUnavailableError, OntologyError
from mcp_support import BACKEND

TOOLS_DOC = BACKEND.parent / "docs" / "mcp-server" / "tools.md"
LEAK = "LEAK-feature-text-or-host"


class AsyncpgConnectionError(Exception):
    """Named and placed like ``asyncpg.PostgresConnectionError``: recognised along the MRO by module and class name."""


AsyncpgConnectionError.__module__ = "asyncpg.exceptions"
AsyncpgConnectionError.__name__ = "PostgresConnectionError"


def invalid_input() -> ValidationError:
    class Model(BaseModel):
        value: int

    try:
        Model.model_validate({"value": LEAK})
    except ValidationError as error:
        return error
    raise AssertionError("unreachable")


MAPPED: list[tuple[Exception, str]] = [
    (InfrastructureUnavailableError("neo4j_unreachable", detail=LEAK), GRAPH_UNAVAILABLE),
    (ConnectionRefusedError(LEAK), DATABASE_UNAVAILABLE),
    (TimeoutError(LEAK), DATABASE_UNAVAILABLE),
    (OperationalError(LEAK, None, Exception(LEAK)), DATABASE_UNAVAILABLE),
    (InterfaceError(LEAK, None, Exception(LEAK)), DATABASE_UNAVAILABLE),
    (AsyncpgConnectionError(LEAK), DATABASE_UNAVAILABLE),
    (ConfigurationError("unknown_label", label=LEAK), INTERNAL_ERROR),
    (OntologyError([LEAK]), INTERNAL_ERROR),
    (ConformanceError("bad_state", detail=LEAK), INTERNAL_ERROR),
    (McpInvariantError(LEAK), INTERNAL_ERROR),
    (RuntimeError(LEAK), INTERNAL_ERROR),
    (invalid_input(), INTERNAL_ERROR),
    (DBAPIError(LEAK, None, Exception(LEAK)), INTERNAL_ERROR),  # a programming error: a bug, not an outage
    (KeyError(LEAK), INTERNAL_ERROR),
]


@pytest.mark.parametrize(("error", "code"), MAPPED, ids=[type(e).__name__ for e, _ in MAPPED])
def test_fr_mcp_tool_08_each_exception_class_maps_to_its_code_and_keeps_only_its_class_name(
    error: Exception, code: str
) -> None:
    """FR-MCP-TOOL-08: the mapper table; the class name (audit only) is kept, the exception text is not."""

    mapped = to_tool_error(error)
    assert mapped.code == code
    assert mapped.error_type == type(error).__name__
    body = error_body(mapped.code)
    assert body.model_dump(mode="json") == {"error": {"code": code, "message": MESSAGES[mapped.code]}}
    assert LEAK not in body.model_dump_json() and LEAK not in str(mapped)


def test_fr_mcp_tool_08_a_graph_outage_wins_over_the_database_reachability_test() -> None:
    """FR-MCP-TOOL-08: ``is_database_unreachable`` counts every ``OSError``; the graph error is decided first."""

    class GraphOsError(InfrastructureUnavailableError, OSError):
        pass

    assert to_tool_error(GraphOsError("neo4j_unreachable")).code == GRAPH_UNAVAILABLE


@pytest.mark.parametrize("code", [PROJECT_NOT_FOUND, INVALID_CURSOR])
def test_fr_mcp_tool_08_a_tool_error_maps_to_itself(code: Any) -> None:
    """FR-MCP-TOOL-08: ``McpToolError`` passes through unchanged (its own class name is its audit type)."""

    own = McpToolError(code)
    assert to_tool_error(own) is own
    assert (own.code, own.error_type) == (code, "McpToolError")


def test_fr_mcp_tool_08_the_messages_equal_the_tools_md_error_codes_table() -> None:
    """FR-MCP-TOOL-08: ``MESSAGES`` is word for word the doc's Error codes table (parsed from tools.md)."""

    table = TOOLS_DOC.read_text(encoding="utf-8").split("## Error codes\n", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `(\w+)` \|.*\| (.+?) \|$", table, re.MULTILINE)
    assert dict(rows) == dict(MESSAGES)
    assert len(rows) == 5


def test_fr_mcp_tool_08_no_message_carries_variable_text() -> None:
    """FR-MCP-TOOL-08: the five messages are plain sentences: no placeholder, id or brace to fill."""

    for message in MESSAGES.values():
        assert re.fullmatch(r"[A-Z][A-Za-z ,.]+", message), message
