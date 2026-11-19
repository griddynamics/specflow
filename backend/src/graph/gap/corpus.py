"""The corpus side of answers: active amendments applied to a revision's text, the effective revision id and the
generated clarifications document (docs/graph/gap/reconcile.md "Applying amendments", "Effective revision id";
answers.md "Clarifications document"; FR-GCOR-01..03, FR-GANS-06, FR-GANS-07).

The host's corpus reader calls these helpers on every snapshot it hands a build: a stored document never changes, only
the text the build reads does (FR-GCOR-01). Everything here is pure except ``document_clarifications``, one graph read.
Reconcile of a new source revision is not here (reconcile.md, a later slice): an amendment whose anchor line is not in
the revision is left out of the text and returned as unanchored, so the caller can log it.

- ``apply_amendments``: each ``active`` amendment's anchor line is looked up in the revision (normalised like a quote,
  under the heading its ``heading_path`` names when that heading is there, else anywhere); ``replace`` puts the text in
  its place, ``insert_after`` after it (after the text of an earlier amendment on the same line). Order: anchor
  position, then answer time. Headings and the other lines stay as they are.
- ``effective_revision_id``: SHA-256 of the revision id and the sorted (amendment id, text hash) pairs applied; the
  revision id itself when none applied, so a lineage without answers keeps the id it has today (FR-GCOR-02).
- ``clarifications_markdown``: one ``## <title> (GAP::<id>)`` section per clarification, in answer order, with the
  answer text and the line "Answers: <question>"; ``clarifications_lineage`` is the document's stable lineage.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from graph.core.identity import NAMESPACE
from graph.core.ontology import Ontology
from graph.core.store import Executor
from graph.gap.gaps import read_gaps
from graph.gap.identity import canonical_json, normalise_quote
from graph.gap.models import AmendmentRow

ACTIVE: Final = "active"
CLARIFICATIONS_TITLE: Final = "Clarifications"
CLARIFICATIONS_PATH: Final = "Specflow/Clarifications.md"
"""The corpus path of the generated document (a folder name no source of the project can take: sources keep their
own names, and an upload has no folder)."""
APPLY_MARK: Final = "apply_requested_at"
DOCUMENT_PLACEMENT: Final = "document"
_HEADING: Final = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE: Final = re.compile(r"^\s{0,3}(```|~~~)")
_HEADING_CHARS: Final = 160


def clarifications_lineage(project_id: str) -> str:
    """The lineage of a project's clarifications document: ``doc:`` and a uuid5 of the project, stable across reads,
    so every regenerated text is a new revision of the same graph document."""

    return f"doc:{uuid.uuid5(NAMESPACE, f'{project_id}|clarifications')}"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AppliedText:
    """A revision's text with its amendments applied, the amendments applied and those whose anchor was not found."""

    markdown: str
    applied: tuple[AmendmentRow, ...] = ()
    unanchored: tuple[AmendmentRow, ...] = ()


@dataclass(frozen=True)
class _Line:
    index: int
    normalised: str
    headings: tuple[str, ...]


def _lines(markdown: str) -> list[_Line]:
    """Every line with its normalised text and the normalised headings above it (code fences hold no heading)."""

    found: list[_Line] = []
    stack: list[tuple[int, str]] = []
    fenced = False
    for index, line in enumerate(markdown.split("\n")):
        if _FENCE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING.match(line)
        if heading is not None:
            level = len(heading.group(1))
            stack = [item for item in stack if item[0] < level]
            stack.append((level, normalise_quote(heading.group(2)).casefold()))
        found.append(_Line(index, normalise_quote(line), tuple(text for _, text in stack)))
    return found


def _anchor_index(lines: Sequence[_Line], row: AmendmentRow) -> int | None:
    """The line the amendment anchors on: equal to its anchor text (normalised), under its section's heading when that
    heading holds one (the first such line), else the first equal line in the document; None when none is equal."""

    wanted = normalise_quote(row.anchor_text)
    if not wanted:
        return None
    equal = [line for line in lines if line.normalised == wanted]
    if not equal:
        return None
    heading = normalise_quote(row.heading_path[-1]).casefold() if row.heading_path else None
    under = [line for line in equal if heading is not None and line.headings[-1:] == (heading,)]
    return (under or equal)[0].index


