"""Two SQLAlchemy rules over the configured Python directories, AST only:

1. `orm-lint-missing-lazy` / `orm-lint-disallowed-lazy`: every `relationship()` declares
   `lazy` as `"raise"`, `"raise_on_sql"` or `"selectin"`, so no attribute access issues a
   hidden query.
2. `orm-lint-unregistered-scan`: a `select()` over a model that is executed with neither a
   `limit` nor a key the request supplies (a function parameter, or a value derived from
   one, in a `where`/`filter`/`filter_by`/`having`/`join`/`select_from`), nor an equality
   on a column the declared schema calls unique, reads every row of the table. Such a read
   is admitted only by the scan register in `sqlcheck.toml` (`[orm_lint.scan_register]`);
   a register entry that admits no read is `orm-lint-stale-scan-entry` /
   `orm-lint-stale-complete-list-entry`, and a method in both lists is
   `orm-lint-doubly-registered-read`.

Rule 2 needs `[orm_lint].models_package` (the package the models are imported from); the
unique columns come from the declared schema and `__tablename__` under `models_dir`."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from sqlcheck.config import Config, ScanRegister
from sqlcheck.files import iter_files
from sqlcheck.findings import Finding, StepResult
from sqlcheck.schema import SqlSchema, load_schema

STEP = "orm-lint"
DOC = "RULES.md#orm-lint"

ALLOWED_LAZY = frozenset({"raise", "raise_on_sql", "selectin"})

#: The session and connection calls that run a statement.
_EXECUTORS = frozenset({"execute", "scalars", "scalar", "stream", "stream_scalars"})
#: A select these are applied to is part of an outer statement.
_SUBQUERY_METHODS = frozenset(
    {
        "subquery",
        "cte",
        "scalar_subquery",
        "exists",
        "alias",
        "lateral",
        "union",
        "union_all",
        "except_",
        "intersect",
        "correlate",
        "label",
    }
)
#: The clauses whose predicate can carry a key.
_KEY_CLAUSES = frozenset({"where", "filter", "filter_by", "having", "join", "select_from"})
#: The calls that fill a collection: a list built from keys holds keys.
_MUTATORS = frozenset({"append", "extend", "add", "update", "insert", "setdefault"})
_SPACES = re.compile(r"\s+")


# --- 1. relationship() declares lazy -----------------------------------------


def _relationship_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "relationship")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "relationship")
        )
    ]


def _check_lazy(rel: str, call: ast.Call) -> Finding | None:
    lazy = next((kw for kw in call.keywords if kw.arg == "lazy"), None)
    if lazy is None:
        return Finding(
            rel,
            call.lineno,
            call.col_offset + 1,
            "orm-lint-missing-lazy",
            "relationship() has no lazy= argument",
            fix=f"pass lazy=... (one of {sorted(ALLOWED_LAZY)})",
            doc=DOC,
        )
    value = lazy.value
    if (
        isinstance(value, ast.Constant)
        and isinstance(value.value, str)
        and value.value in ALLOWED_LAZY
    ):
        return None
    shown = value.value if isinstance(value, ast.Constant) else "<non-literal>"
    return Finding(
        rel,
        value.lineno,
        value.col_offset + 1,
        "orm-lint-disallowed-lazy",
        f"lazy={shown!r} is not one of {sorted(ALLOWED_LAZY)}",
        fix=f"use one of {sorted(ALLOWED_LAZY)}",
        doc=DOC,
    )


# --- 2. A read of every row is registered ------------------------------------


@dataclass(frozen=True)
class ModelContext:
    """What rule 2 knows about the project's models.

    `unique`: `{ModelClass: {column: condition}}`; a `None` condition is unique outright,
    `"true"` is unique only among rows where the column is true."""

    models_package: str = ""
    not_models: frozenset[str] = frozenset()
    unique: dict[str, dict[str, str | None]] = field(default_factory=dict)
    module_root: str = "src"


@dataclass(frozen=True)
class ScanRead:
    """One read of every row: where it is and which register entry, if any, admits it."""

    finding_line: int
    finding_col: int
    method: str
    owner_class: str | None
    admitted_by: str | None


@dataclass
class _Flow:
    calls: list[ast.Call] = field(default_factory=list)
    executed: bool = False
    judged: bool = True


def _truthy_condition(column: str, where: str) -> bool:
    """A partial-unique predicate that a true value of `column` itself satisfies."""
    text = _SPACES.sub(" ", where.strip().lower())
    col = column.lower()
    return text in {"true", col, f"{col} is true", f"{col} = true", f"{col} is 1", f"{col} = 1"}


def model_tables(models_dir: Path | None) -> dict[str, str]:
    """Each ORM model class's table, read off `__tablename__` under `models_dir`."""
    tables: dict[str, str] = {}
    if models_dir is None or not models_dir.is_dir():
        return tables
    for path in sorted(models_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if (
                    isinstance(item, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "__tablename__" for t in item.targets
                    )
                    and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, str)
                ):
                    tables[node.name] = item.value.value
    return tables


