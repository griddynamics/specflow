"""Fixture: application-layer rules still fire on `src/**` code under `**/src/**` scoping.

The positive half of `fixtures/outside_src/tooling.py`."""

from __future__ import annotations

import os


def raise_bare_value_error() -> None:
    # ruleid: kit.errors.no-builtin-raise
    raise ValueError("a real application-layer violation")


def read_feature_flag() -> str | None:
    # ruleid: kit.security.environment-read-outside-settings
    return os.environ.get("FEATURE_FLAG")


def log_with_password(logger: object, password: str) -> None:
    # ruleid: kit.logging.no-secret-kwarg-in-log-call
    logger.info("event", password=password)
