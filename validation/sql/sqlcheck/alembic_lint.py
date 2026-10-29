"""Static checks over an Alembic `versions/` directory, AST only (nothing is imported):

- `alembic-lint-noop-downgrade`: `downgrade()` is empty, `pass` or a bare docstring;
- `alembic-lint-not-single-head`: the revision graph has no head, or more than one;
- `alembic-lint-undocumented-destructive-op`: a `drop_table`/`drop_column`/`drop_index`/
  `drop_constraint`, or an `op.execute` whose SQL (parsed by `sqlfluff`) is a `DROP` or a
  `DELETE`, whose object the revision docstring does not name. A `downgrade()` drop of what
  the same revision's `upgrade()` created is exempt, and so is a call whose object name
  cannot be read at all, which no docstring could clear."""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

from sqlfluff.core import FluffConfig, Linter

from sqlcheck.config import Config
from sqlcheck.findings import Finding, StepResult

STEP = "alembic-lint"
DOC = "RULES.md#alembic-lint"

DESTRUCTIVE_ATTRS = frozenset({"drop_table", "drop_column", "drop_index", "drop_constraint"})
CREATE_ATTRS = frozenset(
    {
        "create_table",
        "add_column",
        "create_index",
        "create_unique_constraint",
        "create_foreign_key",
        "create_check_constraint",
        "create_primary_key",
    }
)
# The statement types an `op.execute("...")` literal parses into that count as
# destructive, via `sqlfluff`: a keyword scan would fire on comments, values and names
# such as `deleted_at`.
_SQL_DESTRUCTIVE_TYPES = (
    "drop_table_statement",
    "drop_index_statement",
    "drop_trigger_statement",
    "drop_view_statement",
    "delete_statement",
)
# Keyword arguments a `drop_*`/`create_*` call names its object through instead of (or
# besides) a positional argument: `op.drop_index("ix_x", table_name="x")`.
_OBJECT_KEYWORDS = ("table_name", "column_name", "constraint_name", "index_name")


# project: sqlcheck.toml carries the SQLAlchemy dialect name; sqlfluff spells PostgreSQL "postgres".
_SQLFLUFF_NAMES = {"postgresql": "postgres"}


@cache
def _linter(dialect: str) -> Linter:
    name = _SQLFLUFF_NAMES.get(dialect, dialect)
    return Linter(config=FluffConfig(overrides={"dialect": name, "templater": "raw"}))


def _sql_destructive_object_names(text: str, dialect: str) -> list[str] | None:
    """`None` unless `text` parses as a destructive statement; otherwise the table, index,
    trigger or view names it targets, read off `sqlfluff`'s parse tree."""
    variants = _linter(dialect).parse_string(text).parsed_variants
    if not variants or variants[0].tree is None:
        return None
    stmt = next(variants[0].tree.recursive_crawl(*_SQL_DESTRUCTIVE_TYPES), None)
    if stmt is None:
        return None
    return [
        ref.raw.rsplit(".", 1)[-1].strip('"`[]')
        for ref in stmt.recursive_crawl(
            "table_reference", "index_reference", "trigger_reference", "view_reference"
        )
    ]


def _module_assignment(node: ast.stmt) -> tuple[str | None, ast.expr | None]:
    """`(name, value)` of a module-level `x = ...` or `x: T = ...` (alembic's generated
    `revision`/`down_revision` lines are annotated)."""
    target: ast.expr | None = None
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        target = node.target
    if isinstance(target, ast.Name) and isinstance(node, ast.Assign | ast.AnnAssign):
        return target.id, node.value
    return None, None


class _Revision:
    def __init__(self, path_rel: str, tree: ast.Module) -> None:
        self.path_rel = path_rel
        self.tree = tree
        self.revision: str | None = None
        self.revision_line = 1
        self.down_revisions: tuple[str, ...] = ()
        self.docstring = ast.get_docstring(tree) or ""
        for node in tree.body:
            target, value_node = _module_assignment(node)
            if target not in ("revision", "down_revision") or value_node is None:
                continue
            if target == "revision":
                raw = value_node.value if isinstance(value_node, ast.Constant) else None
                self.revision = raw if isinstance(raw, str) else None
                self.revision_line = node.lineno
            else:
                self.down_revisions = _down_revisions(value_node)

    def function(self, name: str) -> ast.FunctionDef | None:
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None


