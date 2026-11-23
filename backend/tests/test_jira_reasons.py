"""Reason set and HTTP mapping of the Jira gateway."""

from __future__ import annotations

import pytest

from jira.gateway import JiraError
from jira.reasons import Reason, reason_for_http

CODES = {
    "jira_auth_failed",
    "jira_forbidden",
    "jira_not_found",
    "jira_invalid_field",
    "jira_unreachable",
    "jira_unexpected",
    "jira_issue_types_missing",
    "jira_epic_invalid",
    "jira_not_connected",
    "jira_reconnect_required",
}


def test_reason_values_are_the_api_codes():
    assert {reason.value for reason in Reason} == CODES


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (400, Reason.INVALID_FIELD),
        (401, Reason.AUTH_FAILED),
        (403, Reason.FORBIDDEN),
        (404, Reason.NOT_FOUND),
        (429, Reason.UNREACHABLE),
        (500, Reason.UNREACHABLE),
        (503, Reason.UNREACHABLE),
        (599, Reason.UNREACHABLE),
        (302, Reason.UNEXPECTED),
        (409, Reason.UNEXPECTED),
        (422, Reason.UNEXPECTED),
    ],
)
def test_reason_for_http(status, reason):
    assert reason_for_http(status) is reason


def test_error_text_is_the_reason_only():
    error = JiraError(Reason.FORBIDDEN, http_status=403)
    assert str(error) == "jira_forbidden"
    assert error.reason is Reason.FORBIDDEN
    assert error.http_status == 403
