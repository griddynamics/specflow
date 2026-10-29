"""`archkit`: run the bundled checks, export the OpenAPI snapshot, build a catalogue.

    archkit run --config archkit.toml [pytest options...]
    archkit export-openapi --config archkit.toml
    archkit build-catalogue --doc docs/api.md --heading "Error codes" \\
        --column code=Code --column http=HTTP --int http --out build/error-codes.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from archkit import checks
from archkit.config import (
    ConfigError,
    install_python_path,
    load_config,
    require_checks,
    resolve,
)
from archkit.markdown import table_records


def _run(config_path: str, pytest_args: Sequence[str]) -> int:
    config = load_config(Path(config_path))
    require_checks(config)
    ini = Path(checks.__file__).resolve().parent / "pytest.ini"
    warning_filters = [
        argument
        for entry in config.settings.get("filterwarnings", [])
        for argument in ("-W", str(entry))
    ]
    argv = [
        "-c",
        str(ini),
        "--rootdir",
        str(config.root),
        "-p",
        "archkit.plugin",
        f"--archkit-config={config.path}",
        *warning_filters,
        *pytest_args,
        "--pyargs",
        "archkit.checks",
    ]
    return int(pytest.main(argv))


def _export_openapi(config_path: str) -> int:
    config = load_config(Path(config_path))
    install_python_path(config)
    for key, value in config.settings.get("hermetic", {}).get("env", {}).items():
        os.environ.setdefault(key, str(value))
    snapshot = config.section("openapi_snapshot")
    factory_ref = config.settings.get("app_factory")
    if snapshot is None or not factory_ref:
        msg = "export-openapi needs [archkit] app_factory and [openapi_snapshot] path"
        raise ConfigError(msg)
    target = config.resolve_path(snapshot["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    document = resolve(factory_ref)().openapi()
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(f"wrote {target}\n")
    return 0


def _build_catalogue(args: argparse.Namespace) -> int:
    columns = dict(item.split("=", 1) for item in args.column)
    records = table_records(
        Path(args.doc).read_text(encoding="utf-8"),
        args.heading,
        columns,
        integers=frozenset(args.int),
        lists=frozenset(args.list),
    )
    if not records:
        headers = sorted(columns.values())
        msg = f"no table under {args.heading!r} in {args.doc} has the columns {headers}"
        raise ConfigError(msg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sys.stdout.write(f"wrote {len(records)} records to {out}\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="archkit", description="Architecture, security and parity checks."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run every check")
    run.add_argument("--config", default=os.environ.get("ARCHKIT_CONFIG", "archkit.toml"))

    export = commands.add_parser("export-openapi", help="write the OpenAPI snapshot")
    export.add_argument("--config", default=os.environ.get("ARCHKIT_CONFIG", "archkit.toml"))

    build = commands.add_parser("build-catalogue", help="Markdown table -> JSON catalogue")
    build.add_argument("--doc", required=True, help="the Markdown document")
    build.add_argument("--heading", required=True, help="the heading the table sits under")
    build.add_argument(
        "--column", action="append", required=True, help="KEY=Header text (repeatable)"
    )
    build.add_argument("--int", action="append", default=[], help="KEY converted to int")
    build.add_argument("--list", action="append", default=[], help="KEY split on commas")
    build.add_argument("--out", required=True, help="the JSON file to write")

    args, rest = parser.parse_known_args(argv)
    if args.command != "run" and rest:
        parser.error(f"unrecognised arguments: {' '.join(rest)}")
    try:
        if args.command == "run":
            return _run(args.config, rest)
        if args.command == "export-openapi":
            return _export_openapi(args.config)
        return _build_catalogue(args)
    except (ConfigError, OSError) as error:
        print(f"archkit: {error}", file=sys.stderr)
        return 2
