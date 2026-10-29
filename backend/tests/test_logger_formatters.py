"""RED tests for LOG-04, LOG-05, LOG-06, LOG-07, LOG-08, LOG-11, LOG-12,
plus AC-5 (`severity`), AC-8 (no raw payload body), AC-19 b-d (value caps)
and AC-20 (reserved Cloud Logging keys) of the stdout-only JSON format.

Target-state contract under test: `src/logger/formatters.py`'s `JsonFormatter`
(JSON key order, UTC timestamp format, non-serializable-value fallback,
exception-chain fields) and `src/logger/logger.py`'s `log_event` (funcName via
`stacklevel=2`, message defaulting to `event`, RESERVED/CORRELATION field-name
collision renaming to `field_<k>`).

`src/logger/formatters.py` does not exist yet (Wave 3 builds it), and
`src/logger/logger.py` currently ships as an empty 0-byte skeleton (import
succeeds, attributes don't exist). Every `logger.*` name is therefore
imported **inside** each test function, never at module scope, matching
`tests/logger_harness.py`'s lazy-import convention: that keeps this whole
module collectible today, with each test failing individually for the
expected reason (`ModuleNotFoundError` for `logger.formatters`, or
`AttributeError` via `logger_harness.LogHarness.configure()` calling the
not-yet-implemented `logger.logger.reset()`), rather than the whole file
failing to collect.

Authoritative sources (read in full before writing these tests):
- The central-logger test catalog: harness, rows LOG-04,
  LOG-05, LOG-06, LOG-07, LOG-08, LOG-11, LOG-12.
- The StrFormatter rules (§1 extra-key
  collision, non-serializable values).
- The TracebackFormatter rules (exc_* keys,
  exception chaining).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest


def _raise(error: BaseException, cause: BaseException | None = None) -> NoReturn:
    raise error from cause


def _make_record(
    *,
    name: str = "test.logger_formatters",
    level: int = logging.INFO,
    msg: str = "hello world",
    func: str = "sample_func",
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
        func=func,
    )


# ---------------------------------------------------------------------------
# LOG-04 — JSON key order; asctime `\.\d{3}Z$`; 1 line/record.
# ---------------------------------------------------------------------------


def test_log_04_json_formatter_key_order_and_timestamp_format() -> None:
    from logger.formatters import JsonFormatter

    record = _make_record()
    record.event = "sample.event"
    record.custom_field = "abc"

    formatted = JsonFormatter().format(record)

    # Exactly one JSONL line per record (embedded newlines, if any, must be
    # JSON-escaped, never literal).
    assert formatted.count("\n") == 0

    payload = json.loads(formatted)
    assert list(payload.keys()) == [
        "asctime",
        "levelname",
        "severity",
        "name",
        "funcName",
        "message",
        "event",
        "custom_field",
    ]
    assert payload["levelname"] == "INFO"
    assert payload["severity"] == "INFO"
    assert payload["name"] == "test.logger_formatters"
    assert payload["funcName"] == "sample_func"
    assert payload["message"] == "hello world"
    assert payload["event"] == "sample.event"
    assert payload["custom_field"] == "abc"

    assert re.search(r"\.\d{3}Z$", payload["asctime"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", payload["asctime"])


def test_log_04_json_formatter_places_correlation_keys_between_message_and_event() -> None:
    """Group 2 (correlation keys) sits strictly between group 1 (`message`)
    and group 3 (`event`); group 4 (flat fields) comes after `event`. The
    relative order *within* the correlation-key group is not asserted here
    (not specified by the `formatters.py` row beyond "correlation keys (if
    set)" as a single ordered group) — only that the group boundary holds.
    """

    from logger.formatters import JsonFormatter

    record = _make_record(name="test.logger_formatters.correlation_order")
    record.event = "sample.event"
    record.request_id = "11111111111111111111111111111111"
    record.run_id = "22222222222222222222222222222222"
    record.custom_field = "abc"

    formatted = JsonFormatter().format(record)
    keys = list(json.loads(formatted).keys())

    message_index = keys.index("message")
    event_index = keys.index("event")
    custom_field_index = keys.index("custom_field")

    assert message_index < keys.index("request_id") < event_index
    assert message_index < keys.index("run_id") < event_index
    assert event_index < custom_field_index


# ---------------------------------------------------------------------------
# LOG-05 — log_event: funcName == caller (stacklevel=2); fields top-level;
# message defaults to event.
# ---------------------------------------------------------------------------


def test_log_05_log_event_funcname_reflects_caller_and_fields_are_top_level(
    central_log,
) -> None:
    central_log.configure(LOGS_LEVEL="INFO")

    from logger.logger import INFO, get_logger, log_event

    log = get_logger("test.logger_formatters.log_event_stacklevel")

    def _caller_of_log_event() -> None:
        log_event(log, INFO, "sample.event", user_id="abc123")

    _caller_of_log_event()

    records = central_log.events("sample.event")
    assert len(records) == 1
    record = records[0]

    # stacklevel=2: funcName is the caller of log_event, never log_event
    # itself.
    assert record["funcName"] == "_caller_of_log_event"
    # message defaults to the event name when no explicit message is given.
    assert record["message"] == "sample.event"
    # fields are flat top-level JSON keys, not nested under e.g. "fields".
    assert record["user_id"] == "abc123"
    assert "fields" not in record


def test_log_05_log_event_explicit_message_overrides_the_event_default(
    central_log,
) -> None:
    central_log.configure(LOGS_LEVEL="INFO")

    from logger.logger import INFO, get_logger, log_event

    log = get_logger("test.logger_formatters.log_event_message")
    log_event(log, INFO, "sample.event.with_message", "a custom message", k="v")

    records = central_log.events("sample.event.with_message")
    assert len(records) == 1
    assert records[0]["message"] == "a custom message"
    assert records[0]["event"] == "sample.event.with_message"


# ---------------------------------------------------------------------------
# LOG-06 — reserved/correlation-colliding field names get `field_<k>`-renamed,
# never raise KeyError.
# ---------------------------------------------------------------------------


def test_log_06_field_name_collisions_get_field_prefixed(central_log) -> None:
    central_log.configure(LOGS_LEVEL="INFO")

    from logger.logger import INFO, get_logger, log_event

    log = get_logger("test.logger_formatters.collisions")

    # `event` is positional-only in log_event's signature, so passing it
    # again as a keyword lands in **fields — exactly the collision this test
    # targets. None of these calls may raise (e.g. stdlib's own
    # `KeyError: Attempted to overwrite ... in LogRecord`).
    log_event(
        log,
        INFO,
        "collision.event",
        name="custom-name",
        msg="custom-msg",
        args="custom-args",
        message="custom-message",
        asctime="custom-asctime",
        levelname="custom-levelname",
        run_id="custom-run-id",
        event="custom-event-field",
    )

    records = central_log.events("collision.event")
    assert len(records) == 1
    record = records[0]

    for key in (
        "name",
        "msg",
        "args",
        "message",
        "asctime",
        "levelname",
        "run_id",
        "event",
    ):
        assert f"field_{key}" in record, f"expected a field_{key} rename"

    assert record["field_name"] == "custom-name"
    assert record["field_msg"] == "custom-msg"
    assert record["field_args"] == "custom-args"
    assert record["field_message"] == "custom-message"
    assert record["field_asctime"] == "custom-asctime"
    assert record["field_levelname"] == "custom-levelname"
    assert record["field_run_id"] == "custom-run-id"
    assert record["field_event"] == "custom-event-field"

    # The real reserved keys keep their normal stdlib/central-logger meaning,
    # untouched by the collision.
    assert record["event"] == "collision.event"
    assert record["name"] == "test.logger_formatters.collisions"
    assert isinstance(record["asctime"], str)


# ---------------------------------------------------------------------------
# LOG-07 — `debug("x %s", Spy())` at INFO effective level → __str__ never
# called (stdlib laziness).
# ---------------------------------------------------------------------------


def test_log_07_debug_args_stay_lazy_when_level_disabled(central_log) -> None:
    central_log.configure(LOGS_LEVEL="INFO")

    from logger.logger import get_logger

    log = get_logger("test.logger_formatters.lazy_args")

    class Spy:
        def __init__(self) -> None:
            self.calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return "spy-str"

    spy = Spy()
    log.debug("x %s", spy)  # DEBUG is below the configured INFO level

    assert spy.calls == 0, "a disabled DEBUG call must never format its args"


# ---------------------------------------------------------------------------
# LOG-08 — non-serializable field values → valid JSON, formatter never
# raises. Parametrized over ~9 exotic value types (StrFormatter.md;
# crash_safety.md §1; R3 fallback).
# ---------------------------------------------------------------------------


class _PlainObject:
    """A generic object with no special serialization support."""


class _RaisesOnStr:
    """An object whose `__str__` itself raises — `default=str` can't save it."""

    def __str__(self) -> str:
        raise RuntimeError("str explosion")

    def __repr__(self) -> str:
        return "_RaisesOnStr()"


