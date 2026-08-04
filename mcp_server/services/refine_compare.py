from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "to", "in", "on", "at", "by", "with",
    "is", "are", "be", "when", "if", "should", "must", "will", "does", "do",
    "what", "which", "how", "who",
})
_WORD = re.compile(r"[a-z0-9]+")

def question_key(text: str) -> tuple[str, ...]:
    words = []
    for word in _WORD.findall(text.lower()):
        if word in _STOPWORDS:
            continue
        words.append(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return tuple(words)

@dataclass
class Disagreement:
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
        if len(set(self.phrasings.values())) > 1:
            payload["phrasings"] = dict(sorted(self.phrasings.items()))
        return payload

@dataclass
class Coverage:
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
class MatrixCoverage:
    lens: str
    name: str
    declared: int = 0
    answered: int = 0
    guessed: int = 0
    unanswerable: list[dict[str, Any]] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lens": self.lens,
            "name": self.name,
            "declared": self.declared,
            "answered": self.answered,
            "guessed": self.guessed,
            "unanswerable": self.unanswerable,
            "missing": self.missing,
        }

@dataclass
class Comparison:
    lens_count: int
    readings_total: int = 0
    lenses: list[str] = field(default_factory=list)
    blockers: list[dict[str, Any]] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    coverage: Coverage | None = None
    matrices: list[MatrixCoverage] = field(default_factory=list)

def _where(item: dict[str, Any]) -> str:
    return str(item.get("where") or item.get("spec_anchor") or "")

def merge_blockers(readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    return sorted(found, key=lambda d: (-len(set(d.answers.values())), d.question))

def grid_coverage(
    grid: dict[str, Any], readings: list[dict[str, Any]]
) -> tuple[Coverage, list[Disagreement]]:
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

def matrix_coverage(readings: list[dict[str, Any]]) -> list[MatrixCoverage]:
    found: list[MatrixCoverage] = []
    for reading in readings:
        lens = str(reading.get("lens", "?"))
        for index, matrix in enumerate(reading.get("matrices") or []):
            rows = [str(r) for r in matrix.get("rows") or []]
            cols = [str(c) for c in matrix.get("cols") or []]
            if not rows or not cols:
                continue
            entries = {
                (str(cell.get("row")), str(cell.get("col"))): cell
                for cell in matrix.get("cells") or []
            }
            report = MatrixCoverage(
                lens=lens,
                name=str(matrix.get("name") or f"matrix {index + 1}"),
                declared=len(rows) * len(cols),
            )
            for row in rows:
                for col in cols:
                    cell = entries.get((row, col))
                    at = {"row": row, "col": col}
                    if cell is not None and cell.get("value") is not None:
                        report.answered += 1
                        if cell.get("guessed"):
                            report.guessed += 1
                    elif cell is not None and cell.get("unanswerable"):
                        report.unanswerable.append(
                            {**at, "why": str(cell["unanswerable"])}
                        )
                    else:
                        report.missing.append(at)
            found.append(report)

    return sorted(
        found, key=lambda m: (-len(m.missing), -len(m.unanswerable), m.lens, m.name)
    )

_OBJECT_LISTS = ("decisions", "blockers", "cells", "matrices")

def _matrix_problems(matrix: Any, index: int) -> list[str]:
    label = f"matrices[{index}]"
    if not isinstance(matrix, dict):  # pragma: no cover
        return [f"{label} should be an object, got {type(matrix).__name__}"]
    problems = [
        f"{label}.{key} should be a list of strings"
        for key in ("rows", "cols")
        if not isinstance(matrix.get(key), list)
        or not all(isinstance(item, str) for item in matrix[key])
    ]
    cells = matrix.get("cells")
    if not isinstance(cells, list) or not all(isinstance(i, dict) for i in cells):
        problems.append(f"{label}.cells should be a list of objects")
    return problems

def _reading_problems(reading: dict[str, Any]) -> list[str]:
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
            continue
        if key == "matrices":
            for index, matrix in enumerate(value):
                problems += _matrix_problems(matrix, index)
    return problems

def compare(
    readings: list[dict[str, Any]],
    grid: dict[str, Any] | None = None,
    coherence: dict[str, Any] | None = None,
) -> Comparison:
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
    result.matrices = matrix_coverage(usable)

    if grid:
        result.coverage, from_cells = grid_coverage(grid, usable)
        result.disagreements = _sorted(result.disagreements + from_cells)

    _attach_disagreements(result)
    return result

def _slug(text: str) -> str:
    words = _WORD.findall(text.lower())
    return "-".join(words[:6]) or "unnamed"

def _chosen_by(lens: str, disagreement: Disagreement) -> str:
    asked = disagreement.phrasings.get(lens)
    if asked and asked != disagreement.question:
        return f"chosen independently by {lens}, asked as: {asked}"
    return f"chosen independently by {lens}"

def _attach_disagreements(result: Comparison) -> None:
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

