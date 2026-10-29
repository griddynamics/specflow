"""An enum value hand-typed as a string on a `status`/`kind`/`role`-like name should be the
enum member. Values come from `[enum-literals]` (`values`, `values-from-enums`); skipped
without it. Doc: /README.md#enum-literals"""

from __future__ import annotations

import ast
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAliasType

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import iter_py_files, repo_rel, target_root
from pystatic.harness import Finding, StepResult

STEP = "enum-literals"
DOC = "/README.md#enum-literals"

# The eight keyword/attribute names, case-insensitive; `names` in the configuration replaces them.
ENUM_LIKE_NAMES = frozenset(
    {"status", "kind", "role", "stage", "outcome", "mode", "action", "tier"}
)
# Directories (relative to the target root) a module's import name is computed from.
DEFAULT_IMPORT_ROOTS = ("src", ".")

LiteralFields = TypeAliasType("LiteralFields", dict[str, frozenset[str]])


def _module_names(path: Path, import_roots: tuple[str, ...] = DEFAULT_IMPORT_ROOTS) -> list[str]:
    """Every import name `path` can have: one per import root that contains it."""
    names: list[str] = []
    resolved = path.resolve()
    for import_root in import_roots:
        base = (target_root() / import_root).resolve()
        if not resolved.is_relative_to(base):
            continue
        parts = list(resolved.relative_to(base).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if parts:
            names.append(".".join(parts))
    return names


def _literal_values(annotation: ast.expr) -> frozenset[str] | None:
    """Read direct string members from a `Literal[...]` annotation.

    A mixed or indirect expression is deliberately unresolved: uncertainty keeps the
    finding rather than making the check quieter.
    """
    if not isinstance(annotation, ast.Subscript) or _name_of(annotation.value) != "Literal":
        return None
    members = (
        annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    )
    values: list[str] = []
    for member in members:
        if not isinstance(member, ast.Constant) or not isinstance(member.value, str):
            return None
        values.append(member.value)
    if not values:
        return None
    return frozenset(values)


def _class_literal_fields(node: ast.ClassDef) -> LiteralFields:
    fields: LiteralFields = {}
    for statement in node.body:
        if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
            continue
        values = _literal_values(statement.annotation)
        if values is not None:
            fields[statement.target.id] = values
    return fields


def _absolute_import(current_module: str, imported_module: str | None, level: int) -> str:
    if level == 0:
        return imported_module or ""
    package = current_module.rpartition(".")[0]
    package_parts = package.split(".") if package else []
    keep = len(package_parts) - level + 1
    if keep < 0:
        return ""
    prefix = ".".join(package_parts[:keep])
    return ".".join(part for part in (prefix, imported_module or "") if part)


def _bound_names(tree: ast.Module) -> set[str]:
    """Names rebound outside their one supported class/import declaration."""
    rebound: set[str] = set()
    top_level = {id(statement) for statement in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            rebound.add(node.id)
        elif isinstance(node, ast.arg):
            rebound.add(node.arg)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or (
            isinstance(node, ast.ClassDef) and id(node) not in top_level
        ):
            rebound.add(node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom) and id(node) not in top_level:
            for name in node.names:
                rebound.add(name.asname or name.name.split(".", 1)[0])
    return rebound


@dataclass(frozen=True)
class _FileContracts:
    local: dict[str, LiteralFields]
    imported_classes: dict[str, tuple[str, str]]
    imported_modules: dict[str, str]
    rebound: set[str]


def _file_contracts(
    tree: ast.Module,
    module: str,
    classes: dict[tuple[str, str], LiteralFields],
    names: list[str],
) -> _FileContracts:
    local: dict[str, LiteralFields] = {}
    imported_classes: dict[str, tuple[str, str]] = {}
    imported_modules: dict[str, str] = {}
    declarations: dict[str, int] = {}
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            fields = _class_literal_fields(statement)
            local[statement.name] = fields
            for name in names:
                classes[(name, statement.name)] = fields
            declarations[statement.name] = declarations.get(statement.name, 0) + 1
        elif isinstance(statement, ast.ImportFrom):
            imported = _absolute_import(module, statement.module, statement.level)
            for alias in statement.names:
                if alias.name != "*":
                    bound = alias.asname or alias.name
                    imported_classes[bound] = (imported, alias.name)
                    declarations[bound] = declarations.get(bound, 0) + 1
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                imported_modules[bound] = alias.name
                declarations[bound] = declarations.get(bound, 0) + 1
    ambiguous = {name for name, count in declarations.items() if count != 1}
    return _FileContracts(
        local=local,
        imported_classes=imported_classes,
        imported_modules=imported_modules,
        rebound=_bound_names(tree) | ambiguous,
    )


@dataclass(frozen=True)
class _ContractIndex:
    classes: dict[tuple[str, str], LiteralFields]
    files: dict[Path, _FileContracts]

    @classmethod
    def build(
        cls, paths: list[Path], import_roots: tuple[str, ...] = DEFAULT_IMPORT_ROOTS
    ) -> _ContractIndex:
        classes: dict[tuple[str, str], LiteralFields] = {}
        files: dict[Path, _FileContracts] = {}
        for path in paths:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            names = _module_names(path, import_roots)
            module = names[0] if names else path.stem
            files[path.resolve()] = _file_contracts(tree, module, classes, names or [module])
        return cls(classes=classes, files=files)

    def _fields_for(self, path: Path, callee: ast.expr) -> LiteralFields | None:
        source = self.files.get(path.resolve())
        if source is None:
            return None
        fields: LiteralFields | None = None
        if isinstance(callee, ast.Name):
            if callee.id not in source.rebound:
                fields = source.local.get(callee.id)
                imported = source.imported_classes.get(callee.id)
                if fields is None and imported is not None:
                    fields = self.classes.get(imported)
        elif (
            isinstance(callee, ast.Attribute)
            and isinstance(callee.value, ast.Name)
            and callee.value.id not in source.rebound
        ):
            imported_module = source.imported_modules.get(callee.value.id)
            imported_object = source.imported_classes.get(callee.value.id)
            if imported_module is None and imported_object is not None:
                imported_module = ".".join(imported_object)
            if imported_module is not None:
                fields = self.classes.get((imported_module, callee.attr))
        return fields

    def literal_values(self, path: Path, callee: ast.expr, field: str) -> frozenset[str] | None:
        fields = self._fields_for(path, callee)
        return fields.get(field) if fields is not None else None


def _is_enum_class(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(b, ast.Name) and "Enum" in b.id)
        or (isinstance(b, ast.Attribute) and "Enum" in b.attr)
        for b in node.bases
    )


def harvest_enum_values(paths: list[Path]) -> frozenset[str]:
    """Every string value assigned in the body of an `Enum` subclass among `paths`."""
    values: set[str] = set()
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _is_enum_class(node):
                continue
            for stmt in node.body:
                value = stmt.value if isinstance(stmt, ast.Assign | ast.AnnAssign) else None
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    values.add(value.value)
    return frozenset(values)


def _name_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


@dataclass(frozen=True)
class _Rule:
    values: frozenset[str]
    names: frozenset[str] = ENUM_LIKE_NAMES

    def enum_like(self, node: ast.expr) -> str | None:
        name = _name_of(node)
        return name if name is not None and name.lower() in self.names else None

    def literal(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value if node.value in self.values else None
        return None


def _finding(rel: str, node: ast.expr, name: str, value: str) -> Finding:
    return Finding(
        rel,
        node.lineno,
        node.col_offset + 1,
        "enum-literals-hand-typed",
        f"{name!r} compared/assigned to the string literal {value!r}, a declared enum value",
        fix="use the enum member instead of the literal string",
        doc=DOC,
    )


def _check_compare(rel: str, node: ast.Compare, rule: _Rule) -> list[Finding]:
    sides: list[ast.expr] = [node.left, *node.comparators]
    findings = []
    for a, b in itertools.pairwise(sides):
        for enum_side, literal_side in ((a, b), (b, a)):
            value = rule.literal(literal_side)
            name = rule.enum_like(enum_side)
            if value is not None and name is not None:
                findings.append(_finding(rel, literal_side, name, value))
    return findings


def _check_assign_target(
    rel: str, target: ast.expr, value: ast.expr, rule: _Rule
) -> Finding | None:
    literal = rule.literal(value)
    name = rule.enum_like(target)
    if literal is not None and name is not None:
        return _finding(rel, value, name, literal)
    return None


def _check_keyword(
    rel: str, path: Path, call: ast.Call, kw: ast.keyword, rule: _Rule, contracts: _ContractIndex
) -> Finding | None:
    value = rule.literal(kw.value)
    if kw.arg is None or kw.arg.lower() not in rule.names or value is None:
        return None
    literal_values = contracts.literal_values(path, call.func, kw.arg)
    if literal_values is not None and value in literal_values:
        return None
    return _finding(rel, kw.value, kw.arg, value)


def _check_node(
    rel: str, path: Path, node: ast.AST, rule: _Rule, contracts: _ContractIndex
) -> list[Finding]:
    found: list[Finding | None] = []
    if isinstance(node, ast.Compare):
        return _check_compare(rel, node, rule)
    if isinstance(node, ast.Assign):
        found = [_check_assign_target(rel, t, node.value, rule) for t in node.targets]
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        found = [_check_assign_target(rel, node.target, node.value, rule)]
    elif isinstance(node, ast.Call):
        found = [_check_keyword(rel, path, node, kw, rule, contracts) for kw in node.keywords]
    return [f for f in found if f is not None]


def scan_file(
    path: Path,
    values: frozenset[str],
    contracts: _ContractIndex | None = None,
    names: frozenset[str] = ENUM_LIKE_NAMES,
) -> list[Finding]:
    rel = repo_rel(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []
    if contracts is None:
        contract_paths = set(iter_py_files())
        contract_paths.update(path.parent.glob("*.py"))
        contracts = _ContractIndex.build(sorted(contract_paths))
    enum_class_assigns = {
        id(stmt)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and _is_enum_class(node)
        for stmt in node.body
        if isinstance(stmt, ast.Assign | ast.AnnAssign)
    }
    rule = _Rule(values=values, names=names)
    return [
        f
        for node in ast.walk(tree)
        if id(node) not in enum_class_assigns
        for f in _check_node(rel, path, node, rule, contracts)
    ]


def check() -> list[Finding]:
    table = config.section(STEP)
    paths = list(iter_py_files())
    values = set(config.string_list(table, "values", []))
    if table.get("values-from-enums", False) is True:
        values |= harvest_enum_values(paths)
    configured = config.string_list(table, "names", sorted(ENUM_LIKE_NAMES))
    names = frozenset(name.lower() for name in configured)
    import_roots = tuple(config.string_list(table, "import-roots", list(DEFAULT_IMPORT_ROOTS)))
    contracts = _ContractIndex.build(paths, import_roots)
    findings = [f for path in paths for f in scan_file(path, frozenset(values), contracts, names)]
    return sorted(findings, key=lambda f: (f.path, f.line, f.col))


def run() -> StepResult:
    if not config.has_section(STEP):
        return StepResult.ok(STEP, detail=f"skipped: no [{STEP}] table in the configuration")
    return StepResult.from_findings(STEP, check())


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
