"""The words of the Jira gap channel: the ADF the posts are made of, and the answers read from a person's comment.

Every text Jira shows is fixed here or comes from the project's own gap (title, question, options); a person's comment
is read only to find the answer in it and is never kept, logged or quoted back. Jira's ADF is built here by hand: the
builders of ``jira.adf`` are for the plan sync.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from jira.adf_reader import adf_to_markdown
from jira.markdown_safe import MarkupTooDeepError

Node = dict[str, Any]

SUMMARY_MAX: Final = 240  # Jira refuses a summary over 255 characters
TEXT_MAX: Final = 1500
OWN_MAX: Final = 4000  # an own answer, as on the web card
NUMBERED: Final = re.compile(r"^\s*(\d{1,3})\s*[:.)]\s*(.*)$")
HINT_LEAD: Final = "Not recorded: this comment is not from a member of the project with a connected Atlassian account."
HOW_TO_LINK: Final = (
    "To answer from Jira, join the project and connect your Atlassian account under Integrations in Graphico; then "
    "comment again."
)


@dataclass(frozen=True)
class Question:
    """One question as a sub-task or a numbered section shows it: ``options`` are ``(letter, label, detail,
    recommended)``."""

    number: int
    title: str
    question: str
    why: str
    options: Sequence[tuple[str, str, str, bool]]


def cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _p(*parts: Node | str) -> Node:
    return {"type": "paragraph", "content": [{"type": "text", "text": p} if isinstance(p, str) else p for p in parts]}


def _bold(text: str) -> Node:
    return {"type": "text", "text": text, "marks": [{"type": "strong"}]}


def _doc(*blocks: Node) -> Node:
    return {"type": "doc", "version": 1, "content": list(blocks)}


def _options(options: Sequence[tuple[str, str, str, bool]]) -> list[Node]:
    if not options:
        return []
    items = []
    for letter, label, detail, recommended in options:
        line = f"{cut(label, 200)}{' (recommended)' if recommended else ''}"
        parts: list[Node | str] = [_bold(f"{letter}) "), line]
        if detail:
            parts.append(f" - {cut(detail, 300)}")
        items.append({"type": "listItem", "content": [_p(*parts)]})
    return [{"type": "bulletList", "content": items}]


def _question(item: Question, *, numbered: bool) -> list[Node]:
    head = f"{item.number}. {cut(item.title, 200)}" if numbered else cut(item.title, 200)
    blocks = [_p(_bold(head)), _p(cut(item.question, TEXT_MAX))]
    if item.why.strip():
        blocks.append(_p("Why it matters: ", cut(item.why, TEXT_MAX)))
    return [*blocks, *_options(item.options)]


def answer_help(*, numbered: bool) -> Node:
    if numbered:
        return _p(
            "Answer in a comment, one line per question: the number, a colon, then an option letter or your own words "
            "(for example 1: B)."
        )
    return _p("Answer in a comment: one option letter, or your own words.")


def story_doc(project_name: str) -> Node:
    return _doc(
        _p(f"Open questions of the project {cut(project_name, 150)}, posted by Graphico."),
        _p("Each sub-task asks what the project's documents leave open. Answer in the sub-task's comments."),
    )


def story_summary(project_name: str) -> str:
    return f"Open questions · {cut(project_name, SUMMARY_MAX - 20)}"


def subtask_doc(items: Sequence[Question], *, numbered: bool) -> Node:
    blocks = [block for item in items for block in _question(item, numbered=numbered)]
    return _doc(*blocks, answer_help(numbered=numbered))


def area_summary(title: str) -> str:
    return f"{cut(title, SUMMARY_MAX - 20)} · open questions"


def more_questions_doc(items: Sequence[Question]) -> Node:
    blocks = [_p(_bold("New questions"))]
    blocks += [block for item in items for block in _question(item, numbered=True)]
    return _doc(*blocks, answer_help(numbered=True))


def _mention(account_id: str) -> Node:
    return {"type": "mention", "attrs": {"id": account_id}}


def recorded_doc(account_id: str, lines: Sequence[str]) -> Node:
    """The confirmation: one line per question recorded or refused (fixed words and the option's own label)."""

    bullets = [{"type": "listItem", "content": [_p(line)]} for line in lines]
    return _doc(
        _p(_mention(account_id), " ", "thank you. This is what was done:"), {"type": "bulletList", "content": bullets}
    )


def refused_doc(account_id: str, text: str) -> Node:
    return _doc(_p(_mention(account_id), f" {text}"))


def hint_doc(account_id: str) -> Node:
    return _doc(_p(_mention(account_id), f" {HINT_LEAD}"), _p(HOW_TO_LINK))


def mentions(doc: Any) -> set[str]:
    """The account ids an ADF document mentions (walked with a stack: a deep document cannot overflow it)."""

    found: set[str] = set()
    pending = [doc]
    while pending:
        node = pending.pop()
        if not isinstance(node, Mapping):
            continue
        attrs = node.get("attrs")
        if node.get("type") == "mention" and isinstance(attrs, Mapping) and isinstance(attrs.get("id"), str):
            found.add(attrs["id"])
        children = node.get("content")
        if isinstance(children, list):
            pending.extend(children)
    return found


def is_hint_for(doc: Any, account_id: str) -> bool:
    if account_id not in mentions(doc):
        return False
    try:
        return HINT_LEAD in adf_to_markdown(doc)
    except MarkupTooDeepError:  # nested too deep to read: not a hint
        return False


def comment_text(doc: Any) -> str:
    """The comment as plain Markdown, for reading an answer; never stored. A comment nested too deep to read is no
    answer: empty."""

    try:
        return adf_to_markdown(doc).strip()
    except MarkupTooDeepError:
        return ""


def numbered_answers(text: str) -> dict[int, str]:
    """``{number: answer}`` from lines like ``2: B`` or ``3) own words``; a line without a number continues the answer
    above it; text before the first number is not an answer. A number given twice keeps its first answer."""

    found: dict[int, list[str]] = {}
    current: int | None = None
    for line in text.splitlines():
        match = NUMBERED.match(line)
        if match is not None:
            number = int(match.group(1))
            current = None if number in found else number
            if current is not None:
                found[current] = [match.group(2)]
        elif current is not None and line.strip():
            found[current].append(line.strip())
    return {number: " ".join(" ".join(parts).split()) for number, parts in found.items() if " ".join(parts).strip()}
