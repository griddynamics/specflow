"""Ambiguity mutation: manufacturing ground truth for an instrument that has none.

The open question in this design is whether simulated-build divergence actually
tracks real spec defects. With no builds, there is nothing to check against.

So we manufacture the ground truth. Take a spec, programmatically remove a
constraint or introduce a contradiction, and assert two things: the loop raises a
blocker, and the blocker lands on the requirement we damaged. Detection without
localization is not good enough — a loop that always complains about everything
would pass a detection-only test.

This is also the regression suite for the whole pipeline. A mutation the loop
stops catching is a concrete bug with a reproducible input.

Selection is index-based rather than random so a run is reproducible from its
manifest alone.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Sentences carrying a hard constraint — the ones whose removal creates a real gap.
_CONSTRAINT = re.compile(
    r"\b(must not|must|shall not|shall|always|never|only|exactly|at least|at most|"
    r"required to|no more than|no fewer than)\b",
    re.IGNORECASE,
)
_ERROR_CASE = re.compile(
    r"\b(if .* fails|on failure|on error|when .* is unavailable|timeout|invalid|"
    r"rejected|error case|otherwise)\b",
    re.IGNORECASE,
)
_QUANTITY = re.compile(r"\b(\d+)\s*(seconds?|minutes?|hours?|days?|items?|times?|retries|attempts?)\b", re.IGNORECASE)
_ENUM = re.compile(r"\b(?:one of|either)\s+([^.]+?)(?:\.|$)", re.IGNORECASE)

_INVERSIONS = [
    ("must not", "must"),
    ("must", "must not"),
    ("shall not", "shall"),
    ("shall", "shall not"),
    ("always", "never"),
    ("never", "always"),
]

MUTATIONS = ("drop_constraint", "contradict", "vague_quantity", "drop_error_case", "blur_enum")


@dataclass
class Mutation:
    """One deliberate defect, with everything needed to check for it later."""

    kind: str
    file: str
    line: int
    original: str
    replacement: str
    expect_anchor_file: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "file": self.file,
            "line": self.line,
            "original": self.original,
            "replacement": self.replacement,
            "expect_anchor_file": self.expect_anchor_file,
        }


@dataclass
class Manifest:
    spec_dir: str
    mutated_dir: str
    mutations: list[Mutation] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec_dir": self.spec_dir,
            "mutated_dir": self.mutated_dir,
            "mutations": [m.as_dict() for m in self.mutations],
        }


def _spec_files(spec_dir: Path) -> list[Path]:
    return sorted(
        p for p in spec_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in (".md", ".txt", ".markdown")
    )


def _sentences(text: str) -> list[tuple[int, str]]:
    """(1-based line number, line) for non-trivial prose lines."""
    return [
        (i + 1, line)
        for i, line in enumerate(text.splitlines())
        if len(line.strip()) > 25 and not line.lstrip().startswith(("#", "|", "```"))
    ]


def _pick(candidates: list[tuple[int, str]], index: int) -> tuple[int, str] | None:
    if not candidates:
        return None
    return candidates[index % len(candidates)]


def _mutate_line(kind: str, line: str) -> str | None:
    """Apply one mutation to a line, or return None if it does not apply."""
    if kind == "drop_constraint":
        return ""  # Delete the constraint entirely.

    if kind == "contradict":
        for needle, replacement in _INVERSIONS:
            match = re.search(rf"\b{re.escape(needle)}\b", line, re.IGNORECASE)
            if match:
                return line[: match.start()] + replacement + line[match.end():]
        return None

    if kind == "vague_quantity":
        match = _QUANTITY.search(line)
        if not match:
            return None
        return line[: match.start()] + f"several {match.group(2)}" + line[match.end():]

    if kind == "drop_error_case":
        return ""

    if kind == "blur_enum":
        match = _ENUM.search(line)
        if not match:
            return None
        return line[: match.start(1)] + "an appropriate value" + line[match.end(1):]

    raise ValueError(f"Unknown mutation kind: {kind}")


def apply_mutation(
    spec_dir: Path,
    mutated_dir: Path,
    *,
    kind: str,
    index: int = 0,
) -> Manifest:
    """Copy the spec tree, apply one mutation of ``kind``, and return the manifest."""
    if kind not in MUTATIONS:
        raise ValueError(f"Unknown mutation {kind!r}. Available: {', '.join(MUTATIONS)}")

    if mutated_dir.exists():
        shutil.rmtree(mutated_dir)
    shutil.copytree(spec_dir, mutated_dir)

    manifest = Manifest(spec_dir=str(spec_dir), mutated_dir=str(mutated_dir))

    pattern = {
        "drop_constraint": _CONSTRAINT,
        "contradict": _CONSTRAINT,
        "vague_quantity": _QUANTITY,
        "drop_error_case": _ERROR_CASE,
        "blur_enum": _ENUM,
    }[kind]

    for path in _spec_files(mutated_dir):
        text = path.read_text(encoding="utf-8")
        candidates = [(n, line) for n, line in _sentences(text) if pattern.search(line)]
        chosen = _pick(candidates, index)
        if chosen is None:
            continue
        line_number, original = chosen
        replacement = _mutate_line(kind, original)
        if replacement is None:
            continue

        lines = text.splitlines()
        lines[line_number - 1] = replacement
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        relative = str(path.relative_to(mutated_dir))
        manifest.mutations.append(
            Mutation(
                kind=kind,
                file=relative,
                line=line_number,
                original=original.strip(),
                replacement=replacement.strip(),
                expect_anchor_file=relative,
            )
        )
        return manifest

    raise RuntimeError(
        f"No line in {spec_dir} was eligible for mutation {kind!r} — "
        "the spec may be too short or lack hard constraints to remove."
    )


def verify(manifest: dict[str, Any], blockers: list[dict[str, Any]]) -> dict[str, Any]:
    """Did the loop detect the injected defect, and did it land in the right place?

    Localization is checked separately from detection on purpose. A loop that
    raises blockers everywhere would score well on detection alone and be
    useless.
    """
    results = []
    for mutation in manifest.get("mutations", []):
        expected = mutation["expect_anchor_file"]
        anchored = [
            b for b in blockers
            if (b.get("spec_anchor") or {}).get("file", "").endswith(Path(expected).name)
        ]
        results.append({
            "kind": mutation["kind"],
            "expected_file": expected,
            "detected": bool(blockers),
            "localized": bool(anchored),
            "matching_blockers": [b.get("id") for b in anchored],
        })

    localized = sum(1 for r in results if r["localized"])
    return {
        "mutations": len(results),
        "localized": localized,
        "passed": bool(results) and localized == len(results),
        "results": results,
    }
