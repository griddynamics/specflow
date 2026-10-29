"""The command line every check shares: `--root DIR` (default: the working directory),
`--scan SUBDIR` (repeatable), `--config TOML` (default `<root>/pystatic.toml`) and optional
paths. Exit 0 clean, 1 findings, 2 tool or configuration error."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from pystatic import config, files
from pystatic.harness.core import main as harness_main
from pystatic.harness.models import StepResult


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="tree to check")
    parser.add_argument(
        "--scan",
        action="append",
        metavar="SUBDIR",
        help="directory under --root to scan (repeatable; default depends on the check)",
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="adopter configuration (pystatic.toml)"
    )
    parser.add_argument("paths", nargs="*", help="explicit files, for checks that take them")
    return parser


def apply_target(args: argparse.Namespace) -> None:
    """Point `pystatic.files` and `pystatic.config` at the parsed target."""
    files.configure(args.root, args.scan)
    config.configure(args.config)


def parse_target(argv: Sequence[str], description: str = "") -> argparse.Namespace:
    """Parse `argv` and point `pystatic.files`/`pystatic.config` at the chosen tree."""
    args = build_parser(description).parse_args(list(argv))
    apply_target(args)
    return args


def main(step: str, run_fn: Callable[[], StepResult]) -> int:
    """The `__main__` of a check whose `run()` takes no arguments."""
    parse_target(sys.argv[1:], description=step)
    return harness_main(step, run_fn)
