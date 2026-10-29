"""`python -m cypher_checks all --config cypher-checks.toml`: every check of the kit in one run.

The config file (TOML, every path relative to the file) names the graph schema file, the
`.cypher` query files, the DDL files, the Python tree that loads the queries and the paths
semgrep scans; see `examples/cypher-checks.toml`. Steps run in a fixed order -- `lint`,
`parity`, `no-inline-queries`, `param-names`, `semgrep`, then `cyver` and `explain` on one
throwaway Neo4j container -- and each prints `== <step>` and its findings. A step whose input
is not configured prints `skipped: <reason>`.

Exit codes: 0 when every step is clean or skipped, 1 on any finding, 2 on a tool or
configuration error.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cypher_checks import (
    container,
    cyver_check,
    explain,
    lint,
    no_inline_queries,
    param_names,
    parity,
)
from cypher_checks.findings import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK, Finding, report
from cypher_checks.query_files import expand_targets
from cypher_checks.schema import GraphSchema, SchemaError, load_schema

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_SEMGREP_RULES",
    "ConfigError",
    "PythonSources",
    "RunConfig",
    "load_config",
    "main",
    "run",
]

_LOG = logging.getLogger(__name__)

DEFAULT_CONFIG = Path("cypher-checks.toml")
#: The kit's own semgrep rules; `[semgrep].rules` names a replacement file.
#: An installed wheel carries the file inside the package; a source checkout keeps it beside it.
_PACKAGED_RULES = Path(__file__).resolve().parent / "semgrep" / "neo4j.yml"
DEFAULT_SEMGREP_RULES = (
    _PACKAGED_RULES
    if _PACKAGED_RULES.is_file()
    else Path(__file__).resolve().parents[1] / "semgrep" / "neo4j.yml"
)


class ConfigError(Exception):
    """The run configuration is unreadable or malformed."""


@dataclass(frozen=True)
class PythonSources:
    """The Python tree that loads the queries; `no-inline-queries` and `param-names` scan it.

    `loaders` maps a loader function name to the directory its path argument is relative to.
    """

    root: Path
    loaders: Mapping[str, Path] = field(default_factory=dict)
    query_dirs: tuple[Path, ...] = ()
    orphan_exempt: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    migrations: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunConfig:
    """One run; every path is already resolved against the config file's directory."""

    schema: Path
    root: Path
    queries: tuple[Path, ...] = ()
    ddl: tuple[Path, ...] | None = None
    live: bool = True
    image: str | None = None
    python: PythonSources | None = None
    semgrep_paths: tuple[Path, ...] = ()
    semgrep_rules: Path = DEFAULT_SEMGREP_RULES


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        message = f"[{key}] must be a table"
        raise ConfigError(message)
    return value


