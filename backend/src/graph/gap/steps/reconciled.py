"""What the close step does with an applied answer that reconcile changed (docs/graph/gap/reconcile.md "Outcomes",
"Conflict follow-up", "Orphaned follow-up"; lifecycle.md "Close check"; FR-GREC-04..06).

The corpus reader stores reconcile's outcomes as amendment statuses; the next gap run reads them here, for every
resolved gap whose current clarification is ``applied`` with placement ``amend``:

- a ``suspended`` amendment: the clarification becomes ``conflict`` and a follow-up gap opens (kind
  ``contradiction``, severity at least ``major``, ``FOLLOWS_UP`` reason ``conflict``, ``ABOUT`` the nodes evidenced in
  the owner's fragment and the old gap's nodes still in the graph, ``CITES`` the owner's line, route ``ask``) with the
  options "Keep the upload" (recommended, no edit) and "Restore the earlier answer" (one ``replace`` of the owner's
  line with the earlier text); a new answer in the person's own words is the card's own-text answer;
- an ``orphaned`` amendment (and none suspended): the clarification becomes ``orphaned`` and a follow-up gap opens
  (the old kind and severity, reason ``orphaned``, ``ABOUT`` the old gap's nodes still in the graph) with "Move it to
  the clarifications document" (recommended), "Drop it" (no edit) and, when another document has a section with the
  same heading, "Place it in another section" (an ``insert_after`` at the end of that section);
- every amendment ``retired``: the clarification becomes ``absorbed`` and is ``GROUNDED_IN`` the fragment that holds
  the answer text, else the fragments under the amendment's heading (adopted by meaning);
- otherwise (applied again, re-anchored, undecided) the answer is re-checked like an applying one: grounded again in
  the fragments that hold it now, and a follow-up ``not_fixed`` opens when the gap's detector fires again.

A clarification keeps ``reconcile_state``, the hash of its amendments' ids, statuses, anchors and revisions as the last
close step saw them; only a clarification whose state changed is looked at again, so a run without a new upload reads
the amendment rows and writes nothing. Follow-ups of this module get fixed options and no site (they are not
investigated); the close step never takes them out as obsolete.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from graph.gap.answers import marker
from graph.gap.gaps import StoredGap
from graph.gap.identity import normalise_quote, sha256_hex
from graph.gap.models import AmendmentRow, Edit, EditTarget, OptionRow
from graph.gap.steps.snapshot import GraphSnapshot, heading_path_of

RECONCILE_STATE: Final = "reconcile_state"
CONFLICT: Final = "conflict"
ORPHANED: Final = "orphaned"
ABSORBED: Final = "absorbed"
RECONCILE_REASONS: Final = frozenset({CONFLICT, ORPHANED})
SEVERITY_ORDER: Final = ("minor", "major", "blocker")
TITLE_CHARS: Final = 160
WHY_CHARS: Final = 2000
QUESTION_CHARS: Final = 1000


def amendments_state(rows: Sequence[AmendmentRow]) -> str:
    """The hash of what reconcile may change in a clarification's amendments."""

    return sha256_hex(
        sorted([row.amendment_id, row.status, row.anchor_hash, row.last_revision_id or ""] for row in rows)
    )


def answer_lines(row: AmendmentRow, gap_id: str) -> str:
    """The text of an amendment without its answer marker line (the earlier answer as it was written)."""

    kept = [line for line in row.text.split("\n") if normalise_quote(line) != marker(gap_id)]
    return "\n".join(kept).strip()


def continues(gap: StoredGap, reasons: frozenset[str] = RECONCILE_REASONS) -> str | None:
    """The gap this gap follows up with one of ``reasons``, or None."""

    return next(
        (item.gap_id for item in gap.follow_ups if item.direction == "continues" and item.reason in reasons), None
    )


def _severity(*values: str) -> str:
    ranked = [value for value in values if value in SEVERITY_ORDER]
    return max(ranked, key=SEVERITY_ORDER.index) if ranked else "major"


def _document_name(snapshot: GraphSnapshot, lineage: str) -> str:
    document = snapshot.documents.get(lineage)
    if document is None:
        return lineage
    return str(getattr(document, "title", None) or getattr(document, "path", None) or lineage)


