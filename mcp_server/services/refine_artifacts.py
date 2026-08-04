"""On-disk layout for a refinement run.

Everything lives in the user's own repo as readable JSON. The artifacts are the
state, so there is no database to inspect and no server to query — the user can
read, diff, and edit any of it with the tools they already have.

    <outputs_dir>/refine/
      resolutions.json                  decisions made, cumulative
      findings.json                     latest round's merged view
      round-01/
        grid.json                       the cells every lens must fill
        reading.concurrency.json
        reading.ordering.json
        ...
        coherence.json                  can these answers all be true at once?

Everything under `refine/` belongs to this loop and nothing else reads it — in
particular it sits outside the three directories the 1.0 backend contract
validator searches (the outputs root, `analysis/`, `planning/`), so nothing here
can be taken for a file that flow requires.

Two things an earlier version kept are gone. There is no `state.json`: it existed
only to diff each round against the previous ones, feeding a stop rule the design
cannot justify (see `refine_compare`). Rounds are now simply the directories that
exist. And the loop writes no markdown report of its own — the skill reports to
the user directly, so there was never a file to name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REFINE_SUBDIR = "refine"
RESOLUTIONS_FILE = "resolutions.json"
FINDINGS_FILE = "findings.json"
GRID_FILE = "grid.json"
COHERENCE_FILE = "coherence.json"
READING_PREFIX = "reading."


@dataclass(frozen=True)
class Layout:
    """Resolved paths for one project's refinement run.

    Every path the loop reads or writes is derived here, so a skill never has to
    spell one out. A hardcoded ``docs/refine/...`` in prose stops honouring
    ``outputs_dir`` the moment a user overrides it.
    """

    outputs_dir: Path

    @property
    def root(self) -> Path:
        return self.outputs_dir / REFINE_SUBDIR

    @property
    def resolutions_path(self) -> Path:
        return self.root / RESOLUTIONS_FILE

    @property
    def findings_path(self) -> Path:
        return self.root / FINDINGS_FILE

    def round_dir(self, number: int) -> Path:
        return self.root / f"round-{number:02d}"

    def reading_path(self, number: int, lens: str) -> Path:
        return self.round_dir(number) / f"{READING_PREFIX}{lens}.json"

    def grid_path(self, number: int) -> Path:
        return self.round_dir(number) / GRID_FILE

    def coherence_path(self, number: int) -> Path:
        return self.round_dir(number) / COHERENCE_FILE

    def rounds(self) -> list[int]:
        """Round numbers present on disk, ascending."""
        if not self.root.is_dir():
            return []
        numbers = []
        for entry in self.root.iterdir():
            if entry.is_dir() and entry.name.startswith("round-"):
                suffix = entry.name.removeprefix("round-")
                if suffix.isdigit():
                    numbers.append(int(suffix))
        return sorted(numbers)

    def latest_round(self) -> int | None:
        rounds = self.rounds()
        return rounds[-1] if rounds else None

    def readings(self, number: int) -> list[Path]:
        directory = self.round_dir(number)
        if not directory.is_dir():
            return []
        return sorted(directory.glob(f"{READING_PREFIX}*.json"))

    def has_grid(self, number: int) -> bool:
        return self.grid_path(number).exists()


def layout_for(outputs_dir: str | Path) -> Layout:
    return Layout(Path(outputs_dir))


def read_json(path: Path) -> Any:
    """Read JSON with a message that says which file is broken."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"Not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from None


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def _require_object(path: Path, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{path} should contain an object, got {type(data).__name__}")
    return data


def _require_object_list(path: Path, data: dict[str, Any], key: str) -> None:
    """Refuse a document whose ``key`` is not the list of objects it is read as.

    Only for the round's single-author files. A malformed *reading* is reported
    instead (``refine_compare._reading_problems``) because six lenses write those
    concurrently and one bad file must not cost the round; the grid and the
    coherence pass have one author each, and the grid in particular is the exam
    every lens sat — quietly treating a broken one as absent would report a round
    with no coverage as a round with nothing to cover.
    """
    value = data.get(key)
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(i, dict) for i in value):
        raise ValueError(
            f"{path}: '{key}' should be a list of objects, got "
            f"{type(value).__name__}"
        )