def _make_circular_list() -> list[Any]:
    circular: list[Any] = []
    circular.append(circular)
    return circular


_LOG_08_CASES: list[Any] = [
    pytest.param("plain_object", _PlainObject(), id="object"),
    pytest.param("a_set", {1, 2, 3}, id="set"),
    pytest.param("a_datetime", datetime(2026, 9, 25, tzinfo=UTC), id="datetime"),
    pytest.param("a_path", Path("/work/example"), id="path"),
    pytest.param("some_bytes", b"raw-bytes", id="bytes"),
    pytest.param("circular", _make_circular_list(), id="circular-reference"),
    pytest.param("tuple_keyed", {(1, 2): "value"}, id="tuple-key-dict"),
    pytest.param("raising_str", _RaisesOnStr(), id="raising-dunder-str"),
    pytest.param("not_a_number", float("nan"), id="nan"),
]


@pytest.mark.parametrize(("field_name", "value"), _LOG_08_CASES)
def test_log_08_non_serializable_field_values_never_crash_the_formatter(field_name: str, value: Any) -> None:
    from logger.formatters import JsonFormatter

    record = _make_record(name="test.logger_formatters.log_08")
    record.event = "log08.event"
    setattr(record, field_name, value)

    formatted = JsonFormatter().format(record)

    # Regardless of which internal fallback path fires (default=str success,
    # or the R3 fixed-keys fallback for values that defeat default=str, e.g.
    # circular refs / tuple keys / a raising __str__ / NaN with
    # allow_nan=False), the formatter must never raise, and the result must
    # always be exactly one line of valid JSON.
    assert formatted.count("\n") == 0
    payload = json.loads(formatted)
    assert isinstance(payload, dict)


