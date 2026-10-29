"""No Cypher or SQL text inside a Python string; every loaded query file exists and is loaded.

Findings: a query-shaped string, a loader call whose literal path resolves to no file, and a
query file under a `--query-dir` no loader call reaches. Files under a migrations glob are
scanned only at Alembic's raw-text calls (`op.execute`, `sa.text`); docstrings are exempt.
Run as `python -m cypher_checks no-inline-queries ROOT [--loader NAME=BASE_DIR ...]`.
"""

from __future__ import annotations

import argparse
import ast
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cypher_checks.findings import Finding, report, sort_key
from cypher_checks.query_files import glob_matches

__all__ = ["DEFAULT_EXCLUDES", "DEFAULT_MIGRATION_GLOBS", "DOC", "Config", "check", "main"]

DOC = "RULES.md#no-inline-queries"

#: Test code and fixtures build throwaway queries on purpose; tool and VCS directories hold
#: no project source.
DEFAULT_EXCLUDES = (
    "**/.*/**",
    "**/node_modules/**",
    "**/tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/conftest.py",
    "**/fixtures/**",
)
DEFAULT_MIGRATION_GLOBS = ("**/alembic/**", "**/migrations/**")
_QUERY_SUFFIXES = (".cypher", ".sql")


@dataclass(frozen=True)
class Config:
    """What one run scans. `loaders` maps a loader function name to the directory its path
    argument is relative to; `query_dirs` are searched for orphan query files."""

    root: Path
    loaders: Mapping[str, Path] = field(default_factory=dict)
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES
    migration_globs: tuple[str, ...] = DEFAULT_MIGRATION_GLOBS
    query_dirs: tuple[Path, ...] = ()
    orphan_exempt_globs: tuple[str, ...] = ()


# Clause keywords match in their written uppercase, since prose is the false-positive risk.
# A node/relationship pattern separates Cypher from a bare keyword; it is optional only for
# clause forms that cannot carry one (`CALL db.*`, `SHOW`, DDL).
_CYPHER_PATTERN = r"\(\s*\w*\s*(?::\s*\w+|\{)|-\[|\(\s*\)"
_CYPHER_RES = [
    # A reading/writing clause followed by a node or relationship pattern. The label is
    # optional (`MATCH (n {id: $x})`): requiring one lets a label-less MATCH through.
    re.compile(
        rf"\b(?:OPTIONAL\s+MATCH|MATCH|MERGE|CREATE|UNWIND)\b[^\n]{{0,80}}(?:{_CYPHER_PATTERN})"
    ),
    # Procedure calls: no node pattern of their own.
    re.compile(r"\bCALL\s+(?:db|dbms|apoc|gds)\.\w+"),
    # Schema DDL and introspection. `CREATE ... INDEX` is spelled the same in both dialects,
    # so the Cypher reading needs a Cypher-only tell: a `FOR` clause, or an index type SQL
    # has no word for.
    re.compile(r"\bCREATE\s+(?:VECTOR|FULLTEXT|RANGE|POINT|TEXT)\s+INDEX\b"),
    # Cypher's DDL names its target with `FOR (n:Label)` / `FOR ()-[r:TYPE]-()` where SQL
    # writes `ON <table>`; the statement is routinely wrapped across lines.
    re.compile(r"\bCREATE\s+(?:\w+\s+)*(?:CONSTRAINT|INDEX)\b.{0,80}?\bFOR\s*[(\[]", re.DOTALL),
    re.compile(r"\bCREATE\s+CONSTRAINT\b.{0,120}?\bREQUIRE\b", re.DOTALL),
    re.compile(r"\bDROP\s+CONSTRAINT\b"),
    re.compile(r"\bSHOW\s+(?:INDEXES|CONSTRAINTS)\b"),
    # `DETACH DELETE` is Cypher-only -- SQL has no such clause.
    re.compile(r"\bDETACH\s+DELETE\b"),
]
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
    re.compile(r"\bPRAGMA\s+\w+"),
    re.compile(r"\bVACUUM\b"),
    re.compile(r"\bATTACH\s+DATABASE\b"),
]

# Below this, a string is a token or a label, not a statement. `"SELECT"` alone is a
# column-header constant or an enum value; a real statement names both a verb and a target.
_MIN_QUERY_LEN = 20


