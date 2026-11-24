"""Jira Cloud over httpx: basic auth (``token`` mode) or a user's OAuth bearer token (``oauth`` mode), closed errors,
retries of sync.md#failure-handling.

The token stays a ``SecretStr`` until the client's auth is built; no log line, error or ``repr`` holds it, a URL
query, a body or a header. Jira's text is never kept.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final, Protocol, Self, override
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from jira.gateway import IssueState, IssueSummary, JiraError, JiraProject
from jira.oauth import api_url
from jira.reasons import Reason, reason_for_http
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("jira.http")

TIMEOUT_SECONDS: Final = 30
RATE_TRIES: Final = 5  # waits; a sixth 429 ends the call
RATE_WAIT_MAX: Final = 60.0
SERVER_TRIES: Final = 3  # retries after the first request, backing off 1, 2, 4 s
PAGE_SIZE: Final = 100
RANK_LIMIT: Final = 50
PERMISSIONS: Final = (
    "BROWSE_PROJECTS",
    "CREATE_ISSUES",
    "EDIT_ISSUES",
    "ADD_COMMENTS",
    "LINK_ISSUES",
    "SCHEDULE_ISSUES",
)
STORY_POINTS: Final = "Story Points"
PROPERTY: Final = "specflow"
_UNSET: Final = object()

Sleep = Callable[[float], Awaitable[None]]


def _object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise JiraError(Reason.UNEXPECTED, http_status=response.status_code)
    return body


def retry_after(response: httpx.Response) -> float:
    """Seconds to wait: ``Retry-After`` is a number of seconds or an HTTP date; anything else waits one second."""

    value = response.headers.get("Retry-After", "1")
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 1.0
    return max((when.replace(tzinfo=when.tzinfo or UTC) - datetime.now(UTC)).total_seconds(), 0.0)


def _q(value: str) -> str:
    return quote(value, safe="")


def _jql_keys(keys: Sequence[str]) -> str:
    return "key in (" + ",".join(json.dumps(key) for key in keys) + ")"


def _state(issue: Mapping[str, Any]) -> IssueState:
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    blocked_by = {
        link["inwardIssue"]["key"]: str(link["id"])
        for link in fields.get("issuelinks") or []
        if (link.get("type") or {}).get("name") == "Blocks" and link.get("inwardIssue")
    }
    return IssueState(
        category=(status.get("statusCategory") or {}).get("key", ""),
        status=status.get("name", ""),
        blocked_by=blocked_by,
    )


async def send_with_retries(
    client: httpx.AsyncClient,
    sleep: Sleep,
    method: str,
    path: str,
    *,
    body: Mapping[str, Any] | None = None,
    params: Mapping[str, str] | None = None,
    repeatable: bool = True,
) -> httpx.Response:
    """One call with the retries of sync.md#failure-handling. A call that is not ``repeatable`` (a create, a
    comment, a link) is never sent again after a timeout or a 5xx, because Jira may have done it: the next pass
    recovers it. A 429 is always retried, as Jira did not process that request."""

    rate_tries = server_tries = 0
    while True:
        try:
            response = await client.request(method, path, json=body, params=params)
        except httpx.HTTPError:
            response = None
        status = response.status_code if response is not None else 0
        if response is not None and response.is_success:
            return response
        if status == httpx.codes.TOO_MANY_REQUESTS and response is not None:
            rate_tries += 1
            wait = retry_after(response)
            if rate_tries > RATE_TRIES or wait > RATE_WAIT_MAX:
                raise JiraError(Reason.UNREACHABLE, http_status=status)
            log_event(logger, WARNING, "jira.http.retry", method=method, status=status, attempt=rate_tries)
            await sleep(wait)
        elif status == 0 or status >= httpx.codes.INTERNAL_SERVER_ERROR:
            server_tries += 1
            if server_tries > SERVER_TRIES or not repeatable:
                raise JiraError(Reason.UNREACHABLE, http_status=status or None)
            log_event(logger, WARNING, "jira.http.retry", method=method, status=status, attempt=server_tries)
            await sleep(2 ** (server_tries - 1))
        else:
            raise JiraError(reason_for_http(status), http_status=status)


class TokenSource(Protocol):
    """The access tokens of one user's Atlassian connection (``oauth`` mode)."""

    async def access_token(self) -> str:
        """A valid access token (refreshed when the cached one is about to expire)."""

    async def refreshed(self, used: str) -> str:
        """A new access token after Jira refused ``used`` (one refresh, shared by concurrent callers)."""


