"""Plain data the gateway returns. Folders are not items; trashed is never listed."""

from __future__ import annotations

import collections
from dataclasses import dataclass

from drive.categories import (
    CLASS_ORDER,
    LABELS,
    Category,
    ProcessingClass,
    categorize,
)


@dataclass(frozen=True)
class DriveFolder:
    id: str
    name: str
    in_shared_drive: bool


@dataclass(frozen=True)
class DriveItem:
    id: str
    name: str
    mime_type: str
    parent_id: str
    size: int | None
    modified_time: str | None
    version: int | None
    md5: str | None


@dataclass(frozen=True)
class FolderNode:
    """A visited folder below the root: its name and the folder it sits in (``parent_id``)."""

    id: str
    name: str
    parent_id: str


@dataclass(frozen=True)
class Listing:
    root: DriveFolder
    folder_count: int  # includes the root
    items: tuple[DriveItem, ...]
    revisited: int
    folders: tuple[FolderNode, ...] = ()  # every visited folder except the root


@dataclass(frozen=True)
class CategoryCount:
    category: Category
    processing_class: ProcessingClass
    count: int


def summarize(listing: Listing) -> list[CategoryCount]:
    """Count items per category (unordered; see ``ordered`` for the UI order)."""

    counts = collections.Counter(categorize(i.mime_type, i.name) for i in listing.items)
    return [CategoryCount(c, k, n) for (c, k), n in counts.items()]


def ordered(counts: list[CategoryCount]) -> list[CategoryCount]:
    """Bullet order of FR-GDUI-0010: class, then count desc, then label A-Z."""

    return sorted(
        counts,
        key=lambda r: (
            CLASS_ORDER.index(r.processing_class),
            -r.count,
            LABELS[r.category],
        ),
    )


def folder_path(listing: Listing, item: DriveItem) -> str:
    """The folder names from the root (excluded) down to the item's parent, joined with ``/``."""

    by_id = {f.id: f for f in listing.folders}
    names: list[str] = []
    current = item.parent_id
    while current != listing.root.id and current in by_id and len(names) < len(by_id):
        node = by_id[current]
        names.append(node.name)
        current = node.parent_id
    return "/".join(reversed(names))