def looks_like_query_text(text: str) -> str | None:
    """ "cypher" | "sql" | `None` -- the kind of query text `text` resembles."""
    if len(text.strip()) < _MIN_QUERY_LEN:
        return None
    if any(p.search(text) for p in _CYPHER_RES):
        return "cypher"
    if any(p.search(text) for p in _SQL_RES):
        return "sql"
    return None


def _joined_literal(node: ast.JoinedStr) -> str:
    """Best-effort text of an f-string's literal parts, `{expr}` holes dropped."""
    parts = (v.value for v in node.values if isinstance(v, ast.Constant))
    return "".join(p for p in parts if isinstance(p, str))


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _str_constants(tree: ast.Module) -> dict[str, list[str]]:
    """`NAME = "..."` / `NAME = ("...", "...")` bindings and loop variables over them.

    A loader path is often held in a constant, so the constant is resolved to its strings
    rather than every such file looking orphaned.
    """
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


def _bind_loop_target(target: ast.expr, iterated: ast.expr, out: dict[str, list[str]]) -> None:
    """Bind a loop variable to the strings the iterated constant holds."""
    texts = _literal_strings(iterated) or (
        out.get(iterated.id, []) if isinstance(iterated, ast.Name) else []
    )
    if isinstance(target, ast.Name) and texts:
        out[target.id] = texts


def _literal_strings(value: ast.expr | None) -> list[str]:
    """The string literals `value` is, or collects -- `[]` for anything else."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value.value]
    if isinstance(value, ast.Tuple | ast.List | ast.Set):
        return [
            e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    return []


def _fstring_paths(node: ast.JoinedStr, constants: dict[str, list[str]]) -> list[str] | None:
    """Every path an f-string over one known constant can name, or `None`.

    A name bound to a list of literals expands to one file per value; only bare,
    `.lower()` or `.upper()` uses of a resolvable name expand, anything else stays unknown.
    """
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
            return None  # unresolvable, or a second hole this does not model
        values = [transform(v) for v in bound]
        template.append("\0")
    if values is None:
        return None
    joined = "".join(template)
    return [joined.replace("\0", value) for value in values]


def _loader_paths(node: ast.Call, constants: dict[str, list[str]]) -> list[str] | None:
    """The query-file paths one loader call names: a literal argument, the strings a
    constant binds, or the family an f-string over such a constant expands to."""
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


def _check_loader_call(
    rel: str,
    node: ast.Call,
    referenced: set[Path],
    constants: dict[str, list[str]],
    config: Config,
) -> Finding | None:
    name = _call_name(node)
    if name is None or name not in config.loaders:
        return None
    paths = _loader_paths(node, constants)
    if paths is None:
        return None
    base = config.loaders[name]
    for literal in paths:
        target = (base / literal).resolve()
        referenced.add(target)
        if not target.is_file():
            return Finding(
                rel,
                node.lineno,
                node.col_offset + 1,
                "no-inline-queries-missing-file",
                f"{name}({literal!r}) does not resolve to an existing file",
                fix=f"create {base / literal} or fix the path",
                doc=DOC,
            )
    return None


#: In a migration, raw text reaching one of these is the only SQL that counts; Alembic's
#: `op.create_table`/`op.create_index`/`op.bulk_insert` build SQLAlchemy objects.
_MIGRATION_RAW_CALLS = frozenset({"execute", "text"})


def _migration_findings(rel: str, tree: ast.Module) -> list[Finding]:
    """Findings for a migration module: raw text handed to `op.execute(...)` or
    `sa.text(...)`, and nothing else."""
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
            if text is not None and (kind := looks_like_query_text(text)):
                findings.append(_inline_finding(rel, arg, kind))
    return findings


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Every module/class/function docstring, by identity: a docstring quoting a query
    documents it and is never an executed statement."""
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


