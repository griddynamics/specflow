"""Every member of a registry enum owns exactly one file in its directory, and nothing else is
there. Reads `[[registry-files]]` (enum read by AST, never imported); skipped without it.
Doc: /README.md#registry-files"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import repo_rel, target_root
from pystatic.harness import Finding, StepResult

STEP = "registry-files"
DOC = "/README.md#registry-files"


@dataclass(frozen=True)
class Registry:
    enum_path: Path
    enum_class: str
    directory: Path
    pattern: str = "{value}.md"
    exclude: frozenset[str] = frozenset()
    ignore: frozenset[str] = field(default_factory=lambda: frozenset({"__pycache__"}))


def enum_members(path: Path, class_name: str) -> dict[str, str]:
    """`{member name: string value}` of `class_name` in `path`, read by AST."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            members: dict[str, str] = {}
            for stmt in node.body:
                target: ast.expr | None = None
                value: ast.expr | None = None
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target, value = stmt.targets[0], stmt.value
                elif isinstance(stmt, ast.AnnAssign):
                    target, value = stmt.target, stmt.value
                if isinstance(target, ast.Name) and isinstance(value, ast.Constant):
                    members[target.id] = str(value.value)
            return members
    msg = f"{path}: no top-level class {class_name}"
    raise config.ConfigError(msg)


def expected_files(registry: Registry) -> dict[str, str]:
    """`{file name: member name}` for every member the registry requires a file for."""
    members = enum_members(registry.enum_path, registry.enum_class)
    return {
        registry.pattern.format(name=name, value=value): name
        for name, value in members.items()
        if name not in registry.exclude
    }


def check_registry(registry: Registry) -> list[Finding]:
    expected = expected_files(registry)
    findings = [
        Finding(
            repo_rel(registry.directory / file_name),
            1,
            1,
            "registry-files-missing",
            f"{registry.enum_class}.{member} has no {file_name} in {repo_rel(registry.directory)}",
            fix=f"add {file_name}",
            doc=DOC,
        )
        for file_name, member in sorted(expected.items())
        if not (registry.directory / file_name).is_file()
    ]
    if registry.directory.is_dir():
        findings += [
            Finding(
                repo_rel(entry),
                1,
                1,
                "registry-files-stray",
                f"{entry.name} is not the one file of a {registry.enum_class} member",
                fix="fold its content into the member's file and delete it",
                doc=DOC,
            )
            for entry in sorted(registry.directory.iterdir())
            if entry.name not in expected and entry.name not in registry.ignore
        ]
    return findings


def _registry(entry: Any) -> Registry:
    if not isinstance(entry, dict) or not isinstance(entry.get("enum"), str):
        msg = "each [[registry-files]] entry needs `enum = 'path.py:Class'`"
        raise config.ConfigError(msg)
    module, _, class_name = entry["enum"].partition(":")
    directory = entry.get("directory")
    if not class_name or not isinstance(directory, str):
        msg = "each [[registry-files]] entry needs `enum = 'path.py:Class'` and `directory`"
        raise config.ConfigError(msg)
    root = target_root()
    return Registry(
        enum_path=root / module,
        enum_class=class_name,
        directory=root / directory,
        pattern=str(entry.get("pattern", "{value}.md")),
        exclude=frozenset(config.string_list(entry, "exclude", [])),
        ignore=frozenset(config.string_list(entry, "ignore", ["__pycache__"])),
    )


def check() -> list[Finding]:
    entries = config.section(STEP)
    registries = [_registry(entry) for entry in (entries if isinstance(entries, list) else [])]
    findings = [f for registry in registries for f in check_registry(registry)]
    return sorted(findings, key=lambda f: (f.path, f.line, f.col))


def run() -> StepResult:
    if not config.section(STEP):
        return StepResult.ok(STEP, detail=f"skipped: no [[{STEP}]] entry in the configuration")
    return StepResult.from_findings(STEP, check())


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
