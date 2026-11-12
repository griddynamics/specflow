"""The Markdown tables of the people tools (FR-MCP-STORY-03, tools.md "Tables"): exact text for fixed models, the cell
rules (a backslash then a pipe escaped, line breaks and control characters as one space, backticks kept, empty
values empty), the holder reading, the ``next_cursor`` line and the
rule that the text carries nothing but the table."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_contracts import (
    AreaInProgress,
    AreaItem,
    AreaList,
    FindingItem,
    Holder,
    StoryHeader,
    StoryItem,
    StoryList,
    StoryPreview,
    StoryRefItem,
    StoryTaskItem,
)
from mcp_server import tables

ANA = Holder(name="Ana", you=False, agent_label="Claude Code · laptop")
ME = Holder(name="Alice", you=True)
STRANGER = Holder(name=None, you=False)
AT = datetime(2026, 11, 11, 10, 2, tzinfo=UTC)
PID = "6f1c2a9e-3b0d-4c55-9a51-0d6c1e2f7a10"


def story(**over: object) -> StoryItem:
    base: dict[str, object] = {
        "rank": 1,
        "story_id": "STORY::A::API",
        "title": "Checkout",
        "kind": "feature",
        "state": "ready",
        "holder": None,
        "waits_on": [],
        "tasks": 4,
        "unblocks": 2,
        "findings": 0,
        "question": None,
    }
    return StoryItem.model_validate({**base, **over})


def test_a_holder_reads_you_else_the_name_else_another_member_then_the_label() -> None:
    assert tables.holder_text(ME) == "you" and tables.holder_text(ANA) == "Ana (Claude Code · laptop)"
    assert tables.holder_text(STRANGER) == "another member" and tables.holder_text(None) == ""
    assert tables.holder_text(Holder(name="Bo", you=True, agent_label="x")) == "you (x)"


def test_the_areas_table_has_the_columns_in_order_and_the_in_progress_cell_counts_then_names_the_holders() -> None:
    result = AreaList(
        project_id=PID,
        items=[
            AreaItem(
                area_id="AREA::WEB_SHOP",
                title="Web shop",
                kind="module",
                ready=3,
                in_progress=AreaInProgress(count=2, holders=[ANA, ME]),
                waiting=1,
                blocked=1,
                done=2,
                open_tasks=14,
            ),
            AreaItem(
                area_id="AREA::P",
                title=None,
                kind="project",
                ready=0,
                in_progress=AreaInProgress(count=0, holders=[]),
                waiting=0,
                blocked=0,
                done=0,
                open_tasks=0,
            ),
        ],
    )
    assert tables.render_areas(result).splitlines() == [
        "| Id | Area | Kind | Ready | In progress | Waiting | Blocked | Done | Open tasks |",
        "|---|---|---|---|---|---|---|---|---|",
        "| AREA::WEB_SHOP | Web shop | module | 3 | 2 · Ana (Claude Code · laptop), you | 1 | 1 | 2 | 14 |",
        "| AREA::P |  | project | 0 | 0 | 0 | 0 | 0 | 0 |",
    ]


def test_the_open_stories_table_and_the_next_cursor_line() -> None:
    result = StoryList(
        project_id=PID,
        area_id=None,
        view="open",
        total_count=5,
        items=[
            story(),
            story(
                rank=2,
                story_id="STORY::A::UI",
                title="Pay | later\nnow",
                state="waiting",
                holder=ANA,
                waits_on=["Checkout", "Cart"],
                question="Which | one?",
                findings=2,
            ),
        ],
        next_cursor="abc",
    )
    assert tables.render_stories(result).splitlines() == [
        "| # | Id | Story | Kind | State | Holder | Waits on | Tasks | Unblocks | Findings | Question |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        "| 1 | STORY::A::API | Checkout | feature | ready |  |  | 4 | 2 | 0 |  |",
        (
            "| 2 | STORY::A::UI | Pay \\| later now | feature | waiting | Ana (Claude Code · laptop) | Checkout; Cart"
            " | 4 | 2 | 2 | Which \\| one? |"
        ),
        "2 of 5 · next_cursor abc",
    ]


def test_the_last_page_and_an_empty_list_have_no_cursor_line_and_no_other_prose() -> None:
    last = StoryList(project_id=PID, area_id="AREA::A", view="open", total_count=1, items=[story()], next_cursor=None)
    empty = StoryList(project_id=PID, area_id=None, view="mine", total_count=0, items=[], next_cursor=None)
    assert tables.render_stories(last).count("\n") == 2  # header, separator, one row
    assert tables.render_stories(empty).splitlines() == [
        "| # | Id | Story | Kind | State | Holder | Waits on | Tasks | Unblocks | Findings | Question |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]


def test_the_done_view_has_its_own_columns_and_cuts_the_commit_to_12_characters() -> None:
    result = StoryList(
        project_id=PID,
        area_id=None,
        view="done",
        total_count=1,
        items=[
            story(
                state="completed",
                tasks=0,
                pr_url="https://git.example.test/p/1",
                merge_commit="0123456789abcdef" * 2 + "01234567",
                completed_by=ME,
                completed_at=AT,
            )
        ],
        next_cursor=None,
    )
    assert tables.render_stories(result).splitlines() == [
        "| # | Id | Story | Kind | Tasks | PR | Commit | Completed by | Completed at |",
        "|---|---|---|---|---|---|---|---|---|",
        (
            "| 1 | STORY::A::API | Checkout | feature | 0 | https://git.example.test/p/1 | 0123456789ab | you"
            " | 2026-11-11T10:02:00Z |"
        ),
    ]


def test_a_preview_is_one_table_with_a_part_column_in_the_order_story_unit_tasks_blockers_findings() -> None:
    result = StoryPreview(
        project_id=PID,
        story=StoryHeader(
            story_id="STORY::A::X",
            title="X",
            kind="feature",
            area_id="AREA::A",
            summary=None,
            release=None,
            state="in_progress",
            holder=ANA,
            since=AT,
            branch=None,
            note=None,
        ),
        story_ids=["STORY::A::X", "STORY::A::Y"],
        items=[
            StoryTaskItem(
                task_id="TASK::X1", title="One", status="pending", ready=True, complexity="low", progress=None
            ),
            StoryTaskItem(
                task_id="TASK::X2", title="Two", status="in_progress", ready=False, complexity="high", progress="done"
            ),
        ],
        blockers=[StoryRefItem(story_id="STORY::B", title="B", state="waiting", holder=STRANGER)],
        findings=[
            FindingItem(
                finding_id="FND::1",
                kind="question",
                text="t" * 100,
                blocking=True,
                about=["FEAT::X"],
                author=ME,
            ),
            FindingItem(finding_id="FND::2", kind="note", text="a | b", blocking=False, about=[], author=ANA),
        ],
        unblocks=["STORY::C", "STORY::D", "STORY::E"],
    )
    assert tables.render_preview(result).splitlines() == [
        "| Part | Id | Title | State | Holder | Detail |",
        "|---|---|---|---|---|---|",
        (
            "| story | STORY::A::X | X | in_progress | Ana (Claude Code · laptop) since 2026-11-11T10:02:00Z"
            " | feature · unblocks 3 |"
        ),
        "| unit | STORY::A::Y |  |  |  |  |",
        "| task 1 | TASK::X1 | One | pending · ready |  | complexity low |",
        "| task 2 | TASK::X2 | Two | in_progress |  | complexity high · done |",
        "| blocker | STORY::B | B | waiting | another member |  |",
        f"| finding | FND::1 | {'t' * 80} | blocking | you | question |",
        "| finding | FND::2 | a \\| b | open | Ana (Claude Code · laptop) | note |",
    ]


def test_an_unheld_story_row_has_no_holder_and_no_since() -> None:
    header = StoryHeader(
        story_id="S",
        title="T",
        kind="core",
        area_id="A",
        summary=None,
        release=None,
        state="ready",
        holder=None,
        since=None,
        branch=None,
        note=None,
    )
    result = StoryPreview(
        project_id=PID, story=header, story_ids=["S"], items=[], blockers=[], findings=[], unblocks=[]
    )
    assert tables.render_preview(result).splitlines()[2] == "| story | S | T | ready |  | core · unblocks 0 |"


def _title_cell(title: str) -> str:
    """The Story cell of a one-row open list whose story has ``title``."""

    result = StoryList(
        project_id=PID, area_id=None, view="open", total_count=1, items=[story(title=title)], next_cursor=None
    )
    rows = tables.render_stories(result).splitlines()
    assert len(rows) == 3  # header, separator, the one row: the title never breaks the row
    return rows[2]


@pytest.mark.parametrize(
    ("title", "cell"),
    [
        ("Pay\r\nlater", "Pay later"),
        ("a\rb", "a b"),
        ("a\nb", "a b"),
        ("a\tb", "a b"),
        ("a\x00b", "a b"),
        ("a\x1fb", "a b"),
        ("a\x7fb", "a b"),
        ("a\u0085b", "a b"),
        ("a\u2028b", "a b"),
        ("a\u2029b", "a b"),
    ],
)
def test_a_line_break_or_control_character_in_a_cell_is_one_space(title: str, cell: str) -> None:
    assert f"| {cell} |" in _title_cell(title)


@pytest.mark.parametrize(
    ("title", "cell"),
    [
        ("a\\|b", "a\\\\\\|b"),  # the backslash is escaped first, then the pipe: no pipe is left unescaped
        ("ends\\", "ends\\\\"),
        ("a\\b", "a\\\\b"),
        ("`code` | x", "`code` \\| x"),  # backticks stay as they are
    ],
)
def test_a_backslash_is_escaped_before_the_pipe_and_backticks_stay(title: str, cell: str) -> None:
    assert f"| STORY::A::API | {cell} | feature |" in _title_cell(title)
