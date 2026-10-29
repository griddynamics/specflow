"""RED tests for `logger.config` — LOG-01, LOG-02, LOG-03 (AC-3) and AC-4.

Scenario text: the central-logger test catalog
(rows LOG-01/02/03). Module contract: the ops rules
("Runtime configuration", "Bootstrap rules", "Acceptance tests" #1).

`src/logger/config.py` does not exist yet — Wave 3 of the central-logger
plan (`w3-0`) builds it. Every import below is therefore expected to fail
with `ModuleNotFoundError`/`ImportError` today; that is the correct RED
state for this Wave 2 track (`w2-a1`). Do not skip or xfail these tests.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest

import logger._stderr as stderr_module
from logger.config import LogConfig, raise_exceptions_from_env


@pytest.fixture
def _clean_stderr_dedup():
    """Reset `_stderr.warn_once`'s process-wide dedup state around a test.

    `warn_once(key, message)` (contract: SPECS §4.1 `_stderr.py` row)
    fires at most once per `key` for the whole process — that is the
    point of "once". Cases below share a key (`config:LOGS_STDOUT_LEVEL`),
    so without a reset the second case to run would see zero warnings
    instead of exactly one. `tests/conftest.py`'s autouse fixture only resets
    `logger.correlation`,
    not `_stderr`, so any test here that counts warnings resets it
    locally instead of relying on the shared harness for this.
    """

    def _reset() -> None:
        stderr_module._reset()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# LOG-01 — ops.md "Acceptance tests" #1: bootstrap with `LOGS_LEVEL=banana`
# still starts the pipeline, emits exactly one stderr warning, and the
# effective app level falls back to INFO.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_clean_stderr_dedup")
def test_bad_level_starts_pipeline_with_one_warning_and_info_effective(central_log) -> None:
    central_log.configure(LOGS_LEVEL="banana")

    # Bootstrap "started": the config it published falls back to the
    # documented default instead of blowing up.
    assert central_log.config.level == logging.INFO

    stderr_lines = [line for line in central_log.stderr().splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    assert stderr_lines[0].startswith("[central-logger]")

    from logger.logger import get_logger

    app_logger = get_logger("logger-tests.config")
    assert app_logger.isEnabledFor(logging.INFO)
    assert not app_logger.isEnabledFor(logging.DEBUG)


# ---------------------------------------------------------------------------
# LOG-02 (AC-3) — a bad `LOGS_STDOUT_LEVEL` falls back to INFO with exactly
# one stderr warning; unset or empty falls back silently (0 lines).
# ---------------------------------------------------------------------------

_BAD_VALUE_CASES = [
    pytest.param("not-a-level", id="unknown-name"),
    pytest.param("10", id="numeric"),
]


@pytest.mark.parametrize("bad_value", _BAD_VALUE_CASES)
@pytest.mark.usefixtures("_clean_stderr_dedup")
def test_bad_stdout_level_falls_back_to_info_with_one_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_value: str,
) -> None:
    monkeypatch.setenv("LOGS_STDOUT_LEVEL", bad_value)

    config = LogConfig.from_env()

    assert config.stdout_level == logging.INFO

    stderr_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    assert stderr_lines[0].startswith("[central-logger]")


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
@pytest.mark.usefixtures("_clean_stderr_dedup")
def test_unset_or_empty_stdout_level_falls_back_silently(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("LOGS_STDOUT_LEVEL", raising=False)
    else:
        monkeypatch.setenv("LOGS_STDOUT_LEVEL", value)

    config = LogConfig.from_env()

    assert config.stdout_level == logging.INFO
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# LOG-03 (AC-3) — exactly two fields with the ops.md defaults; level names
# are case-insensitive ("debug" is accepted); the config is frozen.
# ---------------------------------------------------------------------------


def test_config_has_exactly_the_two_level_fields() -> None:
    assert {field.name for field in dataclasses.fields(LogConfig)} == {
        "level",
        "stdout_level",
    }


def test_defaults_match_the_ops_table() -> None:
    config = LogConfig()

    assert config.level == logging.INFO
    assert config.stdout_level == logging.INFO
    assert LogConfig.from_env(environ={}) == config


def test_lowercase_level_name_is_accepted() -> None:
    config = LogConfig.from_env(environ={"LOGS_LEVEL": "debug"})

    assert config.level == logging.DEBUG


def test_config_is_frozen() -> None:
    config = LogConfig()

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.level = logging.DEBUG


# ---------------------------------------------------------------------------
# AC-4 — removed or unknown `LOGS_*` variables are never read: no stderr
# line, no directory created, the pipeline still starts and prints JSON.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_clean_stderr_dedup")
def test_removed_and_unknown_logs_vars_are_ignored_silently(central_log, tmp_path: Path) -> None:
    stale_dir = tmp_path / "x"

    central_log.configure(
        LOGS_DIR=str(stale_dir),
        LOGS_RETENTION_DAYS="abc",
        LOGS_AGENT_FULL="True",
        LOGS_STDOUT_FORMAT="console",
    )

    from logger.logger import INFO, _get_state, get_logger, log_event

    assert _get_state().status == "active"
    log_event(get_logger("logger-tests.removed_vars"), INFO, "removed.vars.ignored")

    assert central_log.stderr() == ""
    assert not stale_dir.exists()

    lines = [line for line in central_log.stdout().splitlines() if line.strip()]
    assert lines, "expected the event on stdout"
    records = [json.loads(line) for line in lines]
    assert [record["event"] for record in records] == ["removed.vars.ignored"]
    assert records[0]["severity"] == "INFO"


# ---------------------------------------------------------------------------
# U4 — `LOGS_RAISE_EXCEPTIONS` is the development switch: boolean words turn
# it on or off; unset, empty or invalid is off (the deployed default), and an
# invalid value warns exactly once.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "Yes", " ON "])
def test_raise_exceptions_switch_reads_true_words(value: str) -> None:
    assert raise_exceptions_from_env({"LOGS_RAISE_EXCEPTIONS": value}) is True


@pytest.mark.parametrize("environ", [{}, {"LOGS_RAISE_EXCEPTIONS": ""}, {"LOGS_RAISE_EXCEPTIONS": "off"}])
@pytest.mark.usefixtures("_clean_stderr_dedup")
def test_raise_exceptions_switch_is_off_silently_by_default(
    capsys: pytest.CaptureFixture[str], environ: dict[str, str]
) -> None:
    assert raise_exceptions_from_env(environ) is False
    assert capsys.readouterr().err == ""


@pytest.mark.usefixtures("_clean_stderr_dedup")
def test_invalid_raise_exceptions_switch_is_off_with_one_warning(capsys: pytest.CaptureFixture[str]) -> None:
    assert raise_exceptions_from_env({"LOGS_RAISE_EXCEPTIONS": "maybe"}) is False
    assert raise_exceptions_from_env({"LOGS_RAISE_EXCEPTIONS": "maybe"}) is False

    stderr_lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    assert stderr_lines[0].startswith("[central-logger]")