def _strings(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        message = f"{where} must be a list of strings"
        raise ConfigError(message)
    return tuple(value)


def _paths(base: Path, value: object, where: str) -> tuple[Path, ...]:
    return tuple(base / v for v in _strings(value, where))


def _python_sources(base: Path, raw: Mapping[str, Any]) -> PythonSources | None:
    """The `[python]` table; `None` when it names no `root`."""
    if "root" not in raw:
        return None
    root = base / str(raw["root"])
    loaders = raw.get("loaders", {})
    if not isinstance(loaders, Mapping) or not all(isinstance(v, str) for v in loaders.values()):
        message = "[python].loaders must map each loader function name to its base directory"
        raise ConfigError(message)
    return PythonSources(
        root=root,
        loaders={str(name): root / base_dir for name, base_dir in loaders.items()},
        query_dirs=_paths(root, raw.get("query_dirs"), "[python].query_dirs"),
        orphan_exempt=_strings(raw.get("orphan_exempt"), "[python].orphan_exempt"),
        exclude=_strings(raw.get("exclude"), "[python].exclude"),
        migrations=_strings(raw.get("migrations"), "[python].migrations"),
    )


def load_config(path: Path) -> RunConfig:
    """Read one run configuration; relative paths resolve against the file's directory."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        message = f"cannot read config {path}: {exc}"
        raise ConfigError(message) from exc
    base = path.resolve().parent
    schema = data.get("schema")
    if not isinstance(schema, str) or not schema:
        message = f"{path}: `schema` must name the graph schema file"
        raise ConfigError(message)
    live = _table(data, "live")
    semgrep = _table(data, "semgrep")
    image = live.get("image")
    rules = semgrep.get("rules")
    return RunConfig(
        schema=base / schema,
        root=base / str(data.get("root", ".")),
        queries=_paths(base, data.get("queries"), "queries"),
        ddl=_paths(base, data["ddl"], "ddl") if "ddl" in data else None,
        live=bool(live.get("enabled", True)),
        image=str(image) if image else None,
        python=_python_sources(base, _table(data, "python")),
        semgrep_paths=(
            _paths(base, semgrep.get("paths"), "[semgrep].paths")
            if semgrep.get("enabled", True)
            else ()
        ),
        semgrep_rules=base / str(rules) if rules else DEFAULT_SEMGREP_RULES,
    )


def _emit(name: str, outcome: list[Finding] | str) -> int:
    """Print one step's header and its findings, or why it was skipped."""
    sys.stdout.write(f"== {name}\n")
    if isinstance(outcome, str):
        sys.stdout.write(f"skipped: {outcome}\n")
        return EXIT_OK
    return report(outcome)


def _parity(cfg: RunConfig, schema: GraphSchema) -> list[Finding] | str:
    if cfg.ddl is None:
        return "no `ddl` configured"
    ddl = parity.ddl_files_among(expand_targets(cfg.ddl), schema)
    return parity.check(ddl, schema, schema_label=str(cfg.schema), root=cfg.root)


def _no_inline_queries(cfg: RunConfig) -> list[Finding] | str:
    src = cfg.python
    if src is None:
        return "no [python] root configured"
    return no_inline_queries.check(
        no_inline_queries.Config(
            root=src.root,
            loaders=src.loaders,
            excludes=(*no_inline_queries.DEFAULT_EXCLUDES, *src.exclude),
            migration_globs=(*no_inline_queries.DEFAULT_MIGRATION_GLOBS, *src.migrations),
            query_dirs=src.query_dirs,
            orphan_exempt_globs=src.orphan_exempt,
        )
    )


def _param_names(cfg: RunConfig) -> list[Finding] | str:
    src = cfg.python
    if src is None or not src.loaders:
        return "no [python] loaders configured"
    return param_names.check(
        param_names.Config(
            root=src.root,
            loaders=src.loaders,
            excludes=(*param_names.DEFAULT_EXCLUDES, *src.exclude),
        )
    )


def _semgrep(cfg: RunConfig) -> int:
    """The kit's semgrep rules over `[semgrep].paths`; semgrep prints its own findings."""
    sys.stdout.write("== semgrep\n")
    if not cfg.semgrep_paths:
        sys.stdout.write("skipped: no [semgrep] paths configured\n")
        return EXIT_OK
    executable = shutil.which("semgrep")
    if executable is None:
        sys.stderr.write(
            "error: semgrep is not on PATH (runtime dependency of cypher-checks; reinstall it)\n"
        )
        return EXIT_ERROR
    sys.stdout.flush()
    command = [
        executable,
        "scan",
        "--config",
        str(cfg.semgrep_rules),
        "--error",
        "--metrics",
        "off",
        "--disable-version-check",
        "--quiet",
        *(str(p) for p in cfg.semgrep_paths),
    ]
    code = subprocess.run(command, check=False).returncode
    return code if code in {EXIT_OK, EXIT_FINDINGS} else EXIT_ERROR


def _live(cfg: RunConfig, schema: GraphSchema, files: list[Path]) -> int:
    """`cyver` and `explain` on one throwaway container, started and stopped here."""
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    try:
        with container.seeded_driver(schema, cfg.image) as driver:
            cyver = cyver_check.check_files(driver, files, schema.policy.ddl_globs, cfg.root)
            plans = explain.check_files(driver, files, schema, cfg.root)
    except container.LIVE_ERRORS:
        _LOG.exception("the live checks could not run; is Docker running?")
        return EXIT_ERROR
    return max(_emit("cyver", cyver), _emit("explain", plans))


def run(cfg: RunConfig, *, static_only: bool = False) -> int:
    """Every step in order; the exit code is the worst step's."""
    try:
        schema = load_schema(cfg.schema)
    except SchemaError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_ERROR
    files = expand_targets([*cfg.queries, *(cfg.ddl or ())])
    codes = [
        _emit("lint", lint.check(files, schema, cfg.root) if files else "no .cypher files"),
        _emit("parity", _parity(cfg, schema)),
        _emit("no-inline-queries", _no_inline_queries(cfg)),
        _emit("param-names", _param_names(cfg)),
        _semgrep(cfg),
    ]
    if static_only or not cfg.live or not files:
        if not files:
            reason = "no .cypher files"
        elif static_only:
            reason = "--static-only"
        else:
            reason = "[live] enabled = false"
        codes += [_emit("cyver", reason), _emit("explain", reason)]
    else:
        codes.append(_live(cfg, schema, files))
    return max(codes)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m cypher_checks all")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"the run configuration (default: ./{DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="skip the live steps (cyver, explain), which need Docker",
    )
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_ERROR
    return run(cfg, static_only=args.static_only)
