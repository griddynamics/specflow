"""`python -m pystatic.openapi_snapshot --app MODULE:ATTR --out FILE [--check]`: writes the
OpenAPI document an app serves as deterministic JSON, or (`--check`) exits 1 when the
committed snapshot differs. Imports the target. Doc: /README.md#openapi-snapshot"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DOC = "/README.md#openapi-snapshot"


def load_app(spec: str) -> Any:
    """`module:attribute`; a callable attribute without `.openapi` is called with no arguments."""
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        msg = f"--app must be MODULE:ATTRIBUTE, got {spec!r}"
        raise ValueError(msg)
    target = getattr(importlib.import_module(module_name), attribute)
    if not hasattr(target, "openapi") and callable(target):
        target = target()
    if not hasattr(target, "openapi"):
        msg = f"{spec} is not an app with an .openapi() method"
        raise TypeError(msg)
    return target


def render(schema: dict[str, Any]) -> str:
    """Deterministic, diff-friendly text: sorted keys, trailing newline."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="export or verify an OpenAPI snapshot")
    parser.add_argument("--app", required=True, help="MODULE:ATTRIBUTE of the app or factory")
    parser.add_argument("--out", type=Path, required=True, help="snapshot file")
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    parser.add_argument("--secret-env", action="append", default=[], metavar="NAME")
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    for name in args.secret_env:
        os.environ[name] = os.environ.get(name) or secrets.token_urlsafe(32)
    text = render(load_app(args.app).openapi())
    current = args.out.read_text(encoding="utf-8") if args.out.is_file() else None
    if args.check:
        if current == text:
            return 0
        sys.stdout.write(
            f"{args.out}:1:1: openapi-snapshot-stale: the committed snapshot differs from the "
            f"served document (fix: rerun without --check and commit; doc: {DOC})\n"
        )
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    sys.stdout.write(f"wrote {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