# ---------------------------------------------------------------------------
# LOG-11 — exc_info in an except block, incl. `raise X from Y`: exc_type /
# exc_message / exc_traceback populated, "direct cause" chain text kept,
# `message` carries no traceback.
# ---------------------------------------------------------------------------


def test_log_11_exception_chain_fields_and_direct_cause_kept(central_log) -> None:
    central_log.configure(LOGS_LEVEL="INFO")

    from logger.logger import ERROR, get_logger, log_event

    log = get_logger("test.logger_formatters.exc_chain")

    try:
        try:
            _raise(ValueError("root cause"))
        except ValueError as root:
            _raise(RuntimeError("wrapped failure"), root)
    except RuntimeError:
        log_event(log, ERROR, "chain.failed", exc_info=True)

    records = central_log.events("chain.failed")
    assert len(records) == 1
    record = records[0]

    assert record["exc_type"] == "RuntimeError"
    assert record["exc_message"] == "wrapped failure"
    assert "ValueError: root cause" in record["exc_traceback"]
    assert "RuntimeError: wrapped failure" in record["exc_traceback"]
    # Python's own chained-exception wording, preserved by
    # traceback.format_exc()/formatException.
    assert "direct cause" in record["exc_traceback"]

    # The top-level message stays clean — no embedded traceback text leaking
    # in via stdlib Formatter.format()'s default exc_text-into-message
    # behavior.
    assert "Traceback" not in record["message"]
    assert record["message"] == "chain.failed"


# ---------------------------------------------------------------------------
# LOG-12 — exc_info=True outside any except block → no exc_* keys, no raise.
# ---------------------------------------------------------------------------


def test_log_12_exc_info_true_outside_except_produces_no_exc_fields(
    central_log,
) -> None:
    central_log.configure(LOGS_LEVEL="INFO")

    from logger.logger import WARNING, get_logger, log_event

    log = get_logger("test.logger_formatters.exc_info_outside_except")

    # No active exception here: sys.exc_info() == (None, None, None). This
    # call must not raise.
    log_event(log, WARNING, "no.active.exception", exc_info=True)

    records = central_log.events("no.active.exception")
    assert len(records) == 1
    record = records[0]

    for key in ("exc_type", "exc_message", "exc_traceback"):
        assert key not in record


# ---------------------------------------------------------------------------
# AC-5 — `severity` (GCP LogSeverity) follows `levelname`; a custom level is
# `DEFAULT`; a caller field named `severity` is renamed, never trusted.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (logging.WARNING, "WARNING"),
        (logging.ERROR, "ERROR"),
        (logging.CRITICAL, "CRITICAL"),
        (25, "DEFAULT"),
    ],
)
def test_severity_follows_levelname(level: int, expected: str) -> None:
    from logger.formatters import JsonFormatter

    payload = json.loads(JsonFormatter().format(_make_record(level=level)))

    assert payload["severity"] == expected
    assert list(payload)[:6] == [
        "asctime",
        "levelname",
        "severity",
        "name",
        "funcName",
        "message",
    ]


