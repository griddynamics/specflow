"""Answers of ``graph.gap``: the checks of an answer and its edits, placement, the answer marker, amendment rows, the
edits preview, the ``gap_edits`` draft of an own answer and the graph writes of a clarification (docs/graph/gap/
answers.md "Answering", "Edits", "Amendments", "Applying answers"; FR-GANS-01..04, FR-GANS-08, FR-GANS-09).

``GapService`` calls these helpers; nothing here opens a transaction of its own except through the writer it is
handed. An answer is checked against what the graph holds now: the gap (its ``CITES`` lines and its ``ABOUT`` nodes)
and the project's active documents and fragments (``AnswerGraph``, read once per call through the core reader). The
checks of an edit are those of answers.md "Edits": the fragment is active in the project and the anchor quote is one
of its lines (normalised whitespace and quote marks), a ``replace`` targets a line the gap cites or a line of a node it
is ``ABOUT``, the text is 1 to 2000 characters with no heading line, at most one edit per anchor line; a
clarifications-document edit is an ``append_section`` under a heading. A failed check is an ``AnswerInvalidError``
(``field`` ``edits``, ``reason`` the check) and nothing is written.

Every amended text ends with the answer marker naming the gap (``> Clarified by GAP::...``). Amendment rows are written
``pending`` with the answer (staged) and become ``active`` when the answers are applied (``apply_now``, "Resolve gaps").
Nothing here logs a question, an answer, a quote or document text (NFR-GSEC-02).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from graph.core.errors import GraphError
from graph.core.llm.gateway import untrusted
from graph.core.ontology import Ontology
from graph.core.reader import GraphDocument, GraphFragment, GraphNode
from graph.core.writer import DOCUMENT, NodeRef, OwnedWriter
from graph.gap.errors import AnswerInvalidError
from graph.gap.follow_ups import reconcile_reason
from graph.gap.gaps import StoredGap, answer_key, clarification_uuid, gap_uuid
from graph.gap.identity import normalise_quote, sha256_hex
from graph.gap.models import AmendmentRow, Edit, EditTarget, OptionRow, SectionPreview
from graph.gap.names import ANSWERS, CLARIFICATION, GAP, PLACED_IN, REPLACES, EditPlacement
from graph.gap.prompts import EDITS_PROMPT, prompt_text
from graph.gap.steps.check import anchor_line
from graph.gap.steps.investigate import WireEdit
from graph.gap.steps.snapshot import GraphSnapshot, heading_path_of

ANSWER_TEXT_CHARS: Final = (1, 4000)
EDIT_TEXT_CHARS: Final = (1, 2000)
REASON_CHARS: Final = (1, 500)
EDITS_MAX: Final = 12
DRAFT_EDITS_MAX: Final = 4
DRAFT_FRAGMENTS_MAX: Final = 6
EDITS_CALL_CLASS: Final = "extraction"
EDITS_ROLE: Final = "gap_edits"
AMENDMENT_PREFIX: Final = "AMD::"
AMENDMENT_HEX: Final = 12
MARKER: Final = "> Clarified by {gap_id}"
CONTRADICTION: Final = "contradiction"
_HEADING: Final = re.compile(r"^\s{0,3}#", re.MULTILINE)


def marker(gap_id: str) -> str:
    """The answer marker line that ends every amended text (answers.md "Answer marker")."""

    return MARKER.format(gap_id=gap_id)


def marked(text: str, gap_id: str) -> str:
    """``text`` with the answer marker as its last line."""

    return f"{text.rstrip()}\n\n{marker(gap_id)}"


def text_ok(text: str | None, limits: tuple[int, int]) -> bool:
    return text is not None and limits[0] <= len(text.strip()) <= limits[1]


@dataclass(frozen=True)
class AnswerGraph:
    """What the answer checks read of the graph for one gap: the gap, its ``ABOUT`` nodes (for their evidence lines)
    and the project's active documents and fragments, as a snapshot."""

    gap: StoredGap
    snapshot: GraphSnapshot

    @classmethod
    async def read(cls, reader: Any, project_id: str, ontology: Ontology, gap: StoredGap) -> AnswerGraph:
        documents: list[GraphDocument] = await reader.documents(project_id)
        fragments: list[GraphFragment] = await reader.fragments(project_id)
        found: list[GraphNode] = [
            node for spec_id in gap.about_ids if (node := await reader.node(project_id, spec_id)) is not None
        ]
        return cls(gap, GraphSnapshot(ontology, found, (), documents, fragments))

    def fragment(self, fragment_id: str | None) -> GraphFragment | None:
        return self.snapshot.fragments.get(fragment_id or "")

    def lineage(self, edit: Edit) -> str | None:
        fragment = self.fragment(edit.target.fragment_id)
        return None if fragment is None else fragment.document_id