@dataclass(frozen=True)
class FollowUpSpec:
    """A follow-up gap to write: its reason, the properties that differ from the old gap's, the nodes it is ``ABOUT``
    (spec id to (label, storage key)), its ``CITES`` (fragment id, quote) and its options (rank given on write)."""

    reason: str
    props: Mapping[str, Any]
    about: Mapping[str, tuple[str, str]]
    cites: tuple[tuple[str, str], ...] = ()
    options: tuple[OptionRow, ...] = ()
    keep_cites: bool = False
    keep_about: bool = True


def _old_about(gap: StoredGap, snapshot: GraphSnapshot) -> dict[str, tuple[str, str]]:
    return {
        link.spec_id: (link.label, link.uuid)
        for link in (*gap.about, *gap.impacts)
        if link.spec_id in snapshot.nodes and link.label and link.uuid
    }


def _fragment_of(snapshot: GraphSnapshot, row: AmendmentRow) -> tuple[str, str] | None:
    """The active fragment of the row's lineage holding its (owner's) anchor line, and that line as written."""

    wanted = normalise_quote(row.anchor_text).casefold()
    for fragment in snapshot.fragments.values():
        if fragment.document_id != row.lineage:
            continue
        for line in fragment.text.split("\n"):
            if wanted and normalise_quote(line).casefold() == wanted:
                return fragment.fragment_id, normalise_quote(line)
    return None


def _evidenced_in(snapshot: GraphSnapshot, fragment_id: str) -> dict[str, tuple[str, str]]:
    return {
        spec_id: (node.label, node.uuid)
        for spec_id, node in snapshot.nodes.items()
        if any(item.fragment_id == fragment_id for item in node.provenance)
    }


def _option(gap_id: str, label: str, detail: str, *, recommended: bool, edits: Sequence[Edit] = ()) -> OptionRow:
    return OptionRow(gap_id=gap_id, rank=1, label=label, detail=detail, recommended=recommended, edits=tuple(edits))


def conflict_follow_up(
    gap: StoredGap, clarification: Mapping[str, Any], rows: Sequence[AmendmentRow], snapshot: GraphSnapshot
) -> FollowUpSpec:
    """The follow-up of a clarification whose amendment a newer upload contradicts (reconcile.md "Conflict
    follow-up")."""

    suspended = [row for row in rows if row.status == "suspended"]
    earlier = "\n\n".join(answer_lines(row, gap.gap_id) for row in suspended)
    owners = [(row, _fragment_of(snapshot, row)) for row in suspended]
    about = _old_about(gap, snapshot)
    cites: list[tuple[str, str]] = []
    restore: list[Edit] = []
    for row, found in owners:
        if found is None:
            continue
        fragment_id, line = found
        about |= _evidenced_in(snapshot, fragment_id)
        cites.append((fragment_id, line))
        restore.append(
            Edit(
                placement="amend",
                target=EditTarget(fragment_id=fragment_id, anchor_quote=line),
                operation="replace",
                text=answer_lines(row, gap.gap_id),
            )
        )
    if not restore:  # the owner's line is not in the graph yet: the earlier answer can still be kept beside it
        title = str(gap.props.get("title") or gap.props.get("question") or gap.gap_id)
        restore = [
            Edit(
                placement="document",
                target=EditTarget(heading=title[:TITLE_CHARS]),
                operation="append_section",
                text=earlier,
            )
        ]
    lineage = suspended[0].lineage if suspended else ""
    owner_text = "\n".join(line for _, line in (found for _, found in owners if found is not None))
    answered = f"{clarification.get('answered_by') or 'someone'} on {clarification.get('answered_at') or '?'}"
    why = (
        f"The earlier answer ({answered}) said: {earlier}\n"
        f"The new upload of {_document_name(snapshot, lineage)} says: {owner_text or 'something else'}"
    )
    title = f"Upload changes an answer: {gap.props.get('title') or gap.props.get('question') or gap.gap_id}"
    props = {
        "kind": "contradiction",
        "severity": _severity(str(gap.props.get("severity") or ""), "major"),
        "title": " ".join(title.split())[:TITLE_CHARS],
        "question": (
            f"The new upload of {_document_name(snapshot, lineage)} changes an earlier answer. Which one is right?"
        )[:QUESTION_CHARS],
        "why": why[:WHY_CHARS],
    }
    options = (
        _option(
            gap.gap_id,
            "Keep the upload",
            "The owner's new text stays what the build reads; the earlier answer is kept as history.",
            recommended=True,
        ),
        _option(
            gap.gap_id,
            "Restore the earlier answer",
            "The earlier answer replaces the owner's line again, as an answer beside the document.",
            recommended=False,
            edits=restore,
        ),
    )
    return FollowUpSpec(CONFLICT, props, about, tuple(cites), options, keep_about=False)


