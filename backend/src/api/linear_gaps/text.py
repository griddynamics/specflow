"""The words of the Linear gap channel: the Markdown the posts are made of, and the marks that tell the channel's own
comments from a person's.

Every text Linear shows is fixed here or comes from the project's own gap (title, question, options); a person's
comment is read only to find the answer in it and is never kept, logged or quoted back. Every comment the channel
makes starts with ``BOT_PREFIX``: the channel acts as the member the project's Linear is connected by, so the author
of a comment does not tell it from that member's own answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from api.jira_gaps.text import OWN_MAX, cut, numbered_answers

__all__ = [
    "BOT_PREFIX",
    "LABEL_NAME",
    "OWN_MAX",
    "Question",
    "cut",
    "hint_comment",
    "is_own",
    "issue_description",
    "issue_title",
    "more_questions_comment",
    "numbered_answers",
    "parent_description",
    "parent_title",
    "recorded_comment",
]

TITLE_MAX: Final = 240  # Linear refuses a title over 255 characters
TEXT_MAX: Final = 1500
BOT_PREFIX: Final = "Graphico: "
HINT_LEAD: Final = "Not recorded: this comment is not from a member of the project with a connected Linear account."
HOW_TO_LINK: Final = (
    "To answer from Linear, join the project and connect your Linear account under Integrations in Graphico; then "
    "comment again."
)
LABEL_NAME: Final = "graphico-gap"


@dataclass(frozen=True)
class Question:
    """One question as an issue or a numbered section shows it: ``options`` are ``(letter, label, detail,
    recommended)``."""

    number: int
    title: str
    question: str
    why: str
    options: Sequence[tuple[str, str, str, bool]]


def is_own(body: str) -> bool:
    """Whether a comment is one of the channel's (its confirmations, hints and numbered follow-up questions)."""

    return body.lstrip().startswith(BOT_PREFIX.strip())


def _options(options: Sequence[tuple[str, str, str, bool]]) -> list[str]:
    lines = []
    for letter, label, detail, recommended in options:
        line = f"- **{letter})** {cut(label, 200)}{' (recommended)' if recommended else ''}"
        lines.append(f"{line} - {cut(detail, 300)}" if detail else line)
    return lines


def _question(item: Question, *, numbered: bool) -> str:
    head = f"{item.number}. {cut(item.title, 200)}" if numbered else cut(item.title, 200)
    parts = [f"**{head}**", cut(item.question, TEXT_MAX)]
    if item.why.strip():
        parts.append(f"Why it matters: {cut(item.why, TEXT_MAX)}")
    if item.options:
        parts.append("\n".join(_options(item.options)))
    return "\n\n".join(parts)


def answer_help(*, numbered: bool) -> str:
    if numbered:
        return (
            "Reply in a comment, one line per question: the number, a colon, then a letter or your own answer "
            "(for example 1: B)."
        )
    return "Reply in a comment with a letter or your own answer."


def parent_title(project_name: str) -> str:
    return f"Open questions · {cut(project_name, TITLE_MAX - 20)}"


def parent_description(project_name: str) -> str:
    return (
        f"Open questions of the project {cut(project_name, 150)}, posted by Graphico.\n\n"
        "Each sub-issue asks what the project's documents leave open. Reply in the sub-issue's comments."
    )


def issue_title(title: str, *, numbered: bool) -> str:
    return f"{cut(title, TITLE_MAX - 20)} · open questions" if numbered else cut(title, TITLE_MAX)


def issue_description(items: Sequence[Question], *, numbered: bool) -> str:
    return "\n\n".join([*(_question(item, numbered=numbered) for item in items), answer_help(numbered=numbered)])


def more_questions_comment(items: Sequence[Question]) -> str:
    blocks = [f"{BOT_PREFIX}**New questions**", *(_question(item, numbered=True) for item in items)]
    return "\n\n".join([*blocks, answer_help(numbered=True)])


def recorded_comment(lines: Sequence[str]) -> str:
    """The confirmation: one line per question recorded or refused (fixed words and the option's own label)."""

    return "\n".join([f"{BOT_PREFIX}thank you. This is what was done:", *(f"- {line}" for line in lines)])


def hint_comment() -> str:
    return f"{BOT_PREFIX}{HINT_LEAD}\n\n{HOW_TO_LINK}"
