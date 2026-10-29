"""Shared test harness for the central-logger test suite (plan `w2-0`).

Contract: the central-logger test harness overview.

Import convention: this repo's
`tests/` directory has no `__init__.py`, so shared test helpers are
imported bare, e.g. `from logger_harness import central_log, events`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

import logger.logger as logger_module

# The standard attributes every stdlib `logging.LogRecord` carries. Anything
# else on a record is a structured field the application logged. This is a
# reasonable best-effort superset of the real `logger.correlation.RESERVED_ATTRS`
# (which doesn't exist yet — Wave 3 builds it).
_RESERVED_LOG_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
    }
)

# Types `json.dumps` accepts without a `default=` fallback.
_JSON_PLAIN_SCALARS = (str, int, float, bool, type(None))


# --------------------------------------------------------------------------
# §1.3 — shared caplog helpers (library and example-entrypoint tests).
# These take objects as arguments rather than importing `logger.*`
# themselves, so they need no lazy-import guarding — kept simple regardless.
# --------------------------------------------------------------------------


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[Any]:
    """Filter `caplog.records` to those whose `event` field equals `name`."""

    matched = []
    for record in caplog.records:
        value = getattr(record, "event", record.__dict__.get("event"))
        if value == name:
            matched.append(record)
    return matched


def fields(record: Any) -> dict[str, Any]:
    """Return the non-reserved structured fields off a LogRecord or dict."""

    if isinstance(record, Mapping):
        source: dict[str, Any] = dict(record)
    else:
        source = dict(vars(record))
    return {key: value for key, value in source.items() if key not in _RESERVED_LOG_RECORD_ATTRS}


def _assert_leaf_json_plain(path: str, value: Any) -> None:
    if isinstance(value, _JSON_PLAIN_SCALARS):
        return
    if isinstance(value, dict):
        for key, sub_value in value.items():
            _assert_leaf_json_plain(f"{path}.{key}", sub_value)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_leaf_json_plain(f"{path}[{index}]", item)
        return
    raise AssertionError(
        f"field {path!r} has a non-JSON-plain value of type "
        f"{type(value).__name__!r} ({value!r}); the real code must convert "
        "this to a plain dict/list/str/int/float/bool/None (e.g. via "
        "dict()/dataclasses.asdict()) before logging, rather than relying "
        "on json.dumps(default=str)"
    )


def assert_json_plain(record: Any) -> None:
    """Assert every field value on `record` is `json.dumps`-able as-is.

    Accepts a `logging.LogRecord` (its non-reserved fields are checked, via
    `fields()`) or a plain dict of already-extracted fields. Raises
    `AssertionError`, naming the offending key, for any value that is not a
    plain `dict`/`list`/`str`/`int`/`float`/`bool`/`None` — e.g. a
    `MappingProxyType` or a dataclass instance, which `json.dumps` can only
    serialize by falling back to `default=str`.
    """

    payload = dict(record) if isinstance(record, Mapping) else fields(record)
    for key, value in payload.items():
        _assert_leaf_json_plain(str(key), value)


# --------------------------------------------------------------------------
# §1.2 — `central_log` fixture / `LogHarness`.
# --------------------------------------------------------------------------


class LogHarness:
    """Drives one test's isolated `logger.logger` pipeline."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._monkeypatch = monkeypatch
        self._capsys = capsys
        self.config: Any = None
        # Buffers for `.stdout()`/`.stderr()` — see the fix note on those
        # methods below for why these exist.
        self._out_buffer = ""
        self._err_buffer = ""
        # Everything captured on stdout during this test, never consumed:
        # `.records()` parses it, independently of `.stdout()` reads.
        self._all_out = ""

    def configure(self, **env: Any) -> Any:
        """Bootstrap a fresh pipeline that prints every record on stdout.

        Defaults `LOGS_STDOUT_LEVEL` to `DEBUG` (the suite-wide conftest
        guard sets `WARNING`), so `LOGS_LEVEL` alone decides what is
        recorded; any keyword overrides (or other `LOGS_*` vars) are
        monkeypatched into the environment first. Then calls
        `logger.logger.reset()` followed by `logger.logger.get_config()`,
        storing and returning the result as `.config`.
        """

        settings: dict[str, str] = {"LOGS_STDOUT_LEVEL": "DEBUG"}
        settings.update({key: str(value) for key, value in env.items()})
        for key, value in settings.items():
            self._monkeypatch.setenv(key, value)

        logger_module.reset()
        self.config = logger_module.get_config()
        return self.config

    def records(self) -> list[dict[str, Any]]:
        """Flush the pipeline, then parse every JSON log line seen on stdout.

        Cumulative for the whole test (unaffected by `.stdout()` reads).
        Only lines starting with `{` are parsed — prints such as `[PASS]`
        are skipped — and a `{` line that is not valid JSON fails the test
        (a torn or interleaved line, LOG-25). Of the parsed objects, only
        log records (dicts carrying `levelname`) are returned.
        """

        self._flush_pipeline()
        self._drain_capsys()
        parsed: list[dict[str, Any]] = []
        for line in self._all_out.splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise AssertionError(f"stdout line is not valid JSON ({exc}): {stripped[:200]!r}") from exc
            if isinstance(value, dict) and "levelname" in value:
                parsed.append(value)
        return parsed

    def events(self, name: str) -> list[dict[str, Any]]:
        """Filter `.records()` down to records whose `event` field == `name`."""

        return [record for record in self.records() if record.get("event") == name]

    def _flush_pipeline(self) -> None:
        """`logger.logger.flush()` before reading captured output.

        Without this, a test reading stdout/stderr
        immediately after logging can race the background `QueueListener`
        thread and observe nothing yet (flaky).
        """

        logger_module.flush()

    def _drain_capsys(self) -> None:
        """Pull whatever `capsys` has captured into our own buffers.

        `capsys.readouterr()` drains stdout *and* stderr together and
        resets both, so calling it independently from `.stdout()` and
        `.stderr()` would empty whichever stream the other method isn't
        reading — losing data if a test calls one then the other. Instead,
        every read goes through this single drain point and accumulates
        into per-stream buffers that `.stdout()`/`.stderr()` each consume
        (and clear) independently, so either method can be called any
        number of times, in either order, within one test without losing
        the other stream's content.
        """

        captured = self._capsys.readouterr()
        self._out_buffer += captured.out
        self._all_out += captured.out
        self._err_buffer += captured.err

    def stdout(self) -> str:
        """Raw stdout captured (via `capsys`) since the last `.stdout()` read."""

        self._flush_pipeline()
        self._drain_capsys()
        out, self._out_buffer = self._out_buffer, ""
        return out

    def stderr(self) -> str:
        """Console stderr captured (via `capsys`) since the last `.stderr()` read."""

        self._flush_pipeline()
        self._drain_capsys()
        err, self._err_buffer = self._err_buffer, ""
        return err

    def _teardown(self) -> None:
        """Reset the pipeline once more so nothing leaks into the next test."""

        logger_module.reset()


@pytest.fixture
def central_log(capsys: pytest.CaptureFixture[str]) -> Iterator[LogHarness]:
    """Yield a `LogHarness` for driving an isolated central-logger pipeline.

    Uses its own `pytest.MonkeyPatch()` instance (rather than depending on
    the built-in `monkeypatch` fixture) so teardown order is exact: env
    vars are undone first, then `logger.logger.reset()` runs again — so no
    test leaks an open queue or listener thread into the next one.

    Function-scoped by design (not session-scoped) for test isolation.
    """

    mp = pytest.MonkeyPatch()
    harness = LogHarness(monkeypatch=mp, capsys=capsys)
    try:
        yield harness
    finally:
        mp.undo()
        harness._teardown()
