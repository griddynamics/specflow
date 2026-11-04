"""The statement files of the ArcadeDB adapter: Cypher under ``overrides/`` and ArcadeDB SQL in ``sql/commands.json``.

No statement text is a string constant in the adapter's modules: each is a file, read once and filled with
validated names (``__NAME__`` tokens, as the canonical query files do). The ArcadeDB SQL of the schema steps is
a JSON table of commands (sent over the HTTP admin API): it is not PostgreSQL, so it is not a ``.sql`` file the
SQL track would try to parse.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Final

from graph.core.query_files import read_query_file, render_query

ROOT: Final = Path(__file__).resolve().parent
OVERRIDE_DIRECTORY: Final = ROOT / "overrides"
SQL_DIRECTORY: Final = ROOT / "sql"


@cache
def cypher_file(group: str, name: str) -> str:
    """The Cypher statement of ``overrides/<group>/<name>`` (comment lines left out)."""

    return read_query_file(OVERRIDE_DIRECTORY / group, name)


def cypher(group: str, name: str, **names: str) -> str:
    """The Cypher statement of an override file with its ``__NAME__`` tokens filled."""

    return render_query(cypher_file(group, name), **names) if names else cypher_file(group, name)


@cache
def _sql_commands() -> dict[str, str]:
    return {
        name: text
        for name, text in json.loads((SQL_DIRECTORY / "commands.json").read_text(encoding="utf-8")).items()
        if not name.startswith("_")
    }


def sql(name: str, **names: str) -> str:
    """The ArcadeDB SQL command ``name`` of ``sql/commands.json`` with its ``__NAME__`` tokens filled (values
    are validated names or integers the caller built; never user text)."""

    text = _sql_commands()[name]
    return render_query(text, **names) if names else text
