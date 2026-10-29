"""Vulture adapter for registered callbacks, schemas and owned tool consumers."""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from pathlib import Path

from vulture import Vulture

DATA_BASES = {
    "BaseModel",
    "BaseSettings",
    "Base",
    "Enum",
    "IntEnum",
    "StrEnum",
    "Protocol",
    "TypedDict",
    "NamedTuple",
    "TypeDecorator",
}
CALLBACK_DECORATORS = {
    "field_validator",
    "model_validator",
    "field_serializer",
    "model_serializer",
    "validator",
    "validates",
    "event.listens_for",
    "pytest.fixture",
    "pytest.hookimpl",
    "override",
}
ROUTE_METHODS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "options",
    "head",
    "websocket",
    "exception_handler",
    "middleware",
    "on_event",
    "output_validator",
}
ORM_HOOKS = {"process_bind_param", "process_result_value"}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _name(node.func)
    if isinstance(node, ast.Subscript):
        return _name(node.value)
    return ""


def _data_classes(trees: list[ast.Module]) -> set[str]:
    classes = [node for tree in trees for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    known = set(DATA_BASES)
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                known.update(
                    alias.asname or alias.name for alias in node.names if alias.name in DATA_BASES
                )
    pending = True
    while pending:
        pending = False
        for node in classes:
            bases = {_name(base).rsplit(".", 1)[-1] for base in node.bases}
            decorators = {_name(decorator).rsplit(".", 1)[-1] for decorator in node.decorator_list}
            if (bases & known or "dataclass" in decorators) and node.name not in known:
                known.add(node.name)
                pending = True
    return known


def _declaration_only(node: ast.stmt) -> bool:
    if isinstance(node, ast.Pass):
        return True
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and (node.value.value is Ellipsis or isinstance(node.value.value, str))
    )


def _schema_members(node: ast.ClassDef) -> set[tuple[int, str]]:
    locations: set[tuple[int, str]] = set()
    for member in node.body:
        if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
            locations.add((member.lineno, member.target.id))
        if isinstance(member, ast.Assign):
            locations.update(
                (member.lineno, target.id)
                for target in member.targets
                if isinstance(target, ast.Name)
            )
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            empty = all(_declaration_only(stmt) for stmt in member.body)
            if empty or member.name in ORM_HOOKS:
                locations.add((member.lineno, member.name))
            if empty:
                arguments = [*member.args.posonlyargs, *member.args.args, *member.args.kwonlyargs]
                locations.update((arg.lineno, arg.arg) for arg in arguments)
    return locations


def _class_callbacks(node: ast.ClassDef) -> set[tuple[int, str]]:
    bases = {_name(base).rsplit(".", 1)[-1] for base in node.bases}
    names = {"setUp", "tearDown"} if "TestCase" in bases else set()
    return {
        (member.lineno, member.name)
        for member in node.body
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (
            member.name in names
            or (bases & {"NodeVisitor", "NodeTransformer"} and member.name.startswith("visit_"))
        )
    }


def _registered_locations(tree: ast.Module, data_classes: set[str]) -> set[tuple[int, str]]:
    registered: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = {_name(decorator) for decorator in node.decorator_list}
            callback = any(
                name in CALLBACK_DECORATORS or name.rsplit(".", 1)[-1] in ROUTE_METHODS
                for name in names
            )
            if callback or node.name in {"setUpModule", "tearDownModule"}:
                lines = [node.lineno, *[decorator.lineno for decorator in node.decorator_list]]
                registered.update((line, node.name) for line in lines)
        if isinstance(node, ast.ClassDef):
            registered.update(_class_callbacks(node))
            if node.name in data_classes:
                registered.update(_schema_members(node))
        if isinstance(node, ast.Assign):
            registered.update(
                (node.lineno, target.id)
                for target in node.targets
                if isinstance(target, ast.Name) and target.id == "pytestmark"
            )
    return registered


def check_sources(paths: list[Path], consumers: list[Path]) -> list[str]:
    """Find unused owned definitions; consumers supply usage but own no findings."""
    if not paths:
        msg = "no owned Python files in dead-code scope"
        raise ValueError(msg)
    owned = {path.resolve() for path in paths}
    sources = {path.resolve(): path.read_text(encoding="utf-8") for path in [*paths, *consumers]}
    trees = {path: ast.parse(source, filename=str(path)) for path, source in sources.items()}
    known = _data_classes(list(trees.values()))
    registered = {path: _registered_locations(tree, known) for path, tree in trees.items()}
    vulture = Vulture()
    for path, source in sources.items():
        vulture.scan(source, filename=str(path))
    if vulture.exit_code:
        msg = "Vulture could not parse the complete source scope"
        raise ValueError(msg)
    owned_attributes = {
        path: {
            (node.lineno, node.attr)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id in {"self", "cls"}
        }
        for path, tree in trees.items()
    }
    findings: list[str] = []
    for item in vulture.get_unused_code():
        path = Path(item.filename).resolve()
        if path not in owned or (item.first_lineno, item.name) in registered[path]:
            continue
        # Attribute writes to external SDK objects are effects, not owned declarations.
        if item.typ == "attribute" and (item.first_lineno, item.name) not in owned_attributes[path]:
            continue
        if "alembic" in path.parts and item.name in {
            "revision",
            "down_revision",
            "branch_labels",
            "depends_on",
        }:
            continue
        findings.append(f"{path}:{item.first_lineno}:1: vulture-dead-code: {item.message}")
    return sorted(findings)


def project_paths(root: Path, patterns: list[str]) -> list[Path]:
    """Files the patterns match under the project, never an environment or cache copy."""
    return sorted(
        {
            path
            for pattern in patterns
            for path in root.glob(pattern)
            if path.is_file()
            and not {".venv", "node_modules", "__pycache__"}.intersection(
                path.relative_to(root).parts
            )
        }
    )


def check_project(root: Path) -> list[str]:
    """Read scopes from the owning project's manifest, never a second inventory."""
    manifest = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    scopes = manifest["tool"]["dead-code"]["scopes"]
    findings: list[str] = []
    for scope in scopes:
        excluded = set(project_paths(root, scope.get("exclude", [])))
        paths = [path for path in project_paths(root, scope["sources"]) if path not in excluded]
        consumers = project_paths(root, scope.get("consumers", []))
        findings.extend(check_sources(paths, consumers))
    return sorted(set(findings))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    try:
        findings = check_project(args.project.resolve())
    except (OSError, KeyError, SyntaxError, ValueError) as error:
        sys.stderr.write(f"dead-code tool-error: {error}\n")
        return 2
    for finding in findings:
        sys.stdout.write(f"{finding}\n")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
