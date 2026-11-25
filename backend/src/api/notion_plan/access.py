"""The access check of the Notion plan form (docs/notion/plan.md "Access check"): what the member that will export can
reach before a destination is saved. Results are closed reasons, never Notion text. Stream E1 fills ``resolve`` and
``check_access``; the reasons are ``NotionPlanAccessReason`` of ``api.schemas``."""

from __future__ import annotations

from dataclasses import dataclass

from api.schemas import NotionPlanAccessFailed, NotionPlanAccessOk, NotionPlanAccessReason
from notion.gateway import NotionGateway
from notion.reasons import NotionError


@dataclass(frozen=True)
class ResolvedParent:
    """The page a link names: its id (with dashes), title and web address."""

    page_id: str
    title: str
    url: str


class AccessRefusedError(Exception):
    """The destination cannot be used; ``reason`` is the closed code."""

    def __init__(self, reason: NotionPlanAccessReason) -> None:
        super().__init__(reason)
        self.reason: NotionPlanAccessReason = reason


def reason_of(error: NotionError) -> NotionPlanAccessReason:
    """The access reason of a Notion failure (a code without its own reason is ``notion_auth_failed``)."""

    raise NotImplementedError("E1 fills reason_of")


async def resolve(gateway: NotionGateway, link: str) -> ResolvedParent:
    """Parse ``link`` (``notion.links.parse_link``), read the page as the member and prove it is a page (not a database)
    the connection may write under. ``AccessRefusedError`` with the reason otherwise."""

    raise NotImplementedError("E1 fills resolve")


async def check_access(gateway: NotionGateway, link: str) -> NotionPlanAccessOk | NotionPlanAccessFailed:
    raise NotImplementedError("E1 fills check_access")