# Checks


def check_edit(graph: AnswerGraph, edit: Edit) -> str | None:
    """The first failed check of answers.md "Edits" for one edit, or None."""

    if not text_ok(edit.text, EDIT_TEXT_CHARS):
        return "edit_text_length"
    if _HEADING.search(edit.text):
        return "edit_text_heading"
    if edit.placement == "document":
        ok = edit.operation == "append_section" and bool((edit.target.heading or "").strip())
        return None if ok else "edit_document_target"
    return _amend_target(graph, edit)


def _amend_target(graph: AnswerGraph, edit: Edit) -> str | None:
    """An amendment names an active fragment of the project and one of its lines; a ``replace`` only a gap line."""

    target = edit.target
    if edit.operation == "append_section":
        return "edit_operation"
    if graph.fragment(target.fragment_id) is None or not target.anchor_quote:
        return "edit_target"
    line = anchor_line(graph.snapshot, str(target.fragment_id), target.anchor_quote)
    if line is None:
        return "edit_anchor"
    if edit.operation == "replace" and not replaceable(graph, str(target.fragment_id), line):
        return "edit_replace_outside_gap"
    return None


def replaceable(graph: AnswerGraph, fragment_id: str, line: str) -> bool:
    """A ``replace`` targets a line the gap cites or a line of a node it is ``ABOUT``."""

    cited = [normalise_quote(cite.quote) for cite in graph.gap.cites if cite.fragment_id == fragment_id]
    if any(quote and (quote in line or line in quote) for quote in cited):
        return True
    for node in graph.snapshot.nodes.values():
        for item in graph.snapshot.lines(node):
            text = normalise_quote(item.text)
            if item.fragment_id == fragment_id and text and (text in line or line in text):
                return True
    return False


def check_edits(graph: AnswerGraph, edits: Sequence[Edit]) -> tuple[Edit, ...]:
    """The edits with their text stripped, or ``AnswerInvalidError(field="edits")`` naming the first failed check."""

    if not 1 <= len(edits) <= EDITS_MAX:
        raise AnswerInvalidError(field="edits", reason="edits_count", gap_id=graph.gap.gap_id)
    anchors: set[tuple[str, str]] = set()
    kept: list[Edit] = []
    for edit in edits:
        failed = check_edit(graph, edit)
        if failed is None and edit.placement == "amend":
            key = (str(edit.target.fragment_id), str(anchor_line(graph.snapshot, *_anchor(edit))))
            failed = "edit_anchor_twice" if key in anchors else None
            anchors.add(key)
        if failed is not None:
            raise AnswerInvalidError(field="edits", reason=failed, gap_id=graph.gap.gap_id)
        kept.append(edit.model_copy(update={"text": edit.text.strip()}))
    return tuple(kept)


def _anchor(edit: Edit) -> tuple[str, str]:
    return str(edit.target.fragment_id), str(edit.target.anchor_quote)


def document_edit(gap: StoredGap, text: str) -> Edit:
    """One clarifications-document section holding ``text``, headed by the gap's title."""

    title = str(gap.props.get("title") or gap.props.get("question") or gap.gap_id)
    return Edit(
        placement="document",
        target=EditTarget(heading=title[:160]),
        operation="append_section",
        text=text.strip(),
    )


def place(
    gap: StoredGap, edits: Sequence[Edit], placement: EditPlacement | None, *, own: bool = False
) -> tuple[tuple[Edit, ...], EditPlacement]:
    """The edits under the asked placement and the clarification's placement (answers.md "Placement rules").

    ``document`` turns amend edits into one clarifications-document section with their texts; ``amend`` needs at
    least one anchored edit. Without an asked placement the clarification is ``amend`` when any edit amends a source
    section, else ``document``. A contradiction amends: with the document placement both lines would stay, so its
    amend edits cannot be switched, and an ``own`` answer to it without an amend edit (an own text without edits too)
    is refused. An option of the gap run keeps the placement of its edits, and an own answer to a reconcile follow-up
    is placed as any own answer (reconcile.md "Conflict follow-up")."""

    amend = [edit for edit in edits if edit.placement == "amend"]
    if gap.props.get("kind") == CONTRADICTION:
        unanchored = own and not amend and reconcile_reason(gap) is None
        if (placement == "document" and amend) or unanchored:
            raise AnswerInvalidError(field="placement", reason="contradiction_amends", gap_id=gap.gap_id)
    if placement == "document" and amend:
        text = "\n\n".join(edit.text.strip() for edit in edits)
        return (document_edit(gap, text),), "document"
    if placement == "amend" and not amend:
        raise AnswerInvalidError(field="placement", reason="no_anchor", gap_id=gap.gap_id)
    return tuple(edits), "amend" if amend else "document"


