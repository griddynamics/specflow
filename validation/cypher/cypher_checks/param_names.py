"""Parameter names match their call sites: a loaded query is run with the `$params` it declares.

For every `CONST = LOADER("path/to/query.cypher")` assignment the loader names, each
`<receiver>.run(CONST, key=value, ...)` call in the scanned Python tree may pass only keyword
names the query file declares as `$key`. Run as
`python -m cypher_checks param-names ROOT --loader NAME=BASE_DIR [...]`.
"""

from __future__ import annotations

import argparse
import ast
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cypher_checks.findings import Finding, report, sort_key
from cypher_checks.lint import code_text
from cypher_checks.query_files import glob_matches

__all__ = ["DEFAULT_EXCLUDES", "DOC", "Config", "check", "main"]

DOC = "RULES.md#parameter-names-match-call-sites"

#: Tool and VCS directories hold no project source; tests are scanned on purpose.
DEFAULT_EXCLUDES = ("**/.*/**", "**/node_modules/**")
_PARAM = re.compile(r"\$(\w+)")
#: The driver call every query goes through: `tx.run(...)` or `session.run(...)`.
_RUN_METHOD = "run"


@dataclass(frozen=True)
class Config:
    """What one run scans. `loaders` maps a loader function name to the directory its path
    argument is relative to."""

    root: Path
    loaders: Mapping[str, Path] = field(default_factory=dict)
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES


@dataclass(frozen=True)
class _Call:
    """One `<receiver>.run(...)` call: where it is, the names it passes and its keywords."""

    path: Path
    line: int
    arg_names: frozenset[str]
    keywords: frozenset[str]


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _loader_target(node: ast.Assign, config: Config) -> Path | None:
    """The query file a `CONST = LOADER("literal")` assignment loads, or `None`."""
    call = node.value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or not call.args:
        return None
    base = config.loaders.get(call.func.id)
    first = call.args[0]
    if base is None or not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return None
    return (base / first.value).resolve()


def _scan(path: Path, config: Config) -> tuple[list[tuple[Path, str]], list[_Call]]:
    """`(loaded (query file, constant name) pairs, run calls)` of one Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return [], []  # a syntax error is the linter's finding, not this rule's
    loaded: list[tuple[Path, str]] = []
    calls: list[_Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            target = _loader_target(node, config)
            if target is not None:
                loaded.extend((target, t.id) for t in node.targets if isinstance(t, ast.Name))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == _RUN_METHOD
        ):
            calls.append(
                _Call(
                    path,
                    node.lineno,
                    frozenset(a.id for a in node.args if isinstance(a, ast.Name)),
                    frozenset(k.arg for k in node.keywords if k.arg is not None),
                )
            )
    return loaded, calls


def check(config: Config) -> list[Finding]:
    """Every keyword a run call passes that the loaded query file does not declare."""
    constants: dict[str, list[Path]] = {}
    calls: list[_Call] = []
    for path in sorted(config.root.rglob("*.py")):
        rel = _relative(path, config.root)
        if any(glob_matches(rel, g) for g in config.excludes):
            continue
        loaded, found = _scan(path, config)
        for target, name in loaded:
            constants.setdefault(name, []).append(target)
        calls.extend(found)
    declared: dict[Path, set[str]] = {}
    findings: set[Finding] = set()
    for call in calls:
        for name in sorted(call.arg_names & constants.keys()):
            for target in constants[name]:
                if not target.is_file():
                    continue
                if target not in declared:
                    text = code_text(target.read_text(encoding="utf-8"))
                    declared[target] = set(_PARAM.findall(text))
                unknown = call.keywords - declared[target]
                if unknown:
                    where = f"{_relative(call.path, config.root)}:{call.line}"
                    findings.add(
                        Finding(
                            _relative(target, config.root),
                            1,
                            1,
                            "cypher-lint-param-mismatch",
                            f"{where} passes {sorted(unknown)}, not declared in this file's "
                            "$parameters",
                            fix="match the keyword arguments to the file's $parameter names",
                            doc=DOC,
                        )
                    )
    return sorted(findings, key=sort_key)


def _loader(value: str) -> tuple[str, str]:
    name, sep, base = value.partition("=")
    if not sep or not name or not base:
        message = f"expected NAME=BASE_DIR, got {value!r}"
        raise argparse.ArgumentTypeError(message)
    return name, base


def _config(argv: Sequence[str]) -> Config:
    parser = argparse.ArgumentParser(prog="python -m cypher_checks param-names")
    parser.add_argument("root", type=Path, help="the source tree to scan")
    parser.add_argument(
        "--loader",
        type=_loader,
        action="append",
        default=[],
        required=True,
        help="NAME=BASE_DIR: a query loader function and the directory (under ROOT) its "
        "path argument is relative to; repeatable",
    )
    parser.add_argument("--exclude", action="append", default=[], help="extra glob to skip")
    args = parser.parse_args(argv)
    root: Path = args.root
    return Config(
        root=root,
        loaders={name: root / base for name, base in args.loader},
        excludes=(*DEFAULT_EXCLUDES, *args.exclude),
    )


def main(argv: Sequence[str]) -> int:
    return report(check(_config(argv)))