def unique_columns(schema: SqlSchema, tables: dict[str, str]) -> dict[str, dict[str, str | None]]:
    """`{ModelClass: {column: condition}}` for every column the schema declares unique: a
    `None` condition is unique outright, `"true"` unique among true rows only, and any
    other partial predicate is left out (no equality alone seeks one row)."""
    by_table: dict[str, dict[str, str | None]] = {}
    for name, table in schema.tables.items():
        columns: dict[str, str | None] = {}
        for col in table.columns.values():
            if not col.unique:
                continue
            if not col.unique_where:
                columns[col.name] = None
            elif _truthy_condition(col.name, col.unique_where):
                columns[col.name] = "true"
        by_table[name] = columns
    return {model: by_table.get(table, {}) for model, table in tables.items()}


def _module_path(rel: str, module_root: str) -> str:
    """`rel`'s dotted module path below the last `module_root` directory."""
    parts = rel.removesuffix(".py").split("/")
    if module_root and module_root in parts:
        parts = parts[len(parts) - parts[::-1].index(module_root) :]
    return ".".join(parts)


def _select_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The names bound to `sqlalchemy.select`, and those bound to the module."""
    selects: set[str] = set()
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlalchemy"):
            selects |= {a.asname or a.name for a in node.names if a.name == "select"}
        elif isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "sqlalchemy"}
    return selects, modules


def _model_names(tree: ast.Module, ctx: ModelContext) -> set[str]:
    """The names this module imports from the ORM model package."""
    if not ctx.models_package:
        return set()
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith(ctx.models_package)
        and node.module not in ctx.not_models
        for alias in node.names
    }


def _is_select(call: ast.Call, selects: set[str], modules: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in selects
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "select"
        and isinstance(func.value, ast.Name)
        and func.value.id in modules
    )


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _bindings(func: ast.AST) -> Iterator[tuple[list[ast.expr], ast.AST]]:
    """Every `(targets, source)` pair that binds a name inside `func`."""
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            yield node.targets, node.value
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            if node.value is not None:
                yield [node.target], node.value
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            yield [node.target], node.iter
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            yield [node.optional_vars], node.context_expr
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATORS
            and isinstance(node.func.value, ast.Name)
        ):
            yield (
                [node.func.value],
                ast.Tuple(elts=[*node.args, *(kw.value for kw in node.keywords)], ctx=ast.Load()),
            )


def _request_values(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The names whose value derives from the function's parameters (`self` and `cls`
    excepted): the keys a request can supply."""
    tainted = {
        a.arg
        for node in ast.walk(func)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
        for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    } - {"self", "cls"}
    for node in ast.walk(func):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            tainted |= {a.arg for a in (node.args.vararg, node.args.kwarg) if a is not None}
    pairs = list(_bindings(func))
    changed = True
    while changed:
        changed = False
        for targets, source in pairs:
            if not _names_in(source) & tainted:
                continue
            new = {n for t in targets for n in _names_in(t)} - tainted
            if new:
                tainted |= new
                changed = True
    return tainted


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    return {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _chain(node: ast.expr, parents: dict[int, ast.AST]) -> tuple[list[ast.Call], ast.expr]:
    """The method calls applied to `node` (`node.where(...).limit(...)`), and the
    outermost expression of that chain."""
    calls: list[ast.Call] = []
    current: ast.expr = node
    while True:
        attribute = parents.get(id(current))
        call = parents.get(id(attribute)) if attribute is not None else None
        if (
            isinstance(attribute, ast.Attribute)
            and attribute.value is current
            and isinstance(call, ast.Call)
            and call.func is attribute
        ):
            calls.append(call)
            current = call
            continue
        return calls, current


def _method(call: ast.Call) -> str:
    return call.func.attr if isinstance(call.func, ast.Attribute) else ""


def _consumer(top: ast.expr, parents: dict[int, ast.AST]) -> tuple[str, str | None]:
    """How the statement ending at `top` is used: `("execute", None)`, `("assign", name)`,
    or `("other", None)`."""
    parent = parents.get(id(top))
    if (
        isinstance(parent, ast.Call)
        and top in parent.args
        and isinstance(parent.func, ast.Attribute)
        and parent.func.attr in _EXECUTORS
    ):
        return "execute", None
    if isinstance(parent, ast.Assign | ast.AnnAssign) and parent.value is top:
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        if len(targets) == 1 and isinstance(targets[0], ast.Name):
            return "assign", targets[0].id
    return "other", None


def _follow(
    start: ast.expr, func: ast.AST, parents: dict[int, ast.AST], flow: _Flow, seen: set[str]
) -> None:
    calls, top = _chain(start, parents)
    flow.calls += calls
    if any(_method(c) in _SUBQUERY_METHODS for c in calls):
        flow.judged = False
        return
    kind, name = _consumer(top, parents)
    if kind == "execute":
        flow.executed = True
    elif kind == "assign" and name is not None:
        if name in seen:
            return
        seen.add(name)
        for use in ast.walk(func):
            if isinstance(use, ast.Name) and use.id == name and isinstance(use.ctx, ast.Load):
                _follow(use, func, parents, flow, seen)
    else:
        flow.judged = False


def _is_one_row_column(column: ast.expr) -> bool:
    """An aggregate `func.*(...)`, an `exists`, or a literal."""
    if isinstance(column, ast.Constant):
        return True
    node: ast.expr = column
    while isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id == "exists"
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr == "exists" or (isinstance(func.value, ast.Name) and func.value.id == "func"):
            return True
        node = func.value
    return False


def _is_aggregate(select: ast.Call, calls: list[ast.Call]) -> bool:
    """Every column one value over the rows, and no `group_by`: one row."""
    return (
        bool(select.args)
        and all(_is_one_row_column(a) for a in select.args)
        and not any(_method(c) == "group_by" for c in calls)
    )


def _is_true(value: ast.expr) -> bool:
    """The literal `True`, or SQLAlchemy's `true()`."""
    if isinstance(value, ast.Constant):
        return value.value is True
    if not (isinstance(value, ast.Call) and not value.args):
        return False
    func = value.func
    if isinstance(func, ast.Name):
        return func.id == "true"
    return isinstance(func, ast.Attribute) and func.attr == "true"


def seeks_unique_column(node: ast.AST, unique: dict[str, dict[str, str | None]]) -> bool:
    """An equality on a column the schema declares unique: one row, whatever the value,
    or, on a column unique only among true rows, the value true."""

    def is_key(side: ast.expr, value: ast.expr) -> bool:
        if not (isinstance(side, ast.Attribute) and isinstance(side.value, ast.Name)):
            return False
        columns = unique.get(side.value.id, {})
        if side.attr not in columns:
            return False
        condition = columns[side.attr]
        return condition is None or (condition == "true" and _is_true(value))

    for test in ast.walk(node):
        if isinstance(test, ast.Compare) and [type(op) for op in test.ops] == [ast.Eq]:
            left, right = test.left, test.comparators[0]
            if is_key(left, right) or is_key(right, left):
                return True
        elif (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Attribute)
            and test.func.attr == "is_"
            and len(test.args) == 1
            and not (isinstance(test.args[0], ast.Constant) and test.args[0].value is None)
            and is_key(test.func.value, test.args[0])
        ):
            return True
    return False