def _text_finding(rel: str, node: ast.AST, skipped: set[int]) -> Finding | None:
    text: str | None = None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = None if id(node) in skipped else node.value
    elif isinstance(node, ast.JoinedStr):
        text = _joined_literal(node)
    if text is None or not isinstance(node, ast.expr):
        return None
    kind = looks_like_query_text(text)
    return _inline_finding(rel, node, kind) if kind else None


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def scan_file(path: Path, referenced: set[Path], config: Config) -> list[Finding]:
    """This rule's findings for one Python file; loader targets are added to `referenced`."""
    rel = _relative(path, config.root)
    if any(glob_matches(rel, g) for g in config.excludes):
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []  # a syntax error is the linter's finding, not this rule's

    # A migration's ORM/Core DDL is not raw SQL; only `op.execute`/`sa.text` is.
    is_migration = any(glob_matches(rel, g) for g in config.migration_globs)
    findings = _migration_findings(rel, tree) if is_migration else []
    # Docstrings document a query; an f-string's literal parts are read once, through it.
    skipped = _docstring_nodes(tree) | {
        id(part) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for part in n.values
    }
    constants = _str_constants(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            finding = _check_loader_call(rel, node, referenced, constants, config)
        elif is_migration:
            finding = None
        else:
            finding = _text_finding(rel, node, skipped)
        if finding is not None:
            findings.append(finding)
    return sorted(findings, key=lambda f: (f.line, f.col))


def _inline_finding(rel: str, node: ast.expr, kind: str) -> Finding:
    return Finding(
        rel,
        node.lineno,
        node.col_offset + 1,
        f"no-inline-queries-{kind}",
        f"this string constant looks like inline {kind} text",
        fix=f"move it to a .{kind} file and load it through a query loader",
        doc=DOC,
    )


def orphan_findings(referenced: set[Path], config: Config) -> list[Finding]:
    """A `.cypher`/`.sql` file under a query directory no loader call references."""
    findings = []
    for query_dir in config.query_dirs:
        if not query_dir.is_dir():
            continue
        for path in sorted(p for p in query_dir.rglob("*") if p.suffix in _QUERY_SUFFIXES):
            rel = _relative(path, config.root)
            if any(glob_matches(rel, g) for g in config.orphan_exempt_globs):
                continue
            if path.resolve() not in referenced:
                findings.append(
                    Finding(
                        rel,
                        1,
                        1,
                        "no-inline-queries-orphan-file",
                        f"no loader call references {rel}",
                        fix="reference it from a loader call, or delete it",
                        doc=DOC,
                    )
                )
    return findings


def check(config: Config) -> list[Finding]:
    referenced: set[Path] = set()
    findings = [
        f for path in sorted(config.root.rglob("*.py")) for f in scan_file(path, referenced, config)
    ]
    if config.loaders:
        findings += orphan_findings(referenced, config)
    return sorted(findings, key=sort_key)


def _loader(value: str) -> tuple[str, str]:
    name, sep, base = value.partition("=")
    if not sep or not name or not base:
        message = f"expected NAME=BASE_DIR, got {value!r}"
        raise argparse.ArgumentTypeError(message)
    return name, base


def _config(argv: Sequence[str]) -> Config:
    parser = argparse.ArgumentParser(prog="python -m cypher_checks no-inline-queries")
    parser.add_argument("root", type=Path, help="the source tree to scan")
    parser.add_argument(
        "--loader",
        type=_loader,
        action="append",
        default=[],
        help="NAME=BASE_DIR: a query loader function and the directory (under ROOT) its "
        "path argument is relative to; repeatable",
    )
    parser.add_argument("--exclude", action="append", default=[], help="extra glob to skip")
    parser.add_argument("--migrations", action="append", default=[], help="extra migrations glob")
    parser.add_argument(
        "--query-dir",
        action="append",
        default=[],
        help="directory (under ROOT) to search for orphan query files; repeatable",
    )
    parser.add_argument(
        "--orphan-exempt",
        action="append",
        default=[],
        help="glob of query files loaded by other means (e.g. DDL); repeatable",
    )
    args = parser.parse_args(argv)
    root: Path = args.root
    return Config(
        root=root,
        loaders={name: root / base for name, base in args.loader},
        excludes=(*DEFAULT_EXCLUDES, *args.exclude),
        migration_globs=(*DEFAULT_MIGRATION_GLOBS, *args.migrations),
        query_dirs=tuple(root / d for d in args.query_dir),
        orphan_exempt_globs=tuple(args.orphan_exempt),
    )


def main(argv: Sequence[str]) -> int:
    return report(check(_config(argv)))
