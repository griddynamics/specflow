"""Every tracked or untracked-but-not-ignored source file belongs to a project whose checks
run over it, or is declared external: by a project in its own `pyproject.toml`, or in the
`[[source-ownership.external]]` entries of the configuration, each with a written reason and,
when a project's checks run over the files, that `owner`. Reads `[source-ownership]`; skipped
without it. Doc: /README.md#source-ownership"""

from __future__ import annotations

import tomllib
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import repo_rel, target_root
from pystatic.harness import Finding, StepResult
from pystatic.harness.gitutil import glob_tracked

STEP = "source-ownership"
DOC = "/README.md#source-ownership"
SOURCE_EXTENSIONS = (
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".css",
    ".sql",
    ".cypher",
    ".sh",
)


def external_sources(projects: Sequence[Path]) -> set[Path]:
    """Files each project declares it checks although they sit outside it."""
    external: set[Path] = set()
    for project in projects:
        manifest = project / "pyproject.toml"
        data = tomllib.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
        patterns = data.get("tool", {}).get("source-ownership", {}).get("external", [])
        matches = (path for pattern in patterns for path in project.glob(pattern))
        external.update(path.resolve() for path in matches if path.is_file())
    return external


@dataclass(frozen=True)
class Declaration:
    """One `[[source-ownership.external]]` entry: glob `paths` under the root, the `reason`
    they sit outside every project and, when one runs its checks over them, the `owner`."""

    paths: tuple[str, ...]
    reason: str
    owner: str | None = None


def _declaration(entry: Any, projects: Collection[str]) -> Declaration:
    if not isinstance(entry, dict) or set(entry) - {"paths", "reason", "owner"}:
        msg = "each [[source-ownership.external]] entry takes only `paths`, `reason` and `owner`"
        raise config.ConfigError(msg)
    paths = config.string_list(entry, "paths", [])
    reason = entry.get("reason")
    owner = entry.get("owner")
    if not paths:
        msg = "a [[source-ownership.external]] entry needs a non-empty `paths` list"
        raise config.ConfigError(msg)
    if not isinstance(reason, str) or not reason.strip():
        msg = f"the [[source-ownership.external]] entry for {paths} needs a written `reason`"
        raise config.ConfigError(msg)
    if owner is not None and (not isinstance(owner, str) or owner not in projects):
        msg = f"`owner` {owner!r} of the entry for {paths} is not one of the configured projects"
        raise config.ConfigError(msg)
    return Declaration(tuple(paths), reason.strip(), owner)


def declarations(table: dict[str, Any], projects: Collection[str]) -> list[Declaration]:
    """The configuration's `[[source-ownership.external]]` entries, validated."""
    entries = table.get("external", [])
    if not isinstance(entries, list):
        msg = "`external` must be an array of tables ([[source-ownership.external]])"
        raise config.ConfigError(msg)
    return [_declaration(entry, projects) for entry in entries]


def declared_sources(root: Path, entries: Sequence[Declaration]) -> set[Path]:
    """Files the configuration declares external. A pattern that matches no file is a
    configuration error: a declaration must not outlive the files it justifies."""
    declared: set[Path] = set()
    for entry in entries:
        for pattern in entry.paths:
            matches = {path.resolve() for path in root.glob(pattern) if path.is_file()}
            if not matches:
                msg = f"external pattern {pattern!r} matches no file under the root"
                raise config.ConfigError(msg)
            declared.update(matches)
    return declared


def unowned_sources(
    root: Path,
    sources: Sequence[Path],
    projects: Sequence[Path],
    extensions: Sequence[str],
    *,
    declared: Collection[Path] = frozenset(),
) -> list[Path]:
    external = external_sources(projects) | {path.resolve() for path in declared}
    resolved_projects = [project.resolve() for project in projects]
    return [
        path
        for path in sources
        if path.suffix in extensions
        and path.resolve() not in external
        and not any(path.resolve().is_relative_to(project) for project in resolved_projects)
        and path.resolve().is_relative_to(root.resolve())
    ]


def check() -> list[Finding]:
    table = config.section(STEP)
    root = target_root()
    names = config.string_list(table, "projects", [])
    projects = [root / p for p in names]
    missing = [p for p in projects if not p.is_dir()]
    if missing:
        msg = f"configured projects do not exist: {[repo_rel(p) for p in missing]}"
        raise config.ConfigError(msg)
    extensions = config.string_list(table, "extensions", list(SOURCE_EXTENSIONS))
    declared = declared_sources(root, declarations(table, names))
    sources = [root / name for name in glob_tracked(".", cwd=root)]
    return [
        Finding(
            repo_rel(path),
            1,
            1,
            "source-without-validation-owner",
            "no configured project runs its checks over this source",
            fix="move it into a project, declare it under that project's "
            "[tool.source-ownership] external, or add a [[source-ownership.external]] entry "
            "with its owner or the reason no project validates it",
            doc=DOC,
        )
        for path in sorted(unowned_sources(root, sources, projects, extensions, declared=declared))
    ]


def run() -> StepResult:
    if not config.has_section(STEP):
        return StepResult.ok(STEP, detail=f"skipped: no [{STEP}] table in the configuration")
    return StepResult.from_findings(STEP, check())


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