# Amendments and previews


def amendment_id(clr_id: str, index: int) -> str:
    digest = hashlib.sha256(f"{clr_id}|{index}".encode()).hexdigest()
    return f"{AMENDMENT_PREFIX}{digest[:AMENDMENT_HEX].upper()}"


def amendment_rows(graph: AnswerGraph, clr_id: str, edits: Sequence[Edit], now: datetime) -> tuple[AmendmentRow, ...]:
    """One ``pending`` row per ``amend`` edit (answers.md "Amendments"); the text carries the answer marker."""

    rows: list[AmendmentRow] = []
    for index, edit in enumerate(edits):
        if edit.placement != "amend":
            continue
        fragment = graph.fragment(edit.target.fragment_id)
        line = anchor_line(graph.snapshot, *_anchor(edit))
        if fragment is None or line is None:  # checked before; kept for the type checker
            raise AnswerInvalidError(field="edits", reason="edit_target", gap_id=graph.gap.gap_id)
        document = graph.snapshot.documents.get(fragment.document_id)
        rows.append(
            AmendmentRow(
                amendment_id=amendment_id(clr_id, index),
                clr_id=clr_id,
                lineage=fragment.document_id,
                heading_path=heading_path_of(fragment),
                anchor_text=line,
                anchor_hash=hashlib.sha256(line.encode("utf-8")).hexdigest(),
                operation=edit.operation,
                text=marked(edit.text, graph.gap.gap_id),
                status="pending",
                last_revision_id=None if document is None else document.revision_id,
                created_at=now,
                updated_at=now,
            )
        )
    return tuple(rows)


def edited(text: str, anchor: str, operation: str, new_text: str) -> str:
    """``text`` with ``new_text`` replacing (``replace``) or following (``insert_after``) the line ``anchor``
    names (the first line equal to it, else the first holding it)."""

    lines = text.splitlines()
    wanted = normalise_quote(anchor)
    index = next((i for i, line in enumerate(lines) if normalise_quote(line) == wanted), None)
    if index is None:
        index = next((i for i, line in enumerate(lines) if wanted in normalise_quote(line)), len(lines) - 1)
    added = new_text.splitlines()
    if operation == "replace":
        lines[index : index + 1] = added
    else:
        lines[index + 1 : index + 1] = ["", *added]
    return "\n".join(lines)


def section_preview(graph: AnswerGraph, edit: Edit) -> SectionPreview:
    """The section an edit changes, before and after (the answer marker included)."""

    gap = graph.gap
    if edit.placement == "document":
        question = str(gap.props.get("question") or "")
        after = f"## {edit.target.heading} ({gap.gap_id})\n\n{edit.text}\n\nAnswers: {question}"
        return SectionPreview(heading_path=(str(edit.target.heading),), after=after)
    fragment = graph.fragment(edit.target.fragment_id)
    if fragment is None:
        raise AnswerInvalidError(field="edits", reason="edit_target", gap_id=gap.gap_id)
    return SectionPreview(
        fragment_id=fragment.fragment_id,
        lineage=fragment.document_id,
        heading_path=heading_path_of(fragment),
        before=fragment.text,
        after=edited(fragment.text, str(edit.target.anchor_quote), edit.operation, marked(edit.text, gap.gap_id)),
    )


def option_of(options: Sequence[OptionRow], rank: int, gap_id: str) -> OptionRow:
    found = next((row for row in options if row.rank == rank), None)
    if found is None:
        raise AnswerInvalidError(field="option_rank", reason="unknown", gap_id=gap_id)
    return found


# The gap_edits draft of an own answer


class EditsAnswer(BaseModel):
    """The ``gap_edits`` answer: the edits of an own answer (strict schema, the investigator's edit shape)."""

    model_config = ConfigDict(extra="forbid")

    edits: list[WireEdit]


def draft_fragments(graph: AnswerGraph) -> tuple[GraphFragment, ...]:
    """The sections the draft call sees: the cited fragments, then the evidence fragments of the ``ABOUT`` nodes."""

    ids = [cite.fragment_id for cite in graph.gap.cites]
    for node in graph.snapshot.nodes.values():
        ids += [line.fragment_id for line in graph.snapshot.lines(node)]
    found = [graph.snapshot.fragments[item] for item in dict.fromkeys(ids) if item in graph.snapshot.fragments]
    return tuple(found[:DRAFT_FRAGMENTS_MAX])


