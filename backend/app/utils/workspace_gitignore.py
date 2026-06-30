"""Seed a deterministic .gitignore into generation workspaces before agents run."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Derived from settings.EXCLUDED_ARTIFACT_PATTERNS — excludes .git (never ignore)
# and standards/ (copied into workspace and should remain tracked).
WORKSPACE_GITIGNORE_ENTRIES: tuple[str, ...] = (
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".uv/",
    "*.egg-info/",
    "node_modules/",
    ".npm/",
    ".yarn/",
    ".pnpm-store/",
    ".angular/",
    "vendor/",
    "target/",
    ".m2/",
    "dist/",
    "build/",
    ".next/",
    ".nuxt/",
    ".output/",
    "out/",
    ".gradle/",
    ".cache/",
)

_GITIGNORE_HEADER = (
    "# SpecFlow workspace defaults — seeded before agents run.\n"
    "# Dependency caches and build output must not be committed.\n"
)


def _normalize_pattern(line: str) -> str:
    """Normalize a gitignore line for presence checks."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    return stripped.rstrip("/")


# Whole-tree wildcards that would match every basename and thus exclude the entire
# working tree if naively fed to shutil.ignore_patterns — never honor these.
_GITIGNORE_MATCH_ALL: frozenset[str] = frozenset({"*", "**", "*.*"})


def read_gitignore_patterns(workspace_path: Path) -> list[str]:
    """Best-effort parse of a workspace ``.gitignore`` into basename patterns for ``shutil.ignore_patterns`` (skips comments, negations, multi-segment, and match-all; swallows I/O errors)."""
    gitignore_path = workspace_path / ".gitignore"
    try:
        if not gitignore_path.is_file():
            return []
        lines = gitignore_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning(
            "Could not read .gitignore in %s (non-fatal): %s", workspace_path, exc
        )
        return []

    patterns: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        candidate = stripped.lstrip("/").rstrip("/")
        if not candidate or "/" in candidate or candidate in _GITIGNORE_MATCH_ALL:
            continue
        if candidate not in patterns:
            patterns.append(candidate)
    return patterns


def ensure_workspace_gitignore(workspace_path: Path) -> bool:
    """Ensure standard ignore patterns exist in the workspace root .gitignore.

    Creates the file when missing. When present, appends any missing entries
    without removing user content.

    Safe to call from init/prep paths: I/O failures are logged and swallowed;
    they never propagate to abort git init or workspace preparation.

    Returns:
        True if the file was created or modified, False if unchanged or on error.
    """
    try:
        return _ensure_workspace_gitignore(workspace_path)
    except OSError as exc:
        logger.warning(
            "Could not seed .gitignore in %s (non-fatal): %s",
            workspace_path,
            exc,
        )
        return False


def _ensure_workspace_gitignore(workspace_path: Path) -> bool:
    gitignore_path = workspace_path / ".gitignore"
    existing_lines: list[str] = []
    if gitignore_path.exists():
        existing_lines = gitignore_path.read_text(encoding="utf-8").splitlines()

    present = {
        _normalize_pattern(line)
        for line in existing_lines
        if _normalize_pattern(line)
    }
    missing = [entry for entry in WORKSPACE_GITIGNORE_ENTRIES if entry.rstrip("/") not in present]
    if not missing and gitignore_path.exists():
        return False

    if not gitignore_path.exists():
        body = _GITIGNORE_HEADER + "\n".join(WORKSPACE_GITIGNORE_ENTRIES) + "\n"
        gitignore_path.write_text(body, encoding="utf-8")
        return True

    suffix = "\n\n# SpecFlow defaults (appended)\n" + "\n".join(missing) + "\n"
    content = gitignore_path.read_text(encoding="utf-8")
    if content and not content.endswith("\n"):
        content += "\n"
    gitignore_path.write_text(content + suffix, encoding="utf-8")
    return True
