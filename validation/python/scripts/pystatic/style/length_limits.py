"""Style adapter for one Python project: module length, comment length and
PEP 695 generic syntax (`def f[T]`, `class C[T]`, `type X = ...`), which blinds semgrep's
`async def` patterns in the file holding it."""

from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from pylint.lint import Run
from pylint.reporters import CollectingReporter

from pystatic.style.dead_code import project_paths

MAX_MODULE_LINES = 3000
MAX_COMMENT_LINES = 5
PYTHON_SOURCES = ["**/*.py", "**/*.pyi"]
TEST_NAMES = ("test_*.py", "*_test.py", "conftest.py")
TEST_DIRECTORIES = frozenset({"tests", "fixtures"})
MODULE_LENGTH_RULE = "pylint-too-many-lines"
COMMENT_LENGTH_RULE = "comment-length"
MODULE_LENGTH_FIX = "split the module by responsibility behind a delegating entry point"
SUPPRESSION_FIX = "remove the pylint pragma; the length limits admit no suppression"
SUPPRESSION_SYMBOLS = ("locally-disabled", "file-ignored", "suppressed-message")
PYLINT_SYMBOLS = ("too-many-lines", *SUPPRESSION_SYMBOLS)
COMMENT_LENGTH_FIX = (
    f"say what and why in at most {MAX_COMMENT_LINES} lines and link the owning heading"
)
PEP695_RULE = "pep695-generic-syntax"
PEP695_FIX = "declare a module-level TypeVar or ParamSpec, or a TypeAliasType alias"
STYLE_DOC = "/README.md#length-limits"
DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
GENERIC = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True, order=True)
class Finding:
    """One `path:line:col: rule: message (fix: ...; doc: ...)` report line."""

    path: str
    line: int
    col: int
    rule: str
    message: str
    fix: str

    def render(self) -> str:
        location = f"{self.path}:{self.line}:{self.col}"
        return f"{location}: {self.rule}: {self.message} (fix: {self.fix}; doc: {STYLE_DOC})"


def is_production(path: Path, repository: Path) -> bool:
    """Production is any Python file outside a `tests/` or `fixtures/` directory and not a test
    module; a check's fixture is that check's test input."""
    relative = path.resolve().relative_to(repository.resolve())
    return not TEST_DIRECTORIES & set(relative.parts[:-1]) and not any(
        fnmatch(relative.name, pattern) for pattern in TEST_NAMES
    )


def _relative(path: Path, repository: Path) -> str:
    return path.resolve().relative_to(repository.resolve()).as_posix()


def module_length_findings(paths: Sequence[Path], repository: Path) -> list[Finding]:
    """Pylint `too-many-lines` (C0302) with the ceiling the style states.

    Pylint's own suppression messages are enabled too, so an inline disable or
    `skip-file` pragma is itself a finding rather than a silent pass.
    """
    if not paths:
        return []
    reporter = CollectingReporter()
    # Parallel workers receive a pickled linter, so its reporter holds no live stream.
    reporter.out = io.StringIO()
    Run(
        [
            "--disable=all",
            f"--enable={','.join(PYLINT_SYMBOLS)}",
            f"--max-module-lines={MAX_MODULE_LINES}",
            "--persistent=n",
            "--score=n",
            "--jobs=0",
            *(str(path) for path in paths),
        ],
        reporter=reporter,
        exit=False,
    )
    findings: list[Finding] = []
    for message in reporter.messages:
        if message.symbol == "too-many-lines":
            rule, fix = MODULE_LENGTH_RULE, MODULE_LENGTH_FIX
        elif message.symbol in SUPPRESSION_SYMBOLS:
            rule, fix = f"pylint-{message.symbol}", SUPPRESSION_FIX
        else:
            error = f"pylint {message.symbol} in {message.abspath}: {message.msg}"
            raise ValueError(error)
        findings.append(
            Finding(
                _relative(Path(message.abspath), repository),
                message.line,
                message.column + 1,
                rule,
                message.msg,
                fix,
            )
        )
    return findings


