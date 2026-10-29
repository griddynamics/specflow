"""Keeps the hand-written `source_modules` of import-linter contracts complete.

A "forbidden" contract that exempts one package cannot use a wildcard (`src.*`), so its
`source_modules` list is written by hand, and a newly added top-level package escapes the
contract silently. This check fails when a top-level package of the root package (a directory
with an `__init__.py` under the source root) is named by no entry of a listed contract, either
as the package itself or as a module inside it.

Usage, from the directory that holds the root package (one `--contract` per contract id,
one `--exempt` per package a contract leaves out on purpose):
    python importlinter_source_coverage.py --contracts importlinter.toml --source-root src
        --contract sdk-confined --exempt gateway
Exit status: 0 complete, 1 a package is missing, 2 a named contract does not exist.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import importlinter.api as importlinter_api


@dataclass(frozen=True)
class Coverage:
    """The packages each listed contract misses, and the listed ids that name no contract."""

    missing: dict[str, list[str]]
    unknown_contracts: list[str]


def top_level_packages(source_root: Path) -> set[str]:
    """Names of the directories directly under `source_root` that are regular packages."""
    return {
        path.name
        for path in source_root.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }


def is_covered(root_package: str, package: str, source_modules: list[str]) -> bool:
    dotted = f"{root_package}.{package}"
    return any(module == dotted or module.startswith(f"{dotted}.") for module in source_modules)


def _as_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def check_coverage(
    contracts_file: Path, source_root: Path, contract_ids: list[str], exempt: set[str]
) -> Coverage:
    config = importlinter_api.read_configuration(str(contracts_file))
    root_package = str(config["session_options"]["root_packages"][0])
    sources_by_id: dict[str, list[str]] = {
        str(row["id"]): _as_list(row.get("source_modules"))
        for row in config["contracts_options"]
        if "id" in row
    }
    packages = sorted(top_level_packages(source_root) - exempt)
    missing: dict[str, list[str]] = {}
    unknown: list[str] = []
    for contract_id in contract_ids:
        if contract_id not in sources_by_id:
            unknown.append(contract_id)
            continue
        gaps = [p for p in packages if not is_covered(root_package, p, sources_by_id[contract_id])]
        if gaps:
            missing[contract_id] = gaps
    return Coverage(missing=missing, unknown_contracts=unknown)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--contracts", type=Path, required=True, help="import-linter config")
    parser.add_argument("--source-root", type=Path, required=True, help="the root package dir")
    parser.add_argument(
        "--contract", action="append", required=True, dest="contract_ids", help="contract id"
    )
    parser.add_argument("--exempt", action="append", default=[], help="exempt package name")
    args = parser.parse_args(argv)
    coverage = check_coverage(
        args.contracts, args.source_root, list(args.contract_ids), set(args.exempt)
    )
    for contract_id in coverage.unknown_contracts:
        sys.stderr.write(f"{args.contracts}: no contract with id {contract_id!r}\n")
    for contract_id, packages in coverage.missing.items():
        sys.stderr.write(
            f"{args.contracts}: contract {contract_id!r} does not cover the packages "
            f"{', '.join(packages)}; add each to its source_modules\n"
        )
    if coverage.unknown_contracts:
        return 2
    return 1 if coverage.missing else 0


if __name__ == "__main__":
    sys.exit(main())
