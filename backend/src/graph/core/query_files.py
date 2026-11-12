"""Loading of the ``.cypher`` query files that ship inside the ``graph`` package."""

from __future__ import annotations

import re
from functools import cache
from os import PathLike
from pathlib import Path
from typing import Final

from graph.core.errors import ConformanceError

GRAPH_ROOT: Final = Path(__file__).resolve().parents[1]
# The canonical query and DDL files; every store whose dialect is Cypher reads them from here.
CORE_QUERY_DIRECTORY: Final = GRAPH_ROOT / "core" / "queries"
BUILD_QUERY_DIRECTORY: Final = GRAPH_ROOT / "build" / "queries"
PLAN_QUERY_DIRECTORY: Final = GRAPH_ROOT / "plan" / "queries"
GAP_QUERY_DIRECTORY: Final = GRAPH_ROOT / "gap" / "queries"
CORE_DDL_DIRECTORY: Final = GRAPH_ROOT / "core" / "ddl"
COMMENT_PREFIX: Final = "//"
STATEMENT_END: Final = ";"
_UNFILLED_TOKEN: Final = re.compile(r"__[A-Z][A-Z_]*__")
_SLOT: Final = re.compile(r"/\*__(?P<name>[A-Z][A-Z_]*)__\*/.*?/\*__(?P=name)__\*/", re.DOTALL)


def _statement_lines(path: PathLike[str] | Path) -> list[str]:
    lines = (line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith(COMMENT_PREFIX)]


@cache
def read_query_file(directory: Path, name: str) -> str:
    """The statement of ``directory/name``: its non-comment lines, stripped and joined with spaces."""

    return " ".join(_statement_lines(directory / name))


@cache
def read_ddl_file(path: PathLike[str]) -> tuple[str, ...]:
    """The statements of the DDL file ``path``, in file order: its non-comment lines split at ``;``,
    each statement's lines stripped and joined with spaces (blank statements dropped)."""

    body = " ".join(_statement_lines(path))
    return tuple(part.strip() for part in body.split(STATEMENT_END) if part.strip())


def load_core_query(name: str) -> str:
    """The text of the core query file ``name`` (relative to ``core/queries/``)."""

    return read_query_file(CORE_QUERY_DIRECTORY, name)


def load_build_query(name: str) -> str:
    """The text of the build query file ``name`` (relative to ``build/queries/``)."""

    return read_query_file(BUILD_QUERY_DIRECTORY, name)


def load_plan_query(name: str) -> str:
    """The text of the plan query file ``name`` (relative to ``plan/queries/``)."""

    return read_query_file(PLAN_QUERY_DIRECTORY, name)


def load_gap_query(name: str) -> str:
    """The text of the gap query file ``name`` (relative to ``gap/queries/``)."""

    return read_query_file(GAP_QUERY_DIRECTORY, name)


def fragment(template: str, name: str) -> str:
    """The default text of the slot ``name`` of ``template``: a fragment file holds the pieces a renderer puts
    together (an arrow, a separator, one hop of a path) as the defaults of its slots, so no query text is written
    in code. Raises ``ConformanceError("query_fragment_missing")`` when the file has no such slot."""

    marker = f"/*__{name.upper()}__*/"
    start = template.find(marker)
    end = template.find(marker, start + len(marker)) if start >= 0 else -1
    if start < 0 or end < 0:
        raise ConformanceError("query_fragment_missing", fragment=name)
    return template[start + len(marker) : end]


def render_query(template: str, **names: str) -> str:
    """``template`` with each ``/*__<NAME>__*/default/*__<NAME>__*/`` slot, then each ``__<NAME>__``
    token, replaced by the (already validated) value; a slot or token left unfilled raises
    ``ConformanceError("cypher_template_unfilled")``."""

    values = {key.upper(): value for key, value in names.items()}

    def fill(slot: re.Match[str]) -> str:
        return values.get(slot["name"], slot[0])

    text = _SLOT.sub(fill, template)
    for key, value in names.items():
        text = text.replace(f"__{key.upper()}__", value)
    if _UNFILLED_TOKEN.search(text) is not None:
        raise ConformanceError("cypher_template_unfilled")
    return text
