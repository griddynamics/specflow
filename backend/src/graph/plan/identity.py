"""Stable ids and uuids of the plan graph."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence

from graph.core.identity import NAMESPACE, slug

AREA_PREFIX = "AREA::"
STORY_PREFIX = "STORY::"
TASK_PREFIX = "TASK::"
PROJECT_AREA_ID = "AREA::PROJECT"
CHAPTER_PREFIX = slug("Chapter")[:2] + "_"
FIRST_REVISION = 2
ACTION_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")
REVISION_PATTERN = re.compile(r"_R(\d+)$")
REVISION_ID_SEPARATOR = "_" + "R"


def plan_uuid(project_id: str, external_id: str) -> str:
    """The storage uuid of a plan node, stable for its external id."""

    return str(uuid.uuid5(NAMESPACE, f"{project_id}|plan|{external_id}"))


def area_id(anchor_spec_id: str, *, taken: Sequence[str] = ()) -> str:
    """The area id of an anchor's spec id, with a collision suffix when needed."""

    value = f"{AREA_PREFIX}{anchor_spec_id.split('::', 1)[-1]}" if anchor_spec_id else PROJECT_AREA_ID
    return _unique(value, taken)


def story_id(area_id: str, name: str, *, taken: Sequence[str] = ()) -> str:
    """A story id under an area, with a collision suffix when needed."""

    value = f"{STORY_PREFIX}{_area_slug(area_id)}::{slug(name)}"
    return _unique(value, taken)


def chapter_slug(document: str, heading: str) -> str:
    """The stable project-area story slug of one document chapter."""

    value = f"{CHAPTER_PREFIX}{slug(document)}_{slug(heading)}"[:60].rstrip("_")
    return value or f"{CHAPTER_PREFIX}{slug('')}"


def task_key(area_id: str, action: str) -> str:
    """A task id from an area and a valid action."""

    if not ACTION_PATTERN.fullmatch(action) or REVISION_PATTERN.search(action):
        raise ValueError("invalid task action")
    return f"{TASK_PREFIX}{_area_slug(area_id)}::{action}"


def revision_id(key: str, revision: int) -> str:
    """The revision id of a task, keeping the base id unchanged."""

    if revision < FIRST_REVISION:
        raise ValueError("task revisions start at 2")
    return f"{key}{REVISION_ID_SEPARATOR}{revision}"


def _area_slug(value: str) -> str:
    return value.removeprefix(AREA_PREFIX).removeprefix("::")


def _unique(value: str, taken: Sequence[str]) -> str:
    if value not in taken:
        return value
    suffix = 2
    while f"{value}_{suffix}" in taken:
        suffix += 1
    return f"{value}_{suffix}"