def _carries_key(call: ast.Call, keys: set[str], ctx: ModelContext) -> bool:
    if _method(call) == "join" and any(
        kw.arg in {"isouter", "full"}
        and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
        for kw in call.keywords
    ):
        return False
    values = (*call.args, *(kw.value for kw in call.keywords))
    return any(_names_in(arg) & keys or seeks_unique_column(arg, ctx.unique) for arg in values)


def _reads_every_row(
    select: ast.Call,
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[int, ast.AST],
    models: set[str],
    keys: set[str],
    ctx: ModelContext,
) -> bool:
    flow = _Flow()
    _follow(select, func, parents, flow, set())
    if not (flow.judged and flow.executed):
        return False
    sources = [
        *select.args,
        *(a for c in flow.calls if _method(c) == "select_from" for a in c.args),
    ]
    if not any(_names_in(s) & models for s in sources):
        return False
    if _is_aggregate(select, flow.calls) or any(_method(c) == "limit" for c in flow.calls):
        return False
    return not any(_method(c) in _KEY_CLAUSES and _carries_key(c, keys, ctx) for c in flow.calls)


_Function = ast.FunctionDef | ast.AsyncFunctionDef


def _scopes(tree: ast.Module) -> Iterator[tuple[_Function, str | None]]:
    """Each module-level function and each method, with its class."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node, None
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield item, node.name


def scan_reads(
    rel: str, tree: ast.Module, register: ScanRegister, ctx: ModelContext
) -> list[ScanRead]:
    """Every read of every row in `tree`, each with the register entry that admits it."""
    selects, modules = _select_names(tree)
    models = _model_names(tree, ctx)
    if not (selects or modules) or not models:
        return []
    parents = _parents(tree)
    module = _module_path(rel, ctx.module_root)
    reads: list[ScanRead] = []
    for func, owner in _scopes(tree):
        keys = _request_values(func)
        method = ".".join(p for p in (module, owner, func.name) if p)
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and _is_select(node, selects, modules)):
                continue
            if not _reads_every_row(node, func, parents, models, keys, ctx):
                continue
            if method in register.methods or method in register.complete_lists:
                admitted: str | None = method
            elif owner is not None and owner in register.classes:
                admitted = owner
            else:
                admitted = None
            reads.append(ScanRead(node.lineno, node.col_offset + 1, method, owner, admitted))
    return reads


def _scan_finding(rel: str, read: ScanRead) -> Finding:
    return Finding(
        rel,
        read.finding_line,
        read.finding_col,
        "orm-lint-unregistered-scan",
        f"{read.method} reads every row of a table: no limit, no key the request supplies, "
        "and the scan register does not admit it",
        fix="narrow the statement by the key its result is bounded by (a WHERE, JOIN or "
        "LIMIT the request supplies), or register the read in [orm_lint.scan_register]",
        doc=DOC,
    )


def check_source(
    rel: str,
    source: str,
    register: ScanRegister | None = None,
    ctx: ModelContext | None = None,
) -> list[Finding]:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError:
        return []
    lazy = (_check_lazy(rel, call) for call in _relationship_calls(tree))
    reads = scan_reads(rel, tree, register or ScanRegister(), ctx or ModelContext())
    return [
        *(f for f in lazy if f is not None),
        *(_scan_finding(rel, r) for r in reads if r.admitted_by is None),
    ]


def stale_entries(register: ScanRegister, reads: list[ScanRead], where: str) -> list[Finding]:
    """A register entry that admits no read of every row is stale; a method listed in both
    lists is a defect. `where` is the config file the findings point at."""
    admitted = {r.admitted_by for r in reads}
    scans = [
        Finding(
            where,
            1,
            1,
            "orm-lint-stale-scan-entry",
            f"the scan register entry {entry!r} admits no read of every row",
            fix="remove the entry, or name the method (or class) that issues the read",
            doc=DOC,
        )
        for entry in sorted(register.methods | register.classes)
        if entry not in admitted
    ]
    complete_lists = [
        Finding(
            where,
            1,
            1,
            "orm-lint-stale-complete-list-entry",
            f"the complete-list entry {entry!r} admits no read of every row",
            fix="remove the entry, or name the method that reads the complete list",
            doc=DOC,
        )
        for entry in sorted(register.complete_lists)
        if entry not in admitted
    ]
    both = [
        Finding(
            where,
            1,
            1,
            "orm-lint-doubly-registered-read",
            f"{entry!r} is listed in both `methods` and `complete_lists`",
            fix="keep the entry in the one list that states why the read needs every row",
            doc=DOC,
        )
        for entry in sorted(register.methods & register.complete_lists)
    ]
    return [*scans, *complete_lists, *both]


def context_from(config: Config) -> ModelContext:
    schema = load_schema(config.schema)
    return ModelContext(
        models_package=config.models_package,
        not_models=frozenset(config.not_models),
        unique=unique_columns(schema, model_tables(config.models_dir)),
        module_root=config.module_root,
    )


def check(config: Config) -> list[Finding]:
    ctx = context_from(config)
    register = config.scan_register
    findings: list[Finding] = []
    reads: list[ScanRead] = []
    for path in iter_files(config.root, config.python_dirs, ".py"):
        rel = config.rel(path)
        source = path.read_text(encoding="utf-8")
        findings += check_source(rel, source, register, ctx)
        try:
            reads += scan_reads(rel, ast.parse(source, filename=rel), register, ctx)
        except SyntaxError:
            continue
    if ctx.models_package:
        findings += stale_entries(register, reads, config.config_label())
    return findings


def run(config: Config) -> StepResult:
    if not iter_files(config.root, config.python_dirs, ".py"):
        return StepResult.ok(STEP, detail="skipped: no [paths].python directory exists")
    detail = (
        ""
        if config.models_package
        else "degraded: [orm_lint].models_package is not set, so only the lazy= rule ran"
    )
    return StepResult.from_findings(STEP, check(config), detail)
