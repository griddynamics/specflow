"""Read-only Atlassian REST over httpx for the importers: one user's bearer token (refreshed once on a ``401``), the
retries of ``jira.http.send_with_retries`` and closed ``JiraError`` reasons, for Confluence Cloud (``/ex/confluence``)
and Jira Cloud (``/ex/jira``) alike. The token never reaches a log, an error or a ``repr``; Atlassian's text is
returned to the caller and never kept here."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Final, override

import httpx

from jira.gateway import JiraError
from jira.http import BearerAuth, Sleep, TokenSource, send_with_retries
from jira.reasons import Reason

TIMEOUT_SECONDS: Final = 30
CONFLUENCE_API_URL: Final = "https://api.atlassian.com/ex/confluence/{cloud_id}"
JIRA_API_URL: Final = "https://api.atlassian.com/ex/jira/{cloud_id}"


def confluence_url(cloud_id: str) -> str:
    """The Confluence REST base of one cloud through the Atlassian API gateway."""

    return CONFLUENCE_API_URL.format(cloud_id=cloud_id)


def jira_url(cloud_id: str) -> str:
    return JIRA_API_URL.format(cloud_id=cloud_id)


class AtlassianReader:
    """GET and POST of JSON objects against one base URL, as one user."""

    def __init__(
        self,
        base_url: str,
        tokens: TokenSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=BearerAuth(tokens),
            headers={"Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )
        self._sleep = sleep

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={str(self._client.base_url)!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        """The JSON object at ``path``; ``path`` may be a ``_links.next`` value, which keeps its own query."""

        return _object(await send_with_retries(self._client, self._sleep, "GET", path, params=params))

    async def post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """A read-only POST (a Jira search or count): safe to repeat."""

        return _object(await send_with_retries(self._client, self._sleep, "POST", path, body=body))


def _object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise JiraError(Reason.UNEXPECTED, http_status=response.status_code)
    return body
