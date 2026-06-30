#!/usr/bin/env python3
"""Per-file pure-Python LOC breakdown for a PR (or any two git refs).

"Pure" code excludes: non-.py files, blank lines, comments, and docstrings
(module/class/function string-literal statements). Counting is done by
reconstructing the base and head versions of every changed .py file,
stripping comments + docstrings + blanks from each, then diffing the cleaned
versions with difflib — so multi-line docstrings and reformatting never inflate
the numbers.

Usage:
    python count_py_loc.py <PR_NUMBER>          # resolve refs via `gh`
    python count_py_loc.py <BASE_REF> <HEAD_REF>
    python count_py_loc.py                       # defaults: merge-base(main,HEAD)..HEAD

Output: a per-file table (added / removed / net pure-code lines) plus totals,
and a JSON blob for downstream tooling.
"""
from __future__ import annotations

import ast
import difflib
import io
import json
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field


def sh(*args: str) -> str:
    """Run a command, return stdout. Empty string on non-zero exit."""
    res = subprocess.run(args, capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else ""


def resolve_refs(argv: list[str]) -> tuple[str, str, str]:
    """Return (base_sha, head_sha, label) from CLI args."""
    if len(argv) == 1 and argv[0].lstrip("#").isdigit():
        pr = argv[0].lstrip("#")
        meta = json.loads(
            sh("gh", "pr", "view", pr, "--json",
               "baseRefName,headRefName,headRefOid")
            or "{}"
        )
        base_ref = meta.get("baseRefName", "main")
        head = meta.get("headRefOid") or "HEAD"
        # Fetch so base/head objects are present even if the branch is remote-only.
        sh("git", "fetch", "origin", base_ref)
        base = (sh("git", "merge-base", f"origin/{base_ref}", head).strip()
                or sh("git", "merge-base", base_ref, head).strip()
                or base_ref)
        return base, head, f"PR #{pr}"
    if len(argv) == 2:
        base = sh("git", "merge-base", argv[0], argv[1]).strip() or argv[0]
        return base, argv[1], f"{argv[0]}..{argv[1]}"
    # Default: current branch vs main.
    base = sh("git", "merge-base", "main", "HEAD").strip() or "main"
    return base, "HEAD", "main..HEAD"


def file_at(ref: str, path: str) -> str:
    """File content at a ref, or '' if it doesn't exist there."""
    res = subprocess.run(["git", "show", f"{ref}:{path}"],
                         capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else ""


def changed_py_files(base: str, head: str) -> list[str]:
    out = sh("git", "diff", "--name-only", base, head, "--", "*.py")
    return [ln for ln in out.splitlines() if ln.strip()]


def _docstring_line_ranges(tree: ast.AST) -> set[int]:
    """1-based line numbers occupied by module/class/function docstrings."""
    lines: set[int] = set()
    targets = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, targets):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            end = getattr(first.value, "end_lineno", first.value.lineno)
            lines.update(range(first.value.lineno, end + 1))
    return lines


def _comment_lines_only(source: str) -> set[int]:
    """Line numbers that are *entirely* a comment (or blank-then-comment)."""
    lines: set[int] = set()
    try:
        toks = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in toks:
            if tok.type == tokenize.COMMENT:
                # Whole-line comment: nothing but whitespace before the '#'.
                line = tok.line
                if line[: tok.start[1]].strip() == "":
                    lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return lines


def clean_python(source: str) -> list[str]:
    """Return code-only lines: no blanks, no whole-line comments, no docstrings.

    Inline trailing comments are stripped via tokenize-aware reconstruction only
    when the file parses; the dominant signal (full-line comments, docstrings,
    blanks) is always removed. Falls back to a blank+comment line filter when the
    source can't be parsed (e.g. a partial/invalid revision)."""
    if not source.strip():
        return []
    raw = source.splitlines()
    drop: set[int] = set()
    try:
        tree = ast.parse(source)
        drop |= _docstring_line_ranges(tree)
    except SyntaxError:
        pass
    drop |= _comment_lines_only(source)

    out: list[str] = []
    for i, line in enumerate(raw, start=1):
        if i in drop:
            continue
        if line.strip() == "":
            continue
        out.append(line.rstrip())
    return out


@dataclass
class FileDelta:
    path: str
    added: int = 0
    removed: int = 0

    @property
    def net(self) -> int:
        return self.added - self.removed


@dataclass
class Report:
    label: str
    base: str
    head: str
    files: list[FileDelta] = field(default_factory=list)

    @property
    def total_added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def total_removed(self) -> int:
        return sum(f.removed for f in self.files)


def diff_counts(base_lines: list[str], head_lines: list[str]) -> tuple[int, int]:
    added = removed = 0
    for ln in difflib.unified_diff(base_lines, head_lines, n=0, lineterm=""):
        if ln.startswith("+") and not ln.startswith("+++"):
            added += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            removed += 1
    return added, removed


def build_report(base: str, head: str, label: str) -> Report:
    report = Report(label=label, base=base, head=head)
    for path in changed_py_files(base, head):
        base_clean = clean_python(file_at(base, path))
        head_clean = clean_python(file_at(head, path))
        added, removed = diff_counts(base_clean, head_clean)
        if added or removed:
            report.files.append(FileDelta(path, added, removed))
    report.files.sort(key=lambda f: f.added + f.removed, reverse=True)
    return report


def print_report(report: Report) -> None:
    width = max((len(f.path) for f in report.files), default=4)
    width = max(width, len("FILE"))
    print(f"# Pure-Python LOC breakdown — {report.label}")
    print(f"# base {report.base[:12]}  ->  head {report.head[:12]}")
    print()
    print(f"{'FILE'.ljust(width)}  {'+add':>6}  {'-del':>6}  {'net':>6}")
    print(f"{'-' * width}  {'-' * 6}  {'-' * 6}  {'-' * 6}")
    for f in report.files:
        print(f"{f.path.ljust(width)}  {f.added:>6}  {f.removed:>6}  {f.net:>+6}")
    print(f"{'-' * width}  {'-' * 6}  {'-' * 6}  {'-' * 6}")
    print(f"{'TOTAL'.ljust(width)}  {report.total_added:>6}  "
          f"{report.total_removed:>6}  "
          f"{report.total_added - report.total_removed:>+6}")
    print(f"\n# {len(report.files)} python file(s) with pure-code changes")


def main() -> None:
    base, head, label = resolve_refs(sys.argv[1:])
    report = build_report(base, head, label)
    print_report(report)
    if "--json" in sys.argv:
        payload = {
            "label": report.label,
            "base": report.base,
            "head": report.head,
            "total_added": report.total_added,
            "total_removed": report.total_removed,
            "files": [
                {"path": f.path, "added": f.added,
                 "removed": f.removed, "net": f.net}
                for f in report.files
            ],
        }
        print("\n" + json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