def test_log_event_severity_field_is_renamed_not_trusted(central_log) -> None:
    central_log.configure(LOGS_LEVEL="INFO")

    from logger.logger import INFO, get_logger, log_event

    log_event(get_logger("test.logger_formatters.severity"), INFO, "sev.event", severity="x")

    [record] = central_log.events("sev.event")
    assert record["field_severity"] == "x"
    assert record["severity"] == record["levelname"] == "INFO"


# ---------------------------------------------------------------------------
# AC-8 (unit) — K_sha256 rule: a raw body `K` next to `K_sha256` never
# reaches the JSON line; its preview does.
# ---------------------------------------------------------------------------


def test_raw_payload_body_with_a_sha256_sibling_is_never_printed() -> None:
    from logger.formatters import JsonFormatter

    raw_body = "RAW-BODY-" + "b" * 500
    record = _make_record()
    record.event = "payload.event"
    record.payload = raw_body
    record.payload_sha256 = "0" * 64
    record.payload_preview = raw_body[:200]

    formatted = JsonFormatter().format(record)
    payload = json.loads(formatted)

    assert raw_body not in formatted
    assert "payload" not in payload
    assert payload["payload_preview"] == raw_body[:200]
    assert payload["payload_sha256"] == "0" * 64


# ---------------------------------------------------------------------------
# AC-19 b-d — JSON value caps: 2,000 chars per value (head kept), 16,000
# chars of traceback (tail kept), each with a visible marker.
# ---------------------------------------------------------------------------


def test_long_string_field_is_capped_at_2000_chars_with_a_marker() -> None:
    from logger.formatters import JsonFormatter

    record = _make_record()
    record.big = "s" * 5_000

    payload = json.loads(JsonFormatter().format(record))

    assert payload["big"] == "s" * 2_000 + "…[truncated 3000 chars]"


def test_oversized_structured_field_becomes_a_capped_string() -> None:
    from logger.formatters import JsonFormatter

    value = {"k": "d" * 5_000}
    serialised = json.dumps(value, ensure_ascii=False)
    record = _make_record()
    record.small = {"k": "v", "n": [1, 2]}
    record.big = value

    payload = json.loads(JsonFormatter().format(record))

    # A normal-size structure keeps its type (queryable as nested JSON).
    assert payload["small"] == {"k": "v", "n": [1, 2]}
    removed = len(serialised) - 2_000
    assert payload["big"] == serialised[:2_000] + f"…[truncated {removed} chars]"


def _long_chained_exc_info() -> Any:
    """An exception whose formatted traceback is far over 16,000 chars."""

    cause: BaseException | None = None
    for index in range(70):
        try:
            _raise(ValueError(f"cause-{index} " + "c" * 500), cause)
        except ValueError as exc:
            cause = exc
    try:
        _raise(RuntimeError("final failure"), cause)
    except RuntimeError:
        return sys.exc_info()


def test_long_traceback_keeps_its_tail_within_16000_chars() -> None:
    from logger.formatters import JsonFormatter

    formatter = JsonFormatter()
    exc_info = _long_chained_exc_info()
    full_traceback = formatter.formatException(exc_info)
    assert len(full_traceback) > 30_000
    record = _make_record(level=logging.ERROR)
    record.exc_info = exc_info

    payload = json.loads(formatter.format(record))

    traceback_text = payload["exc_traceback"]
    marker = f"[truncated {len(full_traceback) - 16_000} chars]…"
    assert traceback_text.startswith("[truncated ")
    assert traceback_text == marker + full_traceback[-16_000:]
    assert traceback_text.endswith("RuntimeError: final failure")
    assert payload["exc_type"] == "RuntimeError"


def test_long_stack_info_keeps_its_tail() -> None:
    from logger.formatters import JsonFormatter

    record = _make_record()
    record.stack_info = "frame\n" * 4_000 + "last frame"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["stack_info"].startswith("[truncated ")
    assert payload["stack_info"].endswith("last frame")
    assert len(payload["stack_info"]) <= 16_000 + len("[truncated 99999 chars]…")


def test_long_message_and_exc_message_are_truncated() -> None:
    from logger.formatters import JsonFormatter

    try:
        _raise(ValueError("e" * 3_000))
    except ValueError:
        exc_info = sys.exc_info()
    record = _make_record(level=logging.ERROR, msg="m" * 3_000)
    record.exc_info = exc_info

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "m" * 2_000 + "…[truncated 1000 chars]"
    assert payload["exc_message"] == "e" * 2_000 + "…[truncated 1000 chars]"


