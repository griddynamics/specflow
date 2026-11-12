"""The Markdown tables of ``list_areas``, ``list_stories`` and ``preview_story`` (tools.md, Tables): the text block
of those tools, rendered from the structured content, so the agent relays one table and the model need not parse JSON.

Pure functions of the result models, standard library only besides the models. A header row and one row per item; a
``\\`` in a cell is written ``\\\\`` and a ``|`` ``\\|``, a line break or other control character becomes a space
(backticks stay), an empty value is an empty cell; one line after the table when more items exist; no other prose.
Names, titles and texts are data and pass through as they are.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from agent_contracts import (
    QUESTION_CHARS,
    AreaList,
    Holder,
    StoryList,
    StoryPreview,
)

# A CRLF pair is one break; every other control character (C0, DEL, NEL) and the Unicode line and paragraph separators
# become one space each, so a cell stays on one line.
_BREAKS: Final = re.compile("\r\n|[\x00-\x1f\x7f\u0085\u2028\u2029]")
_ANOTHER_MEMBER: Final = "another member"
_SEPARATOR: Final = " · "


def _cell(value: object) -> str:
    if value is None:
        return ""
    return _BREAKS.sub(" ", str(value)).replace("\\", "\\\\").replace("|", "\\|")


def _row(cells: Sequence[object]) -> str:
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = [_row(header), "|" + "|".join("---" for _ in header) + "|", *(_row(row) for row in rows)]
    return "\n".join(lines)


def _time(value: datetime | None) -> str:
    return "" if value is None else value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def holder_text(holder: Holder | None) -> str:
    """``you``, else the name, else ``another member``, then `` (<agent_label>)`` when a label is set."""

    if holder is None:
        return ""
    text = "you" if holder.you else (holder.name or _ANOTHER_MEMBER)
    return f"{text} ({holder.agent_label})" if holder.agent_label else text


def render_areas(result: AreaList) -> str:
    rows = []
    for item in result.items:
        holders = ", ".join(holder_text(holder) for holder in item.in_progress.holders)
        progress = f"{item.in_progress.count}{_SEPARATOR}{holders}" if holders else str(item.in_progress.count)
        rows.append(
            [
                item.area_id,
                item.title,
                item.kind,
                item.ready,
                progress,
                item.waiting,
                item.blocked,
                item.done,
                item.open_tasks,
            ]
        )
    header = ["Id", "Area", "Kind", "Ready", "In progress", "Waiting", "Blocked", "Done", "Open tasks"]
    return _table(header, rows)


_OPEN_HEADER: Final = [
    "#",
    "Id",
    "Story",
    "Kind",
    "State",
    "Holder",
    "Waits on",
    "Tasks",
    "Unblocks",
    "Findings",
    "Question",
]
_DONE_HEADER: Final = ["#", "Id", "Story", "Kind", "Tasks", "PR", "Commit", "Completed by", "Completed at"]
_COMMIT_CHARS: Final = 12


def render_stories(result: StoryList) -> str:
    if result.view == "done":
        header = _DONE_HEADER
        rows = [
            [
                item.rank,
                item.story_id,
                item.title,
                item.kind,
                item.tasks,
                item.pr_url,
                (item.merge_commit or "")[:_COMMIT_CHARS],
                holder_text(item.completed_by),
                _time(item.completed_at),
            ]
            for item in result.items
        ]
    else:
        header = _OPEN_HEADER
        rows = [
            [
                item.rank,
                item.story_id,
                item.title,
                item.kind,
                item.state,
                holder_text(item.holder),
                "; ".join(item.waits_on),
                item.tasks,
                item.unblocks,
                item.findings,
                item.question,
            ]
            for item in result.items
        ]
    table = _table(header, rows)
    if result.next_cursor is None:
        return table
    return f"{table}\n{len(result.items)} of {result.total_count}{_SEPARATOR}next_cursor {result.next_cursor}"


def render_preview(result: StoryPreview) -> str:
    story = result.story
    holder = holder_text(story.holder)
    held = f"{holder} since {_time(story.since)}" if holder and story.since is not None else holder
    rows: list[list[object]] = [
        [
            "story",
            story.story_id,
            story.title,
            story.state,
            held,
            f"{story.kind}{_SEPARATOR}unblocks {len(result.unblocks)}",
        ]
    ]
    # the unit lists ids only: the title and state of its other stories are not part of the answer
    rows.extend(["unit", other, "", "", "", ""] for other in result.story_ids if other != story.story_id)
    for number, task in enumerate(result.items, start=1):
        status = f"{task.status}{_SEPARATOR}ready" if task.ready else task.status
        detail = f"complexity {task.complexity}" + (f"{_SEPARATOR}{task.progress}" if task.progress else "")
        rows.append([f"task {number}", task.task_id, task.title, status, "", detail])
    rows.extend(
        ["blocker", blocker.story_id, blocker.title, blocker.state, holder_text(blocker.holder), ""]
        for blocker in result.blockers
    )
    rows.extend(
        [
            "finding",
            finding.finding_id,
            finding.text[:QUESTION_CHARS],
            "blocking" if finding.blocking else "open",
            holder_text(finding.author),
            finding.kind,
        ]
        for finding in result.findings
    )
    return _table(["Part", "Id", "Title", "State", "Holder", "Detail"], rows)
