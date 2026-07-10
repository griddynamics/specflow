"""The ``_Table`` schema dataclass describing a SQLite physical table."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

DOC_ID = "doc_id"
"""Canonical primary-key column name for top-level (non-child) tables."""


@dataclass(frozen=True)
class _Table:
    """A SQLite table.

    ``primary_key`` is one column for a document store (``doc_id``) or several for a
    child table (``generation_id, workspace_id``). ``columns`` are the scalar fields
    promoted out of the JSON ``data`` blob. ``indexes`` are the promoted-column
    combinations worth a SQL index. ``parent`` links a child table to its owner
    (cascade-clear + subdocument addressing).
    """

    name: str
    primary_key: tuple[str, ...]
    columns: Dict[str, str] = field(default_factory=dict)
    indexes: tuple[tuple[str, ...], ...] = ()
    parent: Optional[str] = None
