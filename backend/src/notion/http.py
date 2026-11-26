"""The real ``NotionGateway`` over Notion's REST API (docs/notion/oauth.md). Stream A fills this file.

Rules to implement: ``Notion-Version: 2026-03-11`` and the member's bearer token on every request (``token`` is called
per request, so a refresh is seen); every request takes a place from the bot's ``RateLimiter`` first; ``429`` and
``529`` are retried always (``Retry-After`` through ``RateLimiter.hold``), ``5xx`` for ``GET`` only, five attempts
with exponential backoff and jitter; ``401`` is ``notion_auth_failed`` and is NEVER retried here (the refreshing
wrapper of ``NotionConnections.gateway`` owns the refresh and the one retry); a ``404`` is ``notion_not_shared``; a
``400`` ``validation_error`` on ``GET`` page or markdown is confirmed with one ``database(id)`` call (found: raise
``notion_parent_not_a_page``), and on ``GET`` database with one ``page(id)`` call (found: ``notion_link_invalid``):
the message text is never parsed; any other refusal is one ``NotionCode`` from ``notion.reasons.code_of_status`` and
never carries Notion's text. ``page_markdown`` follows ``unknown_block_ids`` (see ``PageMarkdown``). Pagination is the
caller's: ``query_data_source`` answers one page. The tests are ``test_notion_http.py`` (respx),
``test_notion_fake_parity.py`` and the live ``test_notion_http_live.py``."""

from __future__ import annotations

import asyncio
import math
import random
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final
from urllib.parse import quote

import httpx

from logger.logger import WARNING, get_logger, log_event
from notion.gateway import API_BASE, NOTION_VERSION, PAGE_SIZE
from notion.limiter import RateLimiter, WaitBudget
from notion.model import (
    NotionDatabase,
    NotionDataSource,
    NotionPage,
    PageMarkdown,
    RowsPage,
    data_source_from_json,
    database_from_json,
    page_from_json,
)
from notion.reasons import NotionCode, NotionError, code_of_status

logger = get_logger("notion.http")

TokenSource = Callable[[], Awaitable[str]]

MAX_ATTEMPTS: Final = 5
RETRY_AFTER_CAP: Final = 60.0  # Notion's own ceiling for ``Retry-After``
BACKOFF_BASE: Final = 1.0
BACKOFF_CAP: Final = 30.0
_JITTER: Final = random.SystemRandom()  # backoff jitter, not a secret
TIMEOUT_SECONDS: Final = 30
MAX_FOLLOW_UPS: Final = 25  # ``unknown_block_ids`` subtrees read for one page
UNREADABLE: Final = frozenset({NotionCode.NOT_SHARED, NotionCode.PERMISSIONS_MISSING})  # 404, 403
RETRIED_ALWAYS: Final = frozenset({httpx.codes.TOO_MANY_REQUESTS, 529})  # 529: Notion's "overloaded"


class _ValidationError(NotionError):
    """A 400 ``validation_error``: ``ITEM_INVALID``, unless the caller confirms the id is of the other kind."""

    def __init__(self) -> None:
        super().__init__(NotionCode.ITEM_INVALID)


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def _refusal(response: httpx.Response) -> NotionError:
    """The one closed code of a refused request; Notion's message is never read."""

    status, error_code = response.status_code, _error_code(response)
    if status == httpx.codes.BAD_REQUEST and error_code == "validation_error":
        return _ValidationError()
    return NotionError(code_of_status(status, error_code))


