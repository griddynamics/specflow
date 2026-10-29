"""Loads the SQL statements that live as ``.sql`` files next to the database code."""

from __future__ import annotations

from pathlib import Path
from typing import Final

QUERY_DIR: Final = Path(__file__).resolve().parent / "queries"


def read_query(name: str) -> str:
    """The text of ``src/db/queries/<name>`` (statements are plain SQL, bind parameters only)."""

    return (QUERY_DIR / name).read_text(encoding="utf-8")