def _down_revisions(node: ast.expr) -> tuple[str, ...]:
    """A string, or a tuple/list of strings (a merge revision), else nothing."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, ast.Tuple | ast.List):
        constants = [e.value for e in node.elts if isinstance(e, ast.Constant)]
        return tuple(v for v in constants if isinstance(v, str))
    return ()


def _iter_revisions(versions: Path, root: Path) -> list[_Revision]:
    if not versions.is_dir():
        return []
    revisions = []
    for path in sorted(versions.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        revisions.append(_Revision(rel, tree))
    return revisions


def _is_noop_body(body: list[ast.stmt]) -> bool:
    return all(
        isinstance(stmt, ast.Pass)
        or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        for stmt in body
    )


def _check_downgrade(rev: _Revision) -> list[Finding]:
    downgrade = rev.function("downgrade")
    if downgrade is None or not _is_noop_body(downgrade.body):
        return []
    return [
        Finding(
            rev.path_rel,
            downgrade.lineno,
            downgrade.col_offset + 1,
            "alembic-lint-noop-downgrade",
            "downgrade() is a no-op",
            fix="implement the reverse of upgrade(), or raise with the reason none is possible",
            doc=DOC,
        )
    ]


def _op_f_or_literal(node: ast.expr) -> str | None:
    """A literal string, or `op.f("...")`'s own literal: autogenerate wraps constraint and
    index names for naming-convention rendering (`op.drop_index(op.f("ix_x"), ...)`)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "f"
        and node.args
    ):
        return _op_f_or_literal(node.args[0])
    return None


def _call_object_names(call: ast.Call) -> list[str]:
    """Every literal object name a non-`execute` `op.drop_*`/`create_*` call names, from
    its positional arguments and its object-naming keywords."""
    names = [n for arg in call.args if (n := _op_f_or_literal(arg)) is not None]
    names += [
        n
        for kw in call.keywords
        if kw.arg in _OBJECT_KEYWORDS and (n := _op_f_or_literal(kw.value)) is not None
    ]
    return names


def _execute_text(call: ast.Call) -> str | None:
    return _op_f_or_literal(call.args[0]) if call.args else None


def _is_destructive(call: ast.Call, dialect: str) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in DESTRUCTIVE_ATTRS:
        return True
    if func.attr != "execute":
        return False
    text = _execute_text(call)
    return text is not None and _sql_destructive_object_names(text, dialect) is not None


def _object_names(call: ast.Call, dialect: str) -> list[str]:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return []
    if func.attr == "execute":
        text = _execute_text(call)
        return [] if text is None else _sql_destructive_object_names(text, dialect) or []
    return _call_object_names(call)


def _created_object_names(rev: _Revision) -> set[str]:
    """Every object name this revision's own `upgrade()` creates, lowercased."""
    upgrade = rev.function("upgrade")
    if upgrade is None:
        return set()
    names: set[str] = set()
    for call in ast.walk(upgrade):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in CREATE_ATTRS
        ):
            names.update(n.lower() for n in _call_object_names(call))
    return names


def _check_destructive_documented(rev: _Revision, dialect: str) -> list[Finding]:
    findings = []
    created = _created_object_names(rev)
    docstring = rev.docstring.lower()
    for fn_name in ("upgrade", "downgrade"):
        fn = rev.function(fn_name)
        if fn is None:
            continue
        calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
        for call in (c for c in calls if _is_destructive(c, dialect)):
            names = _object_names(call, dialect)
            if not names:
                continue
            if fn_name == "downgrade" and all(n.lower() in created for n in names):
                continue
            if not any(n.lower() in docstring for n in names if n):
                findings.append(
                    Finding(
                        rev.path_rel,
                        call.lineno,
                        call.col_offset + 1,
                        "alembic-lint-undocumented-destructive-op",
                        f"destructive operation in {fn_name}() is not named in the module "
                        "docstring",
                        fix="name the dropped or deleted table, column, index or constraint "
                        "in the module docstring",
                        doc=DOC,
                    )
                )
    return findings


def _check_single_head(revisions: list[_Revision]) -> list[Finding]:
    if not revisions:
        return []
    referenced = {d for r in revisions for d in r.down_revisions}
    heads = [r for r in revisions if r.revision is not None and r.revision not in referenced]
    if len(heads) == 1:
        return []
    message = (
        "no revision head (a cycle, or every revision is somebody's down_revision)"
        if not heads
        else f"{len(heads)} revision heads: {sorted(r.revision or '?' for r in heads)}"
    )
    return [
        Finding(
            r.path_rel,
            r.revision_line,
            1,
            "alembic-lint-not-single-head",
            message,
            fix="every revision but one must be some other revision's down_revision",
            doc=DOC,
        )
        for r in (heads or revisions)
    ]


def check(versions: Path, root: Path | None = None, dialect: str = "sqlite") -> list[Finding]:
    revisions = _iter_revisions(versions, root or versions)
    findings: list[Finding] = []
    for rev in revisions:
        findings += _check_downgrade(rev)
        findings += _check_destructive_documented(rev, dialect)
    findings += _check_single_head(revisions)
    return sorted(findings, key=lambda f: (f.path, f.line, f.col))


def run(config: Config) -> StepResult:
    if config.alembic_versions is None:
        return StepResult.ok(STEP, detail="skipped: [paths].alembic_versions is not set")
    if not config.alembic_versions.is_dir():
        return StepResult.ok(
            STEP, detail=f"skipped: no directory {config.rel(config.alembic_versions)}"
        )
    return StepResult.from_findings(
        STEP, check(config.alembic_versions, config.root, config.dialect)
    )
