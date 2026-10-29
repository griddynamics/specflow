"""`python -m cypher_checks <command> ...` (or `cypher-checks <command> ...`): every check.

Commands: `all` (every step below from one config file), `lint` (static, no database),
`parity` (schema file against the DDL files, no database), `cyver` and `explain` (start a
throwaway Neo4j testcontainer, seed it from the schema, validate), `no-inline-queries` and
`param-names` (Python sources) and `derive-schema` (build step: draft a schema file from a
live database).
Exit codes: 0 clean, 1 findings, 2 tool or configuration error.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from cypher_checks import (
    container,
    cyver_check,
    derive_schema,
    explain,
    lint,
    no_inline_queries,
    param_names,
    parity,
    run_all,
)
from cypher_checks.findings import EXIT_ERROR, EXIT_OK, Finding, report
from cypher_checks.query_files import expand_targets
from cypher_checks.schema import GraphSchema, SchemaError, load_schema

#: Commands that take their own arguments rather than a schema file and `.cypher` targets.
_STANDALONE: dict[str, Callable[[Sequence[str]], int]] = {
    "all": run_all.main,
    "no-inline-queries": no_inline_queries.main,
    "param-names": param_names.main,
    "derive-schema": derive_schema.main,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cypher_checks")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("lint", "static rules over query text"),
        ("parity", "the schema file and the DDL files declare the same constraints and indexes"),
        ("cyver", "CyVer syntax/schema/properties validators on a seeded Neo4j"),
        ("explain", "EXPLAIN plan checks on a seeded Neo4j"),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--schema", required=True, type=Path, help="the graph schema file")
        sub.add_argument("--root", type=Path, help="paths in findings are relative to this")
        if name in {"cyver", "explain"}:
            sub.add_argument("--image", help="a neo4j:<tag> image (default: pinned)")
        sub.add_argument("targets", nargs="*", help=".cypher files or directories")
    for name, text in (
        ("all", "every check, configured by one cypher-checks.toml"),
        ("no-inline-queries", "no query text in Python strings"),
        ("param-names", "run calls pass only the $params the loaded query declares"),
        ("derive-schema", "draft a schema file from a live database"),
    ):
        commands.add_parser(name, help=text, add_help=False)
    return parser


def _live(
    command: str, schema: GraphSchema, targets: list[Path], args: argparse.Namespace
) -> list[Finding]:
    """Start, seed and stop one container for the whole batch.

    The driver logs every server notification; the checks report the ones that matter."""
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    with container.seeded_driver(schema, args.image) as driver:
        if command == "cyver":
            return cyver_check.check_files(driver, targets, schema.policy.ddl_globs, args.root)
        return explain.check_files(driver, targets, schema, args.root)


def _report_live(
    command: str, schema: GraphSchema, targets: list[Path], args: argparse.Namespace
) -> int:
    """A live check that cannot run (bad image, no Docker, server error) is a tool error."""
    try:
        findings = _live(command, schema, targets, args)
    except container.LIVE_ERRORS as exc:
        sys.stderr.write(f"error: the live checks could not run ({exc}); is Docker running?\n")
        return EXIT_ERROR
    return report(findings)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    standalone = _STANDALONE.get(raw[0]) if raw else None
    if standalone is not None:
        return standalone(raw[1:])
    args = _parser().parse_args(raw)
    try:
        schema = load_schema(args.schema)
    except SchemaError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_ERROR
    targets = expand_targets(args.targets)
    if args.command == "parity":
        # No DDL file is a finding of its own, never a skip.
        ddl = parity.ddl_files_among(targets, schema)
        findings = parity.check(ddl, schema, schema_label=str(args.schema), root=args.root)
        return report(findings)
    if not targets:
        sys.stdout.write("skipped: no .cypher files\n")
        return EXIT_OK
    if args.command == "lint":
        return report(lint.check(targets, schema, args.root))
    return _report_live(args.command, schema, targets, args)


if __name__ == "__main__":
    raise SystemExit(main())