def orphaned_follow_up(
    gap: StoredGap, clarification: Mapping[str, Any], rows: Sequence[AmendmentRow], snapshot: GraphSnapshot
) -> FollowUpSpec:
    """The follow-up of a clarification whose section is gone (reconcile.md "Orphaned follow-up")."""

    del clarification
    lost = [row for row in rows if row.status == ORPHANED]
    earlier = "\n\n".join(answer_lines(row, gap.gap_id) for row in lost)
    title = str(gap.props.get("title") or gap.props.get("question") or gap.gap_id)
    options = [
        _option(
            gap.gap_id,
            "Move it to the clarifications document",
            "The answer becomes a section of the clarifications document; the rule stays in the spec.",
            recommended=True,
            edits=[
                Edit(
                    placement="document",
                    target=EditTarget(heading=title[:TITLE_CHARS]),
                    operation="append_section",
                    text=earlier,
                )
            ],
        ),
        _option(
            gap.gap_id,
            "Drop it: the section was removed on purpose",
            "No edit; the next build no longer has the answer's nodes and the plan retires their tasks.",
            recommended=False,
        ),
    ]
    elsewhere = _same_heading_elsewhere(snapshot, lost)
    if elsewhere is not None:
        fragment_id, line, heading = elsewhere
        options.append(
            _option(
                gap.gap_id,
                f"Place it in the section {heading}",
                "The answer is inserted at the end of the section with the same heading in another document.",
                recommended=False,
                edits=[
                    Edit(
                        placement="amend",
                        target=EditTarget(fragment_id=fragment_id, anchor_quote=line),
                        operation="insert_after",
                        text=earlier,
                    )
                ],
            )
        )
    props = {
        "title": " ".join(f"Answer lost its section: {title}".split())[:TITLE_CHARS],
        "question": (
            f"The section that held the answer to {' '.join(title.split())} is gone. Where does the answer go now?"
        )[:QUESTION_CHARS],
        "why": f"The earlier answer: {earlier}"[:WHY_CHARS],
    }
    return FollowUpSpec(ORPHANED, props, _old_about(gap, snapshot), (), tuple(options))


def _same_heading_elsewhere(snapshot: GraphSnapshot, rows: Sequence[AmendmentRow]) -> tuple[str, str, str] | None:
    """An active fragment of another lineage under the lost section's heading: its id, last line and heading."""

    for row in rows:
        if not row.heading_path:
            continue
        heading = normalise_quote(row.heading_path[-1]).casefold()
        for fragment in snapshot.fragments.values():
            path = heading_path_of(fragment)
            if fragment.document_id == row.lineage or not path or normalise_quote(path[-1]).casefold() != heading:
                continue
            lines = [line for line in fragment.text.split("\n") if line.strip() and not line.lstrip().startswith("#")]
            if lines:
                return fragment.fragment_id, normalise_quote(lines[-1]), path[-1]
    return None


def absorbed_grounds(gap: StoredGap, rows: Sequence[AmendmentRow], snapshot: GraphSnapshot) -> tuple[str, ...]:
    """The fragments an absorbed answer is grounded in: those holding its text, else those under its heading."""

    found: list[str] = []
    for row in rows:
        wanted = normalise_quote(answer_lines(row, gap.gap_id)).casefold()
        holding = [
            fragment.fragment_id
            for fragment in snapshot.fragments.values()
            if fragment.document_id == row.lineage and wanted and wanted in normalise_quote(fragment.text).casefold()
        ]
        if not holding and row.heading_path:
            heading = normalise_quote(row.heading_path[-1]).casefold()
            holding = [
                fragment.fragment_id
                for fragment in snapshot.fragments.values()
                if fragment.document_id == row.lineage
                and heading in (normalise_quote(item).casefold() for item in heading_path_of(fragment))
            ]
        found += holding
    return tuple(dict.fromkeys(found))


@dataclass
class ReconciledOutcome:
    """What the close step did with reconciled answers, by gap id."""

    conflict: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    absorbed: list[str] = field(default_factory=list)
    rechecked: list[str] = field(default_factory=list)
    follow_ups: dict[str, str] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "conflict": sorted(self.conflict),
            "orphaned": sorted(self.orphaned),
            "absorbed": sorted(self.absorbed),
            "rechecked": sorted(self.rechecked),
            "follow_ups": dict(sorted(self.follow_ups.items())),
        }
