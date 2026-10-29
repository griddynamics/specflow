"""`sqlcheck`: the single entry point of the SQL validation kit.

    sqlcheck run [--config sqlcheck.toml] [--step NAME ...] [--format text|json]
    sqlcheck build-schema [--config sqlcheck.toml] (--metadata MOD:ATTR | --url URL) [--out FILE]
    sqlcheck render-semgrep [--config sqlcheck.toml] [--out DIR]

`run` executes every step named in `[run].steps` (or `--step`), fail-slow: each step runs
even after another failed, and the exit status is the worst one (0 clean, 1 findings,
2 a tool or configuration error). Without `--config`, `./sqlcheck.toml` is read when it
exists; otherwise every default applies to the current directory."""

from __future__ import annotations

import argparse
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

from sqlcheck import (
    alembic_lint,
    inline_queries,
    migrations_parity,
    orm_lint,
    schema_parity,
    sql_rules,
    sqlite_pragmas,
    tools,
)
from sqlcheck.build_schema import build, reflect, write
from sqlcheck.config import ALL_STEPS, Config, load_config
from sqlcheck.findings import EXIT_OK, EXIT_TOOL_ERROR, StepResult, dump_json, overall_exit
from sqlcheck.render_semgrep import write_rules
from sqlcheck.schema import load_schema

STEPS: dict[str, Callable[[Config], StepResult]] = {
    "sqlfluff": tools.run_sqlfluff,
    "sql-rules": sql_rules.run,
    "alembic-lint": alembic_lint.run,
    "orm-lint": orm_lint.run,
    "inline-queries": inline_queries.run,
    "semgrep": tools.run_semgrep,
    "schema-parity": schema_parity.run,
    "migrations": migrations_parity.run,
    "sqlite-pragmas": sqlite_pragmas.run,
}


def run_steps(config: Config, names: tuple[str, ...]) -> list[StepResult]:
    results = []
    for name in names:
        try:
            results.append(STEPS[name](config))
        except Exception as exc:  # fail-slow: a crashing step is reported, the rest still run
            results.append(StepResult.tool_error(name, f"{type(exc).__name__}: {exc}"))
    return results


def _config(path: Path | None) -> Config:
    if path is None and Path("sqlcheck.toml").is_file():
        path = Path("sqlcheck.toml")
    return load_config(path)


def _cmd_run(args: argparse.Namespace) -> int:
    config = _config(args.config)
    names = tuple(args.step) if args.step else config.steps
    results = run_steps(config, names)
    if args.format == "json":
        print(dump_json(results))
    else:
        print("\n".join(r.render() for r in results))
    return overall_exit(results)


def _cmd_build_schema(args: argparse.Namespace) -> int:
    config = _config(args.config)
    if bool(args.metadata) == bool(args.url):
        print("build-schema: pass exactly one of --metadata or --url", file=sys.stderr)
        return EXIT_TOOL_ERROR
    metadata = schema_parity.load_metadata(args.metadata) if args.metadata else reflect(args.url)
    target = args.out or config.schema or config.root / "sql-schema.json"
    document = build(metadata, target, config.type_families, config.ignore_tables, config.dialect)
    write(document, target)
    print(f"wrote {target}")
    return EXIT_OK


def _cmd_render_semgrep(args: argparse.Namespace) -> int:
    config = _config(args.config)
    schema = load_schema(config.schema)
    if not schema.loaded:
        print("render-semgrep: no schema file ([paths].schema)", file=sys.stderr)
        return EXIT_TOOL_ERROR
    out_dir = args.out or config.semgrep_generated or config.root / "build" / "sqlcheck"
    print(f"wrote {write_rules(schema, out_dir, config.semgrep_paths_include)}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sqlcheck", description="SQL validation kit")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the configured steps")
    run.add_argument("--config", type=Path, default=None)
    run.add_argument("--step", action="append", choices=ALL_STEPS, default=[])
    run.add_argument("--format", choices=("text", "json"), default="text")
    run.set_defaults(func=_cmd_run)
    schema = sub.add_parser("build-schema", help="write sql-schema.json from metadata")
    schema.add_argument("--config", type=Path, default=None)
    schema.add_argument("--metadata", default="", help="package.module:attribute of a MetaData")
    schema.add_argument("--url", default="", help="a synchronous database URL to reflect")
    schema.add_argument("--out", type=Path, default=None)
    schema.set_defaults(func=_cmd_build_schema)
    render = sub.add_parser("render-semgrep", help="write the schema-bound semgrep rules")
    render.add_argument("--config", type=Path, default=None)
    render.add_argument("--out", type=Path, default=None)
    render.set_defaults(func=_cmd_render_semgrep)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    try:
        return handler(args)
    except (ValueError, TypeError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"sqlcheck: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
