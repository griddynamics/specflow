"""The closed codes of the Notion integration (docs/notion/codes.md). ``str(error)`` is the code: no Notion text, token,
code, ``state``, page title or workspace name is ever inside an error. A code is an API error code (``api.notion``), a
run code of the plan destination, or both."""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus
from typing import Final


class NotionCode(StrEnum):
    UNAVAILABLE = "notion_unavailable"  # the integration is off on this server (API only)
    NOT_CONNECTED = "notion_not_connected"
    RECONNECT_REQUIRED = "notion_reconnect_required"
    STATE_INVALID = "notion_state_invalid"
    CODE_INVALID = "notion_code_invalid"
    UNREACHABLE = "notion_unreachable"  # Notion is down, the network failed or the retries ran out
    AUTH_FAILED = "notion_auth_failed"  # Notion refused the server's own credentials or a fresh token
    LINK_INVALID = "notion_link_invalid"  # the text is not a link to a Notion page or database
    NOT_SHARED = "notion_not_shared"  # no such page or database, or the member did not share it with the connection
    NOT_A_PAGE = "notion_parent_not_a_page"  # the plan destination's parent is a database, not a page
    PERMISSIONS_MISSING = "notion_permissions_missing"  # the connection lacks a capability (insert/update content)
    RATE_LIMITED = "notion_rate_limited"  # the pass waited its whole budget (60 s) for the request limit
    PAGE_GONE = "notion_page_gone"  # a person deleted or archived a row the export made
    DATABASE_GONE = "notion_database_gone"  # a person deleted or archived the plan database
    ITEM_INVALID = "notion_item_invalid"  # Notion refused the content of one row (400)
    # Inside only: a dead refresh token becomes RECONNECT_REQUIRED; any other refusal of the token endpoint becomes
    # CODE_INVALID (sign-in) or AUTH_FAILED (refresh).
    GRANT_INVALID = "notion_grant_invalid"
    REFUSED = "notion_refused"


class NotionError(Exception):
    """A Notion failure as one closed code; ``str(e)`` is the code."""

    def __init__(self, code: NotionCode) -> None:
        super().__init__(code.value)
        self.code = code


_BY_STATUS: Final[dict[int, NotionCode]] = {
    HTTPStatus.UNAUTHORIZED: NotionCode.AUTH_FAILED,
    HTTPStatus.FORBIDDEN: NotionCode.PERMISSIONS_MISSING,
    HTTPStatus.NOT_FOUND: NotionCode.NOT_SHARED,
    HTTPStatus.TOO_MANY_REQUESTS: NotionCode.RATE_LIMITED,
}


def code_of_status(status: int, error_code: str | None = None) -> NotionCode:
    """The code of a refused request from its HTTP status and Notion's ``code`` (stream A refines the 4xx rows)."""

    if status == HTTPStatus.BAD_REQUEST and error_code == "validation_error":
        return NotionCode.ITEM_INVALID
    return _BY_STATUS.get(status, NotionCode.UNREACHABLE)
