"""The central logger's only log record format: one JSON object per line.

Covers extra-key collisions, non-serializable values, `exc_*` keys
and exception chaining.

`JsonFormatter` is *total*: `format()` never raises, regardless of what a
caller stuffed onto a `LogRecord` via `extra=`/`setattr`. This is the last
line of defense before a record lands on stdout -- crash_safety.md §1
("Formatter / emit errors") and §4 ("no self-logging") both depend on that
being true, since a raising formatter would otherwise propagate into
`Handler.emit`'s own try/except and could not be reported through the
central logger's own channel (that would risk recursion).

Every line is bounded: flat values, `message` and `exc_message` are capped
at `JSON_VALUE_CAP` chars (head kept); `exc_traceback` and `stack_info` at
`JSON_TRACEBACK_CAP` chars (tail kept, so the most recent frames and the
exception line survive). Both caps add a visible `[truncated N chars]`
marker.

K_sha256 rule: a field `K` with a sibling `K_sha256` on the same record is
treated as a summarised payload body (`logger.payload.summarise_payload`)
and is never printed; only `K_preview`/`K_size`/`K_sha256`/`K_truncated`
are. Do not name an unrelated field `<x>_sha256` next to an `<x>` you want
printed.

Cloud Logging special JSON keys (`CLOUD_LOGGING_KEYS`, plus anything under
`logging.googleapis.com/`) are reserved: `severity` is always derived from
`levelname`, and a raw record attribute with one of those names is never
printed (`log_event` renames such fields to `field_<name>` up front).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, override

from logger.correlation import CORRELATION_KEYS, RESERVED_ATTRS

#: The keys `JsonFormatter` always includes, in the exact order they must
#: appear as the JSON object's leading keys. Also the only keys a degraded
#: fallback payload ever carries (plus `format_error`).
_FIXED_KEYS: tuple[str, ...] = (
    "asctime",
    "levelname",
    "severity",
    "name",
    "funcName",
    "message",
)

#: Max chars of any flat field value, `message` and `exc_message` (head kept).
JSON_VALUE_CAP: int = 2_000

#: Max chars of `exc_traceback`/`stack_info` (tail kept: the newest frames).
JSON_TRACEBACK_CAP: int = 16_000

#: Max chars of a flat field's *name*; a longer name is cut, plus "…".
JSON_KEY_CAP: int = 200

#: Cloud Logging's special JSON fields (structured-logging docs). Never
#: printed from a record attribute; `log_event` renames them to `field_<k>`.
CLOUD_LOGGING_KEYS: frozenset[str] = frozenset(
    {
        "severity",
        "time",
        "timestamp",
        "timestampSeconds",
        "timestampNanos",
        "trace",
        "spanId",
        "labels",
        "httpRequest",
    }
)

#: Keys `JsonFormatter` itself writes for an exception or a fallback. Like
#: `CLOUD_LOGGING_KEYS`, never printed from a record attribute; `log_event`
#: renames them to `field_<k>`, so a caller cannot fake exception fields.
EXCEPTION_KEYS: frozenset[str] = frozenset({"exc_type", "exc_message", "exc_traceback", "format_error"})

#: Prefix of Cloud Logging's namespaced special keys (e.g. `.../trace`).
_CLOUD_LOGGING_PREFIX = "logging.googleapis.com/"

#: Level names that map 1:1 onto a GCP `LogSeverity`; anything else is
#: `DEFAULT`.
_GCP_SEVERITIES: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

#: Fields excluded from the "flat fields" pass: stdlib's own standard
#: attributes, the four correlation ids (emitted as their own group),
#: `event` (emitted as its own group), and the reserved output keys
#: (`CLOUD_LOGGING_KEYS`, `EXCEPTION_KEYS`).
_FLAT_FIELD_EXCLUDED: frozenset[str] = (
    RESERVED_ATTRS | set(CORRELATION_KEYS) | {"event"} | CLOUD_LOGGING_KEYS | EXCEPTION_KEYS
)

#: Keys whose value is capped from the tail (`_cap_tail`), not the head.
_TAIL_CAPPED_KEYS: frozenset[str] = frozenset({"exc_traceback", "stack_info"})


def utc_iso(created: float) -> str:
    """Render a `LogRecord.created` epoch timestamp as UTC, millisecond precision.

    Format: `YYYY-MM-DDTHH:MM:SS.mmmZ`. `created` is always UTC-aware here
    (epoch seconds are timezone-less by definition); the trailing `Z` marks
    the rendered string as UTC per RFC 3339.
    """

    dt = datetime.fromtimestamp(created, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class JsonFormatter(logging.Formatter):
    """One JSON object per line, in a fixed key order (see module contract).

    Never calls `logging.Formatter.format()` (the stdlib base implementation)
    for the JSON path: that method bakes `record.exc_info` into
    `record.message`/`record.exc_text`, which would leak a traceback into
    the `message` field and double-process it once more here.
    """

    @override
    def format(self, record: logging.LogRecord) -> str:
        try:
            fields = self._build(record)
        except Exception as exc:
            return self._minimal(record, exc, fixed=None)

        try:
            return json.dumps(fields, default=str, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            return self._retry(fields, exc, record)

    def _build(self, record: logging.LogRecord) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "asctime": utc_iso(record.created),
            "levelname": record.levelname,
            "severity": _gcp_severity(record.levelname),
            "name": record.name,
            "funcName": record.funcName,
            "message": _cap_head(record.getMessage()),
        }

        for key in CORRELATION_KEYS:
            if key in record.__dict__:
                fields[key] = record.__dict__[key]

        if "event" in record.__dict__:
            fields["event"] = record.__dict__["event"]

        for key, value in _flat_fields(record):
            fields[_cap_key(key)] = _cap_value(value)

        if record.exc_info and record.exc_info[0] is not None:
            fields["exc_type"] = record.exc_info[0].__name__
            fields["exc_message"] = _cap_head(str(record.exc_info[1]))
            fields["exc_traceback"] = _cap_tail(self.formatException(record.exc_info))
        if getattr(record, "stack_info", None):
            fields["stack_info"] = _cap_tail(str(record.stack_info))

        return fields

    def _retry(self, fields: dict[str, Any], exc: Exception, record: logging.LogRecord) -> str:
        """R3 fallback: stringify (and cap) every non-string value, tag the failure."""

        stringified: dict[str, Any] = {}
        for key, value in fields.items():
            # Fixed keys and strings were already capped by `_build`.
            if key in _FIXED_KEYS or isinstance(value, str):
                stringified[key] = value
                continue
            text = _safe_str(value)
            capped = _cap_tail(text) if key in _TAIL_CAPPED_KEYS else _cap_head(text)
            stringified[key] = capped
        stringified["format_error"] = type(exc).__name__

        try:
            return json.dumps(stringified, default=str, ensure_ascii=False, allow_nan=False)
        except Exception as exc2:
            return self._minimal(record, exc2, fixed=fields)

    def _minimal(
        self,
        record: logging.LogRecord,
        exc: Exception,
        *,
        fixed: dict[str, Any] | None,
    ) -> str:
        """Absolute last resort: only the 6 fixed keys, plus `format_error`."""

        result: dict[str, Any] = {}
        for key in _FIXED_KEYS:
            if fixed is not None and key in fixed:
                result[key] = fixed[key]
            else:
                result[key] = _safe_fixed_attr(record, key)
        result["format_error"] = type(exc).__name__

        try:
            return json.dumps(result, default=str, ensure_ascii=False, allow_nan=False)
        except Exception:
            return "{}"


def _safe_fixed_attr(record: logging.LogRecord, key: str) -> Any:
    """Best-effort read of one fixed key straight off `record`, never raising."""

    try:
        if key == "asctime":
            return utc_iso(record.created)
        if key == "message":
            return _cap_head(record.getMessage())
        if key == "severity":
            return _gcp_severity(getattr(record, "levelname", ""))
        return getattr(record, key, "")
    except Exception:
        return ""


def _gcp_severity(levelname: Any) -> str:
    """GCP `LogSeverity` name for a stdlib level name (`DEFAULT` if unknown)."""

    return levelname if levelname in _GCP_SEVERITIES else "DEFAULT"


def _flat_fields(record: logging.LogRecord) -> Iterator[tuple[str, Any]]:
    """Yield the record's printable structured fields, in insertion order.

    The single source of truth for what `JsonFormatter` prints beyond its
    fixed/correlation/`event` groups. Skips stdlib attributes, correlation
    ids, `event`, private (`_`-prefixed) keys, the reserved Cloud Logging
    and exception keys, and any `K` whose sibling `K_sha256` exists (the K_sha256 rule in
    the module docstring: a summarised payload body never reaches stdout).
    """

    record_dict = record.__dict__
    for key, value in list(record_dict.items()):
        if (
            key in _FLAT_FIELD_EXCLUDED
            or key.startswith(("_", _CLOUD_LOGGING_PREFIX))
            or f"{key}_sha256" in record_dict
        ):
            continue
        yield key, value


def _cap_head(text: str) -> str:
    """Keep the first `JSON_VALUE_CAP` chars of `text`, plus a marker."""

    if len(text) <= JSON_VALUE_CAP:
        return text
    return f"{text[:JSON_VALUE_CAP]}…[truncated {len(text) - JSON_VALUE_CAP} chars]"


def _cap_tail(text: str) -> str:
    """Keep the last `JSON_TRACEBACK_CAP` chars of `text`, behind a marker."""

    if len(text) <= JSON_TRACEBACK_CAP:
        return text
    removed = len(text) - JSON_TRACEBACK_CAP
    return f"[truncated {removed} chars]…{text[-JSON_TRACEBACK_CAP:]}"


def _cap_key(key: str) -> str:
    """Cut a field name longer than `JSON_KEY_CAP` chars, marking the cut."""

    return key if len(key) <= JSON_KEY_CAP else f"{key[:JSON_KEY_CAP]}…"


def _cap_value(value: Any) -> Any:
    """Bound one flat field value to `JSON_VALUE_CAP` serialised chars.

    A string is head-capped; a non-finite float (NaN/inf, invalid JSON)
    becomes its `str()`. Any other value is serialised the same way the line
    itself is; if that text fits, the original structured value is kept
    (still queryable as a nested JSON object), otherwise the capped text
    replaces it. A value that cannot be serialised (a nested NaN, a circular
    reference, a tuple key) becomes its capped `str()`, so one bad field
    never sends the whole line to `format()`'s `_retry` fallback.
    """

    if isinstance(value, str):
        return _cap_head(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (bool, float)):
        return value
    try:
        text = json.dumps(value, default=str, ensure_ascii=False, allow_nan=False)
    except Exception:
        return _cap_head(_safe_str(value))
    if len(text) <= JSON_VALUE_CAP:
        return value
    return _cap_head(text)


def _safe_str(value: Any) -> str:
    """`str(value)`, or a fixed placeholder if its `__str__` raises."""

    try:
        return str(value)
    except Exception:
        return f"<unrepr {type(value).__name__}>"
