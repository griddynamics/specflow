"""Self-test for `sqlcheck.migrations_parity`, against the sample Alembic project."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from sqlcheck import migrations_parity as rule
from sqlcheck.config import Config
from tests.support import SAMPLE_APP

PROJECT = SAMPLE_APP / "migrations"
URL = "sqlite:///{path}"


def _copy(tmp_path: Path, versions: list[str]) -> Path:
    """A copy of the sample project holding only the named revision files."""
    target = tmp_path / "migrations"
    (target / "versions").mkdir(parents=True)
    shutil.copy(PROJECT / "alembic.ini", target / "alembic.ini")
    shutil.copy(PROJECT / "env.py", target / "env.py")
    for name in versions:
        shutil.copy(PROJECT / "versions" / name, target / "versions" / name)
    return target / "alembic.ini"


def _rules(ini: Path) -> list[str]:
    return [f.rule for f in rule.check(ini, "alembic.ini", URL, "SQLCHECK_TEST_DATABASE_URL")]


def test_the_sample_project_is_clean() -> None:
    assert _rules(PROJECT / "alembic.ini") == []


def test_a_clean_run_writes_nothing_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    _rules(PROJECT / "alembic.ini")
    assert capsys.readouterr().out == ""


def test_a_model_change_without_its_migration_is_drift(tmp_path: Path) -> None:
    assert _rules(_copy(tmp_path, ["0001_ledger_entry.py"])) == ["migrations-drift"]


def test_two_heads_are_reported(tmp_path: Path) -> None:
    ini = _copy(tmp_path, ["0001_ledger_entry.py", "0002_ledger_note.py"])
    (ini.parent / "versions" / "0003_branch.py").write_text(
        '"""a second branch off 0001."""\n'
        'revision = "0003"\ndown_revision = "0001"\n'
        "def upgrade() -> None:\n    pass\n"
        "def downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    assert _rules(ini) == ["migrations-not-single-head"]


def test_a_failing_upgrade_is_reported(tmp_path: Path) -> None:
    ini = _copy(tmp_path, ["0001_ledger_entry.py"])
    (ini.parent / "versions" / "0002_broken.py").write_text(
        '"""references a table that does not exist."""\n'
        "from alembic import op\n"
        'revision = "0002"\ndown_revision = "0001"\n'
        "def upgrade() -> None:\n"
        '    op.create_index("ix_missing", "missing_table", ["x"])\n'
        "def downgrade() -> None:\n"
        '    op.drop_index("ix_missing", table_name="missing_table")\n',
        encoding="utf-8",
    )
    assert _rules(ini) == ["migrations-upgrade-failed"]


def test_a_downgrade_that_does_not_reverse_is_reported(tmp_path: Path) -> None:
    ini = _copy(tmp_path, ["0001_ledger_entry.py"])
    (ini.parent / "versions" / "0002_one_way.py").write_text(
        '"""adds note without a usable downgrade."""\n'
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        'revision = "0002"\ndown_revision = "0001"\n'
        "def upgrade() -> None:\n"
        '    op.add_column("ledger_entry", sa.Column("note", sa.String(200), nullable=True))\n'
        "def downgrade() -> None:\n"
        '    op.drop_table("no_such_table")\n',
        encoding="utf-8",
    )
    assert _rules(ini) == ["migrations-round-trip-failed"]


def test_the_url_env_is_restored(tmp_path: Path) -> None:
    os.environ.pop("SQLCHECK_TEST_DATABASE_URL", None)
    _rules(_copy(tmp_path, ["0001_ledger_entry.py", "0002_ledger_note.py"]))
    assert "SQLCHECK_TEST_DATABASE_URL" not in os.environ


def test_run_skips_or_errors_on_a_missing_ini(tmp_path: Path) -> None:
    assert rule.run(Config(root=tmp_path)).detail.startswith("skipped:")
    missing = rule.run(Config(root=tmp_path, alembic_ini=tmp_path / "alembic.ini"))
    assert missing.exit_code == 2


def test_an_unimportable_env_is_an_environment_failure_not_a_finding(tmp_path: Path) -> None:
    ini = _copy(tmp_path, ["0001_ledger_entry.py"])
    (ini.parent / "env.py").write_text("import module_that_does_not_exist\n", encoding="utf-8")
    with pytest.raises(rule.ProjectEnvironmentError):
        rule.check(ini, "alembic.ini", URL, "SQLCHECK_TEST_DATABASE_URL")
    result = rule.run(Config(root=tmp_path, alembic_ini=ini))
    assert result.exit_code == 2
    assert "cannot be imported" in result.detail
