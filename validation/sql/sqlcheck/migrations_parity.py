"""Runs the project's Alembic migrations against a throwaway database:

- `migrations-not-single-head`: the history has exactly one head;
- `migrations-upgrade-failed`: `upgrade head` succeeds on an empty database;
- `migrations-drift`: `alembic check` then finds no difference between the migrated
  database and the models the project's `env.py` compares against;
- `migrations-round-trip-failed`: every revision upgrades, downgrades to its parent and
  upgrades again, each from its own empty database.

The database URL is `[migrations].url_template` with `{path}` a temporary file; it is set
as the config's `sqlalchemy.url` and exported as `[migrations].url_env` for an `env.py`
that reads the environment. The project's `env.py` and models are imported, so this step
runs in the project's own environment."""

from __future__ import annotations

import io
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from alembic.util import CommandError

from sqlcheck.config import Config
from sqlcheck.findings import Finding, StepResult

STEP = "migrations"
DOC = "RULES.md#migrations"


@contextmanager
def _database(ini: Path, url_template: str, url_env: str) -> Iterator[AlembicConfig]:
    """An Alembic config pointed at a fresh database file, removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="sqlcheck-migrations-") as tmp:
        url = url_template.format(path=Path(tmp) / "migrations.db")
        previous = os.environ.get(url_env)
        os.environ[url_env] = url
        try:
            alembic_config = AlembicConfig(str(ini), stdout=io.StringIO())
            alembic_config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
            yield alembic_config
        finally:
            if previous is None:
                os.environ.pop(url_env, None)
            else:
                os.environ[url_env] = previous


class ProjectEnvironmentError(Exception):
    """env.py or the models cannot be imported here: a setup problem, not a migration defect."""


def _is_import_failure(exc: BaseException | None) -> bool:
    while exc is not None:
        if isinstance(exc, ImportError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _finding(where: str, rule: str, message: str, fix: str) -> Finding:
    return Finding(where, 1, 1, rule, message, fix=fix, doc=DOC)


def check(ini: Path, where: str, url_template: str, url_env: str) -> list[Finding]:
    script = ScriptDirectory.from_config(AlembicConfig(str(ini)))
    findings: list[Finding] = []
    heads = script.get_heads()
    if len(heads) != 1:
        findings.append(
            _finding(
                where,
                "migrations-not-single-head",
                f"the history has {len(heads)} heads: {sorted(heads)}",
                "merge the branches into one linear history",
            )
        )
        return findings
    with _database(ini, url_template, url_env) as alembic_config:
        try:
            command.upgrade(alembic_config, "head")
        except Exception as exc:  # any failure of the project's migrations is the finding
            if _is_import_failure(exc):
                raise ProjectEnvironmentError(f"{type(exc).__name__}: {exc}") from exc
            findings.append(
                _finding(
                    where,
                    "migrations-upgrade-failed",
                    f"upgrade head failed on an empty database: {exc!r}",
                    "fix the failing migration",
                )
            )
            return findings
        try:
            command.check(alembic_config)
        except CommandError as exc:
            findings.append(
                _finding(
                    where,
                    "migrations-drift",
                    f"alembic check found drift after upgrade head: {exc}",
                    "generate a migration for the model change, or fix the migration",
                )
            )
    for revision in script.walk_revisions():
        down = revision.down_revision
        target = "base" if down is None else (down if isinstance(down, str) else down[0])
        with _database(ini, url_template, url_env) as alembic_config:
            try:
                command.upgrade(alembic_config, revision.revision)
                command.downgrade(alembic_config, target)
                command.upgrade(alembic_config, revision.revision)
            except Exception as exc:  # any failure of the round trip is the finding
                findings.append(
                    _finding(
                        where,
                        "migrations-round-trip-failed",
                        f"revision {revision.revision} does not round-trip "
                        f"(up, down to {target}, up): {exc!r}",
                        "make downgrade() the exact reverse of upgrade()",
                    )
                )
    return findings


def run(config: Config) -> StepResult:
    if config.alembic_ini is None:
        return StepResult.ok(STEP, detail="skipped: [migrations].alembic_ini is not set")
    if not config.alembic_ini.is_file():
        return StepResult.tool_error(STEP, f"no file {config.rel(config.alembic_ini)}")
    try:
        findings = check(
            config.alembic_ini,
            config.rel(config.alembic_ini),
            config.migrations_url_template,
            config.migrations_url_env,
        )
    except ProjectEnvironmentError as exc:
        return StepResult.tool_error(
            STEP,
            f"env.py or the models cannot be imported; run from the project env: {exc}",
        )
    return StepResult.from_findings(STEP, findings)