def _retry_after(response: httpx.Response) -> float | None:
    try:
        seconds = float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None
    return None if math.isnan(seconds) else min(max(0.0, seconds), RETRY_AFTER_CAP)  # NaN is no answer


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter (half to the full delay) after ``attempt`` failed attempts."""

    return min(BACKOFF_CAP, BACKOFF_BASE * 2.0 ** (attempt - 1)) * _JITTER.uniform(0.5, 1.0)


def _parse[T](parser: Callable[[Mapping[str, Any]], T], body: Mapping[str, Any]) -> T:
    try:
        return parser(body)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise NotionError(NotionCode.UNREACHABLE) from None


def _key(object_id: str) -> str:
    return object_id.replace("-", "").lower()


def _path(*parts: str) -> str:
    return "/v1/" + "/".join(quote(part, safe="") for part in parts)


class HttpNotionGateway:
    """``NotionGateway`` over ``httpx``; one per pass or import (it holds the pass's ``WaitBudget``)."""

    def __init__(
        self,
        token: TokenSource,
        limiter: RateLimiter,
        *,
        budget: WaitBudget | None = None,
        base_url: str = API_BASE,
        version: str = NOTION_VERSION,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = token
        self._limiter = limiter
        self._budget = budget or WaitBudget()
        self._base_url = base_url.rstrip("/")
        self._version = version
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep

    async def aclose(self) -> None:
        """Close the HTTP client this gateway made; a client passed in stays its owner's."""

        if self._client is not None and self._owns_client:
            client, self._client = self._client, None
            await client.aclose()

    # Read ----------------------------------------------------------------------------------------------------------

    async def page(self, page_id: str) -> NotionPage:
        body = await self._read_page_route(_path("pages", page_id), page_id)
        return _parse(page_from_json, body)

    async def database(self, database_id: str) -> NotionDatabase:
        try:
            body = await self._request("GET", _path("databases", database_id))
        except _ValidationError:
            await self._confirm_other_kind(_path("pages", database_id), NotionCode.LINK_INVALID)
            raise  # unreachable: the confirmation raises
        return _parse(database_from_json, body)

    async def data_source(self, data_source_id: str) -> NotionDataSource:
        body = await self._request("GET", _path("data_sources", data_source_id))
        return _parse(data_source_from_json, body)

    async def page_markdown(self, page_id: str) -> PageMarkdown:
        first = await self._read_page_route(_path("pages", page_id, "markdown"), page_id, markdown=True)
        parts = [str(first.get("markdown", ""))]
        unread: deque[str] = deque(self._unknown_ids(first))
        seen = {_key(page_id)}
        cut = bool(first.get("truncated")) and not unread  # cut by Notion's size limit: nothing more to ask for
        reads = 0
        while unread and reads < MAX_FOLLOW_UPS:
            block_id = unread.popleft()
            if _key(block_id) in seen:
                continue
            seen.add(_key(block_id))
            reads += 1
            try:
                sub = await self._request(
                    "GET", _path("pages", block_id, "markdown"), params={"markdown_version": "v2"}
                )
            except NotionError as error:
                if error.code not in UNREADABLE:
                    raise
                log_event(logger, WARNING, "notion.http.subtree_unreadable", code=error.code.value)
                continue
            if str(sub.get("markdown", "")):
                parts.append(str(sub["markdown"]))
            more = self._unknown_ids(sub)
            cut = cut or (bool(sub.get("truncated")) and not more)
            unread.extend(more)
        left = tuple(dict.fromkeys(i for i in unread if _key(i) not in seen))
        return PageMarkdown("\n\n".join(part for part in parts if part), cut or bool(left), left)

    async def query_data_source(
        self,
        data_source_id: str,
        *,
        start_cursor: str | None = None,
        page_size: int = PAGE_SIZE,
        where: Mapping[str, Any] | None = None,
    ) -> RowsPage:
        payload: dict[str, Any] = {"page_size": min(max(page_size, 1), PAGE_SIZE)}
        if start_cursor:
            payload["start_cursor"] = start_cursor
        if where is not None:
            payload["filter"] = dict(where)
        body = await self._request("POST", _path("data_sources", data_source_id, "query"), json=payload, safe=True)
        results = body.get("results")
        if not isinstance(results, list):
            raise NotionError(NotionCode.UNREACHABLE)
        cursor = body.get("next_cursor")
        more = bool(body.get("has_more")) and isinstance(cursor, str) and bool(cursor)
        return RowsPage(tuple(_parse(page_from_json, row) for row in results), cursor if more else None)

    # Write ---------------------------------------------------------------------------------------------------------

    async def create_database(self, parent_page_id: str, title: str, properties: Mapping[str, Any]) -> NotionDatabase:
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "initial_data_source": {"properties": dict(properties)},
        }
        return _parse(database_from_json, await self._request("POST", _path("databases"), json=payload))

    async def update_data_source(self, data_source_id: str, properties: Mapping[str, Any]) -> NotionDataSource:
        body = await self._request(
            "PATCH", _path("data_sources", data_source_id), json={"properties": dict(properties)}
        )
        return _parse(data_source_from_json, body)

    async def create_page(
        self, data_source_id: str, *, properties: Mapping[str, Any], markdown: str = ""
    ) -> NotionPage:
        payload: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": dict(properties),
        }
        if markdown:
            payload.update(markdown=markdown, markdown_version="v2")
        return _parse(page_from_json, await self._request("POST", _path("pages"), json=payload))

    async def update_page(self, page_id: str, *, properties: Mapping[str, Any]) -> NotionPage:
        body = await self._request("PATCH", _path("pages", page_id), json={"properties": dict(properties)})
        return _parse(page_from_json, body)

    async def replace_markdown(self, page_id: str, markdown: str) -> None:
        payload = {"markdown_version": "v2", "type": "replace_content", "replace_content": {"new_str": markdown}}
        await self._request("PATCH", _path("pages", page_id, "markdown"), json=payload)

    async def create_comment(self, page_id: str, markdown: str) -> None:
        await self._request("POST", _path("comments"), json={"parent": {"page_id": page_id}, "markdown": markdown})

    # Inside --------------------------------------------------------------------------------------------------------

    @staticmethod
    def _unknown_ids(body: Mapping[str, Any]) -> list[str]:
        ids = body.get("unknown_block_ids")
        return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []

    async def _read_page_route(self, path: str, object_id: str, *, markdown: bool = False) -> dict[str, Any]:
        """GET of a page route: a 400 ``validation_error`` is confirmed with one ``GET`` of the database route."""

        params = {"markdown_version": "v2"} if markdown else None
        try:
            return await self._request("GET", path, params=params)
        except _ValidationError:
            await self._confirm_other_kind(_path("databases", object_id), NotionCode.NOT_A_PAGE)
            raise  # unreachable: the confirmation raises

    async def _confirm_other_kind(self, path: str, found: NotionCode) -> None:
        """Notion answers a wrong-kind id with a bare 400 ``validation_error``: ask the other route once. When it
        answers, the id is of the other kind (``found``); a refusal of the same sort leaves ``ITEM_INVALID``."""

        try:
            await self._request("GET", path)
        except NotionError as error:
            if isinstance(error, _ValidationError) or error.code is NotionCode.NOT_SHARED:
                raise NotionError(NotionCode.ITEM_INVALID) from None
            raise
        raise NotionError(found)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        safe: bool = False,
    ) -> dict[str, Any]:
        """One logical request: the limiter's place, the retries, the closed error. ``safe`` marks a POST that only
        reads, so a 5xx may be repeated as for a GET."""

        repeatable = method == "GET" or safe
        headers = {"Authorization": f"Bearer {await self._token()}", "Notion-Version": self._version}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._limiter.acquire(self._budget)
            last = attempt == MAX_ATTEMPTS
            try:
                response = await self._http().request(
                    method, self._base_url + path, headers=headers, json=json, params=params
                )
            except httpx.TransportError as error:
                # A connection that never opened is safe to repeat for any method; one that broke mid-flight only
                # for a read (a write may have run).
                if last or not (repeatable or isinstance(error, httpx.ConnectError | httpx.ConnectTimeout)):
                    raise NotionError(NotionCode.UNREACHABLE) from None
                await self._backoff(attempt, "transport")
                continue
            status = response.status_code
            if httpx.codes.is_success(status):
                return self._object(response)
            if not last and status in RETRIED_ALWAYS:
                self._limiter.hold(_backoff(attempt) if (after := _retry_after(response)) is None else after)
                log_event(logger, WARNING, "notion.http.retry", status=status, attempt=attempt)
                continue
            if not last and status >= httpx.codes.INTERNAL_SERVER_ERROR and repeatable:
                await self._backoff(attempt, str(status))
                continue
            raise _refusal(response)
        raise NotionError(NotionCode.UNREACHABLE)  # unreachable: the loop always returns or raises

    async def _backoff(self, attempt: int, why: str) -> None:
        wait = _backoff(attempt)
        try:
            self._budget.spend(wait)
        except NotionError:
            raise NotionError(NotionCode.UNREACHABLE) from None
        log_event(logger, WARNING, "notion.http.retry", status=why, attempt=attempt)
        await self._sleep(wait)

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            raise NotionError(NotionCode.UNREACHABLE) from None
        if not isinstance(body, dict):
            raise NotionError(NotionCode.UNREACHABLE)
        return body

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS, trust_env=False)
        return self._client
