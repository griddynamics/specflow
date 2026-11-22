"""Atlassian Document Format builders: the description layout and the change comment (sync.md)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

QUOTE_LIMIT: Final = 2000
_BLANK_LINE: Final = re.compile(r"\n\s*\n")

Node = dict[str, Any]
CommentItem = str | tuple[str, str]


def _text(value: str, *marks: str) -> Node:
    node: Node = {"type": "text", "text": value}
    if marks:
        node["marks"] = [{"type": mark} for mark in marks]
    return node


def _paragraph(value: str, *marks: str) -> Node:
    return {"type": "paragraph", "content": [_text(value, *marks)]}


def _paragraphs(value: str | None) -> list[Node]:
    parts = (part.strip() for part in _BLANK_LINE.split(value or ""))
    return [_paragraph(part) for part in parts if part]


def _bullets(items: Sequence[Sequence[Node]]) -> Node:
    return {"type": "bulletList", "content": [{"type": "listItem", "content": list(item)} for item in items]}


def _implements(specs: Sequence[Mapping[str, str]]) -> list[Node]:
    if not specs:
        return []
    heading = {"type": "heading", "attrs": {"level": 3}, "content": [_text("Implements")]}
    return [heading, _bullets([[_paragraph(f"{spec['name']} ({spec['id']})")] for spec in specs])]


def _done_when(items: Sequence[str]) -> list[Node]:
    if not items:
        return []
    tasks = [
        {
            "type": "taskItem",
            "attrs": {"localId": f"done-{index}", "state": "TODO"},
            "content": [_text(item)],
        }
        for index, item in enumerate(items, 1)
    ]
    heading = {"type": "heading", "attrs": {"level": 3}, "content": [_text("Done when")]}
    return [heading, {"type": "taskList", "attrs": {"localId": "done-when"}, "content": tasks}]


def _labelled(label: str, value: str | None) -> list[Node]:
    return [_paragraph(f"{label}: {value}")] if value else []


def description_doc(
    kind: Literal["story", "subtask"], content: Mapping[str, Any], *, story_id: str | None = None
) -> Node:
    """The description of a Story (``story_id`` closes it) or a Sub-task, from the canonical content."""

    blocks = _paragraphs(content.get("text"))
    specs = content.get("specs") or []
    if kind == "story":
        blocks += _labelled("Release", content.get("release")) + _implements(specs)
        if story_id:
            blocks.append({"type": "paragraph", "content": [_text("Specflow: "), _text(story_id, "code")]})
    else:
        blocks += _done_when(content.get("done_when") or []) + _labelled("Phase", content.get("phase"))
        blocks += _implements(specs)
        if content.get("task_id"):
            blocks.append({"type": "paragraph", "content": [_text("Specflow: "), _text(content["task_id"], "code")]})
    return {"type": "doc", "version": 1, "content": blocks}


def _cut(value: str) -> str:
    return value if len(value) <= QUOTE_LIMIT else value[: QUOTE_LIMIT - 1] + "…"


def comment_doc(lead: str, items: Sequence[CommentItem]) -> Node:
    """A lead paragraph and one bullet per change; an item ``(text, quote)`` carries a quote cut at 2000 characters."""

    blocks = [_paragraph(lead)]
    entries: list[list[Node]] = []
    for item in items:
        text, quote = (item, None) if isinstance(item, str) else item
        entry = [_paragraph(text)]
        if quote:
            entry.append({"type": "blockquote", "content": [_paragraph(_cut(quote))]})
        entries.append(entry)
    if entries:
        blocks.append(_bullets(entries))
    return {"type": "doc", "version": 1, "content": blocks}
