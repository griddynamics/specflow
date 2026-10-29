"""No SQL text inside a Python file: statements live in query files and are loaded.

Findings: a query-shaped string constant, a loader call whose literal path resolves to no
file, and a query file no loader call reaches. In migration directories only text handed
to `execute(...)` / `text(...)` counts, since Core DDL constructs are not raw SQL.
Docstrings are exempt. Loader names, scanned directories and orphan roots come from
`[inline_queries]` in the configuration."""

from __future__ import annotations

import ast
import fnmatch
import re
from collections.abc import Callable
from pathlib import Path

from sqlcheck.config import Config
from sqlcheck.files import iter_files
from sqlcheck.findings import Finding, StepResult

STEP = "inline-queries"
DOC = "RULES.md#inline-queries"

_SQL_RES = [
    re.compile(r"\bSELECT\b.+\bFROM\b", re.DOTALL),
    re.compile(r"\bINSERT\s+(?:OR\s+(?:REPLACE|IGNORE|ABORT|FAIL|ROLLBACK)\s+)?INTO\b"),
    re.compile(r"\bUPDATE\b.+\bSET\b", re.DOTALL),
    re.compile(r"\bDELETE\s+FROM\b"),
    re.compile(
        r"\b(?:CREATE|ALTER|DROP)\s+(?:(?:UNIQUE|TEMP|TEMPORARY)\s+)*"
        r"(?:TABLE|INDEX|VIEW|TRIGGER|TYPE)\b"
    ),
    re.compile(r"\bTRUNCATE\s+TABLE\b"),
    re.compile(r"\bVACUUM\b"),
    re.compile(r"\bATTACH\s+DATABASE\b"),
]

# Below this, a string is a token or a label, not a statement: `"SELECT"` alone is a
# column-header constant; a real statement names both a verb and its target.
_MIN_QUERY_LEN = 20

# In a migration, raw text reaching one of these is the only SQL that counts.
_MIGRATION_RAW_CALLS = frozenset({"execute", "text"})


def looks_like_sql(text: str) -> bool:
    if len(text.strip()) < _MIN_QUERY_LEN:
        return False
    return any(p.search(text) for p in _SQL_RES)


