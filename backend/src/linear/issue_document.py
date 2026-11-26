"""Linear issues and project documents as Markdown documents (the Linear project importer stores them). The text of an
issue lives in the document only; nothing here logs.

Personal data never reaches a document: a mention is written as the display name (Linear's mention tags and profile
links can carry a user id or a handle), and every e-mail address is removed from the text, the stored original too. The
output is deterministic (no read time in it), so the same issue read twice has the same bytes and the writer's content
hash finds the second copy."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Final

from linear.models import Comment, Document, Issue

EMAIL_REMOVED: Final = "[email removed]"
SOMEONE: Final = "Someone"
PRIORITIES: Final = {1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}

_UUID: Final = r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
_MENTION_TAG: Final = re.compile(r"<(mention|user)\b[^>]*?(?:/>|>(.*?)</\1>)", re.DOTALL)
_PROFILE_LINK: Final = re.compile(r"\[([^\]]*)\]\(\s*https?://linear\.app/[^)\s]*/profiles/[^)\s]*\s*\)")
_PROFILE_URL: Final = re.compile(r"https?://linear\.app/[^\s)>\]]*/profiles/[^\s)>\]/?#]+")
_MENTION_ID: Final = re.compile(rf"@{_UUID}")
_MAILTO_LINK: Final = re.compile(r"\[([^\]]*)\]\(\s*mailto:[^)]*\)", re.IGNORECASE)
_EMAIL: Final = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def clean_text(text: str | None) -> str:
    """``text`` with the mentions as display names and no e-mail address."""

    if not text:
        return ""
    found = _MENTION_TAG.sub(lambda m: _handle(m.group(2)), text)
    found = _PROFILE_LINK.sub(lambda m: _handle(m.group(1)), found)
    found = _PROFILE_URL.sub("@someone", found)
    found = _MENTION_ID.sub("@someone", found)
    found = _MAILTO_LINK.sub(lambda m: m.group(1), found)
    return _EMAIL.sub(EMAIL_REMOVED, found)


def _handle(name: str | None) -> str:
    shown = (name or "").strip().lstrip("@").strip()
    return f"@{shown}" if shown else "@someone"


def issue_title(issue: Issue) -> str:
    """``ENG-12 Title``, the heading of the document and the name of its item."""

    return clean_text(f"{issue.identifier} {issue.title}".strip())


def issue_markdown(issue: Issue, parent: Issue | None, comments: Sequence[Comment]) -> str:
    """The issue as Markdown: heading, its facts, the description, then the comments."""

    facts = [
        f"State: {issue.state.name}",
        f"Labels: {', '.join(label.name for label in issue.labels)}" if issue.labels else "",
        f"Priority: {PRIORITIES[issue.priority]}" if issue.priority in PRIORITIES else "",
        f"Estimate: {issue.estimate}" if issue.estimate is not None else "",
        f"Assignee: {clean_text(issue.assignee_name)}" if issue.assignee_name else "",
    ]
    blocks = [
        f"# {issue_title(issue)}",
        " | ".join(fact for fact in facts if fact),
        f"Parent: {issue_title(parent)}" if parent is not None else "",
        f"URL: {issue.url}",
        clean_text(issue.description),
    ]
    if comments:
        blocks.append("## Comments")
        blocks += [_comment(comment) for comment in comments]
    return "\n\n".join(block for block in blocks if block)


def _comment(comment: Comment) -> str:
    head = f"**{clean_text(comment.user_name) or SOMEONE}** ({comment.created_at.date().isoformat()})"
    body = clean_text(comment.body)
    return f"{head}:\n\n{body}" if body else head


def issue_original(issue: Issue, parent: Issue | None, comments: Sequence[Comment]) -> bytes:
    """The stored original: the issue and its comments as JSON (the text cleaned as in the Markdown)."""

    body: dict[str, Any] = {
        "issue": {
            "identifier": issue.identifier,
            "title": clean_text(issue.title),
            "url": issue.url,
            "state": issue.state.name,
            "labels": [label.name for label in issue.labels],
            "priority": issue.priority,
            "estimate": issue.estimate,
            "assignee": clean_text(issue.assignee_name) or None,
            "parent": None if parent is None else parent.identifier,
            "description": clean_text(issue.description),
        },
        "comments": [
            {
                "author": clean_text(comment.user_name) or None,
                "created": comment.created_at.isoformat(),
                "body": clean_text(comment.body),
            }
            for comment in comments
        ],
    }
    return _dump(body)


def document_title(document: Document) -> str:
    return clean_text(document.title).strip() or "Untitled document"


def document_markdown(document: Document) -> str:
    """A project document: its title as the heading, then its content."""

    return "\n\n".join(block for block in (f"# {document_title(document)}", clean_text(document.content)) if block)


def document_original(document: Document) -> bytes:
    return _dump(
        {
            "document": {
                "id": document.id,
                "title": clean_text(document.title),
                "url": document.url,
                "content": clean_text(document.content),
            }
        }
    )


def _dump(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
