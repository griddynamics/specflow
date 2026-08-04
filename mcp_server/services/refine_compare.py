"""Compare independent readings of the same spec.

This is the only place code belongs in the refinement loop, and the boundary is
worth stating because the first version of this got it backwards.

**Code does comparison.** Did two lenses answer the same question differently?
Which cells did nobody fill? Has this blocker already been resolved? These are
string comparisons and set differences over lists too long to hold reliably in a
model's head — an agent doing them by eye will silently drop the fourteenth item
of twenty, and will not give the same answer twice.

**A model does judgment.** Is this architecture sound? Is the design complete?
Is this decision worth interrupting a human over? Is the spec ready? Is another
round worth running? None of that is checkable, and dressing it up as arithmetic
— a weighted score, a completeness gate over a self-declared checklist, a
saturation rule over round counts — only hides the judgment behind a number
nobody calibrated. The skill decides those, out loud, and the user can argue
with it.

**The grid is how a reading gets forced.** Building a system forced every
ambiguity into the open because code will not compile half a decision. Reading a
spec forces nothing — an agent slides past a gap without noticing. A grid
restores the forcing at a thousandth of the cost: the cells are enumerated from
the spec up front, every lens fills the same ones, and a cell nobody filled is a
gap you can count instead of judge. Counting filled cells is list work; deciding
whether the answer in one is *right* is not, and stays where the rest of the
judgment lives.

So nothing here scores, ranks, or passes verdict. It reports what differs and
what is unfilled. The reading of *that* is the orchestrator's job.
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


def question_key(text: str) -> tuple[str, ...]:
    """A deterministic, order-preserving key, so two phrasings of one question collide.

    Word order is kept deliberately. As an unordered bag, "does the hold expire
    before the payment?" and "does the payment expire before the hold?" are the
    same question — so one lens's *no* lands next to the other's *yes* under a
    single phrasing, and the user is shown an answer to a question nobody asked.
    Within one lens the same collision silently overwrote the earlier decision.

    The trade is asymmetric, which is why order wins: a collision missed loses
    one disagreement, while a false collision reports a wrong one and drops a
    real answer to make room.

    No embeddings and no model call: this runs inside the measurement, and a
    model here would make "did they disagree?" depend on who asked.
    """
    words = []
    for word in _WORD.findall(text.lower()):
        if word in _STOPWORDS:
            continue
        words.append(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return tuple(words)


@dataclass
class Disagreement:
    """One question the lenses answered differently — a located ambiguity."""

    question: str
    where: str
    answers: dict[str, str] = field(default_factory=dict)
    phrasings: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "question": self.question,
            "where": self.where,
            "answers": dict(sorted(self.answers.items())),
            "distinct": len(set(self.answers.values())),
        }
        # Only when the lenses worded it differently. ``question`` can carry one
        # lens's phrasing, and a reader pairing another lens's answer with it
        # misreads the disagreement — so where the wordings differ, all of them
        # are kept rather than one standing in for the rest.
        if len(set(self.phrasings.values())) > 1:
            payload["phrasings"] = dict(sorted(self.phrasings.items()))
        return payload


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
    """One round's merged view.

    ``lens_count``/``lenses`` count the readings that could actually be
    compared, not the files on disk — ``readings_total`` counts those. The two
    differ exactly when a reading was unusable, and reporting six lenses when
    one wrote something uncomparable would overstate the only evidence this
    design has.

    ``incomplete`` is why a reading was excluded; ``notes`` is everything worth
    saying about a reading that *was* included.
    """

    lens_count: int
    readings_total: int = 0
    lenses: list[str] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
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


def find_disagreements(
    readings: list[dict[str, Any]],
) -> tuple[list[Disagreement], list[str]]:
    """Questions where independent readings landed on different answers.

    This is the whole signal. Each lens read the same spec with no knowledge of
    the others; where they still diverge, the spec did not determine the answer.
    No heuristics and no judgment — the answers are short strings, so different
    means different.

    Returns the disagreements and any notes about the readings themselves. The
    one note this raises is a lens answering the same question twice with
    different values: that is a contradiction inside a single reading rather than
    between two, so it is not a disagreement, but dropping the second answer
    without saying so would hide it. The first answer stands.
    """
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    notes: list[str] = []

    for reading in readings:
        lens = str(reading.get("lens", "?"))
        for decision in reading.get("decisions", []):
            question = str(decision.get("question", "")).strip()
            value = decision.get("value")
            if not question or value is None:
                continue
            key = question_key(question)
            if not key:
                continue
            slot = grouped.setdefault(
                key,
                {
                    "question": question,
                    "where": _where(decision),
                    "answers": {},
                    "phrasings": {},
                },
            )
            previous = slot["answers"].get(lens)
            if previous is not None and previous != str(value):
                notes.append(
                    f"{lens}: answered \"{question}\" twice — kept "
                    f"'{previous}', ignored '{value}'"
                )
                continue
            slot["answers"][lens] = str(value)
            slot["phrasings"][lens] = question

    found = [
        Disagreement(
            question=slot["question"],
            where=slot["where"],
            answers=slot["answers"],
            phrasings=slot["phrasings"],
        )
        for slot in grouped.values()
        if len(set(slot["answers"].values())) > 1
    ]
    return _sorted(found), notes


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


# Every one of these is walked as a list of objects downstream.
_OBJECT_LISTS = ("decisions", "blockers", "cells")


def _reading_problems(reading: dict[str, Any]) -> list[str]:
    """Why this reading cannot be compared at all, if it cannot.

    Key presence is not enough. Six lenses write these files concurrently and one
    of them getting a container wrong — ``"blockers": {...}`` where a list was
    asked for — is a routine occurrence, not a corner case. It has to be reported
    here, where ``incomplete`` exists precisely so one bad reading does not kill
    the round, rather than surfacing as an AttributeError two functions down.

    ``cells`` is optional, but a present ``cells`` must still be the right shape.
    """
    # Set by ``load_readings`` for a file that would not parse at all. Its own
    # message names the file and the syntax error, so nothing is added to it.
    unreadable = reading.get("_unreadable")
    if unreadable:
        return [str(unreadable)]

    problems = [
        f"missing {key}"
        for key in ("lens", "decisions", "blockers")
        if key not in reading
    ]
    for key in _OBJECT_LISTS:
        if key not in reading:
            continue
        value = reading[key]
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            problems.append(
                f"{key} should be a list of objects, got {type(value).__name__}"
            )
    return problems


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

    For the same reason an unusable reading is not counted either: the filter
    runs before the counts, so ``lens_count`` is what was compared and
    ``readings_total`` is what was found on disk.
    """
    usable: list[dict[str, Any]] = []
    incomplete: list[str] = []
    notes: list[str] = []
    for reading in readings:
        problems = _reading_problems(reading)
        if problems:
            incomplete.append(
                f"{reading.get('lens', reading.get('_path', '?'))}: "
                + "; ".join(problems)
            )
            continue
        usable.append(reading)
        # Set by ``refine_artifacts.load_readings`` when the file's own ``lens``
        # field disagreed with its filename. The reading is still usable — the
        # filename won — but two files claiming one lens name is how a fan-out
        # silently sends the same lens twice, so the user hears about it.
        declared = reading.get("_lens_declared")
        if declared:
            notes.append(
                f"{reading['lens']}: file declares lens '{declared}' — used the "
                "filename; check the fan-out did not run one lens twice"
            )

    result = Comparison(
        lens_count=len(usable),
        readings_total=len(readings),
        lenses=sorted(str(r.get("lens", "?")) for r in usable),
        incomplete=incomplete,
        notes=notes,
    )
    if not usable:
        return result

    sources = list(usable)
    if coherence and coherence.get("blockers"):
        sources.append({"lens": "coherence", "blockers": coherence["blockers"]})

    result.blockers = merge_blockers(sources)
    result.disagreements, from_decisions = find_disagreements(usable)
    result.notes += from_decisions

    if grid:
        result.coverage, from_cells = grid_coverage(grid, usable)
        result.disagreements = _sorted(result.disagreements + from_cells)

    _attach_disagreements(result)
    return result