class BearerAuth(httpx.Auth):
    """``Authorization: Bearer`` from a ``TokenSource``; a ``401`` refreshes once and sends the request once more, so a
    second ``401`` reaches the gateway (``jira_auth_failed``)."""

    def __init__(self, tokens: TokenSource) -> None:
        self._tokens = tokens

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    @override
    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        raise RuntimeError("BearerAuth works with an async client only")

    @override
    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = await self._tokens.access_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == httpx.codes.UNAUTHORIZED:
            request.headers["Authorization"] = f"Bearer {await self._tokens.refreshed(token)}"
            yield request


class HttpJiraGateway:
    """The ``JiraGateway`` over Jira Cloud REST v3 (and the agile rank endpoint)."""

    def __init__(
        self,
        site_url: str,
        email: str | None = None,
        token: SecretStr | None = None,
        *,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if httpx.URL(site_url).scheme != "https":
            raise ValueError("the Jira site URL must use https")
        if auth is None:
            if email is None or token is None:
                raise ValueError("the Jira gateway needs an email and a token, or an auth")
            auth = httpx.BasicAuth(email, token.get_secret_value())
        self._client = httpx.AsyncClient(
            base_url=site_url.rstrip("/"),
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )
        self._sleep = sleep
        self._points: object = _UNSET

    @classmethod
    def oauth(
        cls,
        cloud_id: str,
        tokens: TokenSource,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> Self:
        """The gateway of one user's connection: ``https://api.atlassian.com/ex/jira/{cloudId}`` with a bearer auth."""

        return cls(api_url(cloud_id), auth=BearerAuth(tokens), transport=transport, sleep=sleep)

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}(site_url={str(self._client.base_url)!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        repeatable: bool = True,
    ) -> httpx.Response:
        """One call with the retries of ``send_with_retries``."""

        return await send_with_retries(
            self._client, self._sleep, method, path, body=body, params=params, repeatable=repeatable
        )

    async def myself(self) -> str:
        return str(_object(await self._send("GET", "/rest/api/3/myself")).get("accountId", ""))

    async def project(self, key: str) -> JiraProject:
        body = _object(await self._send("GET", f"/rest/api/3/project/{_q(key)}"))
        types = body.get("issueTypes") or []

        def type_id(match: Callable[[Mapping[str, Any]], bool]) -> str | None:
            return next((str(t["id"]) for t in types if match(t)), None)

        return JiraProject(
            name=str(body.get("name", "")),
            epic_type_id=type_id(lambda t: t.get("name") == "Epic"),
            story_type_id=type_id(lambda t: t.get("name") == "Story"),
            subtask_type_id=type_id(lambda t: bool(t.get("subtask"))),
        )

    async def missing_permissions(self, key: str) -> frozenset[str]:
        params = {"projectKey": key, "permissions": ",".join(PERMISSIONS)}
        body = _object(await self._send("GET", "/rest/api/3/mypermissions", params=params))
        granted = body.get("permissions") or {}
        return frozenset(name for name in PERMISSIONS if not (granted.get(name) or {}).get("havePermission"))

    async def issue_summary(self, key: str) -> IssueSummary:
        params = {"fields": "summary,issuetype,project"}
        fields = _object(await self._send("GET", f"/rest/api/3/issue/{_q(key)}", params=params)).get("fields") or {}
        return IssueSummary(
            project_key=str((fields.get("project") or {}).get("key", "")),
            type_name=str((fields.get("issuetype") or {}).get("name", "")),
            summary=str(fields.get("summary", "")),
        )

    async def story_points_field(self) -> str | None:
        if self._points is _UNSET:
            response = await self._send("GET", "/rest/api/3/field")
            fields = response.json() if response.content else []
            found = [f.get("id") for f in fields if isinstance(f, dict) and f.get("name") == STORY_POINTS]
            self._points = found[0] if found else None
        return self._points if isinstance(self._points, str) else None

    async def create_issue(self, fields: Mapping[str, Any], *, project_id: str, node_id: str) -> str:
        value = {"project_id": project_id, "node_id": node_id}
        body = {"fields": fields, "properties": [{"key": PROPERTY, "value": value}]}
        created = _object(await self._send("POST", "/rest/api/3/issue", body=body, repeatable=False))
        if not created.get("key"):
            raise JiraError(Reason.UNEXPECTED)
        return str(created["key"])

    async def edit_issue(
        self,
        key: str,
        *,
        fields: Mapping[str, Any],
        add_labels: Sequence[str] = (),
        remove_labels: Sequence[str] = (),
    ) -> None:
        operations = [{"add": label} for label in add_labels] + [{"remove": label} for label in remove_labels]
        body: dict[str, Any] = {}
        if fields:
            body["fields"] = fields
        if operations:
            body["update"] = {"labels": operations}
        if body:
            await self._send("PUT", f"/rest/api/3/issue/{_q(key)}", body=body)

    async def _search(self, body: Mapping[str, Any]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            sent = {**body, "nextPageToken": token} if token else body
            page = _object(await self._send("POST", "/rest/api/3/search/jql", body=sent))
            issues += [issue for issue in page.get("issues") or [] if isinstance(issue, dict)]
            token = page.get("nextPageToken")
            if not token:
                return issues

    async def _state_of(self, key: str) -> IssueState | None:
        try:
            response = await self._send("GET", f"/rest/api/3/issue/{_q(key)}", params={"fields": "status,issuelinks"})
        except JiraError as error:
            if error.reason is Reason.NOT_FOUND:
                return None
            raise
        return _state(_object(response))

    async def issue_states(self, keys: Sequence[str]) -> dict[str, IssueState | None]:
        states: dict[str, IssueState | None] = {}
        for start in range(0, len(keys), PAGE_SIZE):
            chunk = list(keys[start : start + PAGE_SIZE])
            body = {"jql": _jql_keys(chunk), "fields": ["status", "issuelinks"], "maxResults": PAGE_SIZE}
            try:
                found = {issue.get("key"): _state(issue) for issue in await self._search(body)}
            except JiraError as error:
                if error.reason is not Reason.INVALID_FIELD:
                    raise
                found = {}
            for key in chunk:
                states[key] = found[key] if key in found else await self._state_of(key)
        return states

    async def find_created(self, project_key: str, *, project_id: str, minutes: int) -> dict[str, str]:
        jql = f"project = {json.dumps(project_key)} AND labels = {PROPERTY} AND created >= -{int(minutes)}m"
        body = {"jql": jql, "fields": ["labels"], "properties": [PROPERTY], "maxResults": PAGE_SIZE}
        created: dict[str, str] = {}
        for issue in await self._search(body):
            value = (issue.get("properties") or {}).get(PROPERTY) or {}
            if isinstance(value, dict) and value.get("project_id") == project_id and value.get("node_id"):
                created[str(value["node_id"])] = str(issue.get("key", ""))
        return created

    async def add_comment(self, key: str, doc: Mapping[str, Any]) -> None:
        await self._send("POST", f"/rest/api/3/issue/{_q(key)}/comment", body={"body": doc}, repeatable=False)

    async def add_blocks_link(self, *, prerequisite: str, waiter: str) -> None:
        body = {
            "type": {"name": "Blocks"},
            "inwardIssue": {"key": prerequisite},
            "outwardIssue": {"key": waiter},
        }
        await self._send("POST", "/rest/api/3/issueLink", body=body, repeatable=False)

    async def delete_link(self, link_id: str) -> None:
        await self._send("DELETE", f"/rest/api/3/issueLink/{_q(link_id)}")

    async def rank(self, keys: Sequence[str], *, before: str | None = None, after: str | None = None) -> None:
        if len(keys) > RANK_LIMIT or (before is None) == (after is None):
            raise ValueError("rank takes at most 50 keys and exactly one of before and after")
        anchor = {"rankBeforeIssue": before} if before is not None else {"rankAfterIssue": after}
        response = await self._send("PUT", "/rest/agile/1.0/issue/rank", body={"issues": list(keys), **anchor})
        if response.status_code == httpx.codes.MULTI_STATUS:
            # 207: Jira ranked some of the issues and refused others; the move is not done
            raise JiraError(Reason.INVALID_FIELD, http_status=response.status_code)

    async def upsert_remote_link(self, key: str, *, global_id: str, url: str, title: str) -> None:
        body = {"globalId": global_id, "object": {"url": url, "title": title}}
        await self._send("POST", f"/rest/api/3/issue/{_q(key)}/remotelink", body=body)
