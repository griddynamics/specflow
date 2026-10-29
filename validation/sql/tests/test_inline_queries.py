"""Self-test for `sqlcheck.inline_queries`."""

from __future__ import annotations

from pathlib import Path

from sqlcheck import inline_queries as rule
from sqlcheck.config import Config
from tests.support import FIXTURES

ROOT = FIXTURES / "inline_queries"


def _config(**overrides: object) -> Config:
    base = {
        "root": ROOT,
        "python_dirs": ("src",),
        "sql_dirs": ("src",),
        "inline_loaders": {"read_query": "src/app"},
    }
    return Config(**{**base, **overrides})  # type: ignore[arg-type]


def _rules(config: Config) -> set[tuple[str, str]]:
    return {(Path(f.path).name, f.rule) for f in rule.check(config)}


def test_inline_sql_and_missing_loader_file_are_flagged() -> None:
    found = _rules(_config())
    assert ("positive.py", "inline-queries-sql") in found
    assert ("positive.py", "inline-queries-missing-file") in found


def test_inline_sql_findings_cover_plain_and_f_strings() -> None:
    found = [
        f.line
        for f in rule.check(_config())
        if f.rule == "inline-queries-sql" and f.path.endswith("positive.py")
    ]
    assert sorted(found) == [3, 4]  # each f-string is reported once


def test_negative_module_is_clean() -> None:
    assert not [f for f in rule.check(_config()) if f.path.endswith("negative.py")]


def test_unreferenced_query_file_is_an_orphan() -> None:
    orphans = {Path(f.path).name for f in rule.check(_config()) if f.rule.endswith("orphan-file")}
    assert orphans == {"orphan.sql"}


def test_without_loaders_orphans_are_not_judged_and_step_is_degraded() -> None:
    config = _config(inline_loaders={})
    assert not any(f.rule.endswith("orphan-file") for f in rule.check(config))
    assert rule.run(config).detail.startswith("degraded:")
    assert rule.run(config).status in {"degraded", "findings"}


def test_migration_counts_only_text_handed_to_execute() -> None:
    config = _config(python_dirs=("src",), inline_migration_dirs=("alembic",))
    found = _rules(config)
    assert ("0001_raw.py", "inline-queries-sql") in found
    assert not any(name == "0002_core.py" for name, _ in found)


def test_exclude_globs_skip_a_file() -> None:
    config = _config(inline_exclude=("src/app/positive.py",))
    assert not [f for f in rule.check(config) if f.path.endswith("positive.py")]