def apply_amendments(markdown: str, amendments: Sequence[AmendmentRow]) -> AppliedText:
    """``markdown`` with every ``active`` amendment of ``amendments`` applied (module docstring); other statuses are
    ignored. ``append_section`` rows belong to the clarifications document and are ignored here too."""

    lines = _lines(markdown)
    placed: list[tuple[int, str, AmendmentRow]] = []
    unanchored: list[AmendmentRow] = []
    for row in amendments:
        if row.status != ACTIVE or row.operation not in {"replace", "insert_after"}:
            continue
        index = _anchor_index(lines, row)
        if index is None:
            unanchored.append(row)
        else:
            placed.append((index, row.created_at.isoformat(), row))
    if not placed:
        return AppliedText(markdown, (), tuple(unanchored))
    placed.sort(key=lambda item: (item[0], item[2].operation != "replace", item[1], item[2].amendment_id))
    out = markdown.split("\n")
    by_line: dict[int, list[AmendmentRow]] = {}
    for index, _, row in placed:
        by_line.setdefault(index, []).append(row)
    applied: list[AmendmentRow] = []
    for index in sorted(by_line, reverse=True):  # from the bottom, so earlier indexes stay valid
        current = [out[index]]
        replaced = False
        for row in by_line[index]:
            text = row.text.rstrip("\n").split("\n")
            if row.operation == "replace" and not replaced:
                current = text
                replaced = True
                applied.append(row)
            elif row.operation == "insert_after":
                current = [*current, "", *text]
                applied.append(row)
            else:  # a second replace of one line: an edit check refuses it; never applied twice
                unanchored.append(row)
        out[index : index + 1] = current
    applied.sort(key=lambda row: row.amendment_id)
    return AppliedText("\n".join(out), tuple(applied), tuple(unanchored))


def effective_revision_id(revision_id: str, applied: Sequence[AmendmentRow]) -> str:
    """The revision id the build sees (reconcile.md "Effective revision id"): ``revision_id`` when nothing applied,
    else SHA-256 of it and the sorted (amendment id, text hash) pairs."""

    if not applied:
        return revision_id
    pairs = sorted((row.amendment_id, text_hash(row.text)) for row in applied)
    return text_hash(canonical_json({"revision_id": revision_id, "amendments": pairs}))


@dataclass(frozen=True)
class DocumentClarification:
    """One section of the clarifications document: the gap, its title and question, the answer text and when it was
    answered."""

    gap_id: str
    clr_id: str
    title: str
    question: str
    text: str
    answered_at: str


def section_heading(title: str, gap_id: str) -> str:
    """The heading text of a clarification's section: the gap's title (one line) and its id in brackets."""

    line = " ".join(title.split())[:_HEADING_CHARS] or gap_id
    return f"{line} ({gap_id})"


def clarifications_markdown(entries: Sequence[DocumentClarification]) -> str:
    """The clarifications document (answers.md "Clarifications document"); empty when there is no entry."""

    if not entries:
        return ""
    parts = [f"# {CLARIFICATIONS_TITLE}", ""]
    for entry in sorted(entries, key=lambda item: (item.answered_at, item.gap_id, item.clr_id)):
        body = entry.text.strip()
        question = " ".join(entry.question.split())
        parts += [f"## {section_heading(entry.title, entry.gap_id)}", "", body, "", f"Answers: {question}", ""]
    return "\n".join(parts)


def in_document(props: Any) -> bool:
    """Whether a clarification belongs in the clarifications document: placement ``document`` and ``applied``, or
    ``pending`` once its answers were sent to an update build."""

    if props.get("placement") != DOCUMENT_PLACEMENT:
        return False
    status = props.get("status")
    return status == "applied" or (status == "pending" and bool(props.get(APPLY_MARK)))


async def document_clarifications(
    executor: Executor, project_id: str, ontology: Ontology
) -> tuple[DocumentClarification, ...]:
    """The clarifications of the project's clarifications document, read from the graph (FR-GCOR-03)."""

    gaps = await read_gaps(executor, project_id, ontology, statuses=("answered", "applying", "resolved"))
    return tuple(
        DocumentClarification(
            gap_id=gap.gap_id,
            clr_id=str(props.get("clr_id") or ""),
            title=str(gap.props.get("title") or gap.props.get("question") or ""),
            question=str(gap.props.get("question") or ""),
            text=str(props.get("text") or ""),
            answered_at=str(props.get("answered_at") or ""),
        )
        for gap in gaps
        for props in gap.clarifications
        if in_document(props) and str(props.get("text") or "").strip()
    )