def load_readings(layout: Layout, number: int) -> list[dict[str, Any]]:
    """Load every lens reading for a round, naming each from its filename.

    **The filename is authoritative for ``lens``.** It has to be: the reading's
    own field is written by hand by one of several concurrent subagents, and a
    copy-pasted prompt yields two files whose bodies claim the same lens name.
    Comparison keys answers by lens, so two readings sharing a name collapse into
    one — the disagreement between them disappears while the lens count still
    says two. A mismatch is preserved as ``_lens_declared`` so ``compare`` can
    report it, since it is evidence the fan-out ran one lens twice.

    **One unreadable file does not cost the round.** Broken JSON from a truncated
    write is at least as likely as a wrong shape, and both come from the same
    place: several subagents writing these concurrently, where one failing is a
    routine occurrence. So a file that will not parse is returned as an unusable
    reading — reported by ``compare`` in ``incomplete``, counted in
    ``readings_total`` but not in ``lens_count`` — rather than raised, which would
    throw away five good readings over one bad one. A round where *nothing* is
    usable still refuses, in ``cmd_round``.

    Shape is otherwise checked only far enough to compare the readings. Whether a
    reading is any *good* is a judgment, and no validator was ever going to make
    it.
    """
    loaded = []
    for path in layout.readings(number):
        from_filename = path.stem.removeprefix(READING_PREFIX)
        try:
            data = _require_object(path, read_json(path))
        except (ValueError, OSError) as exc:
            loaded.append({
                "lens": from_filename,
                "_path": str(path),
                "_unreadable": str(exc),
            })
            continue
        declared = data.get("lens")
        data["lens"] = from_filename
        if declared and str(declared) != from_filename:
            data["_lens_declared"] = str(declared)
        data["_path"] = str(path)
        loaded.append(data)
    return loaded


def load_grid(layout: Layout, number: int) -> dict[str, Any]:
    """The round's cell list, or an empty document if this round has no grid.

    Optional on purpose: a round without a grid still compares readings, which
    is what every round did before grids existed.
    """
    path = layout.grid_path(number)
    if not path.exists():
        return {}
    data = _require_object(path, read_json(path))
    _require_object_list(path, data, "cells")
    return data


def load_coherence(layout: Layout, number: int) -> dict[str, Any]:
    """The coherence pass's blockers, or an empty document if it did not run."""
    path = layout.coherence_path(number)
    if not path.exists():
        return {}
    data = _require_object(path, read_json(path))
    _require_object_list(path, data, "blockers")
    return data


def load_findings(layout: Layout) -> dict[str, Any]:
    """The latest merged view, or an empty document before the first round."""
    if not layout.findings_path.exists():
        return {}
    return read_json(layout.findings_path)


def load_resolutions(layout: Layout) -> list[dict[str, Any]]:
    """Decisions already made. Used to keep later rounds from re-asking.

    Validated on the way in because these artifacts are advertised as
    hand-editable, so a user's own edit is a normal input and deserves a message
    naming the file rather than a traceback from wherever it is first indexed.
    """
    path = layout.resolutions_path
    if not path.exists():
        return []
    data = read_json(path)
    if isinstance(data, dict):
        _require_object_list(path, data, "resolved")
        return data.get("resolved", [])
    if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
        raise ValueError(
            f"{path} should contain a list of objects or an object with a "
            f"'resolved' list, got {type(data).__name__}"
        )
    return data


def resolved_ids(layout: Layout) -> set[str]:
    return {r["blocker_id"] for r in load_resolutions(layout) if "blocker_id" in r}
