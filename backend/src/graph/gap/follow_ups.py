"""Answers to follow-up gaps and what they do to the answer they follow up (docs/graph/gap/reconcile.md "Conflict
follow-up", "Orphaned follow-up", "Edge cases"; lifecycle.md "Clarification transitions").

- ``predecessors``: the clarifications of the gaps a gap follows up that an answer to it takes the place of: the
  ``conflict`` or ``orphaned`` clarification of a reconcile follow-up, and the ``pending`` clarification of a
  ``not_fixed`` follow-up whose answer was sent to a build and never grounded (it never reached the spec, so the
  follow-up's answer replaces it rather than standing beside it). When the follow-up's answer is applied they become
  ``superseded`` and their amendments ``reverted`` (``REVERTED_ROWS``), as when a resolved gap is answered again.
- ``settles_at_once``: an option without edits ("Keep the upload", "Drop it") on a reconcile follow-up needs no build:
  the follow-up resolves when it is answered (``pointer`` to the owner's line for "Keep the upload").
- Dismissing a reconcile follow-up reverts the suspended or orphaned amendments of the answer it follows up; the
  upload stays what the build reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from graph.gap.corpus import APPLY_MARK
from graph.gap.gaps import StoredGap

NOT_FIXED: Final = "not_fixed"
RECONCILE_REASONS: Final = frozenset({"conflict", "orphaned"})
APPLIED_ROWS: Final = frozenset({"active", "undecided"})
SET_ASIDE_ROWS: Final = frozenset({"suspended", "orphaned"})
REVERTED_ROWS: Final = APPLIED_ROWS | SET_ASIDE_ROWS
"""Amendment statuses an answer that takes the place of their clarification reverts (an answer settled at once
requests an update build when applied rows leave the corpus); a dismissed follow-up reverts only the set-aside ones."""


@dataclass(frozen=True)
class Predecessor:
    """A clarification of a followed-up gap that an answer to the follow-up replaces."""

    gap_id: str
    clr_id: str
    reason: str


def continued(gap: StoredGap) -> tuple[tuple[str, str], ...]:
    """The (gap id, reason) pairs this gap follows up."""

    return tuple((item.gap_id, item.reason) for item in gap.follow_ups if item.direction == "continues")


def reconcile_reason(gap: StoredGap) -> str | None:
    """``conflict`` or ``orphaned`` when the gap is a reconcile follow-up, else None."""

    return next((reason for _, reason in continued(gap) if reason in RECONCILE_REASONS), None)


def _replaced(props: Mapping[str, Any], reason: str) -> bool:
    status = props.get("status")
    if reason in RECONCILE_REASONS:
        return status == reason
    return reason == NOT_FIXED and status == "pending" and bool(props.get(APPLY_MARK))


def predecessors(gap: StoredGap, by_id: Mapping[str, StoredGap]) -> tuple[Predecessor, ...]:
    """The clarifications an answer to ``gap`` replaces (module docstring)."""

    found: list[Predecessor] = []
    for old_id, reason in continued(gap):
        old = by_id.get(old_id)
        if old is None:
            continue
        found += [
            Predecessor(old_id, str(props["clr_id"]), reason)
            for props in old.clarifications
            if props.get("clr_id") and _replaced(props, reason)
        ]
    return tuple(found)


def settles_at_once(gap: StoredGap, mode: str, edits: Sequence[Any]) -> bool:
    """Whether an answer resolves the gap at once: an option without edits on a reconcile follow-up."""

    return mode == "option" and not edits and reconcile_reason(gap) is not None
