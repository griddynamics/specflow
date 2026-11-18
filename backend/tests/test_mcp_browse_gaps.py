"""``browse_gaps`` and ``open_gap`` through the MCP app (tools.md "browse_gaps", "open_gap"): the SDK's client against
the MCP app, whose API is the real app over the shop gap corpus after a gap run (``gap_agent_support``). The flow of the
web Gaps screen: list with a filter, open one gap, then answer it with ``answer_gap`` (``test_mcp_answer_gap.py``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from gap_agent_support import ALPHA, QUESTION
from mcp_gap_support import WEB, Served, serve_mcp_gaps
from mcp_support import ALICE, call, sdk_session


@pytest.fixture
async def served(monkeypatch, valid_env) -> AsyncIterator[Served]:
    async with serve_mcp_gaps(monkeypatch, valid_env) as served:
        yield served


async def test_my_gaps_as_rows_then_one_gap_whole_with_its_card(served) -> None:
    gap_id = served.gaps.question_gap()
    alice = served.gaps.rig.alice_id
    await served.gaps.gaps.assign_gap(ALPHA, gap_id, alice, by=alice)

    async with sdk_session(served.app, ALICE) as session:
        mine = await call(session, "browse_gaps", project_id=ALPHA, assigned="me", status=["open"])
        opened = await call(session, "open_gap", project_id=ALPHA, gap_id=gap_id)

    assert not mine.is_error and mine.structured_content is not None
    rows = mine.structured_content["items"]
    assert [(row["gap_id"], row["status"], row["assignee"]["you"]) for row in rows] == [(gap_id, "open", True)]
    assert set(rows[0]) == {"gap_id", "title", "severity", "kind", "status", "assignee"}
    assert not opened.is_error and opened.structured_content is not None
    detail = opened.structured_content
    assert (detail["gap_id"], detail["question"], detail["answerable"]) == (gap_id, QUESTION, True)
    assert "**Status:** open · **Assigned to:** you" in detail["card"]
    assert detail["web_url"] == f"{WEB}/projects/{ALPHA}/gaps?gap={gap_id}"


async def test_a_member_filter_by_name_and_an_unknown_gap(served) -> None:
    async with sdk_session(served.app, ALICE) as session:
        unknown_member = await call(session, "browse_gaps", project_id=ALPHA, assignee="Nobody Here")
        unknown_gap = await call(session, "open_gap", project_id=ALPHA, gap_id="GAP::000000000000")

    assert unknown_member.is_error and unknown_member.structured_content is not None
    assert unknown_member.structured_content["error"]["code"] == "invalid_arguments"
    assert unknown_member.structured_content["error"]["details"] == {"argument": "assignee"}
    assert unknown_gap.is_error and unknown_gap.structured_content is not None
    assert unknown_gap.structured_content["error"]["code"] == "gap_not_found"
