"""Compare independent readings of the same spec, and remember what was asked.

This is the only place code belongs in the refinement loop, and the boundary is
worth stating because the first version of this got it backwards.

**Code does comparison and memory.** Did two lenses answer the same question
differently? Has this blocker already been resolved? Did this round raise
anything the previous rounds did not? These are string comparisons and set
differences over lists too long to hold reliably in a model's head — an agent
doing them by eye will silently drop the fourteenth item of twenty.

**A model does judgment.** Is this architecture sound? Is the design complete?
Is this decision worth interrupting a human over? Is the spec ready? None of
that is checkable, and dressing it up as arithmetic — a weighted score, a
completeness gate over a self-declared checklist — only hides the judgment
behind a number nobody calibrated. The skill decides those, out loud, and the
user can argue with it.

**The grid is how a reading gets forced.** Building a system forced every
ambiguity into the open because code will not compile half a decision. Reading a
spec forces nothing — an agent slides past a gap without noticing. A grid
restores the forcing at a thousandth of the cost: the cells are enumerated from
the spec up front, every lens fills the same ones, and a cell nobody filled is a
gap you can count instead of judge. Counting filled cells is list work; deciding
whether the answer in one is *right* is not, and stays where the rest of the
judgment lives.

So nothing here scores, ranks, or passes verdict. It reports what differs, what
is unfilled, and what is new. The reading of *that* is the orchestrator's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Dropped when matching two questions that may be worded differently.
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "to", "in", "on", "at", "by", "with",
    "is", "are", "be", "when", "if", "should", "must", "will", "does", "do",
    "what", "which", "how", "who",
})
_WORD = re.compile(r"[a-z0-9]+")


def question_key(text: str) -> frozenset[str]:
    """A deterministic bag of words, so two phrasings of one question collide.

    No embeddings and no model call: this runs inside the measurement, and a
    model here would make "did they disagree?" depend on who asked.
    """
    words = []
    for word in _WORD.findall(text.lower()):
        if word in _STOPWORDS:
            continue
        words.append(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return frozenset(words)


@dataclass
class Disagreement:
    """One question the lenses answered differently — a located ambiguity."""

    question: str
    where: str
    answers: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "where": self.where,
            "answers": dict(sorted(self.answers.items())),
            "distinct": len(set(self.answers.values())),
        }


@dataclass
class Coverage:
    """What the lenses did and did not fill in on this round's grid."""

    cells_total: int = 0
    cells_filled: int = 0
    uncovered: list[dict[str, Any]] = field(default_factory=list)
    agreed_guesses: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cells_total": self.cells_total,
            "cells_filled": self.cells_filled,
            "uncovered": self.uncovered,
            "agreed_guesses": self.agreed_guesses,
        }


@dataclass
class Comparison:
    lens_count: int
    lenses: list[str] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    coverage: Coverage | None = None


def _where(item: dict[str, Any]) -> str:
    """Where in the spec this came from. Free-form; used for grouping only."""
    return str(item.get("where") or item.get("spec_anchor") or "")


