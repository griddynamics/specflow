"""Ranking and disposition: deciding what is worth a human's attention.

Every question has a cost, so the loop must not hand the user a flat list of
everything it found. Two inputs decide each blocker's fate:

  *Cost asymmetry* — how expensive is being wrong? A choice that is cheap to
  reverse should be assumed and logged, not asked about. A choice that locks the
  architecture should be asked about even if only one lens raised it.

  *Concordance* — how many independent lenses hit the same thing? Agreement is
  evidence; a lone cosmetic nitpick is probably one agent being pedantic.

The output is a three-way disposition rather than a queue, because "ask the
user" is only correct for a minority of findings:

  ask     blocking or irreversible — the user decides
  assume  apply the recommendation, record it, move on
  note    recorded for the audit trail, not surfaced

Nothing here is a score. The numbers order the list and then stop existing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Impact(str, Enum):
    """How far a wrong choice propagates. Ordered by weight, cheapest last."""

    BLOCKS_BUILD = "blocks_build"
    CHANGES_ARCHITECTURE = "changes_architecture"
    CHANGES_BEHAVIOUR = "changes_behaviour"
    COSMETIC = "cosmetic"


class Disposition(str, Enum):
    """What the loop does with a blocker. The only three outcomes there are."""

    ASK = "ask"
    ASSUME = "assume"
    NOTE = "note"


IMPACT_WEIGHT = {
    Impact.BLOCKS_BUILD: 4,
    Impact.CHANGES_ARCHITECTURE: 3,
    Impact.CHANGES_BEHAVIOUR: 2,
    Impact.COSMETIC: 1,
}

# Shorthand for callers that read a disposition off a Ranked.
ASK, ASSUME, NOTE = Disposition.ASK, Disposition.ASSUME, Disposition.NOTE


def impact_of(value: Any) -> Impact:
    """Coerce an artifact's ``impact`` string, defaulting to the middle weight.

    One place decides what an unrecognised impact means, so the default cannot
    drift between the score and the disposition.
    """
    try:
        return Impact(value)
    except ValueError:
        return Impact.CHANGES_BEHAVIOUR


@dataclass
class Ranked:
    blocker: dict[str, Any]
    score: float
    disposition: Disposition
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.blocker,
            "_score": round(self.score, 2),
            "_disposition": self.disposition.value,
            "_rationale": self.rationale,
        }


def _disposition(
    blocker: dict[str, Any], concordance: float
) -> tuple[Disposition, str]:
    impact = impact_of(blocker.get("impact"))
    reversible = bool(blocker.get("reversible", True))

    if impact is Impact.BLOCKS_BUILD:
        return ASK, "nothing can be built until this is decided"

    if not reversible and IMPACT_WEIGHT[impact] >= IMPACT_WEIGHT[Impact.CHANGES_BEHAVIOUR]:
        return ASK, "expensive to undo once chosen"

    if impact is Impact.COSMETIC and concordance < 0.5:
        return NOTE, "cosmetic, and most lenses did not raise it"

    if reversible:
        return (
            ASSUME,
            "cheap to change later — applying the recommendation and recording it",
        )

    return ASK, "irreversible"


def rank(
    blockers: list[dict[str, Any]],
    *,
    lens_count: int,
    already_resolved: set[str] | None = None,
) -> list[Ranked]:
    """Order blockers and assign each a disposition.

    ``already_resolved`` drops blockers the user has decided in an earlier round,
    which is what stops the loop re-asking the same question. Note this
    deliberately dedups against *resolved* ids and not against everything ever
    seen — a blocker that was noted rather than resolved should come back if a
    later round finds it more strongly.
    """
    already_resolved = already_resolved or set()
    lens_count = max(lens_count, 1)
    ranked: list[Ranked] = []

    for blocker in blockers:
        identifier = blocker.get("id", "")
        aliases = set(blocker.get("also_known_as", []))
        if identifier in already_resolved or aliases & already_resolved:
            continue

        found_by = blocker.get("found_by") or []
        concordance = len(found_by) / lens_count
        weight = IMPACT_WEIGHT[impact_of(blocker.get("impact"))]
        irreversibility = 2.0 if not blocker.get("reversible", True) else 1.0

        score = weight * (1.0 + concordance) * irreversibility
        disposition, rationale = _disposition(blocker, concordance)

        ranked.append(
            Ranked(
                blocker=blocker,
                score=score,
                disposition=disposition,
                rationale=rationale,
            )
        )

    ranked.sort(key=lambda r: (-r.score, r.blocker.get("id", "")))
    return ranked


def partition(ranked: list[Ranked]) -> dict[Disposition, list[dict[str, Any]]]:
    """Split into the lists the resolve step works from, one per disposition."""
    buckets: dict[Disposition, list[dict[str, Any]]] = {d: [] for d in Disposition}
    for item in ranked:
        buckets[item.disposition].append(item.as_dict())
    return buckets


def summarize(buckets: dict[Disposition, list[dict[str, Any]]]) -> dict[str, Any]:
    """Counts by disposition. Counts, deliberately — not a readiness score.

    Derived from the buckets rather than walked separately, so the two cannot
    disagree about what the dispositions are.
    """
    return {
        "total": sum(len(items) for items in buckets.values()),
        **{d.value: len(buckets[d]) for d in Disposition},
    }