def test_fallback_paths_apply_the_same_caps() -> None:
    """`_retry` (a NaN `event` -- not a capped flat field -- defeats
    `allow_nan=False`) and `_minimal` (the last resort, called directly)
    still emit bounded JSON."""

    from logger.formatters import JsonFormatter

    record = _make_record(msg="m" * 3_000)
    record.event = float("nan")
    record.big = "s" * 5_000

    payload = json.loads(JsonFormatter().format(record))

    assert payload["format_error"] == "ValueError"
    assert payload["big"] == "s" * 2_000 + "…[truncated 3000 chars]"
    assert payload["message"] == "m" * 2_000 + "…[truncated 1000 chars]"

    minimal = JsonFormatter()._minimal(record, RuntimeError("x"), fixed=None)
    minimal_payload = json.loads(minimal)
    assert list(minimal_payload) == [
        "asctime",
        "levelname",
        "severity",
        "name",
        "funcName",
        "message",
        "format_error",
    ]
    assert minimal_payload["severity"] == "INFO"
    assert minimal_payload["message"] == "m" * 2_000 + "…[truncated 1000 chars]"


# ---------------------------------------------------------------------------
# AC-20 — reserved Cloud Logging keys: `log_event` renames them to
# `field_<name>`; a raw attribute with such a name is never printed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("time", "t"),
        ("trace", "x"),
        ("labels", {"a": 1}),
        ("timestamp", "ts"),
        ("spanId", "s"),
        ("httpRequest", {"status": 200}),
    ],
)
def test_reserved_cloud_logging_keys_are_renamed_or_dropped(central_log, field: str, value: Any) -> None:
    central_log.configure(LOGS_LEVEL="INFO")

    from logger.formatters import JsonFormatter
    from logger.logger import INFO, get_logger, log_event

    log_event(
        get_logger("test.logger_formatters.reserved"),
        INFO,
        "reserved.event",
        **{field: value},
    )

    [record] = central_log.events("reserved.event")
    assert record[f"field_{field}"] == value
    assert field not in record
    assert record["severity"] == record["levelname"] == "INFO"

    raw = _make_record()
    raw.__dict__[field] = value
    raw.__dict__["logging.googleapis.com/trace"] = "spoofed"
    raw.__dict__["severity"] = "EMERGENCY"
    payload = json.loads(JsonFormatter().format(raw))
    assert "logging.googleapis.com/trace" not in payload
    assert field not in payload
    assert payload["severity"] == "INFO"


# ---------------------------------------------------------------------------
# Hardening — one bad value degrades alone; field names are bounded;
# exception fields cannot be faked through `log_event`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "expected"),
    [
        (float("nan"), "nan"),
        (float("inf"), "inf"),
        ({"x": float("nan")}, "{'x': nan}"),
    ],
    ids=["nan", "inf", "nested-nan"],
)
def test_non_finite_floats_degrade_only_their_own_field(bad: Any, expected: str) -> None:
    from logger.formatters import JsonFormatter

    record = _make_record()
    record.a = bad
    record.n = 3
    record.d = {"k": 1}

    payload = json.loads(JsonFormatter().format(record))

    assert "format_error" not in payload
    assert payload["a"] == expected
    assert payload["n"] == 3
    assert payload["d"] == {"k": 1}


def test_a_very_long_field_name_is_cut_at_200_chars() -> None:
    from logger.formatters import JsonFormatter

    record = _make_record()
    setattr(record, "k" * 5_000, "v")

    formatted = JsonFormatter().format(record)
    payload = json.loads(formatted)

    assert len(formatted) < 2_500
    assert payload["k" * 200 + "…"] == "v"


def test_log_event_cannot_fake_exception_fields(central_log) -> None:
    central_log.configure(LOGS_LEVEL="INFO")

    from logger.logger import INFO, get_logger, log_event

    log_event(
        get_logger("test.logger_formatters.fake_exc"),
        INFO,
        "fake.exc",
        exc_type="FakeError",
        exc_message="m",
        exc_traceback="t",
        format_error="e",
    )

    [record] = central_log.events("fake.exc")
    for key, value in {
        "exc_type": "FakeError",
        "exc_message": "m",
        "exc_traceback": "t",
        "format_error": "e",
    }.items():
        assert key not in record
        assert record[f"field_{key}"] == value
