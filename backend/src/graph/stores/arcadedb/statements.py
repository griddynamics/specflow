"""The statement files of the ArcadeDB adapter: Cypher under ``overrides/`` and ArcadeDB SQL under ``sql/``.

No statement text is a string constant in the adapter's modules: each is a file, read once and filled with
validated names (``__NAME__`` tokens, as the canonical query files do). SQL here is ArcadeDB's SQL, sent over
the HTTP admin API; it is not PostgreSQL and no SQL track checks it.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

from graph.core.query_files import read_query_file, render_query

ROOT: Final = Path(__file__).resolve().parent
OVERRIDE_DIRECTORY: Final = ROOT / "overrides"
SQL_DIRECTORY: Final = ROOT / "sql"
COMMENT_PREFIX: Final = "--"


@cache
def cypher_file(group: str, name: str) -> str:
    """The Cypher statement of ``overrides/<group>/<name>`` (comment lines left out)."""

    return read_query_file(OVERRIDE_DIRECTORY / group, name)


def cypher(group: str, name: str, **names: str) -> str:
    """The Cypher statement of an override file with its ``__NAME__`` tokens filled."""

    return render_query(cypher_file(group, name), **names) if names else cypher_file(group, name)


@cache
def _sql_file(name: str) -> str:
    lines = (line.strip() for line in (SQL_DIRECTORY / name).read_text(encoding="utf-8").splitlines())
    return " ".join(line for line in lines if line and not line.startswith(COMMENT_PREFIX))


def sql(name: str, **names: str) -> str:
    """The ArcadeDB SQL command of ``sql/<name>`` with its ``__NAME__`` tokens filled (values are validated
    names or integers the caller built; never user text)."""

    return render_query(_sql_file(name), **names) if names else _sql_file(name)
