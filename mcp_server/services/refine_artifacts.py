"""On-disk layout for a refinement run.

Everything lives in the user's own repo as readable JSON. The artifacts are the
state, so there is no database to inspect and no server to query — the user can
read, diff, and edit any of it with the tools they already have.

    <outputs_dir>/refine/
      state.json                        which blockers each round asked about
      resolutions.json                  decisions made, cumulative
      findings.json                     latest round's merged view
      round-01/
        reading.concurrency.json
        reading.ordering.json
        ...
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REFINE_SUBDIR = "refine"
STATE_FILE = "state.json"
RESOLUTIONS_FILE = "resolutions.json"
FINDINGS_FILE = "findings.json"
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
    def state_path(self) -> Path:
        return self.root / STATE_FILE

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


def load_readings(layout: Layout, number: int) -> list[dict[str, Any]]:
    """Load every lens reading for a round, tagging each with its lens name.

    Shape is checked only far enough to compare the readings. Whether a reading
    is any *good* is a judgment, and no validator was ever going to make it.
    """
    loaded = []
    for path in layout.readings(number):
        data = read_json(path)
        if not isinstance(data, dict):
            raise ValueError(
                f"{path} should contain an object, got {type(data).__name__}"
            )
        data.setdefault("lens", path.stem.removeprefix(READING_PREFIX))
        data["_path"] = str(path)
        loaded.append(data)
    return loaded


def load_state(layout: Layout) -> dict[str, Any]:
    if not layout.state_path.exists():
        return {"rounds": []}
    return read_json(layout.state_path)


def load_findings(layout: Layout) -> dict[str, Any]:
    """The latest merged view, or an empty document before the first round."""
    if not layout.findings_path.exists():
        return {}
    return read_json(layout.findings_path)


def load_resolutions(layout: Layout) -> list[dict[str, Any]]:
    """Decisions already made. Used to keep later rounds from re-asking."""
    if not layout.resolutions_path.exists():
        return []
    data = read_json(layout.resolutions_path)
    return data.get("resolved", []) if isinstance(data, dict) else data


def resolved_ids(layout: Layout) -> set[str]:
    return {r["blocker_id"] for r in load_resolutions(layout) if "blocker_id" in r}
