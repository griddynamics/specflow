"""Self-test for `sqlcheck.alembic_lint`."""

from __future__ import annotations

from pathlib import Path

from sqlcheck import alembic_lint as rule
from sqlcheck.config import Config
from tests.support import FIXTURES, SAMPLE_APP

VERSIONS = FIXTURES / "alembic_lint"

_HEADER = (
    "from __future__ import annotations\n"
    "from alembic import op\n"
    'revision: str = "0001"\n'
    "down_revision: str | None = None\n"
)


def _revision(tmp_path: Path, docstring: str, upgrade: str, downgrade: str) -> Path:
    (tmp_path / "0001.py").write_text(
        f'"""{docstring}"""\n{_HEADER}'
        f"def upgrade() -> None:\n    {upgrade}\n"
        f"def downgrade() -> None:\n    {downgrade}\n",
        encoding="utf-8",
    )
    return tmp_path


def _destructive(tmp_path: Path, docstring: str, upgrade: str, downgrade: str) -> bool:
    findings = rule.check(_revision(tmp_path, docstring, upgrade, downgrade))
    return any(f.rule == "alembic-lint-undocumented-destructive-op" for f in findings)


def test_positive_fixture_is_flagged() -> None:
    findings = rule.check(VERSIONS / "positive_versions")
    assert {f.rule for f in findings} == {
        "alembic-lint-noop-downgrade",
        "alembic-lint-undocumented-destructive-op",
        "alembic-lint-not-single-head",
    }


def test_negative_fixture_is_clean() -> None:
    assert rule.check(VERSIONS / "negative_versions") == []


def test_the_sample_project_is_clean() -> None:
    assert rule.check(SAMPLE_APP / "migrations" / "versions") == []


def test_decorative_docstring_that_never_names_the_object_is_still_flagged() -> None:
    """Alembic's template always fills in a docstring, so "non-empty" alone is no gate."""
    findings = rule.check(VERSIONS / "positive_versions")
    decorative = [
        f
        for f in findings
        if "0004_decorative_docstring.py" in f.path
        and f.rule == "alembic-lint-undocumented-destructive-op"
    ]
    assert len(decorative) == 1


def test_docstring_naming_the_dropped_table_clears_the_finding(tmp_path: Path) -> None:
    upgrade, downgrade = 'op.drop_table("scratch")', 'op.create_table("scratch")'
    assert not _destructive(tmp_path, "drops the scratch table.", upgrade, downgrade)


def test_docstring_mentioning_an_unrelated_table_does_not_clear_it(tmp_path: Path) -> None:
    assert _destructive(
        tmp_path, "cleans up after a release.", 'op.drop_table("scratch")', 'op.create_table("x")'
    )


def test_execute_object_is_extracted_from_the_sql_text(tmp_path: Path) -> None:
    assert not _destructive(
        tmp_path, "drops the legacy_audit table.", 'op.execute("DROP TABLE legacy_audit")', "pass"
    )


def test_execute_delete_is_destructive(tmp_path: Path) -> None:
    assert _destructive(tmp_path, "tidies rows.", 'op.execute("DELETE FROM widget")', "pass")


def test_execute_of_a_column_named_like_a_keyword_is_not_destructive(tmp_path: Path) -> None:
    update = 'op.execute("UPDATE widget SET deleted_at = NULL WHERE id = 1")'
    assert not _destructive(tmp_path, "resets a flag.", update, "pass")


def test_downgrade_reversing_its_own_upgrade_is_exempt(tmp_path: Path) -> None:
    assert not _destructive(
        tmp_path, "adds a scratch table.", 'op.create_table("scratch")', 'op.drop_table("scratch")'
    )


def test_downgrade_drop_of_an_object_not_created_by_this_revision_is_flagged(
    tmp_path: Path,
) -> None:
    assert _destructive(
        tmp_path, "adds widget_extra.", 'op.create_table("widget_extra")', 'op.drop_table("shim")'
    )


def test_op_f_wrapped_and_keyword_named_object_clears_the_finding(tmp_path: Path) -> None:
    upgrade = 'op.drop_index(op.f("ix_widget_legacy"), table_name="widget")'
    assert not _destructive(tmp_path, "drops ix_widget_legacy.", upgrade, "pass")


def test_call_with_no_extractable_object_name_is_not_flagged(tmp_path: Path) -> None:
    upgrade = 'op.drop_constraint(None, type_="foreignkey")'
    assert not _destructive(tmp_path, "batch-alters a table.", upgrade, "pass")


def test_merge_revision_tuple_counts_both_parents(tmp_path: Path) -> None:
    for name, down in (("a", None), ("b", "a"), ("c", "a")):
        (tmp_path / f"{name}.py").write_text(
            f'"""rev {name}"""\nrevision = "{name}"\ndown_revision = {down!r}\n'
            "def upgrade() -> None:\n    x = 1\n"
            "def downgrade() -> None:\n    x = 2\n",
            encoding="utf-8",
        )
    assert {f.rule for f in rule.check(tmp_path)} == {"alembic-lint-not-single-head"}
    (tmp_path / "d.py").write_text(
        '"""merge"""\nrevision = "d"\ndown_revision = ("b", "c")\n'
        "def upgrade() -> None:\n    x = 1\n"
        "def downgrade() -> None:\n    x = 2\n",
        encoding="utf-8",
    )
    assert rule.check(tmp_path) == []


def test_missing_versions_dir_is_clean(tmp_path: Path) -> None:
    assert rule.check(tmp_path / "does-not-exist") == []


def test_run_skips_without_a_configured_versions_dir(tmp_path: Path) -> None:
    assert rule.run(Config(root=tmp_path)).detail.startswith("skipped:")
    missing = Config(root=tmp_path, alembic_versions=tmp_path / "nope")
    assert rule.run(missing).detail.startswith("skipped:")


def test_run_reports_paths_relative_to_the_root() -> None:
    config = Config(root=FIXTURES, alembic_versions=VERSIONS / "positive_versions")
    result = rule.run(config)
    assert result.exit_code == 1
    assert all(f.path.startswith("alembic_lint/positive_versions/") for f in result.findings)
