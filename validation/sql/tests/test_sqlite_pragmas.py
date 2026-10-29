"""Self-test for `sqlcheck.sqlite_pragmas`, against the sample engine factories."""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlcheck import sqlite_pragmas as rule
from sqlcheck.config import Config
from sqlcheck.loader import resolve

EXPECTED = {"journal_mode": "wal", "foreign_keys": 1, "busy_timeout": 7000}
KWARGS = {"busy_timeout_s": 7}


def _mismatches(factory: str, url: str) -> list[str]:
    findings = rule.check(resolve(factory), url, KWARGS, EXPECTED, "sqlcheck.toml")
    return [f.message.split(" ", 2)[1] for f in findings]


def test_a_factory_that_sets_every_pragma_is_clean() -> None:
    assert _mismatches("sample_app.engine:build_engine", "sqlite:///{path}") == []


def test_an_async_factory_is_read_through_its_event_loop() -> None:
    url = "sqlite+aiosqlite:///{path}"
    assert _mismatches("sample_app.engine:build_async_engine", url) == []


def test_a_factory_that_sets_none_is_reported_per_pragma() -> None:
    mismatches = _mismatches("sample_app.engine:build_plain_engine", "sqlite:///{path}")
    assert mismatches == ["busy_timeout", "foreign_keys", "journal_mode"]


def test_a_pragma_name_is_never_interpolated_unchecked() -> None:
    with pytest.raises(ValueError, match="not a pragma name"):
        rule.check(
            resolve("sample_app.engine:build_engine"),
            "sqlite:///{path}",
            KWARGS,
            {"journal_mode; DROP TABLE x": "wal"},
            "sqlcheck.toml",
        )


def test_run_skips_without_a_factory_or_expectations(tmp_path: Path) -> None:
    assert rule.run(Config(root=tmp_path)).detail.startswith("skipped:")
    no_expectations = Config(root=tmp_path, engine_factory="sample_app.engine:build_engine")
    assert rule.run(no_expectations).detail.startswith("skipped:")


def test_run_reads_the_configured_factory(tmp_path: Path) -> None:
    config = Config(
        root=tmp_path,
        engine_factory="sample_app.engine:build_plain_engine",
        factory_kwargs=KWARGS,
        expected_pragmas={"foreign_keys": 1},
    )
    result = rule.run(config)
    assert [f.rule for f in result.findings] == ["sqlite-pragmas-mismatch"]
