"""Keep independent Python assertion oracles out of implementation clone comparisons."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path


def import_lines(source: str) -> set[int]:
    """The lines of every import statement: what a production module leaves out."""
    return {
        line
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }


def assertion_lines(source: str) -> set[int]:
    """Omit assertion oracles and imports; preserve actions passed to assertion helpers."""
    tree = ast.parse(source)
    shadowed = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            shadowed.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            shadowed.add(node.name)
        elif isinstance(node, ast.alias):
            shadowed.add(node.asname or node.name.split(".")[0])
    return {
        line
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        or (isinstance(node, ast.Assert) and _observation(node, shadowed))
        or _assertion_helper(node, shadowed)
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }


def _assertion_helper(node: ast.AST, shadowed: set[str]) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    call = node.value
    if not isinstance(call.func, ast.Name) or not call.func.id.startswith("assert_"):
        return False
    return all(
        _observation(value, shadowed)
        for value in (*call.args, *(keyword.value for keyword in call.keywords))
    )


def _observation(node: ast.AST, shadowed: set[str]) -> bool:
    # Intrinsic value checks are allowed only when no local/import binding shadows them.
    # Unknown calls, including getter-named methods, remain in the native comparison.
    intrinsics = {"len", "str", "set", "sorted", "isinstance", "all", "any"}
    return all(
        not isinstance(argument, (ast.Await, ast.NamedExpr, ast.Yield, ast.YieldFrom))
        and (
            not isinstance(argument, ast.Call)
            or (
                isinstance(argument.func, ast.Name)
                and argument.func.id in intrinsics - shadowed
                and "*" not in shadowed
            )
        )
        for argument in ast.walk(node)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="resolve stdin paths here")
    args = parser.parse_args(argv)
    paths: list[str] = json.load(sys.stdin)
    result: dict[str, list[int]] = {}
    for path in paths:
        try:
            source = (args.root / path).read_text(encoding="utf-8")
            result[path] = sorted(assertion_lines(source))
        except (OSError, SyntaxError) as error:
            print(f"{path}:1:1: clone-inputs-unreadable: {error}")
            return 2
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
