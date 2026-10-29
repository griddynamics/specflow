"""An `async def` body calling a known-blocking library call (sync SDK client, pandas/openpyxl
reader) outside a thread offload blocks the event loop.
Ruff's `ASYNC` rules own the stdlib cases; this owns the library calls. The call set is the
documented default, replaced or extended by the `[blocking-in-async]` table.
Doc: /README.md#blocking-in-async."""

from __future__ import annotations

import ast
from typing import override

from pystatic import config
from pystatic.cli import main as harness_main
from pystatic.files import iter_py_files, repo_rel
from pystatic.harness import Finding, StepResult

STEP = "blocking-in-async"
DOC = "/README.md#blocking-in-async"

THREAD_WRAPPERS = frozenset({"to_thread", "run_sync", "run_in_threadpool"})

# Attribute suffixes (the last dotted component of a call target) that name a
# known-blocking call -- pandas/openpyxl readers, and sync cloud-SDK client
# constructors and their I/O methods. The default; `[blocking-in-async]` overrides it.
BLOCKING_ATTRS = frozenset(
    {
        "read_csv",
        "read_excel",
        "load_workbook",
        "Client",  # sync SDK client constructors, e.g. storage.Client()
        "DocumentProcessorServiceClient",
        "download_as_bytes",
        "download_to_filename",
        "upload_from_string",
        "upload_from_filename",
        "blob",
        "get_bucket",
    }
)


def _call_attr(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_thread_wrapped(node: ast.Call) -> bool:
    return _call_attr(node) in THREAD_WRAPPERS


class _AsyncBodyWalker(ast.NodeVisitor):
    """Walks one `async def`'s statements, stopping at a nested `def`/
    `async def`/lambda ("not nested defs") and at a thread-wrapper
    call's own arguments (blocking calls made *through* the wrapper are the
    point of the wrapper)."""

    def __init__(self, rel: str, calls: frozenset[str]) -> None:
        self.rel = rel
        self.calls = calls
        self.findings: list[Finding] = []

    @override
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        del node  # a nested sync def is its own scope -- not this walk's concern

    @override
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        del node  # a nested coroutine is its own scope

    @override
    def visit_Lambda(self, node: ast.Lambda) -> None:
        del node

    @override
    def visit_Call(self, node: ast.Call) -> None:
        if _is_thread_wrapped(node):
            return  # the wrapped call is exactly what the wrapper is for
        attr = _call_attr(node)
        if attr in self.calls:
            self.findings.append(
                Finding(
                    self.rel,
                    node.lineno,
                    node.col_offset + 1,
                    "blocking-in-async",
                    f"{attr}(...) called directly inside an async function",
                    fix="wrap it in asyncio.to_thread/run_in_threadpool, "
                    "or move it behind a sync helper called that way",
                    doc=DOC,
                )
            )
        self.generic_visit(node)


def _check_async_def(rel: str, node: ast.AsyncFunctionDef, calls: frozenset[str]) -> list[Finding]:
    walker = _AsyncBodyWalker(rel, calls)
    for stmt in node.body:
        walker.visit(stmt)
    return walker.findings


def configured_calls() -> frozenset[str]:
    """`calls` replaces the default set; `extra-calls` extends it."""
    table = config.section(STEP)
    base = config.string_list(table, "calls", sorted(BLOCKING_ATTRS))
    return frozenset([*base, *config.string_list(table, "extra-calls", [])])


def check_source(rel: str, source: str, calls: frozenset[str] = BLOCKING_ATTRS) -> list[Finding]:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError:
        return []
    return [
        f
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        for f in _check_async_def(rel, node, calls)
    ]


def check() -> list[Finding]:
    calls = configured_calls()
    findings = [
        f
        for path in iter_py_files()
        for f in check_source(repo_rel(path), path.read_text(encoding="utf-8"), calls)
    ]
    return sorted(findings, key=lambda f: (f.path, f.line, f.col))


def run() -> StepResult:
    return StepResult.from_findings(STEP, check())


def main() -> int:
    return harness_main(STEP, run)


if __name__ == "__main__":
    raise SystemExit(main())
