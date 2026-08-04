from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_NON_ID = re.compile(r"[^a-z0-9._-]+")

def _normalized_answer(value: str) -> str:
    return " ".join(value.split()).casefold()

@dataclass
class Disagreement:
    cell_id: str
    question: str
    where: str
    answers: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "question": self.question,
            "where": self.where,
            "answers": dict(sorted(self.answers.items())),
            "distinct": len({_normalized_answer(value) for value in self.answers.values()}),
        }

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

def _sorted(found: list[Disagreement]) -> list[Disagreement]:
    return sorted(
        found,
        key=lambda d: (
            -len({_normalized_answer(value) for value in d.answers.values()}),
            d.cell_id,
        ),
    )

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
        if len({_normalized_answer(value) for value in answers.values()}) > 1:
            disagreements.append(
                Disagreement(
                    cell_id=str(cell["id"]),
                    question=question,
                    where=where,
                    answers=answers,
                )
            )
        elif len(filled) > 1 and all(guessed for _, guessed in filled.values()):
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
    result.matrices = matrix_coverage(usable)

    if grid:
        result.coverage, result.disagreements = grid_coverage(grid, usable)
        result.disagreements = _sorted(result.disagreements)

    _add_disagreement_blockers(result)
    return result

def _chosen_by(lens: str, disagreement: Disagreement) -> str:
    return f"chosen independently by {lens}"

def _add_disagreement_blockers(result: Comparison) -> None:
    by_id = {b["id"]: b for b in result.blockers if b.get("id")}
    for disagreement in result.disagreements:
        safe_cell_id = _NON_ID.sub("-", disagreement.cell_id.casefold()).strip("-")
        blocker_id = f"diverged-{safe_cell_id or 'unnamed'}"
        clash = by_id.get(blocker_id)
        if clash is not None:
            clash.setdefault("disagreements", []).append(disagreement.as_dict())
            continue

        synthesized = {
            "id": blocker_id,
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
        by_id[blocker_id] = synthesized
        result.blockers.append(synthesized)

