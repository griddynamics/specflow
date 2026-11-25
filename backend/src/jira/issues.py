"""The Jira Cloud REST v3 reads of the importers (an issue with its comments, the issues of an epic), over
``AtlassianReader``. Read-only; only the granted ``read:jira-work`` scope. Errors are the closed ``JiraError`` reasons;
nothing Jira says is kept here."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Final
from urllib.parse import quote

from jira.reader import AtlassianReader

FIELDS: Final = "summary,description,status,issuetype,labels,comment"
COMMENT_PAGE: Final = 100
SEARCH_PAGE: Final = 100
SEARCH_FIELDS: Final = FIELDS.split(",")
LIST_FIELDS: Final = ["summary", "issuetype", "status"]
CHILD_FIELDS: Final = [*SEARCH_FIELDS, "updated"]


def _q(value: str) -> str:
    return quote(value, safe="")


class JiraIssueReader:
    """The reads of one user's Jira."""

    def __init__(self, reader: AtlassianReader) -> None:
        self._reader = reader

    async def issue(self, key: str) -> dict[str, Any]:
        """One issue with the fields of ``FIELDS`` (``jira_not_found`` for a key the user cannot see)."""

        return await self._reader.get(f"/rest/api/3/issue/{_q(key)}", {"fields": FIELDS})

    async def _search(self, jql: str, fields: list[str]) -> AsyncIterator[dict[str, Any]]:
        """The issues of one search, page by page."""

        token: str | None = None
        while True:
            body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": SEARCH_PAGE}
            if token:
                body["nextPageToken"] = token
            page = await self._reader.post("/rest/api/3/search/jql", body)
            for issue in page.get("issues") or []:
                if isinstance(issue, dict):
                    yield issue
            token = page.get("nextPageToken")
            if not token:
                return

    async def epic_issues(
        self, epic_key: str, limit: int, fields: list[str] = SEARCH_FIELDS
    ) -> tuple[list[dict[str, Any]], bool]:
        """The issues whose parent is the epic, in key order, at most ``limit``; and whether there were more. Issues the
        user cannot see are not returned by Jira at all."""

        issues: list[dict[str, Any]] = []
        async for issue in self._search(f"parent = {json.dumps(epic_key)} ORDER BY key", fields):
            if len(issues) >= limit:
                return issues, True
            issues.append(issue)
        return issues, False

    async def epic_children(self, epic_key: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        """The same issues as ``epic_issues`` with only summary, issue type and status (no comments), for the check."""

        return await self.epic_issues(epic_key, limit, LIST_FIELDS)

    async def count_epic_issues(self, epic_key: str) -> int:
        """How many issues the epic has for this user."""

        body = await self._reader.post(
            "/rest/api/3/search/approximate-count", {"jql": f"parent = {json.dumps(epic_key)}"}
        )
        count = body.get("count")
        return count if isinstance(count, int) else 0

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

    async def updated_children(self, parent_key: str, within_minutes: int | None) -> list[dict[str, Any]]:
        """The issues whose parent is ``parent_key`` and that changed in the last ``within_minutes`` (all of them for
        ``None``), with their comments and ``updated``: one search, paged. The window is relative, so it does not depend
        on the time zone of the account."""

        since = "" if within_minutes is None else f" AND updated >= -{max(int(within_minutes), 1)}m"
        jql = f"parent = {json.dumps(parent_key)}{since} ORDER BY key"
        return [issue async for issue in self._search(jql, CHILD_FIELDS)]
