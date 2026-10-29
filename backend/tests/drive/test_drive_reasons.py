"""INT-GDUI-0002 reasons and the HTTP mapping."""

from __future__ import annotations

import json

import pytest

from drive.reasons import (
    FileErrorReason,
    Reason,
    google_reason_from_content,
    reason_for_http,
)


@pytest.mark.parametrize(
    ("status", "token", "reason"),
    [
        (404, None, Reason.NO_ACCESS),
        (403, "ACCESS_TOKEN_SCOPE_INSUFFICIENT", Reason.SETUP_ERROR),
        (403, "SERVICE_DISABLED", Reason.SETUP_ERROR),
        (403, "accessNotConfigured", Reason.SETUP_ERROR),
        (403, "rateLimitExceeded", Reason.DRIVE_UNAVAILABLE),
        (403, "userRateLimitExceeded", Reason.DRIVE_UNAVAILABLE),
        (403, "forbidden", Reason.NO_ACCESS),
        (403, None, Reason.NO_ACCESS),
        (429, None, Reason.DRIVE_UNAVAILABLE),
        (500, None, Reason.DRIVE_UNAVAILABLE),
        (503, None, Reason.DRIVE_UNAVAILABLE),
        (400, None, Reason.UNKNOWN_ERROR),
        (401, None, Reason.SETUP_ERROR),
    ],
)
def test_reason_for_http(status, token, reason):
    assert reason_for_http(status, token) is reason


def test_reason_sets_are_the_closed_ones():
    assert {r.value for r in Reason} == {
        "invalid_link",
        "no_access",
        "not_a_folder",
        "already_connected",
        "drive_unavailable",
        "setup_error",
        "too_slow",
        "unknown_error",
    }
    assert "import_failed" in {r.value for r in FileErrorReason}
    assert "import_failed" not in {r.value for r in Reason}


def body(**error):
    return json.dumps({"error": error}).encode()


def test_token_from_details_or_errors():
    assert google_reason_from_content(body(code=403, details=[{"reason": "SERVICE_DISABLED"}])) == "SERVICE_DISABLED"
    assert (
        google_reason_from_content(body(code=403, errors=[{"reason": "rateLimitExceeded", "message": "x"}]))
        == "rateLimitExceeded"
    )


def test_known_token_wins_over_unknown():
    assert (
        google_reason_from_content(
            body(
                errors=[{"reason": "forbidden"}],
                details=[{"reason": "SERVICE_DISABLED"}],
            )
        )
        == "SERVICE_DISABLED"
    )


def test_text_never_becomes_a_token():
    assert google_reason_from_content(body(errors=[{"reason": "File Q4 plan of jane@example.com"}])) is None
    assert google_reason_from_content(b"not json") is None
    assert google_reason_from_content(None) is None
    assert google_reason_from_content(b"[]") is None
