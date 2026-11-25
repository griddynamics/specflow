"""Shared parts of the Notion routes (docs/notion/codes.md): the service of the runtime and the API errors of the Notion
codes. Messages are fixed text; no Notion text, token, code, ``state``, page title or workspace name reaches an
error."""

from __future__ import annotations

from typing import Final

from api.errors import ApiError
from api.runtime import Runtime
from notion.connections import NotionConnections
from notion.reasons import NotionCode, NotionError

_ERRORS: Final[dict[NotionCode, tuple[int, str]]] = {
    NotionCode.NOT_CONNECTED: (409, "Connect your Notion account first."),
    NotionCode.RECONNECT_REQUIRED: (409, "Your Notion connection expired. Connect Notion again."),
    NotionCode.STATE_INVALID: (400, "The sign-in to Notion expired or belongs to another session. Try again."),
    NotionCode.CODE_INVALID: (400, "Notion did not accept the sign-in. Try again."),
    NotionCode.UNREACHABLE: (502, "Notion could not be reached. Try again later."),
    NotionCode.AUTH_FAILED: (502, "Notion refused the request of this server."),
    NotionCode.LINK_INVALID: (422, "The link is not a Notion page or database link."),
    NotionCode.NOT_SHARED: (
        409,
        "The page or database does not exist, or you did not share it with Graphico when you connected Notion.",
    ),
    NotionCode.RATE_LIMITED: (503, "Notion is busy right now. Try again in a minute."),
    NotionCode.NOT_A_PAGE: (422, "The link is a database. Paste the link of a page."),
    NotionCode.PERMISSIONS_MISSING: (
        409,
        "The Notion connection cannot insert or update content. Connect Notion again and allow it.",
    ),
}


# Codes of a pass or an item that a route can still meet: they answer with the closest code of their own.
_ALIAS: Final[dict[NotionCode, NotionCode]] = {
    NotionCode.PAGE_GONE: NotionCode.NOT_SHARED,
    NotionCode.DATABASE_GONE: NotionCode.NOT_SHARED,
    NotionCode.ITEM_INVALID: NotionCode.LINK_INVALID,
}


def notion_unavailable() -> ApiError:
    return ApiError(503, "notion_unavailable", "The Notion integration is not configured on this server.")


def notion_error(error: NotionError) -> ApiError:
    """The API error of a Notion failure; a code without its own answer is ``notion_auth_failed``."""

    code = _ALIAS.get(error.code, error.code)
    code = code if code in _ERRORS else NotionCode.AUTH_FAILED
    status, message = _ERRORS[code]
    return ApiError(status, code.value, message)


def notion_connections(runtime: Runtime) -> NotionConnections:
    """The runtime's Notion connections; ``503 notion_unavailable`` when the integration is off."""

    connections: NotionConnections | None = runtime.notion
    if connections is None:
        raise notion_unavailable()
    return connections
