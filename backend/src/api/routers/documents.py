"""Helpers of the project-scoped routes: the membership check and the filename rule.

The documents routes themselves are gone (the batch routes of ``api.routers.source_batches`` own uploads); the
membership check ``require_project`` and the ``filename`` query rule ``clean_filename`` predate them and serve every
project-scoped router. ``db.repositories`` owns the rows.
"""

from __future__ import annotations

import unicodedata
import uuid
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import Query

from api.errors import ApiError, project_not_found
from api.runtime import Runtime
from db import repositories as repo

FILENAME_MAX = 255

# Control, format (bidi overrides, zero-width), line and paragraph separators.
FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

FilenameQuery = Annotated[str | None, Query(max_length=FILENAME_MAX * 4)]


async def require_project(runtime: Runtime, user_id: uuid.UUID, pid: uuid.UUID) -> None:
    async with runtime.transaction() as session:
        if await repo.get_member_project(session, user_id, pid) is None:
            raise project_not_found()


def clean_filename(raw: str | None) -> str:
    """The base name of the submitted filename, else ``missing_filename`` / 422."""

    path = (raw or "").replace("\\", "/")
    name = PurePosixPath(path).name.strip()
    if path.rsplit("/", 1)[-1].strip() == ".":
        name = "."  # PurePosixPath drops a trailing "."; "." is never a filename
    if not name:
        raise ApiError(400, "missing_filename", "The filename query parameter is required.")
    if (
        len(name) > FILENAME_MAX
        or name in {".", ".."}
        or any(unicodedata.category(char) in FORBIDDEN_CATEGORIES for char in name)
    ):
        raise ApiError(
            422,
            "validation_failed",
            "The request is invalid.",
            details={"errors": [{"loc": ["query", "filename"], "type": "invalid_filename"}]},
        )
    return name
