"""Closed Jira reason codes and the HTTP mapping (api.md#sync-codes)."""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus
from typing import Final


class Reason(StrEnum):
    """The Jira codes of a run, an item or an access check."""

    AUTH_FAILED = "jira_auth_failed"
    FORBIDDEN = "jira_forbidden"
    NOT_FOUND = "jira_not_found"
    INVALID_FIELD = "jira_invalid_field"
    UNREACHABLE = "jira_unreachable"
    UNEXPECTED = "jira_unexpected"
    ISSUE_TYPES_MISSING = "jira_issue_types_missing"
    EPIC_INVALID = "jira_epic_invalid"
    NOT_CONNECTED = "jira_not_connected"  # oauth: the acting user has no Atlassian connection
    RECONNECT_REQUIRED = "jira_reconnect_required"  # oauth: the acting connection expired; its owner reconnects


_SERVER_ERRORS: Final = range(HTTPStatus.INTERNAL_SERVER_ERROR, 600)
_BY_STATUS: Final[dict[int, Reason]] = {
    HTTPStatus.BAD_REQUEST: Reason.INVALID_FIELD,
    HTTPStatus.UNAUTHORIZED: Reason.AUTH_FAILED,
    HTTPStatus.FORBIDDEN: Reason.FORBIDDEN,
    HTTPStatus.NOT_FOUND: Reason.NOT_FOUND,
    HTTPStatus.TOO_MANY_REQUESTS: Reason.UNREACHABLE,
}


def reason_for_http(status: int) -> Reason:
    """Map a Jira answer to one reason (retries are already spent)."""

    if status in _SERVER_ERRORS:
        return Reason.UNREACHABLE
    return _BY_STATUS.get(status, Reason.UNEXPECTED)
