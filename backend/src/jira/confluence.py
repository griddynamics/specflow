"""The Confluence Cloud REST v2 reads of the importer (pages, descendants, spaces), over ``AtlassianReader``.

Only the scopes the site admin granted (``read:page``, ``read:hierarchical-content``, ``read:space``, all
``:confluence``). Errors are the closed ``JiraError`` reasons of ``jira.reader``; nothing Confluence says is kept.
"""

from __future__ import annotations

from typing import Any, Final
from urllib.parse import quote

from jira.gateway import JiraError
from jira.reader import AtlassianReader
from jira.reasons import Reason

API: Final = "/wiki/api/v2"
PAGE_LIMIT: Final = 250  # the v2 maximum of one page of results


def _q(value: str) -> str:
    return quote(value, safe="")


def _relative(body: dict[str, Any]) -> str | None:
    """The ``_links.next`` of a result page when it is a path on this site (one ``/``, no backslash); anything else
    ends the paging, so the bearer token is never sent to another host."""

    link = (body.get("_links") or {}).get("next")
    if isinstance(link, str) and link.startswith("/") and not link.startswith("//") and "\\" not in link:
        return link
    return None


class ConfluenceClient:
    """The reads of one user's Confluence."""

    def __init__(self, reader: AtlassianReader) -> None:
        self._reader = reader

    async def page(self, page_id: str) -> dict[str, Any]:
        """One page with its storage-format body (``body.storage.value``)."""

        return await self._reader.get(f"{API}/pages/{_q(page_id)}", {"body-format": "storage"})

    async def descendants(self, page_id: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        """The pages below a page, top to bottom (folders and other content are left out), at most ``limit``; and
        whether there were more."""

        return await self._paged(f"{API}/pages/{_q(page_id)}/descendants", {"limit": str(PAGE_LIMIT)}, limit, "page")

    async def space(self, key: str) -> dict[str, Any]:
        """The space with this key (``id``, ``key``, ``name``); ``jira_not_found`` when there is none."""

        found = (await self._reader.get(f"{API}/spaces", {"keys": key, "limit": "1"})).get("results") or []
        if not found or not isinstance(found[0], dict):
            raise JiraError(Reason.NOT_FOUND)
        return found[0]

    async def space_pages(self, space_id: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        """The current pages of a space (id, title), at most ``limit``; and whether there were more."""

        params = {"status": "current", "limit": str(PAGE_LIMIT)}
        return await self._paged(f"{API}/spaces/{_q(space_id)}/pages", params, limit, None)

    async def find_page(self, space_id: str, title: str) -> str | None:
        """The id of the current page called ``title`` in a space, or ``None``."""

        params = {"space-id": space_id, "title": title, "status": "current", "limit": "1"}
        found = (await self._reader.get(f"{API}/pages", params)).get("results") or []
        return str(found[0]["id"]) if found and isinstance(found[0], dict) and found[0].get("id") else None

    async def _paged(
        self, path: str, params: dict[str, str], limit: int, kind: str | None
    ) -> tuple[list[dict[str, Any]], bool]:
        items: list[dict[str, Any]] = []
        next_path: str | None = path
        query: dict[str, str] | None = params
        while next_path is not None:
            body = await self._reader.get(next_path, query)
            for item in body.get("results") or []:
                if not isinstance(item, dict) or (kind is not None and item.get("type") != kind):
                    continue
                if len(items) >= limit:
                    return items, True
                items.append(item)
            next_path, query = _relative(body), None
        return items, False