def edits_messages(graph: AnswerGraph, text: str, placement: EditPlacement | None) -> list[dict[str, Any]]:
    """The ``gap_edits`` call: ids and the hint as JSON, every text as an untrusted block."""

    gap = graph.gap
    fragments = draft_fragments(graph)
    head = {
        "gap_id": gap.gap_id,
        "kind": gap.props.get("kind"),
        "about": list(gap.about_ids),
        "hint": placement,
        "fragments": [fragment.fragment_id for fragment in fragments],
    }
    blocks = [
        untrusted("question", str(gap.props.get("question") or "")),
        untrusted("why", str(gap.props.get("why") or "")),
    ]
    blocks += [
        untrusted(f"witness {number} {cite.fragment_id}", cite.quote) for number, cite in enumerate(gap.cites, start=1)
    ]
    blocks += [untrusted(f"fragment {fragment.fragment_id}", fragment.text) for fragment in fragments]
    blocks.append(untrusted("answer", text))
    user = "\n\n".join(["Turn this answer into edits (JSON, then data blocks).", json.dumps(head), *blocks])
    return [{"role": "system", "content": prompt_text("edits")}, {"role": "user", "content": user}]


async def draft_edits(
    gateway: Any, graph: AnswerGraph, text: str, placement: EditPlacement | None
) -> tuple[tuple[Edit, ...] | None, str | None]:
    """The checked edits ``gap_edits`` drafts for an own answer, or None and why the text goes to the
    clarifications document as written instead (no gateway, the call failed, or its edits did not check)."""

    if gateway is None:
        return None, "gap_edits_unavailable"
    try:
        answer: EditsAnswer = await gateway.structured(
            EDITS_CALL_CLASS, None, edits_messages(graph, text, placement), EditsAnswer, prompt=EDITS_PROMPT
        )
    except (GraphError, ValidationError) as error:
        return None, error.code if isinstance(error, GraphError) else "gap_edits_invalid"
    edits = tuple(Edit.model_validate(edit.model_dump(mode="json")) for edit in answer.edits[:DRAFT_EDITS_MAX])
    try:
        return place(graph.gap, check_edits(graph, edits), placement, own=True)[0], None
    except AnswerInvalidError as error:
        return None, error.reason


# Graph writes


@dataclass(frozen=True)
class ClarificationWrite:
    """One clarification as ``answer_gap`` writes it."""

    clr_id: str
    gap: StoredGap
    props: Mapping[str, Any]
    lineages: tuple[str, ...]
    replaces: str | None = None


async def write_clarification(tx: OwnedWriter, project_id: str, write: ClarificationWrite) -> None:
    """The clarification node, ``ANSWERS`` to its gap, ``PLACED_IN`` each source document of its amendments and
    ``REPLACES`` the applied clarification it takes the place of, in the caller's transaction."""

    gap = write.gap
    node = NodeRef(CLARIFICATION, clarification_uuid(project_id, write.clr_id))
    await tx.upsert_node(CLARIFICATION, node.key, dict(write.props))
    owner = NodeRef(GAP, gap_uuid(project_id, gap.gap_id))
    await tx.upsert_relationship(ANSWERS, node, owner, answer_key(write.clr_id, ANSWERS, gap.gap_id), {})
    for lineage in write.lineages:
        await tx.upsert_relationship(
            PLACED_IN, node, NodeRef(DOCUMENT, lineage), answer_key(write.clr_id, PLACED_IN, lineage), {}
        )
    if write.replaces is not None:
        old = NodeRef(CLARIFICATION, clarification_uuid(project_id, write.replaces))
        await tx.upsert_relationship(REPLACES, node, old, answer_key(write.clr_id, REPLACES, write.replaces), {})


def clarification_props(
    clr_id: str,
    *,
    mode: str,
    placement: str,
    option_label: str,
    text: str,
    answered_by: str,
    answered_at: datetime,
) -> dict[str, Any]:
    return {
        "clr_id": clr_id,
        "mode": mode,
        "placement": placement,
        "status": "pending",
        "option_label": option_label,
        "text": text,
        "answered_by": answered_by,
        "answered_at": answered_at.isoformat(timespec="milliseconds"),
    }


def noise_rule_id(scope: str, value: str) -> str:
    """``NR::`` and 12 hex digits of the scope and value: the same rule added twice is one rule."""

    return f"NR::{sha256_hex([scope, value])[:AMENDMENT_HEX].upper()}"
