"""The stop rule.

One legitimate job of a metric is telling you when to stop, and dropping the
score leaves that job open. Saturation fills it without reintroducing a number
to defend: stop when a fresh round of independent lenses finds nothing new worth
asking about.

That is directly observable and needs no threshold to calibrate. It is also
honest about what it claims — not "the spec is now 94% complete" but "another
round of six independent readings surfaced nothing new."
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import rank


@dataclass
class RoundRecord:
    """One round's history entry. Serialized by ``asdict`` — this *is* the
    on-disk shape of ``state.json``'s ``rounds`` entries, so a new field is
    persisted without a second edit."""

    number: int
    lens_count: int
    ask_ids: list[str] = field(default_factory=list)
    new_ask_ids: list[str] = field(default_factory=list)
    dry: bool = False


@dataclass
class SaturationVerdict:
    converged: bool
    dry_streak: int
    required_streak: int
    reason: str
    record: RoundRecord

    def as_dict(self) -> dict[str, Any]:
        # Not asdict(self): the record is published under "round".
        return {
            "converged": self.converged,
            "dry_streak": self.dry_streak,
            "required_streak": self.required_streak,
            "reason": self.reason,
            "round": asdict(self.record),
        }


def evaluate(
    state: dict[str, Any],
    ranked: list[rank.Ranked],
    *,
    round_number: int,
    lens_count: int,
    resolved: set[str] | None = None,
    required_streak: int = 1,
) -> SaturationVerdict:
    """Decide whether this round converged, and return the updated record.

    ``required_streak`` is how many consecutive dry rounds end the loop. One is
    the default because each extra round costs a full fan-out for diminishing
    return; raise it to two when the spec is high-stakes and a missed gap is
    expensive.

    Novelty is measured against every previously *asked* id plus everything
    already resolved. Blockers that were assumed or merely noted are not counted
    as seen, so a finding that shows up more strongly in a later round can still
    reopen the loop.
    """
    resolved = resolved or set()
    history = [RoundRecord(**r) for r in state.get("rounds", [])]
    seen: set[str] = set(resolved)
    for record in history:
        seen.update(record.ask_ids)

    ask_ids = [
        item.blocker.get("id", "")
        for item in ranked
        if item.disposition == rank.ASK and item.blocker.get("id")
    ]
    new_ask_ids = sorted(set(ask_ids) - seen)

    record = RoundRecord(
        number=round_number,
        lens_count=lens_count,
        ask_ids=sorted(set(ask_ids)),
        new_ask_ids=new_ask_ids,
        dry=not new_ask_ids,
    )

    dry_streak = 0
    for previous in reversed(history):
        if previous.dry:
            dry_streak += 1
        else:
            break
    if record.dry:
        dry_streak += 1
    else:
        dry_streak = 0

    converged = dry_streak >= required_streak
    if converged:
        reason = (
            f"{dry_streak} consecutive round(s) with no new blockers to ask about — "
            "further rounds are unlikely to find more"
        )
    elif record.dry:
        reason = f"this round was dry but {required_streak} in a row are required"
    else:
        reason = (
            f"{len(new_ask_ids)} new blocker(s) need a decision: "
            f"{', '.join(new_ask_ids[:4])}"
            + (" ..." if len(new_ask_ids) > 4 else "")
        )

    return SaturationVerdict(
        converged=converged,
        dry_streak=dry_streak,
        required_streak=required_streak,
        reason=reason,
        record=record,
    )


def updated_state(
    state: dict[str, Any], verdict: SaturationVerdict
) -> dict[str, Any]:
    """Append this round to the state, replacing any record with the same number."""
    rounds = [r for r in state.get("rounds", []) if r.get("number") != verdict.record.number]
    rounds.append(asdict(verdict.record))
    rounds.sort(key=lambda r: r["number"])
    return {
        **state,
        "rounds": rounds,
        "converged": verdict.converged,
        "dry_streak": verdict.dry_streak,
    }
