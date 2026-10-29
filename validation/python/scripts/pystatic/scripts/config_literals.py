"""Configuration-driven code holds no magic numbers, and configuration classes have no
defaults and are never built bare. Reads `[config-literals]`; skipped without it.
Doc: /README.md#config-literals"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import iter_py_files, repo_rel, target_root
from pystatic.harness import Finding, StepResult

STEP = "config-literals"
DOC = "/README.md#config-literals"

DEFAULT_ALLOWED = frozenset({0, 1, -1})
_FIELD_CONSTRAINT_KEYWORDS = frozenset({"ge", "gt", "le", "lt"})


@dataclass(frozen=True)
class Settings:
    keys: frozenset[str]
    allowed: frozenset[int | float] = DEFAULT_ALLOWED
    config_classes: frozenset[str] = frozenset()


def _numeric_value(node: ast.expr) -> int | float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return None if isinstance(node.value, bool) else node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, int | float)
        and not isinstance(node.operand.value, bool)
    ):
        return -node.operand.value
    return None


def _target_name(node: ast.stmt) -> str | None:
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        return target.id if isinstance(target, ast.Name) else None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _is_keyed_constant(node: ast.stmt, keys: frozenset[str]) -> bool:
    name = _target_name(node)
    return name is not None and name.isupper() and name.lower() in keys


def _exempt_value_nodes(tree: ast.Module, keys: frozenset[str]) -> set[int]:
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and node.value is not None
            and _is_keyed_constant(node, keys)
        ):
            exempt.add(id(node.value))
        if isinstance(node, ast.Subscript):
            # Tuple/list positions identify a value's shape; they are not a tunable quantity.
            exempt.add(id(node.slice))
        if isinstance(node, ast.Call) and _callee_name(node.func) == "Field":
            # Pydantic validation bounds enforce the schema's closed domain.
            exempt.update(
                id(keyword.value)
                for keyword in node.keywords
                if keyword.arg in _FIELD_CONSTRAINT_KEYWORDS
            )
    return exempt


def literal_findings(rel: str, tree: ast.Module, settings: Settings) -> list[Finding]:
    exempt = _exempt_value_nodes(tree, settings.keys)
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.expr):
            continue
        value = _numeric_value(node)
        if value is None or value in settings.allowed or id(node) in exempt:
            continue
        findings.append(
            Finding(
                rel,
                node.lineno,
                node.col_offset + 1,
                "config-literals-numeric",
                f"numeric literal {value!r} in configuration-driven code",
                fix="add a configuration key and read the value from settings",
                doc=DOC,
            )
        )
    return findings


def _defaulted_field_name(stmt: ast.stmt) -> str | None:
    if not isinstance(stmt, ast.AnnAssign) or stmt.value is None:
        return None
    if (
        isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Name)
        and stmt.value.func.id == "Field"
        and not stmt.value.args
        and all(
            keyword.arg not in {"default", "default_factory"} for keyword in stmt.value.keywords
        )
    ):
        return None
    return stmt.target.id if isinstance(stmt.target, ast.Name) else None


def default_field_findings(rel: str, tree: ast.Module, settings: Settings) -> list[Finding]:
    return [
        Finding(
            rel,
            stmt.lineno,
            stmt.col_offset + 1,
            "config-literals-class-default",
            f"{cls.name}.{name} has a default value",
            fix=f"remove the default; {cls.name} is built only from configuration",
            doc=DOC,
        )
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef) and cls.name in settings.config_classes
        for stmt in cls.body
        if (name := _defaulted_field_name(stmt)) is not None
    ]


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def zero_arg_construction_findings(rel: str, tree: ast.Module, settings: Settings) -> list[Finding]:
    return [
        Finding(
            rel,
            node.lineno,
            node.col_offset + 1,
            "config-literals-class-no-config",
            f"{name}() constructed with no configuration",
            fix=f"build {name} from the active configuration",
            doc=DOC,
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and not node.args
        and not node.keywords
        and (name := _callee_name(node.func)) in settings.config_classes
    ]


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return None


def _settings(table: dict[str, object]) -> Settings:
    allowed = table.get("allowed", sorted(DEFAULT_ALLOWED))
    if not isinstance(allowed, list) or not all(
        isinstance(v, int | float) and not isinstance(v, bool) for v in allowed
    ):
        msg = "`allowed` must be a list of numbers"
        raise config.ConfigError(msg)
    return Settings(
        keys=frozenset(k.lower() for k in config.string_list(table, "keys", [])),
        allowed=frozenset(allowed),
        config_classes=frozenset(config.string_list(table, "config-classes", [])),
    )


def numeric_scan_files(entries: list[str]) -> list[Path]:
    """The production modules the numeric rule reads: each `scan` entry is a directory (every
    `.py` under it) or one `.py` file, relative to the target root; `test_*` files are left out.
    An entry that is neither an existing directory nor an existing `.py` file is a
    configuration error, so a typo cannot silently scan nothing."""
    chosen: set[Path] = set()
    for entry in entries:
        target = target_root() / entry
        if target.is_dir():
            chosen.update(target.rglob("*.py"))
        elif target.is_file() and target.suffix == ".py":
            chosen.add(target)
        else:
            msg = f"`scan` entry {entry!r} is neither a directory nor a .py file under the root"
            raise config.ConfigError(msg)
    return sorted(path for path in chosen if not path.name.startswith("test_"))


def check() -> list[Finding]:
    table = config.section(STEP)
    settings = _settings(table)
    findings: list[Finding] = []
    for path in numeric_scan_files(config.string_list(table, "scan", [])):
        if (tree := _parse(path)) is not None:
            findings += literal_findings(repo_rel(path), tree, settings)
    if settings.config_classes:
        for path in iter_py_files():
            tree = _parse(path)
            if tree is not None:
                findings += default_field_findings(repo_rel(path), tree, settings)
                findings += zero_arg_construction_findings(repo_rel(path), tree, settings)
    return sorted(findings, key=lambda f: (f.path, f.line, f.col))


def run() -> StepResult:
    if not config.has_section(STEP):
        return StepResult.ok(STEP, detail=f"skipped: no [{STEP}] table in the configuration")
    return StepResult.from_findings(STEP, check())


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
