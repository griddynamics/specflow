"""Loading of the ``.sql`` files that ship inside this adapter.

A query file holds one statement under ``--`` header comments; the text handed to the driver is the file's
non-comment lines, stripped and joined with newlines, without the closing ``;``. A DDL file holds several
statements, each ended by ``;`` (a ``$$`` quoted function body may hold ``;``). Like the canonical Cypher
files, every query file is a complete statement as it stands; a part only known at run time (a hop count,
a node match) is a slot ``/*__NAME__*/default/*__NAME__*/`` the dialect fills.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

ADAPTER_ROOT: Final = Path(__file__).resolve().parent
CORE_SQL_DIRECTORY: Final = ADAPTER_ROOT / "queries" / "core"
BUILD_SQL_DIRECTORY: Final = ADAPTER_ROOT / "queries" / "build"
DDL_DIRECTORY: Final = ADAPTER_ROOT / "ddl"
COMMENT_PREFIX: Final = "--"
STATEMENT_END: Final = ";"
DOLLAR_QUOTE: Final = "$$"


def _statement_lines(path: Path) -> list[str]:
    lines = (line.rstrip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line.strip() and not line.strip().startswith(COMMENT_PREFIX)]


@cache
def read_sql_file(directory: Path, name: str) -> str:
    """The statement of ``directory/name`` without comment lines and the closing ``;``."""

    return "\n".join(_statement_lines(directory / name)).strip().removesuffix(STATEMENT_END).strip()


def load_core_sql(name: str) -> str:
    """The text of the core query file ``name`` (relative to ``queries/core/``)."""

    return read_sql_file(CORE_SQL_DIRECTORY, name)


def load_build_sql(name: str) -> str:
    """The text of the build query file ``name`` (relative to ``queries/build/``)."""

    return read_sql_file(BUILD_SQL_DIRECTORY, name)


def load_ddl_sql(name: str) -> str:
    """The whole text of the DDL file ``name`` (relative to ``ddl/``), comment lines left out."""

    return "\n".join(_statement_lines(DDL_DIRECTORY / name))


def split_statements(text: str) -> tuple[str, ...]:
    """The statements of ``text``, split at ``;`` outside ``$$`` quotes (blank ones dropped)."""

    statements: list[str] = []
    current: list[str] = []
    quoted = False
    index = 0
    while index < len(text):
        if text.startswith(DOLLAR_QUOTE, index):
            quoted = not quoted
            current.append(DOLLAR_QUOTE)
            index += len(DOLLAR_QUOTE)
            continue
        char = text[index]
        if char == STATEMENT_END and not quoted:
            statements.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    statements.append("".join(current).strip())
    return tuple(statement for statement in statements if statement)
