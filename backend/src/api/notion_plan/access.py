"""The access check of the Notion plan form (docs/notion/plan.md "Access check"): what the member that will export can
reach before a destination is saved. Results are closed reasons, never Notion text. Stream E1 fills ``resolve`` and
``check_access``; the reasons are ``NotionPlanAccessReason`` of ``api.schemas``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from api.schemas import NotionPlanAccessFailed, NotionPlanAccessOk, NotionPlanAccessReason
from notion.gateway import NotionGateway
from notion.links import parse_link
from notion.reasons import NotionCode, NotionError


@dataclass(frozen=True)
class ResolvedParent:
    """The page a link names: its id (with dashes), title and web address."""

    page_id: str
    title: str
    url: str


_REASONS: Final[dict[NotionCode, NotionPlanAccessReason]] = {
    NotionCode.NOT_CONNECTED: "notion_not_connected",
    NotionCode.RECONNECT_REQUIRED: "notion_reconnect_required",
    NotionCode.UNREACHABLE: "notion_unreachable",
    NotionCode.LINK_INVALID: "notion_link_invalid",
    NotionCode.NOT_SHARED: "notion_not_shared",
    NotionCode.PAGE_GONE: "notion_not_shared",  # on a route a gone page answers as a page that is not shared
    NotionCode.DATABASE_GONE: "notion_not_shared",
    NotionCode.NOT_A_PAGE: "notion_parent_not_a_page",
    NotionCode.PERMISSIONS_MISSING: "notion_permissions_missing",
}


class AccessRefusedError(Exception):
    """The destination cannot be used; ``reason`` is the closed code."""

    def __init__(self, reason: NotionPlanAccessReason) -> None:
        super().__init__(reason)
        self.reason: NotionPlanAccessReason = reason


def reason_of(error: NotionError) -> NotionPlanAccessReason:
    """The access reason of a Notion failure (a code without its own reason is ``notion_auth_failed``)."""

    return _REASONS.get(error.code, "notion_auth_failed")


async def resolve(gateway: NotionGateway, link: str) -> ResolvedParent:
    """Parse ``link`` (``notion.links.parse_link``), read the page as the member and prove it is a page (not a database)
    the connection may write under. ``AccessRefusedError`` with the reason otherwise."""

    try:
        found = await gateway.page(parse_link(link).id)
    except NotionError as error:
        raise AccessRefusedError(reason_of(error)) from None
    if found.in_trash:
        raise AccessRefusedError("notion_not_shared")  # an archived page cannot take a database
    return ResolvedParent(found.id, found.title, found.url)


async def check_access(gateway: NotionGateway, link: str) -> NotionPlanAccessOk | NotionPlanAccessFailed:
    try:
        parent = await resolve(gateway, link)
    except AccessRefusedError as refused:
        return NotionPlanAccessFailed(reason=refused.reason)
    return NotionPlanAccessOk(parent_page_title=parent.title)
