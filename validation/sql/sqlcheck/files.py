"""The file walk every file-based step shares: the files with one suffix under the
configured directories, skipping virtual environments, caches and build output."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

SKIPPED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "build",
        "dist",
    }
)


def iter_files(root: Path, dirs: Iterable[str], suffix: str) -> list[Path]:
    """Every `*<suffix>` file under `root/<dir>` for each of `dirs`, sorted, de-duplicated."""
    found: set[Path] = set()
    for entry in dirs:
        base = root / entry
        if base.is_file() and base.suffix == suffix:
            found.add(base)
            continue
        if not base.is_dir():
            continue
        found.update(
            path
            for path in base.rglob(f"*{suffix}")
            if path.is_file() and not SKIPPED_DIRS.intersection(path.relative_to(base).parts)
        )
    return sorted(found)
