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
from typing import Any

# How far a wrong choice propagates.
IMPACT_WEIGHT = {
    "blocks_build": 4,
    "changes_architecture": 3,
    "changes_behaviour": 2,
    "cosmetic": 1,
}

ASK = "ask"
ASSUME = "assume"
NOTE = "note"


@dataclass
class Ranked:
    blocker: dict[str, Any]
    score: float
    disposition: str
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.blocker,
            "_score": round(self.score, 2),
            "_disposition": self.disposition,
            "_rationale": self.rationale,
        }


def _disposition(
    blocker: dict[str, Any], concordance: float
) -> tuple[str, str]:
    impact = blocker.get("impact", "changes_behaviour")
    reversible = bool(blocker.get("reversible", True))
    weight = IMPACT_WEIGHT.get(impact, 2)

    if impact == "blocks_build":
        return ASK, "nothing can be built until this is decided"

    if not reversible and weight >= IMPACT_WEIGHT["changes_behaviour"]:
        return ASK, "expensive to undo once chosen"

    if impact == "cosmetic" and concordance < 0.5:
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
        weight = IMPACT_WEIGHT.get(blocker.get("impact", "changes_behaviour"), 2)
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


def summarize(ranked: list[Ranked]) -> dict[str, Any]:
    """Counts by disposition. Counts, deliberately — not a readiness score."""
    counts = {ASK: 0, ASSUME: 0, NOTE: 0}
    for item in ranked:
        counts[item.disposition] = counts.get(item.disposition, 0) + 1
    return {
        "total": len(ranked),
        "ask": counts[ASK],
        "assume": counts[ASSUME],
        "note": counts[NOTE],
    }


def partition(ranked: list[Ranked]) -> dict[str, list[dict[str, Any]]]:
    """Split into the three lists the resolve step works from."""
    buckets: dict[str, list[dict[str, Any]]] = {ASK: [], ASSUME: [], NOTE: []}
    for item in ranked:
        buckets[item.disposition].append(item.as_dict())
    return buckets
