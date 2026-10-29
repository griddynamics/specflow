"""Fixtures for semgrep/logging.yml: positive and negative
cases per rule, one section per rule id."""

import structlog

logger = structlog.get_logger(__name__)


# --- kit.logging.no-secret-kwarg-in-log-call ---


def log_login_attempt(password: str) -> None:
    # ruleid: kit.logging.no-secret-kwarg-in-log-call
    logger.info("login attempt", password=password)


def log_request(token: str) -> None:
    # ruleid: kit.logging.no-secret-kwarg-in-log-call
    logger.bind(token=token)


def log_login_outcome(outcome: str) -> None:
    # ok: kit.logging.no-secret-kwarg-in-log-call
    logger.info("login attempt", outcome=outcome)


# Semgrep resolves a `from ... import`ed symbol to a dotted name, which fits the
# `$LOGGER.$METHOD(...)` shape though this is a testcontainer constructor; the
# `$METHOD` allow-list excludes it.
from testcontainers.postgres import PostgresContainer


def build_test_container(password: str) -> object:
    # ok: kit.logging.no-secret-kwarg-in-log-call
    return PostgresContainer(password=password).with_name("test-container")


# --- kit.logging.exception-text-exposed ---


def handle_upstream_error(exc: Exception) -> dict[str, str]:
    # ruleid: kit.logging.exception-text-exposed
    return {"error": {"code": "INTERNAL", "message": str(exc)}}


def handle_upstream_error_safely(exc: Exception, logger_: object) -> dict[str, str]:
    # ok: kit.logging.exception-text-exposed
    logger_.error("internal error", error_type=type(exc).__name__)
    return {"error": {"code": "INTERNAL", "message": "An unexpected error occurred."}}


def describe_reason(reason_code: object) -> str:
    # not an exception-named variable
    # ok: kit.logging.exception-text-exposed
    return str(reason_code)
