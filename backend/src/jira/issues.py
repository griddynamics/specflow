"""The Jira Cloud REST v3 reads of the importers (an issue with its comments, the issues of an epic), over
``AtlassianReader``. Read-only; only the granted ``read:jira-work`` scope. Errors are the closed ``JiraError`` reasons;
nothing Jira says is kept here."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import quote

from jira.reader import AtlassianReader

FIELDS: Final = "summary,description,status,issuetype,labels,comment"
COMMENT_PAGE: Final = 100
SEARCH_PAGE: Final = 100
SEARCH_FIELDS: Final = FIELDS.split(",")


def _q(value: str) -> str:
    return quote(value, safe="")


class JiraIssueReader:
    """The reads of one user's Jira."""

    def __init__(self, reader: AtlassianReader) -> None:
        self._reader = reader

    async def issue(self, key: str) -> dict[str, Any]:
        """One issue with the fields of ``FIELDS`` (``jira_not_found`` for a key the user cannot see)."""

        return await self._reader.get(f"/rest/api/3/issue/{_q(key)}", {"fields": FIELDS})

    async def epic_issues(self, epic_key: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        """The issues whose parent is the epic, in key order, at most ``limit``; and whether there were more. Issues the
        user cannot see are not returned by Jira at all."""

        issues: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            body: dict[str, Any] = {
                "jql": f"parent = {json.dumps(epic_key)} ORDER BY key",
                "fields": SEARCH_FIELDS,
                "maxResults": SEARCH_PAGE,
            }
            if token:
                body["nextPageToken"] = token
            page = await self._reader.post("/rest/api/3/search/jql", body)
            for issue in page.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                if len(issues) >= limit:
                    return issues, True
                issues.append(issue)
            token = page.get("nextPageToken")
            if not token:
                return issues, False

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
