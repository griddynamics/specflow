"""Cross-lens agreement — the triage function.

Multiplicity was never primarily about producing a number. Independent readings
give two things a single reading cannot: better recall (the union of what each
lens found) and a way to rank (agreement between lenses that could not see each
other's work).

Human attention is the scarce resource in this design, so agreement is spent on
deciding *what to ask about*, not on scoring the spec. Nothing here is shown to
the user as a metric.

Matching is anchored on the spec, not on names. Comparing entity or field names
globally would measure synonyms — one lens's ``User`` against another's
``Account``. Comparing within a requirement's scope reduces that to a small
local problem, which is why every artifact element carries a spec anchor.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# Dropped before comparing labels within an anchor's scope.
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "to", "in", "on", "at", "by", "with",
    "is", "are", "be", "when", "if", "should", "must", "will", "does",
})
_WORD = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> frozenset[str]:
    """Deterministic bag-of-words for within-scope comparison.

    Lowercase, split, drop stopwords, strip a trailing plural 's'. No embeddings
    and no model call: the comparison has to be reproducible, and an LLM here
    would inject judgment into the measurement.
    """
    words = []
    for word in _WORD.findall(text.lower()):
        if word in _STOPWORDS:
            continue
        words.append(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return frozenset(words)


def _anchor_key(anchor: dict[str, Any] | None) -> str:
    anchor = anchor or {}
    parts = [str(anchor.get("file", "")), str(anchor.get("section", ""))]
    return "::".join(p for p in parts if p)


@dataclass
class Divergence:
    """One located disagreement between lenses."""

    kind: str
    where: str
    detail: str
    lenses: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "where": self.where,
            "detail": self.detail,
            "lenses": self.lenses,
        }


@dataclass
class ConcordanceResult:
    lens_count: int
    blockers: list[dict[str, Any]] = field(default_factory=list)
    divergences: list[Divergence] = field(default_factory=list)
    coverage: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lens_count": self.lens_count,
            "blockers": self.blockers,
            "divergences": [d.as_dict() for d in self.divergences],
            "coverage": self.coverage,
        }


def _merge_blockers(
    interpretations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union the blockers, recording which lenses independently raised each.

    Two passes: exact id collision first (ids are stable slugs, so the same
    decision found twice should collide), then within-anchor label overlap to
    catch the same gap described in different words.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for interpretation in interpretations:
        lens = interpretation.get("lens", "?")
        for blocker in interpretation.get("blockers", []):
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
                    existing["recommended"] = blocker.get("recommended", existing.get("recommended"))

    # Second pass: same anchor, overlapping wording, different id.
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for blocker in by_id.values():
        grouped[_anchor_key(blocker.get("spec_anchor"))].append(blocker)

    absorbed: set[str] = set()
    for peers in grouped.values():
        for i, left in enumerate(peers):
            if left["id"] in absorbed:
                continue
            left_words = normalize(left.get("title", ""))
            for right in peers[i + 1:]:
                if right["id"] in absorbed:
                    continue
                right_words = normalize(right.get("title", ""))
                if not left_words or not right_words:
                    continue
                overlap = len(left_words & right_words) / len(left_words | right_words)
                if overlap >= 0.6:
                    for lens in right["found_by"]:
                        if lens not in left["found_by"]:
                            left["found_by"].append(lens)
                    left.setdefault("also_known_as", []).append(right["id"])
                    absorbed.add(right["id"])

    return [b for b in by_id.values() if b["id"] not in absorbed]


def _dimension_divergences(
    interpretations: list[dict[str, Any]]
) -> list[Divergence]:
    """Different lenses locking the same dimension to different values.

    This is the strongest and cheapest signal available: Part A and Part D are
    enums or short strings, so disagreement is unambiguous — no matching
    heuristics, no judgment. A dimension the spec determined would not diverge.
    """
    found: list[Divergence] = []
    picks: dict[str, dict[str, str]] = defaultdict(dict)

    for interpretation in interpretations:
        lens = interpretation.get("lens", "?")
        dimensions = interpretation.get("dimensions") or {}

        for name, entry in (dimensions.get("part_a") or {}).items():
            if isinstance(entry, dict):
                value = entry.get("value")
                if value is None and name == "scope_boundaries":
                    value = " | ".join(sorted(entry.get("in_scope", [])))
                if value is not None:
                    picks[f"part_a.{name}"][lens] = str(value)

        part_d = dimensions.get("part_d") or {}
        for group in ("naming", "patterns"):
            for name, value in (part_d.get(group) or {}).items():
                if isinstance(value, str):
                    picks[f"part_d.{group}.{name}"][lens] = value

    for where, by_lens in sorted(picks.items()):
        distinct = set(by_lens.values())
        if len(distinct) > 1:
            found.append(
                Divergence(
                    kind="dimension",
                    where=where,
                    detail=(
                        f"{len(distinct)} different values across {len(by_lens)} lenses — "
                        "the spec does not determine this"
                    ),
                    lenses=dict(sorted(by_lens.items())),
                )
            )
    return found


def _phase_divergences(interpretations: list[dict[str, Any]]) -> list[Divergence]:
    """Disagreement about how to sequence the work.

    Attempting to phase a build is its own forcing function — you cannot
    sequence work you do not understand. Lenses that decompose the same spec
    very differently are telling you the spec underdetermines the work.
    """
    counts = {
        i.get("lens", "?"): len(i.get("phases", []))
        for i in interpretations
        if i.get("phases")
    }
    if len(counts) < 2:
        return []
    low, high = min(counts.values()), max(counts.values())
    if low and high >= low * 2:
        return [
            Divergence(
                kind="decomposition",
                where="phases",
                detail=(
                    f"phase counts range {low}-{high} — lenses do not agree on how much "
                    "work this is"
                ),
                lenses={k: str(v) for k, v in sorted(counts.items())},
            )
        ]
    return []


def _coverage(interpretations: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Which lenses addressed each spec anchor.

    A requirement only some lenses engaged with is either unclear or hard to
    find — both worth knowing.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    for interpretation in interpretations:
        lens = interpretation.get("lens", "?")
        for collection in ("entities", "operations", "state_machines", "failure_modes", "blockers"):
            for item in interpretation.get(collection, []):
                key = _anchor_key(item.get("spec_anchor"))
                if key:
                    seen[key].add(lens)
    return {anchor: sorted(lenses) for anchor, lenses in sorted(seen.items())}


def compute(interpretations: list[dict[str, Any]]) -> ConcordanceResult:
    """Merge a round's lens artifacts into one ranked, attributed view."""
    if not interpretations:
        return ConcordanceResult(lens_count=0)

    result = ConcordanceResult(lens_count=len(interpretations))
    result.blockers = _merge_blockers(interpretations)
    result.divergences = _dimension_divergences(interpretations) + _phase_divergences(interpretations)
    result.coverage = _coverage(interpretations)

    # A diverged dimension is a concrete, located gap. Surface it as a blocker
    # so it flows into the same ranking and resolution path as everything else.
    for divergence in result.divergences:
        if divergence.kind != "dimension":
            continue
        blocker_id = f"divergent-{divergence.where.replace('.', '-').replace('_', '-')}"
        if any(b["id"] == blocker_id for b in result.blockers):
            continue
        options = [
            {"label": value, "consequence": f"chosen independently by: {lens}"}
            for lens, value in sorted(
                {v: k for k, v in divergence.lenses.items()}.items()
            )
        ]
        result.blockers.append({
            "id": blocker_id,
            "title": f"Lenses disagree on {divergence.where}",
            "spec_anchor": {"file": "<derived>", "section": divergence.where},
            "scenario": divergence.detail,
            "question": f"Which value should {divergence.where} lock to?",
            "options": options,
            "recommended": options[0]["label"] if options else "",
            "impact": "changes_architecture",
            "reversible": False,
            "found_by": sorted(divergence.lenses),
        })

    return result