def _joined_literal(node: ast.JoinedStr) -> str:
    """The literal parts of an f-string with the `{expr}` holes dropped."""
    parts = (v.value for v in node.values if isinstance(v, ast.Constant))
    return "".join(p for p in parts if isinstance(p, str))


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal_strings(value: ast.expr | None) -> list[str]:
    """The string literals `value` is, or collects; `[]` for anything else."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value.value]
    if isinstance(value, ast.Tuple | ast.List | ast.Set):
        return [
            e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    return []


def _bind_loop_target(target: ast.expr, iterated: ast.expr, out: dict[str, list[str]]) -> None:
    """Bind a loop variable to the strings the iterated constant holds."""
    texts = _literal_strings(iterated) or (
        out.get(iterated.id, []) if isinstance(iterated, ast.Name) else []
    )
    if isinstance(target, ast.Name) and texts:
        out[target.id] = texts


def _str_constants(tree: ast.Module) -> dict[str, list[str]]:
    """`NAME = "..."` / `NAME = ("...", "...")` bindings and loop variables over them, so a
    loader path held in a constant resolves to its strings."""
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            texts = _literal_strings(node.value)
            for target in targets:
                if isinstance(target, ast.Name) and texts:
                    out[target.id] = texts
        elif isinstance(node, ast.For | ast.AsyncFor):
            _bind_loop_target(node.target, node.iter, out)
        elif isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp):
            for generator in node.generators:
                _bind_loop_target(generator.target, generator.iter, out)
    return out


def _fstring_paths(node: ast.JoinedStr, constants: dict[str, list[str]]) -> list[str] | None:
    """Every path an f-string over one known constant can name, or `None`.

    Only bare, `.lower()` or `.upper()` uses of one resolvable name expand."""
    values: list[str] | None = None
    template: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            template.append(part.value)
            continue
        if not isinstance(part, ast.FormattedValue):
            return None
        expr = part.value
        transform: Callable[[str], str] = str
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
            if expr.func.attr not in ("lower", "upper") or expr.args:
                return None
            transform = str.lower if expr.func.attr == "lower" else str.upper
            expr = expr.func.value
        if not isinstance(expr, ast.Name):
            return None
        bound = constants.get(expr.id)
        if bound is None or values is not None:
            return None
        values = [transform(v) for v in bound]
        template.append("\0")
    if values is None:
        return None
    joined = "".join(template)
    return [joined.replace("\0", value) for value in values]


def _loader_paths(node: ast.Call, constants: dict[str, list[str]]) -> list[str] | None:
    """The query-file paths one loader call names, or `None` when nothing resolves."""
    if not node.args:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return [arg.value]
    if isinstance(arg, ast.Name):
        return constants.get(arg.id)
    if isinstance(arg, ast.JoinedStr):
        return _fstring_paths(arg, constants)
    return None


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Every module/class/function docstring, by identity."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                out.add(id(first.value))
    return out


def _inline_finding(rel: str, node: ast.expr) -> Finding:
    return Finding(
        rel,
        node.lineno,
        node.col_offset + 1,
        "inline-queries-sql",
        "this string constant looks like inline SQL text",
        fix="move it to a .sql file and load it with a configured loader",
        doc=DOC,
    )


def _is_test_file(rel: str) -> bool:
    parts = rel.split("/")
    return "tests" in parts[:-1] or parts[-1].startswith("test_") or parts[-1].endswith("_test.py")


def _is_excluded(config: Config, rel: str) -> bool:
    return _is_test_file(rel) or any(fnmatch.fnmatch(rel, g) for g in config.inline_exclude)


def _is_migration(config: Config, rel: str) -> bool:
    return any(
        rel.startswith(f"{d.strip('/')}/") or f"/{d.strip('/')}/" in rel
        for d in config.inline_migration_dirs
    )


def _migration_findings(rel: str, tree: ast.Module) -> list[Finding]:
    """Raw text handed to `execute(...)` or `text(...)` in a migration module."""
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in _MIGRATION_RAW_CALLS:
            continue
        for arg in node.args:
            text = (
                arg.value
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                else _joined_literal(arg)
                if isinstance(arg, ast.JoinedStr)
                else None
            )
            if text is not None and looks_like_sql(text):
                findings.append(_inline_finding(rel, arg))
    return findings


def _check_loader_call(
    config: Config,
    rel: str,
    node: ast.Call,
    referenced: set[Path],
    constants: dict[str, list[str]],
) -> Finding | None:
    name = _call_name(node)
    if name is None or name not in config.inline_loaders:
        return None
    paths = _loader_paths(node, constants)
    if paths is None:
        return None
    base = config.inline_loaders[name]
    for literal in paths:
        target = (config.root / base / literal).resolve()
        referenced.add(target)
        if not target.is_file():
            return Finding(
                rel,
                node.lineno,
                node.col_offset + 1,
                "inline-queries-missing-file",
                f"{name}({literal!r}) does not resolve to an existing file",
                fix=f"create {base}/{literal} or fix the path",
                doc=DOC,
            )
    return None


def _fstring_parts(tree: ast.Module) -> set[int]:
    """Every string constant nested in an f-string, by identity: the f-string reports itself."""
    return {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in ast.walk(node)
        if isinstance(part, ast.Constant)
    }


def _node_finding(
    config: Config,
    rel: str,
    node: ast.AST,
    referenced: set[Path],
    docstrings: set[int],
    constants: dict[str, list[str]],
    *,
    scan_text: bool,
) -> Finding | None:
    if isinstance(node, ast.Call):
        return _check_loader_call(config, rel, node, referenced, constants)
    if not scan_text:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if id(node) in docstrings:
            return None
        return _inline_finding(rel, node) if looks_like_sql(node.value) else None
    if isinstance(node, ast.JoinedStr):
        return _inline_finding(rel, node) if looks_like_sql(_joined_literal(node)) else None
    return None


def _scan_file(config: Config, path: Path, referenced: set[Path]) -> list[Finding]:
    rel = config.rel(path)
    if _is_excluded(config, rel):
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []  # a syntax error is the linter's finding, not this rule's
    is_migration = _is_migration(config, rel)
    findings = _migration_findings(rel, tree) if is_migration else []
    docstrings = _docstring_nodes(tree) | _fstring_parts(tree)
    constants = _str_constants(tree)
    findings += [
        finding
        for node in ast.walk(tree)
        if (
            finding := _node_finding(
                config, rel, node, referenced, docstrings, constants, scan_text=not is_migration
            )
        )
        is not None
    ]
    return sorted(findings, key=lambda f: (f.line, f.col))


def _orphan_findings(config: Config, referenced: set[Path]) -> list[Finding]:
    roots = config.inline_orphan_roots or config.sql_dirs
    findings = []
    for suffix in config.inline_query_suffixes:
        for path in iter_files(config.root, roots, suffix):
            if path.resolve() in referenced:
                continue
            rel = config.rel(path)
            findings.append(
                Finding(
                    rel,
                    1,
                    1,
                    "inline-queries-orphan-file",
                    f"no configured loader call references {rel}",
                    fix="reference it from a loader call, or delete it",
                    doc=DOC,
                )
            )
    return findings


def check(config: Config) -> list[Finding]:
    scan_dirs = tuple(dict.fromkeys((*config.python_dirs, *config.inline_migration_dirs)))
    referenced: set[Path] = set()
    findings = [
        f
        for path in iter_files(config.root, scan_dirs, ".py")
        for f in _scan_file(config, path, referenced)
    ]
    if config.inline_loaders:
        findings += _orphan_findings(config, referenced)
    return sorted(findings, key=lambda f: (f.path, f.line, f.col))


def run(config: Config) -> StepResult:
    scan_dirs = tuple(dict.fromkeys((*config.python_dirs, *config.inline_migration_dirs)))
    if not iter_files(config.root, scan_dirs, ".py"):
        return StepResult.ok(STEP, detail="skipped: no python file under [paths].python")
    detail = (
        ""
        if config.inline_loaders
        else "degraded: [inline_queries].loaders is empty, so only the string check ran"
    )
    return StepResult.from_findings(STEP, check(config), detail)