def merge_blockers(readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union the blockers, recording which lenses independently raised each.

    Collision is on ``id`` alone. Ids are stable slugs, so the same decision
    found by three lenses collides on its own. Deciding that two *differently*
    worded blockers are really the same decision is a judgment — the skill does
    that when it presents them, rather than a word-overlap threshold doing it
    badly here.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for reading in readings:
        lens = reading.get("lens", "?")
        for blocker in reading.get("blockers", []):
            key = blocker.get("id")
            if not key:
                continue
            existing = by_id.get(key)
            if existing is None:
                merged = dict(blocker)
                merged["found_by"] = [lens]
                by_id[key] = merged
            elif lens not in existing["found_by"]:
                existing["found_by"].append(lens)
                # Keep the richest option set we have seen.
                if len(blocker.get("options", [])) > len(existing.get("options", [])):
                    existing["options"] = blocker["options"]
                    existing["recommended"] = blocker.get(
                        "recommended", existing.get("recommended")
                    )

    ordered = sorted(
        by_id.values(),
        key=lambda b: (-len(b["found_by"]), b.get("id", "")),
    )
    for blocker in ordered:
        blocker["found_by"] = sorted(blocker["found_by"])
    return ordered


def find_disagreements(readings: list[dict[str, Any]]) -> list[Disagreement]:
    """Questions where independent readings landed on different answers.

    This is the whole signal. Each lens read the same spec with no knowledge of
    the others; where they still diverge, the spec did not determine the answer.
    No heuristics and no judgment — the answers are short strings, so different
    means different.
    """
    grouped: dict[frozenset[str], dict[str, Any]] = {}

    for reading in readings:
        lens = reading.get("lens", "?")
        for decision in reading.get("decisions", []):
            question = str(decision.get("question", "")).strip()
            value = decision.get("value")
            if not question or value is None:
                continue
            key = question_key(question)
            if not key:
                continue
            slot = grouped.setdefault(
                key, {"question": question, "where": _where(decision), "answers": {}}
            )
            slot["answers"][lens] = str(value)

    found = [
        Disagreement(
            question=slot["question"], where=slot["where"], answers=slot["answers"]
        )
        for slot in grouped.values()
        if len(set(slot["answers"].values())) > 1
    ]
    return _sorted(found)


def _sorted(found: list[Disagreement]) -> list[Disagreement]:
    """Widest divergence first — most lenses landing on most distinct answers."""
    return sorted(found, key=lambda d: (-len(set(d.answers.values())), d.question))


def grid_coverage(
    grid: dict[str, Any], readings: list[dict[str, Any]]
) -> tuple[Coverage, list[Disagreement]]:
    """Check the round's readings against the cells the grid says exist.

    Three outcomes per cell, and each means something different:

    *Nobody filled it* — the gap no reading even reached. This is the case a
    prose reading loses silently, because nothing in a paragraph demands an
    answer the way an empty cell does.

    *Two lenses filled it differently* — a disagreement, returned as one so it
    joins the ones found in prose and reaches the user through the same path.
    Grouping cells needs no word matching: the cell id already says these are
    answers to one question.

    *Everyone filled it, and everyone guessed* — agreement that is not evidence.
    Independent readings converging on an answer the spec never gave is a shared
    blind spot wearing consensus, and it is invisible to a design that only looks
    for divergence. Reported, never counted as a disagreement, because the lenses
    genuinely did agree.
    """
    cells = [cell for cell in grid.get("cells", []) if cell.get("id")]
    answered: dict[str, dict[str, tuple[str, bool]]] = {}
    for reading in readings:
        lens = reading.get("lens", "?")
        for entry in reading.get("cells", []):
            cell_id, value = entry.get("id"), entry.get("value")
            if not cell_id or value is None:
                continue
            answered.setdefault(cell_id, {})[lens] = (
                str(value), bool(entry.get("guessed"))
            )

    coverage = Coverage(cells_total=len(cells))
    disagreements: list[Disagreement] = []
    for cell in cells:
        filled = answered.get(cell["id"], {})
        question = str(cell.get("question") or cell["id"])
        where = _where(cell)
        if not filled:
            coverage.uncovered.append(
                {"id": cell["id"], "question": question, "where": where}
            )
            continue

        coverage.cells_filled += 1
        answers = {lens: value for lens, (value, _) in filled.items()}
        if len(set(answers.values())) > 1:
            disagreements.append(
                Disagreement(question=question, where=where, answers=answers)
            )
        elif all(guessed for _, guessed in filled.values()):
            coverage.agreed_guesses.append({
                "id": cell["id"],
                "question": question,
                "where": where,
                "value": next(iter(answers.values())),
                "lenses": sorted(answers),
            })
    return coverage, disagreements


def _missing_keys(reading: dict[str, Any]) -> list[str]:
    """Keys a reading needs before it can be compared at all."""
    return [key for key in ("lens", "decisions", "blockers") if key not in reading]


def compare(
    readings: list[dict[str, Any]],
    grid: dict[str, Any] | None = None,
    coherence: dict[str, Any] | None = None,
) -> Comparison:
    """Merge a round's readings into one attributed view.

    ``coherence`` is the pass that asks whether the answers can all be true at
    once. It contributes blockers and nothing else: it read every lens's output,
    so it is not an independent reading and must never vote in a disagreement.
    It is also left out of ``lenses``/``lens_count``, which count independent
    readings — inflating that number would overstate the only evidence this
    design has.
    """
    result = Comparison(
        lens_count=len(readings),
        lenses=sorted(r.get("lens", "?") for r in readings),
    )
    if not readings:
        return result

    usable = []
    for reading in readings:
        missing = _missing_keys(reading)
        if missing:
            result.incomplete.append(
                f"{reading.get('lens', reading.get('_path', '?'))}: "
                f"missing {', '.join(missing)}"
            )
        else:
            usable.append(reading)

    sources = list(usable)
    if coherence and coherence.get("blockers"):
        sources.append({"lens": "coherence", "blockers": coherence["blockers"]})

    result.blockers = merge_blockers(sources)
    result.disagreements = find_disagreements(usable)

    if grid:
        result.coverage, from_cells = grid_coverage(grid, usable)
        result.disagreements = _sorted(result.disagreements + from_cells)

    _attach_disagreements(result)
    return result


def _slug(text: str) -> str:
    """A readable, deterministic id for a synthesized blocker."""
    words = _WORD.findall(text.lower())
    return "-".join(words[:6]) or "unnamed"


def _attach_disagreements(result: Comparison) -> None:
    """Route each disagreement to a blocker, existing or synthesized.

    Grouped by location, not by wording. If a lens already raised something at
    the same place, the disagreement is evidence for that decision rather than a
    second entry — two list items for one gap makes the list look longer than
    the problem is. A disagreement at a location *nobody* flagged becomes its own
    blocker, and that is the valuable case: a gap no single reading noticed.

    Location, not word overlap, because location is stated in the artifact and
    needs no threshold. Deciding that two differently worded blockers are really
    the same decision stays with the skill.
    """
    by_where: dict[str, dict[str, Any]] = {}
    for blocker in result.blockers:
        where = _where(blocker)
        if where:
            by_where.setdefault(where, blocker)

    known = {b.get("id") for b in result.blockers}
    for disagreement in result.disagreements:
        host = by_where.get(disagreement.where) if disagreement.where else None
        if host is not None:
            host.setdefault("disagreements", []).append(disagreement.as_dict())
            continue

        slug = f"diverged-{_slug(disagreement.question)}"
        if slug in known:
            continue
        known.add(slug)
        result.blockers.append({
            "id": slug,
            "title": f"Lenses disagree: {disagreement.question}",
            "question": disagreement.question,
            "where": disagreement.where,
            "options": [
                {"label": value, "consequence": f"chosen independently by {lens}"}
                for lens, value in sorted(disagreement.answers.items())
            ],
            "found_by": sorted(disagreement.answers),
            "from_disagreement": True,
        })


def novelty(
    state: dict[str, Any], blocker_ids: list[str], resolved: set[str]
) -> dict[str, Any]:
    """Split this round's blockers into new, repeat, and already-decided.

    Pure bookkeeping over every previous round, which is exactly the kind of
    thing worth spending code on. Whether "nothing new" means the spec is ready
    is not decided here — the skill reads this and says so, or does another
    round.
    """
    seen: set[str] = set()
    for record in state.get("rounds", []):
        seen.update(record.get("blocker_ids", []))

    current = set(blocker_ids)
    return {
        "new": sorted(current - seen - resolved),
        "repeat": sorted((current & seen) - resolved),
        "resolved": sorted(current & resolved),
    }


def record_round(
    state: dict[str, Any], *, number: int, lenses: list[str], blocker_ids: list[str]
) -> dict[str, Any]:
    """Append this round to the state, replacing any record with the same number."""
    rounds = [r for r in state.get("rounds", []) if r.get("number") != number]
    rounds.append({
        "number": number,
        "lenses": lenses,
        "blocker_ids": sorted(set(blocker_ids)),
    })
    rounds.sort(key=lambda r: r["number"])
    return {**state, "rounds": rounds}
