#!/usr/bin/env python3
"""Lightweight backend quality hints for IDE hooks (Cursor) and manual/Claude use.

Runs ruff and radon (cyclomatic) on paths under backend/app when a .py file is edited.
Not a replacement for `make check` or CI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"


def _walk_paths(obj: Any, out: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("file_path", "path", "target_file", "file", "uri") and isinstance(v, str):
                if v.endswith(".py"):
                    out.append(v)
            _walk_paths(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _walk_paths(x, out)
    elif isinstance(obj, str) and obj.endswith(".py") and "backend" in obj.replace("\\", "/"):
        out.append(obj)


def _to_backend_relative_py(abs_path: str) -> Path | None:
    """Return path relative to `backend/` (e.g. app/services/foo.py)."""
    p = Path(abs_path).resolve()
    try:
        return p.relative_to(BACKEND)
    except ValueError:
        sp = abs_path.replace("\\", "/")
        m = re.search(r"backend/((?:app|test)/.+\.py)$", sp)
        if not m:
            return None
        return Path(m.group(1))


def _run(cmd: list[str], *, cwd: Path) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    out = (r.stdout or "") + (r.stderr or "")
    # Drop uv "VIRTUAL_ENV does not match" noise from hook context
    lines = [
        ln
        for ln in out.splitlines()
        if "VIRTUAL_ENV" not in ln and "will be ignored" not in ln
    ]
    return r.returncode, "\n".join(lines).strip()


def analyze_file(app_rel: Path) -> str:
    rel_s = app_rel.as_posix()
    pieces: list[str] = []
    # Match Makefile: `cd backend && uv run ruff` / `uv run radon`
    code, o = _run(
        ["uv", "run", "ruff", "check", rel_s],
        cwd=BACKEND,
    )
    if o:
        pieces.append(f"ruff (exit {code}):\n{o[:4000]}")
    code2, o2 = _run(
        ["uv", "run", "radon", "cc", "-s", rel_s],
        cwd=BACKEND,
    )
    if o2:
        pieces.append(f"radon cc (exit {code2}):\n{o2[:4000]}")
    if not pieces:
        return ""
    return "\n\n".join(pieces)


def from_hook_stdin() -> str:
    raw = sys.stdin.read()
    if not raw.strip():
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    paths: list[str] = []
    _walk_paths(data, paths)
    # de-dupe, keep order
    seen: set[str] = set()
    uniques: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            uniques.append(p)

    messages: list[str] = []
    for p in uniques:
        rel = _to_backend_relative_py(p)
        if rel is None:
            continue
        if rel.parts[0] != "app":
            continue
        block = analyze_file(rel)
        if block:
            messages.append(f"### {rel}\n{block}")
    if not messages:
        return ""
    return (
        "[SpecFlow backend quality hook]\n"
        "Review SRP/DRY and whether new logic belongs in services vs state. "
        "See `docs/PATTERNS/INDEX.md` and `make check` before merge.\n\n"
        + "\n\n".join(messages)
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--file",
        action="append",
        dest="files",
        help="Path to a file under backend/app (can repeat). If omitted, reads hook JSON from stdin.",
    )
    p.add_argument("--print-json", action="store_true", help="Print Cursor hook JSON to stdout (postToolUse).")
    args = p.parse_args()
    if args.files:
        lines: list[str] = []
        for f in args.files:
            rel = _to_backend_relative_py(os.path.abspath(f))
            if rel is None or rel.parts[0] != "app":
                print(f"Skip (not under backend/app): {f}", file=sys.stderr)
                continue
            b = analyze_file(rel)
            if b:
                lines.append(f"### {rel}\n{b}")
        text = "\n\n".join(lines)
        if args.print_json:
            out = {"additional_context": text} if text else {}
            print(json.dumps(out))
        else:
            print(text)
        return 0

    text = from_hook_stdin()
    if args.print_json or os.environ.get("IDE_HOOK_FORMAT") == "json":
        out = {"additional_context": text} if text else {}
        print(json.dumps(out))
    else:
        if text:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