def _comment_blocks(source: str) -> Iterator[tuple[int, int, int]]:
    """Runs of consecutive whole-line `#` comments as (first line, column, line count)."""
    lines: dict[int, int] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and not token.line[: token.start[1]].strip():
            lines[token.start[0]] = token.start[1]
    for start in sorted(line for line in lines if line - 1 not in lines):
        end = start
        while end + 1 in lines:
            end += 1
        yield start, lines[start], end - start + 1


def _docstrings(tree: ast.Module) -> Iterator[tuple[str, ast.Expr]]:
    for node in ast.walk(tree):
        if not isinstance(node, DOCUMENTED) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            owner = "module" if isinstance(node, ast.Module) else repr(node.name)
            yield owner, first


def comment_length_findings(path: Path, repository: Path) -> list[Finding]:
    """Comment blocks and module, class or function docstrings above the line limit."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    relative = _relative(path, repository)
    findings = [
        Finding(
            relative,
            line,
            column + 1,
            COMMENT_LENGTH_RULE,
            f"comment block spans {count} lines (limit {MAX_COMMENT_LINES})",
            COMMENT_LENGTH_FIX,
        )
        for line, column, count in _comment_blocks(source)
        if count > MAX_COMMENT_LINES
    ]
    for owner, docstring in _docstrings(tree):
        count = (docstring.end_lineno or docstring.lineno) - docstring.lineno + 1
        if count > MAX_COMMENT_LINES:
            findings.append(
                Finding(
                    relative,
                    docstring.lineno,
                    docstring.col_offset + 1,
                    COMMENT_LENGTH_RULE,
                    f"docstring of {owner} spans {count} lines (limit {MAX_COMMENT_LINES})",
                    COMMENT_LENGTH_FIX,
                )
            )
    return findings


def pep695_findings(path: Path, repository: Path) -> list[Finding]:
    """PEP 695 type parameters and `type` statements: semgrep stops matching `async def`
    patterns in a file holding either, so the generic is spelled with `typing` instead."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    relative = _relative(path, repository)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.TypeAlias):
            subject = f"type statement {node.name.id!r}"
        elif isinstance(node, GENERIC) and node.type_params:
            subject = f"type parameters of {node.name!r}"
        else:
            continue
        message = f"PEP 695 {subject}"
        findings.append(
            Finding(relative, node.lineno, node.col_offset + 1, PEP695_RULE, message, PEP695_FIX)
        )
    return findings


def project_sources(root: Path) -> list[Path]:
    """The project's own Python files plus the external roots it declares it checks."""
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    ownership = manifest.get("tool", {}).get("source-ownership", {})
    external = [
        pattern for pattern in ownership.get("external", []) if pattern.endswith((".py", ".pyi"))
    ]
    return project_paths(root, [*PYTHON_SOURCES, *external])


def check_project(root: Path, repository: Path) -> list[Finding]:
    """Module length over the production files; comment length and generic syntax over
    every file."""
    paths = project_sources(root)
    if not paths:
        msg = "no owned Python files in length-limit scope"
        raise ValueError(msg)
    production = [path for path in paths if is_production(path, repository)]
    findings = module_length_findings(production, repository)
    for path in paths:
        findings.extend(comment_length_findings(path, repository))
        findings.extend(pep695_findings(path, repository))
    return sorted(findings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--repository", type=Path, help="report paths relative to it")
    args = parser.parse_args()
    repository = (args.repository or args.project).resolve()
    try:
        findings = check_project(args.project.resolve(), repository)
    except (OSError, KeyError, SyntaxError, ValueError, tokenize.TokenError) as error:
        sys.stderr.write(f"length-limits tool-error: {error}\n")
        return 2
    for finding in findings:
        sys.stdout.write(f"{finding.render()}\n")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
