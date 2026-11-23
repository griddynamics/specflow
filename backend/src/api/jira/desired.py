"""The Jira issues a plan wants, with content, links and rank orders (docs/jira-sync/sync.md#mapping). Pure."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from api.jira import content as content_rules
from graph.plan.dag import topological_order
from graph.plan.invariants import CLOSED
from graph.plan.models import CurrentTask
from graph.plan.planned import base_key
from graph.plan.reader import LoadedPlan
from jira.reasons import Reason

Kind = Literal["epic", "story", "subtask"]
EPIC: Final = "EPIC"
POINTS: Final = {"low": 1, "medium": 3, "high": 5}
_IMPLEMENTS: Final = "IMPLEMENTS"
_KIND_RANK: Final = {"epic": 0, "story": 1, "subtask": 2}


@dataclass(frozen=True)
class Row:
    """A map row as the engine reads it."""

    node_id: str
    kind: str
    jira_key: str | None
    requested_at: datetime
    content: Mapping[str, Any] | None
    content_hash: str | None
    position: int | None
    removed_at: datetime | None
    error_code: str | None
    synced_at: datetime | None


@dataclass(frozen=True)
class SpecRef:
    """The current id and the name of a spec node."""

    id: str
    name: str


@dataclass(frozen=True)
class Item:
    """One wanted issue: its content without the keys that depend on other issues (``content`` adds them)."""

    node_id: str
    kind: Kind
    parent: str | None
    values: Mapping[str, Any]
    blocked_by: tuple[str, ...]


@dataclass(frozen=True)
class Desired:
    """``items`` in creation order: the Epic, then each story followed by its sub-tasks; ``orders`` per rank scope (the
    parent node id); the three id lists are sorted."""

    items: dict[str, Item]
    orders: dict[str, tuple[str, ...]]
    removed: tuple[str, ...]
    revived: tuple[str, ...]
    deleted: frozenset[str]


def is_deleted(row: Row) -> bool:
    return row.removed_at is not None and row.error_code == Reason.NOT_FOUND.value


def content(item: Item, keys: Mapping[str, str], epic_key: str | None, *, linked: bool = True) -> dict[str, Any]:
    """The canonical content of ``item``; ``blocked_by`` keeps the nodes that have a key (none when not ``linked``)."""

    parent = (epic_key or keys.get(EPIC)) if item.parent == EPIC else keys.get(item.parent or "")
    blocked = sorted(node for node in item.blocked_by if node in keys) if linked else []
    return {**item.values, "parent": parent, "blocked_by": blocked}


def spec_ids(plan: LoadedPlan) -> list[str]:
    """Every spec id the issue descriptions can name."""

    stories = {story.story_id for story in plan.current.stories}
    found = {link.target for link in plan.current.links if link.type == _IMPLEMENTS and link.source in stories}
    return sorted(found.union(*(task.implements for task in plan.current.tasks)))


def _ordered(ids: Iterable[str], edges: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    ids = set(ids)
    ordered = topological_order(ids, edges)
    return (*ordered, *sorted(ids - set(ordered)))


def _representatives(plan: LoadedPlan) -> dict[str, CurrentTask]:
    found: dict[str, CurrentTask] = {}
    for task in plan.current.tasks:
        base = base_key(task.task_id)
        if base not in found or task.revision > found[base].revision:
            found[base] = task
    return {base: task for base, task in found.items() if task.status not in CLOSED}


def _labels(area_id: str) -> list[str]:
    area = content_rules.area_label(area_id)
    return sorted([content_rules.OWNED_LABEL, area] if area else [content_rules.OWNED_LABEL])


def _specs(spec_refs: Iterable[str], names: Mapping[str, SpecRef]) -> list[dict[str, str]]:
    refs = {ref.id: ref for ref in (names.get(spec, SpecRef(spec, spec)) for spec in spec_refs)}
    return [{"id": ref.id, "name": ref.name} for _, ref in sorted(refs.items())]


def _values(**given: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "v": content_rules.MAPPING_VERSION,
        "text": None,
        "release": None,
        "phase": None,
        "done_when": [],
        "specs": [],
        "task_id": None,
        "points": None,
    }
    return base | given


def build(
    plan: LoadedPlan, *, project_name: str, epic_key: str | None, rows: Mapping[str, Row], names: Mapping[str, SpecRef]
) -> Desired:
    """What Jira should hold for ``plan``; ``rows`` decide removed, revived and deleted-in-Jira nodes."""

    deleted = frozenset(node for node, row in rows.items() if is_deleted(row))
    live = _representatives(plan)
    stories = {story.story_id: story for story in plan.current.stories if story.status == "active"}
    tasks = {base: task for base, task in live.items() if task.story_id in stories}
    skipped = {base for base, task in tasks.items() if base in deleted or task.story_id in deleted}
    wanted_stories = [story_id for story_id in stories if story_id not in deleted]
    wanted_tasks = {base: task for base, task in tasks.items() if base not in skipped}

    story_edges = {
        (task.story_id, live[base_key(wait)].story_id)
        for task in live.values()
        for wait in plan.waits.get(task.task_id, ())
        if base_key(wait) in live
    }
    story_edges = {edge for edge in story_edges if edge[0] != edge[1] and {*edge} <= set(wanted_stories)}
    task_edges = {
        (base, base_key(wait))
        for base, task in wanted_tasks.items()
        for wait in plan.waits.get(task.task_id, ())
        if base_key(wait) in wanted_tasks and base_key(wait) != base
    }
    story_blocked = {story_id: tuple(sorted(p for w, p in story_edges if w == story_id)) for story_id in wanted_stories}
    task_blocked = {base: tuple(sorted(p for w, p in task_edges if w == base)) for base in wanted_tasks}

    points: dict[str, int] = {}
    for task in tasks.values():
        points[task.story_id] = points.get(task.story_id, 0) + POINTS[task.complexity]
    implemented: dict[str, list[str]] = {}
    for link in plan.current.links:
        if link.type == _IMPLEMENTS and link.source in stories:
            implemented.setdefault(link.source, []).append(link.target)

    orders = {EPIC: _ordered(wanted_stories, story_edges)}
    for story_id in wanted_stories:
        in_story = [base for base, task in wanted_tasks.items() if task.story_id == story_id]
        orders[story_id] = _ordered(in_story, [edge for edge in task_edges if edge[0] in in_story])

    items: dict[str, Item] = {}
    if epic_key is None:
        values = _values(summary=content_rules.summary(project_name, EPIC), labels=[content_rules.OWNED_LABEL])
        items[EPIC] = Item(EPIC, "epic", None, values, ())
    for story_id in orders[EPIC]:  # a story, then its sub-tasks: each story is whole in Jira before the next starts
        story = stories[story_id]
        values = _values(
            summary=content_rules.summary(story.title, story_id),
            text=content_rules.text(story.summary),
            release=story.release,
            specs=_specs(implemented.get(story_id, ()), names),
            points=points.get(story_id) or None,
            labels=_labels(story.area_id),
        )
        items[story_id] = Item(story_id, "story", EPIC, values, story_blocked[story_id])
        for base in orders[story_id]:
            task = wanted_tasks[base]
            values = _values(
                summary=content_rules.summary(task.title, base),
                text=content_rules.text(task.description),
                phase=task.phase,
                done_when=[entry.text for entry in task.done_when if entry.text.strip()],
                specs=_specs(task.implements, names),
                task_id=task.task_id,
                points=POINTS[task.complexity],
                labels=_labels(task.area_id),
            )
            items[base] = Item(base, "subtask", story_id, values, task_blocked[base])

    mapped = {node: row for node, row in rows.items() if row.jira_key and node not in deleted}
    removed = [
        node for node, row in mapped.items() if row.removed_at is None and node not in items and node not in skipped
    ]
    revived = [node for node, row in mapped.items() if row.removed_at is not None and node in items]

    def order(node: str) -> tuple[int, str]:
        return _KIND_RANK[rows[node].kind], node

    return Desired(items, orders, tuple(sorted(removed, key=order)), tuple(sorted(revived, key=order)), deleted)
