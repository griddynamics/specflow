"""Check: response schemas must not use unconstrained dicts.

Scans the `.py` files directly in each scan directory (`--scan`, else `scan` in the
`[no-dict-schemas]` configuration table, default `src/schemas`). Doc: /README.md#no-dict-schemas
"""

from __future__ import annotations

import ast
from typing import override

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import repo_rel, scan_dirs
from pystatic.harness import Finding, StepResult

STEP = "no-dict-schemas"
DOC = "/README.md#no-dict-schemas"
SCHEMAS_DIRS = ("src/schemas",)
_TUPLE_PAIR_LEN = 2

# Field names a project's contract declares as open maps: `open-fields` in the configuration.
OPEN_BY_CONTRACT: frozenset[str] = frozenset()

RESPONSE_SUFFIXES = (
    "Out",
    "Response",
    "Result",
    "Row",
    "DTO",
    "View",
)


def _is_banned_dict(node: ast.Subscript) -> str | None:
    is_dict = (isinstance(node.value, ast.Name) and node.value.id in {"dict", "Dict"}) or (
        isinstance(node.value, ast.Attribute) and node.value.attr in {"dict", "Dict"}
    )
    if (
        not is_dict
        or not isinstance(node.slice, ast.Tuple)
        or len(node.slice.elts) != _TUPLE_PAIR_LEN
    ):
        return None
    key_node, val_node = node.slice.elts
    if not (isinstance(key_node, ast.Name) and key_node.id == "str"):
        return None
    if isinstance(val_node, ast.Name) and val_node.id in {"Any", "object"}:
        return "dict[str, Any]"
    if isinstance(val_node, ast.Attribute) and val_node.attr == "Any":
        return "dict[str, Any]"
    if isinstance(val_node, ast.Name) and val_node.id == "str":
        return "dict[str, str]"
    return None


class _DictFinder(ast.NodeVisitor):
    def __init__(self) -> None:
        self.banned: list[tuple[int, int, str]] = []

    @override
    def visit_Subscript(self, node: ast.Subscript) -> None:
        desc = _is_banned_dict(node)
        if desc:
            self.banned.append((node.lineno, node.col_offset + 1, desc))
        self.generic_visit(node)


def check_source(
    rel_path: str,
    source: str,
    *,
    open_fields: frozenset[str] = OPEN_BY_CONTRACT,
    suffixes: tuple[str, ...] = RESPONSE_SUFFIXES,
) -> list[Finding]:
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return []
    findings: list[Finding] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not node.name.endswith(suffixes):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign):
                finder = _DictFinder()
                finder.visit(stmt.annotation)
                for line, col, banned_type in finder.banned:
                    name = stmt.target.id if isinstance(stmt.target, ast.Name) else "<field>"
                    if name in open_fields:
                        continue
                    findings.append(
                        Finding(
                            rel_path,
                            line,
                            col,
                            STEP,
                            f"Field '{name}' in response schema '{node.name}' "
                            f"uses banned unconstrained '{banned_type}'",
                            fix="Define a concrete Pydantic model with explicit fields",
                            doc=DOC,
                        )
                    )
    return findings


def check() -> list[Finding]:
    table = config.section(STEP)
    open_fields = frozenset(config.string_list(table, "open-fields", sorted(OPEN_BY_CONTRACT)))
    suffixes = tuple(config.string_list(table, "suffixes", list(RESPONSE_SUFFIXES)))
    dirs = scan_dirs(config.string_list(table, "scan", list(SCHEMAS_DIRS)))
    findings: list[Finding] = []
    for p in sorted(p for d in dirs for p in d.glob("*.py")):
        if not p.name.startswith("__"):
            source = p.read_text("utf-8")
            found = check_source(repo_rel(p), source, open_fields=open_fields, suffixes=suffixes)
            findings.extend(found)
    return sorted(findings, key=lambda f: (f.path, f.line, f.col))


def run() -> StepResult:
    return StepResult.from_findings(STEP, check())


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
