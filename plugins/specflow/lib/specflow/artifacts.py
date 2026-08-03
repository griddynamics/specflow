"""On-disk layout for a refinement run.

Everything lives in the user's own repo as readable JSON. That is deliberate:
the artifacts are the state, so there is no database to inspect, no server to
query, and the user can read, diff, and edit any of it with the tools they
already have. A refinement run is reviewable in a pull request.

    <outputs_dir>/refine/
      state.json                        round counter + saturation history
      resolutions.json                  decisions made, cumulative
      blockers.json                     current ranked list
      round-01/
        interpretation.concurrency.json
        interpretation.ordering.json
        ...
      contracts/
        schema.sql  api.json  types.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REFINE_SUBDIR = "refine"
STATE_FILE = "state.json"
RESOLUTIONS_FILE = "resolutions.json"
BLOCKERS_FILE = "blockers.json"
CONTRACTS_SUBDIR = "contracts"
INTERPRETATION_PREFIX = "interpretation."


@dataclass(frozen=True)
class Layout:
    """Resolved paths for one project's refinement run."""

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
    def blockers_path(self) -> Path:
        return self.root / BLOCKERS_FILE

    @property
    def contracts_dir(self) -> Path:
        return self.root / CONTRACTS_SUBDIR

    def round_dir(self, number: int) -> Path:
        return self.root / f"round-{number:02d}"

    def interpretation_path(self, number: int, lens: str) -> Path:
        return self.round_dir(number) / f"{INTERPRETATION_PREFIX}{lens}.json"

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

    def interpretations(self, number: int) -> list[Path]:
        directory = self.round_dir(number)
        if not directory.is_dir():
            return []
        return sorted(directory.glob(f"{INTERPRETATION_PREFIX}*.json"))


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


def load_interpretations(layout: Layout, number: int) -> list[dict[str, Any]]:
    """Load every lens artifact for a round, tagging each with its lens name."""
    loaded = []
    for path in layout.interpretations(number):
        data = read_json(path)
        if not isinstance(data, dict):
            raise ValueError(f"{path} should contain an object, got {type(data).__name__}")
        data.setdefault("lens", path.stem.removeprefix(INTERPRETATION_PREFIX))
        data["_path"] = str(path)
        loaded.append(data)
    return loaded


def load_state(layout: Layout) -> dict[str, Any]:
    if not layout.state_path.exists():
        return {"rounds": [], "converged": False}
    return read_json(layout.state_path)


def load_resolutions(layout: Layout) -> list[dict[str, Any]]:
    """Decisions already made. Used to keep later rounds from re-asking."""
    if not layout.resolutions_path.exists():
        return []
    data = read_json(layout.resolutions_path)
    return data.get("resolved", []) if isinstance(data, dict) else data


def resolved_ids(layout: Layout) -> set[str]:
    return {r["blocker_id"] for r in load_resolutions(layout) if "blocker_id" in r}
