"""Cursor codecs of the MCP tools (FR-MCP-TOOL-07)."""

from __future__ import annotations

import base64

import pytest

from api.mcp.errors import INVALID_CURSOR, McpToolError
from api.mcp.tools import MAX_CURSOR_CHARS, cursor_within_limit
from api.mcp.tools.graph import decode_feature_cursor, encode_feature_cursor
from mcp_support import cursor_of


def test_fr_mcp_tool_07_the_feature_cursor_round_trips_and_matches_the_doc_example() -> None:
    assert encode_feature_cursor("FEAT::CHECKOUT") == "eyJ2IjoxLCJhZnRlciI6IkZFQVQ6OkNIRUNLT1VUIn0"
    assert decode_feature_cursor(encode_feature_cursor("FEAT::CHECKOUT")) == "FEAT::CHECKOUT"


@pytest.mark.parametrize(
    "cursor",
    [
        cursor_of('{"v":1,"after":"\\ud800"}'),  # a lone surrogate cannot be re-encoded
        cursor_of('{"v":2,"after":"FEAT::A"}'),
        cursor_of('{"v":1}'),
        cursor_of('{"v":1,"after":""}'),
        cursor_of('["x"]'),
        "!!!",
        "a" * 513,
    ],
)
def test_fr_mcp_tool_07_an_undecodable_feature_cursor_is_invalid_cursor(
    cursor: str,
) -> None:
    with pytest.raises(McpToolError) as caught:
        decode_feature_cursor(cursor)
    assert caught.value.code == INVALID_CURSOR


@pytest.mark.parametrize(
    "cursor",
    [
        base64.urlsafe_b64encode(b'{"v":1,"after":"FEAT::A"}').decode(),  # padded
        cursor_of('{"after":"FEAT::A","v":1}'),  # key order
        cursor_of('{"v":1,"after":"FEAT::A","project_id":"x"}'),  # extra key
        cursor_of('{"v":true,"after":"FEAT::A"}'),  # true is not 1
        cursor_of('{"v": 1, "after": "FEAT::A"}'),  # whitespace
        cursor_of('{"v":1,"after":7}'),  # not text
        "Zm9yZ2Vk",  # base64 of "forged": not JSON
    ],
)
def test_fr_mcp_tool_07_only_the_canonical_feature_cursor_is_accepted(cursor: str) -> None:
    """FR-MCP-TOOL-07: padding, key order, extra keys, ``true`` for 1, whitespace, a non-text position."""

    with pytest.raises(McpToolError) as caught:
        decode_feature_cursor(cursor)
    assert caught.value.code == INVALID_CURSOR


def test_fr_mcp_tool_07_the_cursor_limit_is_512_characters_inclusive() -> None:
    """FR-MCP-TOOL-07: 512 characters pass the length check (and fail as undecodable); 513 fail at once."""

    assert cursor_within_limit("a" * MAX_CURSOR_CHARS) == "a" * 512
    with pytest.raises(McpToolError) as caught:
        cursor_within_limit("a" * (MAX_CURSOR_CHARS + 1))
    assert caught.value.code == INVALID_CURSOR


def test_fr_mcp_tool_07_a_non_ascii_spec_id_round_trips() -> None:
    """FR-MCP-TOOL-07: ``ensure_ascii=False`` keeps a non-ASCII ``spec_id`` canonical."""

    spec_id = "FEAT::ÜBERWEISUNG"
    assert decode_feature_cursor(encode_feature_cursor(spec_id)) == spec_id
