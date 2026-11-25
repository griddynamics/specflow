"""What a Linear issue of the plan holds: the canonical content of an item, its Markdown description, the estimate and
the comment that lists a change (plans/LINEAR/LINEAR-PLAN.md section 3). Pure. The items come from the shared builder
``api.jira.desired.build``; this module only turns them into Linear's shapes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from api.jira import content as jira_content
from api.jira.desired import Item
from graph.plan.identity import REVISION_PATTERN

OWNED_LABEL: Final = "graphico"
REMOVED_LABEL: Final = "graphico-removed"
AREA_GROUP: Final = "Graphico area"  # the single-select group of the area labels
LINK_TITLE: Final = "Open in Graphico"
CHANGED, REMOVED, REVIVED = jira_content.CHANGED, jira_content.REMOVED, jira_content.REVIVED
_SHARED_OWNED: Final = jira_content.OWNED_LABEL  # the label the shared builder puts on every item
_NOTE: Final = (
    "> [!NOTE]\n"
    "> Managed by Graphico. While this issue is in Triage, Backlog or Todo the plan's changes overwrite it; "
    "once work has started they arrive as comments."
)
_KIND_WORD: Final = {"story": "Story", "subtask": "Task"}


@dataclass(frozen=True)
class Rendered:
    """What an issue create or overwrite carries."""

    title: str
    description: str
    estimate: int | None
    label_names: tuple[str, ...]  # the owned label first, then the area label (if any)
    release: str | None


def area_of(labels: Iterable[str]) -> str | None:
    """The area slug of the item's labels (``area-payments`` is ``payments``)."""

    return next((name.removeprefix(jira_content.AREA_PREFIX) for name in labels if name.startswith("area-")), None)


def canonical(item: Item) -> dict[str, Any]:
    """The content a pass stores and hashes for ``item``: the builder's values with Graphico's own owned label, the
    kind and the parent's node id. No Linear key is in it, so it is known before any issue exists; the relations are
    kept apart (``blocked_by`` in the stored row is what was linked, not part of this hash)."""

    values = dict(item.values)
    labels = [OWNED_LABEL if name == _SHARED_OWNED else name for name in values.get("labels") or []]
    values["labels"] = sorted(dict.fromkeys(labels))
    values["kind"] = item.kind
    values["parent"] = item.parent if item.kind == "subtask" else None
    return values


def revision_of(task_id: str | None) -> int:
    found = REVISION_PATTERN.search(task_id or "")
    return int(found.group(1)) if found else 1


def _checklist(done_when: Sequence[str]) -> list[str]:
    return ["### Done when", *(f"- [ ] {entry}" for entry in done_when)] if done_when else []


def _specs(specs: Sequence[Mapping[str, str]]) -> list[str]:
    return ["**Specs:** " + ", ".join(f"{spec['name']} ({spec['id']})" for spec in specs)] if specs else []


def _facts(values: Mapping[str, Any], node_id: str) -> list[str]:
    area = area_of(values.get("labels") or [])
    revision = revision_of(values.get("task_id")) if values["kind"] == "subtask" else None
    rows = [
        ("Node", f"`{node_id}`"),
        ("Kind", _KIND_WORD[values["kind"]]),
        ("Area", area),
        ("Phase", values.get("phase")),
        ("Revision", revision),
        ("Release", values.get("release")),
        ("Points", values.get("points")),
    ]
    return [">>> Graphico", *(f"- {name}: {value}" for name, value in rows if value not in (None, "")), ">>>"]


def render(values: Mapping[str, Any], node_id: str) -> Rendered:
    """The issue of canonical ``values``: title, Markdown description, estimate (the points) and label names."""

    blocks: list[list[str]] = [
        [values["text"]] if values.get("text") else [],
        [_NOTE],
        _checklist([str(entry) for entry in values.get("done_when") or []]),
        _specs(values.get("specs") or []),
        _facts(values, node_id),
    ]
    description = "\n\n".join("\n".join(block) for block in blocks if block)
    labels = tuple(values.get("labels") or ())
    ordered = (OWNED_LABEL, *(name for name in labels if name != OWNED_LABEL))
    return Rendered(str(values["summary"]), description, values.get("points"), ordered, values.get("release"))


def lead(node_id: str, event: str, state_name: str | None = None) -> str:
    head = f"Graphico plan update for `{node_id}`."
    if event == REMOVED:
        return f"{head} This item was removed from the Graphico plan."
    if event == REVIVED:
        return f"{head} This item is back in the Graphico plan."
    return f'{head} This issue is in state "{state_name}", so Graphico left its fields as they are.'


def change_items(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> list[str]:
    """The Markdown list lines of what differs between the stored ``old`` and ``new`` (the wording of the Jira
    comment, ``api.jira.content.changes``); a description change quotes the new text."""

    before = None if old is None else {**old, "blocked_by": []}
    lines: list[str] = []
    for item in jira_content.changes(before, {**new, "blocked_by": []}):
        if isinstance(item, str):
            lines.append(f"- {item}")
            continue
        label, text = item
        quoted = "\n".join(f"  > {line}" if line else "  >" for line in text.splitlines())
        lines.append(f"- {label}\n\n{quoted}")
    return lines


def comment(node_id: str, event: str, items: Sequence[str] = (), state_name: str | None = None) -> str:
    """One comment: the lead, then the change list."""

    parts = [lead(node_id, event, state_name)]
    if items:
        parts.append("\n".join(items))
    return "\n\n".join(parts)
