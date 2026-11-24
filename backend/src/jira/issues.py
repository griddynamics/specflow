"""The Jira Cloud REST v3 reads of the importers (an issue with its comments), over
``AtlassianReader``. Read-only; only the granted ``read:jira-work`` scope. Errors are the closed ``JiraError`` reasons;
nothing Jira says is kept here."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import quote

from jira.reader import AtlassianReader

FIELDS: Final = "summary,description,status,issuetype,labels,comment"
COMMENT_PAGE: Final = 100


def _q(value: str) -> str:
    return quote(value, safe="")


class JiraIssueReader:
    """The reads of one user's Jira."""

    def __init__(self, reader: AtlassianReader) -> None:
        self._reader = reader

    async def issue(self, key: str) -> dict[str, Any]:
        """One issue with the fields of ``FIELDS`` (``jira_not_found`` for a key the user cannot see)."""

        return await self._reader.get(f"/rest/api/3/issue/{_q(key)}", {"fields": FIELDS})

    async def comments(self, issue: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Every comment of an issue read with ``FIELDS``: the ones it came with, then the rest page by page."""

        field = (issue.get("fields") or {}).get("comment") or {}
        found = [comment for comment in field.get("comments") or [] if isinstance(comment, dict)]
        total = field.get("total")
        key = str(issue.get("key", ""))
        while isinstance(total, int) and len(found) < total and key:
            page = await self._reader.get(
                f"/rest/api/3/issue/{_q(key)}/comment", {"startAt": str(len(found)), "maxResults": str(COMMENT_PAGE)}
            )
            more = [comment for comment in page.get("comments") or [] if isinstance(comment, dict)]
            if not more:
                break
            found += more
        return found
