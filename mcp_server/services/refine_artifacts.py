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
    value = data.get(key)
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(i, dict) for i in value):
        raise ValueError(
            f"{path}: '{key}' should be a list of objects, got "
            f"{type(value).__name__}"
        )

def load_readings(layout: Layout, number: int) -> list[dict[str, Any]]:
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
    path = layout.grid_path(number)
    if not path.exists():
        return {}
    data = _require_object(path, read_json(path))
    _require_object_list(path, data, "cells")
    return data

def load_coherence(layout: Layout, number: int) -> dict[str, Any]:
    path = layout.coherence_path(number)
    if not path.exists():
        return {}
    data = _require_object(path, read_json(path))
    _require_object_list(path, data, "blockers")
    return data

def load_findings(layout: Layout) -> dict[str, Any]:
    if not layout.findings_path.exists():
        return {}
    return read_json(layout.findings_path)

def load_resolutions(layout: Layout) -> list[dict[str, Any]]:
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
