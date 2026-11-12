"""The source lines behind one spec node (api.md "Read and search", ``GraphReader.evidence``).

A fragment's text holds its lines ``line_start`` to ``line_end`` (``text.split("\\n")``, as the build reads them), so a
document's lines are rebuilt from its fragments: the quote from the node's own fragment, the context lines from the
document's ``active`` fragments, never past the document. Document text is data: nothing here logs it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict

MAX_CONTEXT_LINES: Final = 5
ACTIVE: Final = "active"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class EvidenceNode(_Frozen):
    spec_id: str
    label: str
    name: str


class EvidenceItem(_Frozen):
    """One evidence fragment of a node: its lines, the quote (those lines) with context, and the other nodes whose
    evidence lines overlap them."""

    document_id: str | None
    path: str | None
    fragment_id: str
    line_start: int | None
    line_end: int | None
    quote: str
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    others: tuple[EvidenceNode, ...] = ()


class FragmentLines(_Frozen):
    """What a fragment says about the lines of its document."""

    fragment_id: str
    status: str | None = None
    text: str = ""
    line_start: int | None = None
    line_end: int | None = None


def fragment_lines(fragment: FragmentLines) -> dict[int, str]:
    """The fragment's lines by line number (none when it has no line range)."""

    if fragment.line_start is None:
        return {}
    return {fragment.line_start + index: line for index, line in enumerate(fragment.text.split("\n"))}


def document_lines(fragments: Iterable[FragmentLines]) -> dict[int, str]:
    """The lines of a document from its ``active`` fragments."""

    lines: dict[int, str] = {}
    for fragment in fragments:
        if fragment.status == ACTIVE:
            lines.update(fragment_lines(fragment))
    return lines


def quote_lines(
    own: Mapping[int, str], document: Mapping[int, str], line_start: int, line_end: int, context_lines: int
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """The quote (lines ``line_start`` to ``line_end`` of the node's fragment ``own``, the document's where the
    fragment lacks one; None when no line of the range is known) and up to ``context_lines`` document lines before
    and after it."""

    numbers = range(line_start, line_end + 1)
    known = any(number in own or number in document for number in numbers)
    quote = "\n".join(own.get(number, document.get(number, "")) for number in numbers) if known else None
    before = tuple(
        document[number] for number in range(max(1, line_start - context_lines), line_start) if number in document
    )
    following = range(line_end + 1, line_end + context_lines + 1)
    after = tuple(document[number] for number in following if number in document)
    return quote, before, after
