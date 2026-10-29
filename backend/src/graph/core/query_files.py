"""Loading of the ``.cypher`` query files that ship inside the ``graph`` package.

A query file holds one statement under a ``//`` header comment. The text handed to the driver is the
file's statement lines joined with single spaces, so a query reads the same whether it is kept in one
line or broken at its clauses. A file for a query whose names are only known at run time (relationship
types, labels, hop counts) holds ``__NAME__`` tokens, filled by :func:`render_query`.

Every query file is a complete, valid statement as it stands, so the Cypher checks parse and plan it
without rendering. A ``__NAME__`` token stands where Cypher takes a name (a label, a relationship type,
a property key, a variable), and the graph schema declares it as a placeholder. Any other run-time part
(an arrow's direction, a hop count, a match fragment, a list of UNION branches, a whole pattern) is a
slot: ``/*__NAME__*/default/*__NAME__*/``. The default between the two markers is the text one valid
instance of the query has there; the factory replaces the whole slot, markers included, with the value.

A schema DDL file (:func:`read_ddl_file`) holds several statements, each ended by ``;``; each is
joined the same way, since Neo4j takes one schema statement per call.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Final

from graph.core.errors import ConformanceError

COMMENT_PREFIX: Final = "//"
STATEMENT_END: Final = ";"
_UNFILLED_TOKEN: Final = re.compile(r"__[A-Z][A-Z_]*__")
_SLOT: Final = re.compile(r"/\*__(?P<name>[A-Z][A-Z_]*)__\*/.*?/\*__(?P=name)__\*/", re.DOTALL)


def _statement_lines(path: Path) -> list[str]:
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith(COMMENT_PREFIX)]


@cache
def read_query_file(directory: Path, name: str) -> str:
    """The statement of ``directory/name``: its non-comment lines, stripped and joined with spaces."""

    return " ".join(_statement_lines(directory / name))


@cache
def read_ddl_file(path: Path) -> tuple[str, ...]:
    """The statements of the DDL file ``path``, in file order: its non-comment lines split at ``;``,
    each statement's lines stripped and joined with spaces (blank statements dropped)."""

    body = " ".join(_statement_lines(path))
    return tuple(part.strip() for part in body.split(STATEMENT_END) if part.strip())


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
