"""The target tree a check scans: its root and the directories under it.

A check run standalone reads both from its command line (`pystatic.cli`); a test passes
`root` explicitly or calls `configure` first. Any `fixtures` directory is skipped: it holds
deliberately invalid samples, which self-tests hand to a rule directly."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "DEFAULT_SCAN_ROOTS",
    "configure",
    "iter_files",
    "iter_py_files",
    "repo_rel",
    "scan_dirs",
    "scan_roots",
    "target_root",
]

DEFAULT_SCAN_ROOTS: tuple[str, ...] = ("src", "tests")
SKIPPED_PARTS = frozenset({".git", ".venv", "node_modules", "__pycache__", "fixtures"})


@dataclass
class _Target:
    root: Path = field(default_factory=Path.cwd)
    scan_roots: tuple[str, ...] | None = None


_TARGET = _Target()


def configure(root: Path, scan_roots: Sequence[str] | None = None) -> None:
    """Point every later call at `root`; `scan_roots` (relative to it) narrows the walk."""
    _TARGET.root = root.resolve()
    _TARGET.scan_roots = tuple(scan_roots) if scan_roots else None


def target_root() -> Path:
    return _TARGET.root


def scan_roots(defaults: Sequence[str] = DEFAULT_SCAN_ROOTS) -> tuple[str, ...]:
    """The configured scan directories (relative to the target root), else `defaults`."""
    return _TARGET.scan_roots or tuple(defaults)


def scan_dirs(defaults: Sequence[str] = DEFAULT_SCAN_ROOTS) -> list[Path]:
    """`scan_roots(defaults)`, resolved under the target root."""
    return [_TARGET.root / r for r in scan_roots(defaults)]


def _skipped(path: Path, base: Path) -> bool:
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        parts = path.parts
    return bool(SKIPPED_PARTS.intersection(parts[:-1]))


def iter_files(
    suffix: str, roots: Sequence[str] | None = None, *, root: Path | None = None
) -> list[Path]:
    """Every `*suffix` file under `roots` (relative to `root`), skipped parts excluded, sorted."""
    base = _TARGET.root if root is None else root
    chosen = tuple(roots) if roots is not None else scan_roots()
    return sorted(
        p
        for r in chosen
        if (base / r).is_dir()
        for p in (base / r).rglob(f"*{suffix}")
        if not _skipped(p, base)
    )


def iter_py_files(roots: Sequence[str] | None = None, *, root: Path | None = None) -> list[Path]:
    """Every `.py` file under the scan roots, fixtures excluded, sorted."""
    return iter_files(".py", roots, root=root)


def repo_rel(path: Path) -> str:
    """`path` relative to the target root (`walk_up`, so a test's tmp path still reads)."""
    root = _TARGET.root
    resolved = path if path.is_absolute() else (root / path)
    try:
        return str(resolved.relative_to(root, walk_up=True))
    except ValueError:
        return str(resolved)