def _slug(text: str) -> str:
    """A readable, deterministic id for a synthesized blocker."""
    words = _WORD.findall(text.lower())
    return "-".join(words[:6]) or "unnamed"


def _chosen_by(lens: str, disagreement: Disagreement) -> str:
    """Attribute an option, naming the lens's own wording when it differs.

    ``title`` and ``question`` can only carry one phrasing. Reading another lens's
    answer against it is how a reader concludes two lenses answered the same
    sentence when they answered their own — so where the wordings differ, the
    answer says which question it answers.
    """
    asked = disagreement.phrasings.get(lens)
    if asked and asked != disagreement.question:
        return f"chosen independently by {lens}, asked as: {asked}"
    return f"chosen independently by {lens}"


def _attach_disagreements(result: Comparison) -> None:
    """Route each disagreement to a blocker, existing or synthesized.

    A blocker absorbs **at most one** disagreement — the widest-divergence one at
    its location, since the list arrives in that order. Absorption exists so a
    single gap is not listed twice; it was never meant to make N gaps look like
    one. Unbounded, it did exactly that: the grid format encourages one coarse
    ``where`` per section, so every conflicting cell under one heading collapsed
    into a single open blocker and ``counts.open`` under-reported by however many
    were hidden. Whether that happened depended on whether some lens happened to
    raise a blocker at that location, which made the count arbitrary.

    Bounded at one, the arithmetic is the same either way: N disagreements at a
    location produce N items — one host carrying evidence plus N-1 synthesized,
    or N synthesized. A synthesized blocker deliberately does not become a host
    itself; that is what keeps the two paths equal.

    Location, not word overlap, because location is stated in the artifact and
    needs no threshold. Deciding that two differently worded blockers are really
    the same decision stays with the skill.
    """
    hosts: dict[str, dict[str, Any]] = {}
    for blocker in result.blockers:
        where = _where(blocker)
        if where:
            hosts.setdefault(where, blocker)

    by_id = {b["id"]: b for b in result.blockers if b.get("id")}
    for disagreement in result.disagreements:
        host = hosts.pop(disagreement.where, None) if disagreement.where else None
        if host is not None:
            host.setdefault("disagreements", []).append(disagreement.as_dict())
            continue

        slug = f"diverged-{_slug(disagreement.question)}"
        clash = by_id.get(slug)
        if clash is not None:
            # Two different questions slugged the same. Attach rather than drop:
            # skipping here removed the disagreement from ``blockers`` entirely
            # and said nothing about it.
            clash.setdefault("disagreements", []).append(disagreement.as_dict())
            continue

        synthesized = {
            "id": slug,
            "title": f"Lenses disagree: {disagreement.question}",
            "question": disagreement.question,
            "where": disagreement.where,
            "options": [
                {"label": value, "consequence": _chosen_by(lens, disagreement)}
                for lens, value in sorted(disagreement.answers.items())
            ],
            "found_by": sorted(disagreement.answers),
            "from_disagreement": True,
        }
        by_id[slug] = synthesized
        result.blockers.append(synthesized)


# There is deliberately no round-to-round novelty diff here, and no stop rule.
#
# An earlier version had one: it split each round's blockers into new / repeat /
# already-decided, and the skill read "nothing new this round" as evidence the
# well was dry. That inference does not hold. `new == 0` has two causes that leave
# byte-identical artifacts — the spec has nothing left to give, or this round's
# lenses simply found less, and nothing holds lens effort constant between rounds.
# Turning it into a threshold ("two dry rounds") would need the false-negative
# rate of a single round, which is unmeasured; picking a number without it is the
# same mistake as the deleted completeness gate, one step further back.
#
# What survived is the part that does not depend on that inference:
# ``resolutions.json`` and ``resolved_ids``, so a decision already made stops
# being re-asked. That is a fact about the user's input, not a claim about
# coverage. Rounds are whatever directories exist on disk (``Layout.rounds``).
