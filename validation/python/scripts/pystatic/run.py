"""`python -m pystatic.run [--root DIR] [--config TOML] [--level quick|all] [--reviewed TOML]`:
every check against one tree, with reports under `PYSTATIC_REPORTS_ROOT`. `quick` leaves out
`package-data`, which builds a wheel. Exit: the worst step's code."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from pystatic.cli import apply_target, build_parser
from pystatic.docs import check as docs_check
from pystatic.harness import core
from pystatic.harness.models import StepResult
from pystatic.scripts import (
    blocking_in_async,
    clones,
    config_literals,
    config_scoping,
    deployment_files,
    enum_literals,
    file_size,
    json_schemas,
    no_dict_schemas,
    package_data,
    registry_files,
    runtime_defaults,
    source_ownership,
)

UNIT = "python"
QUICK_STEPS = [
    "blocking-in-async",
    "no-dict-schemas",
    "enum-literals",
    "config-literals",
    "registry-files",
    "config-scoping",
    "json-schemas",
    "deployment-files",
    "file-size",
    "source-ownership",
    "runtime-defaults",
    "docs-bundle",
    "clones",
]
LEVELS: dict[str, list[str]] = {"quick": QUICK_STEPS, "all": [*QUICK_STEPS, "package-data"]}


def step_table(args: argparse.Namespace) -> dict[str, Callable[[], StepResult]]:
    """Each step's zero-argument run function, bound to this invocation's options."""
    return {
        "blocking-in-async": blocking_in_async.run,
        "no-dict-schemas": no_dict_schemas.run,
        "enum-literals": enum_literals.run,
        "config-literals": config_literals.run,
        "registry-files": registry_files.run,
        "config-scoping": lambda: config_scoping.run(reviewed_path=args.reviewed),
        "json-schemas": json_schemas.run,
        "deployment-files": deployment_files.run,
        "file-size": file_size.run,
        "source-ownership": source_ownership.run,
        "runtime-defaults": runtime_defaults.run,
        "docs-bundle": docs_check.run,
        "clones": clones.run,
        "package-data": package_data.run,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser("pystatic runner: every check against one tree")
    parser.add_argument("--level", choices=tuple(LEVELS), default="quick")
    parser.add_argument("--reviewed", type=Path, default=None, help="reviewed-suppressions TOML")
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    apply_target(args)
    steps = step_table(args)

    def dispatch(name: str) -> StepResult:
        result = core.run_step(name, steps[name])
        core.write_report(UNIT, name, result)
        return result

    results = core.run_level(UNIT, LEVELS[args.level], dispatch)
    exit_code = core.aggregate_exit(results)
    total = sum(len(r.findings) for r in results)
    counts = f"{len(results)} steps, {total} findings, exit {exit_code}"
    sys.stderr.write(f"{UNIT} {args.level}: {counts}\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
