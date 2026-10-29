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
class Listing:
    root: DriveFolder
    folder_count: int  # includes the root
    items: tuple[DriveItem, ...]
    revisited: int


@dataclass(frozen=True)
class CategoryCount:
    category: Category
    processing_class: ProcessingClass
    count: int


def summarize(listing: Listing) -> list[CategoryCount]:
    """Count items per category (unordered; see ``ordered`` for the UI order)."""

    counts = collections.Counter(categorize(i.mime_type) for i in listing.items)
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
