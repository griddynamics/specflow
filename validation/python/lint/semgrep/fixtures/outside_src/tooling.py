"""Fixture: application-layer violation shapes outside `src/` that `**/src/**` rules skip.

Tooling is not an application layer. The still-fires half is
`fixtures/src/repositories/scoped_violations.py`; both are asserted by
`tests/test_semgrep_rules.py::test_application_layer_rules_scoped_to_src_via_real_scan`."""

from __future__ import annotations

import os

from fastapi import HTTPException
from fastapi.responses import JSONResponse


def raise_bare_value_error() -> None:
    # ok: kit.errors.no-builtin-raise
    raise ValueError("tooling-only code, not an application layer")


def raise_http_exception_by_hand() -> None:
    # ok: kit.errors.http-exception-outside-handlers
    raise HTTPException(status_code=500, detail="tool error")


def build_error_envelope_by_hand() -> object:
    # ok: kit.errors.envelope-outside-handlers
    return JSONResponse(status_code=500, content={"error": {"code": "TOOL_ERROR"}})


def read_feature_flag() -> str | None:
    # ok: kit.security.environment-read-outside-settings
    return os.environ.get("TOOL_FEATURE_FLAG")


def log_with_password(logger: object, password: str) -> None:
    # ok: kit.logging.no-secret-kwarg-in-log-call
    logger.info("tool event", password=password)
