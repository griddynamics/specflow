"""Runtime configuration for the central logger.

Read once from the process environment (or an explicit mapping, mainly for
tests) into a frozen `LogConfig`. The variable table and defaults
below are the runtime configuration and bootstrap rules of the logger.

Only `LOGS_LEVEL`, `LOGS_STDOUT_LEVEL` and `LOGS_RAISE_EXCEPTIONS` are
read; any other `LOGS_*` variable (including the removed directory,
retention and full-payload ones) is never read and silently ignored. Every
value is optional. An unset or empty variable falls back to its default
silently. An invalid value (unknown level name, or a flag that is not a
boolean word) also falls back to its default, but additionally emits
exactly one `logger._stderr.warn_once` self-diagnostic per variable
(crash_safety.md: config errors never raise, never flood).

`LOGS_RAISE_EXCEPTIONS` is the development/deployment switch for
`logging.raiseExceptions` (U4): on, a failing handler or a malformed record
dumps loudly to stderr so the defect is seen while developing; off (the
default, as deployed), the failure is silent and rate-limited. It is not a
`LogConfig` field: the config snapshot holds the two levels only (LOG-03).
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Mapping

from logger._stderr import warn_once

_LEVEL_NAMES: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_DEFAULT_LEVEL = logging.INFO
_DEFAULT_STDOUT_LEVEL = logging.INFO

RAISE_EXCEPTIONS_VARIABLE = "LOGS_RAISE_EXCEPTIONS"
_TRUE_WORDS = frozenset({"1", "TRUE", "YES", "ON"})
_FALSE_WORDS = frozenset({"0", "FALSE", "NO", "OFF"})
_DEFAULT_RAISE_EXCEPTIONS = False


@dataclasses.dataclass(frozen=True, slots=True)
class LogConfig:
    """Frozen snapshot of the `LOGS_*` environment variables."""

    level: int = _DEFAULT_LEVEL
    stdout_level: int = _DEFAULT_STDOUT_LEVEL

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> LogConfig:
        """Build a `LogConfig` from `environ` (default: `os.environ`)."""

        source = os.environ if environ is None else environ

        return cls(
            level=_parse_level(source.get("LOGS_LEVEL"), "LOGS_LEVEL"),
            stdout_level=_parse_level(source.get("LOGS_STDOUT_LEVEL"), "LOGS_STDOUT_LEVEL"),
        )


def raise_exceptions_from_env(environ: Mapping[str, str] | None = None) -> bool:
    """Read `LOGS_RAISE_EXCEPTIONS` from `environ` (default: `os.environ`).

    Accepts 1/true/yes/on and 0/false/no/off, case-insensitive. Unset or
    empty is off; any other value is off plus one `warn_once` line.
    """

    source = os.environ if environ is None else environ
    raw = source.get(RAISE_EXCEPTIONS_VARIABLE)
    if not raw:
        return _DEFAULT_RAISE_EXCEPTIONS
    word = raw.strip().upper()
    if word in _TRUE_WORDS:
        return True
    if word not in _FALSE_WORDS:
        warn_once(
            f"config:{RAISE_EXCEPTIONS_VARIABLE}",
            f"invalid {RAISE_EXCEPTIONS_VARIABLE}={raw!r}; expected one of "
            f"{sorted(_TRUE_WORDS | _FALSE_WORDS)} (case-insensitive); "
            f"using default {_DEFAULT_RAISE_EXCEPTIONS}",
        )
    return _DEFAULT_RAISE_EXCEPTIONS


def _parse_level(raw: str | None, var_name: str) -> int:
    if not raw:
        return _DEFAULT_LEVEL
    level = _LEVEL_NAMES.get(raw.strip().upper())
    if level is None:
        warn_once(
            f"config:{var_name}",
            f"invalid {var_name}={raw!r}; expected one of "
            f"{sorted(_LEVEL_NAMES)} (case-insensitive); "
            f"using default {logging.getLevelName(_DEFAULT_LEVEL)}",
        )
        return _DEFAULT_LEVEL
    return level
