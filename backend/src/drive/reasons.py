"""Closed reason sets and the Google HTTP mapping (INT-GDUI-0002)."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from http import HTTPStatus
from typing import Final


class Reason(StrEnum):
    """Why a test, a save or a whole import failed (INT-GDUI-0002)."""

    INVALID_LINK = "invalid_link"
    NO_ACCESS = "no_access"
    NOT_A_FOLDER = "not_a_folder"
    ALREADY_CONNECTED = "already_connected"
    DRIVE_UNAVAILABLE = "drive_unavailable"
    SETUP_ERROR = "setup_error"
    TOO_SLOW = "too_slow"
    UNKNOWN_ERROR = "unknown_error"


class FileErrorReason(StrEnum):
    """Why one file ended in error. ``import_failed`` is file-level only."""

    NO_ACCESS = "no_access"
    DRIVE_UNAVAILABLE = "drive_unavailable"
    CONVERT_FAILED = "convert_failed"
    PARSE_FAILED = "parse_failed"
    STORE_FAILED = "store_failed"
    IMPORT_FAILED = "import_failed"
    UNKNOWN_ERROR = "unknown_error"


SETUP_TOKENS: Final = frozenset({"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "SERVICE_DISABLED", "accessNotConfigured"})
RATE_TOKENS: Final = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
_SERVER_ERRORS: Final = range(HTTPStatus.INTERNAL_SERVER_ERROR, 600)
_TOKEN: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _forbidden_reason(google_reason: str | None) -> Reason:
    if google_reason in SETUP_TOKENS:
        return Reason.SETUP_ERROR
    if google_reason in RATE_TOKENS:
        return Reason.DRIVE_UNAVAILABLE
    return Reason.NO_ACCESS


def reason_for_http(status: int, google_reason: str | None) -> Reason:
    """Map a Google API answer to one reason (retries are already spent)."""

    if status == HTTPStatus.NOT_FOUND:
        return Reason.NO_ACCESS
    if status == HTTPStatus.UNAUTHORIZED:
        return Reason.SETUP_ERROR
    if status == HTTPStatus.FORBIDDEN:
        return _forbidden_reason(google_reason)
    if status == HTTPStatus.TOO_MANY_REQUESTS or status in _SERVER_ERRORS:
        return Reason.DRIVE_UNAVAILABLE
    return Reason.UNKNOWN_ERROR


def google_reason_from_content(content: bytes | str | None) -> str | None:
    """The machine token of a Google error body, never any of its text.

    Only a short identifier-like token survives; messages, names and emails
    inside the body are dropped here and nowhere else.
    """

    if not content:
        return None
    try:
        body = json.loads(content)
    except (ValueError, TypeError):
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    tokens: list[str] = []
    for key in ("details", "errors"):
        entries = error.get(key)
        if isinstance(entries, list):
            tokens.extend(
                entry["reason"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("reason"), str)
            )
    known = SETUP_TOKENS | RATE_TOKENS
    for token in tokens:
        if token in known:
            return token
    for token in tokens:
        if _TOKEN.fullmatch(token):
            return token
    return None
