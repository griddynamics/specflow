"""One Jira issue and its comments as a Markdown document (the importers store it; ``jira.adf_reader`` reads the
descriptions and comments). The text of the issue lives in the document only; nothing here logs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from jira.adf_reader import adf_to_markdown


def issue_title(issue: Mapping[str, Any]) -> str:
    """``KEY: summary``, the heading of the document and the name of its item."""

    summary = str((issue.get("fields") or {}).get("summary") or "").strip()
    return f"{issue.get('key', '')}: {summary}".rstrip(": ")


def issue_markdown(issue: Mapping[str, Any], comments: Sequence[Mapping[str, Any]]) -> str:
    """The issue as Markdown: heading, a line of type, status and labels, the description, then the comments."""

    fields = issue.get("fields") or {}
    facts = [
        f"Type: {(fields.get('issuetype') or {}).get('name')}" if (fields.get("issuetype") or {}).get("name") else "",
        f"Status: {(fields.get('status') or {}).get('name')}" if (fields.get("status") or {}).get("name") else "",
        f"Labels: {', '.join(fields['labels'])}" if fields.get("labels") else "",
    ]
    blocks = [
        f"# {issue_title(issue)}",
        " | ".join(fact for fact in facts if fact),
        adf_to_markdown(fields.get("description")),
    ]
    if comments:
        blocks.append("## Comments")
        blocks += [_comment(comment) for comment in comments]
    return "\n\n".join(block for block in blocks if block)


def _comment(comment: Mapping[str, Any]) -> str:
    author = str((comment.get("author") or {}).get("displayName") or "Someone")
    day = str(comment.get("created") or "")[:10]
    head = f"**{author}**" + (f" ({day})" if day else "")
    body = adf_to_markdown(comment.get("body"))
    return f"{head}:\n\n{body}" if body else head


def issue_original(issue: Mapping[str, Any], comments: Sequence[Mapping[str, Any]]) -> bytes:
    """The stored original: the issue and its comments as JSON."""

    return json.dumps({"issue": issue, "comments": list(comments)}, sort_keys=True, ensure_ascii=False).encode()
