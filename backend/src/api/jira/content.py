"""Canonical content, field limits and the change list of a comment (docs/jira-sync/sync.md)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from jira.adf import CommentItem

MAPPING_VERSION: Final = 1
SUMMARY_LIMIT: Final = 255
TEXT_LIMIT: Final = 30_000
AREA_LIMIT: Final = 200
OWNED_LABEL: Final = "specflow"
REMOVED_LABEL: Final = "specflow-removed"
AREA_PREFIX: Final = "area-"
CHANGED, REMOVED, REVIVED = "changed", "removed", "revived"
_WHITESPACE: Final = re.compile(r"\s+")
_NOT_SLUG: Final = re.compile(r"[^a-z0-9]+")
_ELLIPSIS: Final = "…"


def _cut(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + _ELLIPSIS


def summary(value: str, fallback: str = "") -> str:
    """The one-line summary; ``fallback`` (the node id) stands in for a blank title, which Jira refuses."""

    return _cut(_WHITESPACE.sub(" ", value).strip() or fallback, SUMMARY_LIMIT)


def text(value: str | None) -> str | None:
    return _cut(value, TEXT_LIMIT) if value and value.strip() else None


def area_label(area_id: str) -> str | None:
    slug = _NOT_SLUG.sub("-", area_id.removeprefix("AREA::").lower()).strip("-")[:AREA_LIMIT].rstrip("-")
    return AREA_PREFIX + slug if slug else None


def lead(node_id: str, event: str, status: str | None = None) -> str:
    head = f"Specflow plan update for {node_id}."
    if event == REMOVED:
        return f"{head} This item was removed from the Specflow plan."
    if event == REVIVED:
        return f"{head} This item is back in the Specflow plan."
    return f'{head} This issue is in status "{status}", so Specflow left its fields as they are.'


def _none(value: object) -> str:
    return "none" if value is None or value == "" else str(value)


def _moved(label: str, old: object, new: object) -> list[CommentItem]:
    return [f"{label}: {_none(old)} → {_none(new)}"] if old != new else []


def _added_removed(label: str, old: list[str], new: list[str]) -> list[CommentItem]:
    found: list[CommentItem] = [f"{label}: added {item}" for item in new if item not in old]
    found += [f"{label}: removed {item}" for item in old if item not in new]
    return found


def _quoted(values: list[str] | None) -> list[str]:
    return [f'"{value}"' for value in values or []]


def _specs(content: Mapping[str, Any]) -> list[str]:
    return [f"{spec['name']} ({spec['id']})" for spec in content.get("specs") or []]


def changes(
    old: Mapping[str, Any] | None, new: Mapping[str, Any], keys: Mapping[str, str] | None = None
) -> list[CommentItem]:
    """The comment items for ``new`` against the stored ``old`` (none stored = an empty baseline), in spec order."""

    before, jira_keys = old or {}, keys or {}
    items: list[CommentItem] = []
    if before.get("summary") != new["summary"]:
        items.append(f'Summary: "{before.get("summary") or ""}" → "{new["summary"]}"')
    if before.get("text") != new["text"]:
        items.append(
            ("Description changed. New text:", new["text"]) if new["text"] else "Description changed. New text: none"
        )
    items += _added_removed("Done when", _quoted(before.get("done_when")), _quoted(new["done_when"]))
    items += _moved("Release", before.get("release"), new["release"]) + _moved(
        "Phase", before.get("phase"), new["phase"]
    )
    items += _added_removed("Implements", _specs(before), _specs(new))
    items += _moved("Task id", before.get("task_id"), new["task_id"])
    items += _moved("Story points", before.get("points"), new["points"])
    items += _added_removed("Label", before.get("labels") or [], new["labels"])
    items += _moved("Parent", before.get("parent"), new["parent"])
    old_blocked = [jira_keys.get(node, node) for node in before.get("blocked_by") or []]
    new_blocked = [jira_keys.get(node, node) for node in new["blocked_by"]]
    return items + _added_removed("Blocked by", old_blocked, new_blocked)
