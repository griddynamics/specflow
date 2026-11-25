"""Support of the Notion HTTP client tests: a fake clock, a gateway on ``httpx.MockTransport`` and ``FixtureNotion``,
a stand-in for Notion's REST API that answers the recorded JSON of ``notion/fixtures/`` (Notion's own shapes, so the
parity test compares what the real client reads with what ``FakeNotionGateway`` returns)."""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from notion.fake import FIXTURES, normal
from notion.http import HttpNotionGateway
from notion.limiter import RateLimiter, WaitBudget

TOKEN: Final = "secret-token-value"
Handler = Callable[[httpx.Request], httpx.Response]


class Clock:
    """A clock that only moves when somebody sleeps: ``sleeps`` records every wait."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Rig:
    gateway: HttpNotionGateway
    clock: Clock
    requests: list[httpx.Request]
    limiter: RateLimiter
    budget: WaitBudget
    tokens: list[str] = field(default_factory=list)


def make_rig(handler: Handler, *, per_minute: int = 600, budget: float = 60.0) -> Rig:
    """A gateway whose requests go to ``handler`` and whose waits (limiter and backoff) advance one ``Clock``."""

    clock, requests, tokens = Clock(), [], []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    async def token() -> str:
        tokens.append(TOKEN)
        return TOKEN

    limiter = RateLimiter(per_minute, clock=clock.time, sleep=clock.sleep)
    wait = WaitBudget(budget)
    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    gateway = HttpNotionGateway(
        token, limiter, budget=wait, base_url="https://notion.test", client=client, sleep=clock.sleep
    )
    return Rig(gateway, clock, requests, limiter, wait, tokens)


def reply(status: int = 200, body: Any | None = None, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=body if body is not None else {}, headers=headers)


def error(status: int, code: str, **headers: str) -> httpx.Response:
    """An error answer; the message is text a client must never repeat."""

    return reply(status, {"object": "error", "status": status, "code": code, "message": "PROVIDER-TEXT"}, **headers)


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _rich(parts: Any) -> Any:
    """Notion adds ``plain_text`` to every rich-text part it stores."""

    if not isinstance(parts, list):
        return parts
    return [{**p, "plain_text": (p.get("text") or {}).get("content", p.get("plain_text", ""))} for p in parts]


def _stored(properties: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in properties.items():
        value = dict(value)
        for kind in ("title", "rich_text"):
            if isinstance(value.get(kind), list):
                value[kind] = _rich(value[kind])
        if "title" in value and "type" not in value and isinstance(value["title"], list):
            value["type"] = "title"
        out[name] = {"id": name, **value}
    return out


class FixtureNotion:
    """Notion's read routes over the fixtures and write routes that keep what is written, like ``FakeNotionGateway``."""

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = load("pages")
        self.databases: dict[str, dict[str, Any]] = load("databases")
        self.data_sources: dict[str, dict[str, Any]] = load("data_sources")
        self.markdown: dict[str, dict[str, Any]] = load("markdown")
        self.rows: dict[str, list[str]] = {k: [r["id"] for r in v] for k, v in load("queries").items()}
        self.comments: list[tuple[str, str]] = []
        self.unshared: set[str] = set()
        self._ids = itertools.count(1)

    def unshare(self, *ids: str) -> None:
        self.unshared.update(normal(i) for i in ids)

    def new_id(self) -> str:
        return normal(f"fa4e0000{next(self._ids):024x}")

    def handle(self, request: httpx.Request) -> httpx.Response:
        route = re.fullmatch(r"/v1/(\w+)(?:/([0-9a-fA-F-]+))?(?:/(\w+))?", request.url.path)
        assert route, request.url.path
        kind, raw_id, tail = route.groups()
        object_id = normal(raw_id) if raw_id else None
        body = json.loads(request.content) if request.content else {}
        parent = body.get("parent") or {}
        parent_id = next((normal(v) for v in parent.values() if isinstance(v, str) and len(v) >= 32), None)
        if object_id in self.unshared or parent_id in self.unshared:
            return error(404, "object_not_found")
        if request.method == "GET":
            return self._get(kind, object_id, tail)
        return self._write(request.method, kind, object_id, tail, body)

    def _missing(self) -> httpx.Response:
        return error(404, "object_not_found")

    def _wrong_kind(self) -> httpx.Response:
        return error(400, "validation_error")

    def _get(self, kind: str, object_id: str | None, tail: str | None) -> httpx.Response:
        assert object_id
        if kind == "pages" and tail == "markdown":
            if object_id in self.databases:
                return self._wrong_kind()
            return reply(200, self.markdown[object_id]) if object_id in self.markdown else self._missing()
        if kind == "pages":
            if object_id in self.databases:
                return self._wrong_kind()
            return reply(200, self.pages[object_id]) if object_id in self.pages else self._missing()
        if kind == "databases":
            if object_id in self.pages:
                return self._wrong_kind()
            return reply(200, self.databases[object_id]) if object_id in self.databases else self._missing()
        if kind == "data_sources":
            return reply(200, self.data_sources[object_id]) if object_id in self.data_sources else self._missing()
        raise AssertionError(kind)

    def _write(
        self, method: str, kind: str, object_id: str | None, tail: str | None, body: dict[str, Any]
    ) -> httpx.Response:
        if kind == "data_sources" and tail == "query":
            return self._query(object_id, body)
        if kind == "databases" and method == "POST":
            return self._create_database(body)
        if kind == "data_sources" and method == "PATCH":
            return self._update_source(object_id, body)
        if kind == "pages" and method == "POST":
            return self._create_page(body)
        if kind == "pages" and tail == "markdown":
            if object_id not in self.pages:
                return self._missing()
            self.markdown[object_id] = {"id": object_id, "markdown": body["replace_content"]["new_str"]}
            return reply(200, self.pages[object_id])
        if kind == "pages" and method == "PATCH":
            return self._update_page(object_id, body)
        if kind == "comments":
            page_id = normal(body["parent"]["page_id"])
            if page_id not in self.pages:
                return self._missing()
            self.comments.append((page_id, body["markdown"]))
            return reply(200, {"object": "comment", "id": self.new_id()})
        raise AssertionError((method, kind, tail))

    def _query(self, source_id: str | None, body: dict[str, Any]) -> httpx.Response:
        if source_id not in self.rows:
            return self._missing()
        ids = [i for i in self.rows[source_id] if normal(i) not in self.unshared]
        wanted = ((body.get("filter") or {}).get("rich_text") or {}).get("equals")
        if body.get("filter"):
            name = body["filter"]["property"]
            ids = [i for i in ids if _text(self.pages[normal(i)]["properties"].get(name)) == wanted]
        start, size = int(body.get("start_cursor") or 0), body["page_size"]
        more = start + size < len(ids)
        results = [self.pages[normal(i)] for i in ids[start : start + size]]
        return reply(
            200,
            {
                "object": "list",
                "results": results,
                "next_cursor": str(start + size) if more else None,
                "has_more": more,
            },
        )

    def _create_database(self, body: dict[str, Any]) -> httpx.Response:
        parent = normal(body["parent"]["page_id"])
        if parent not in self.pages:
            return self._missing()
        database_id, source_id = self.new_id(), self.new_id()
        title = _rich(body["title"])
        name = "".join(p["plain_text"] for p in title)
        self.databases[database_id] = {
            "object": "database",
            "id": database_id,
            "url": f"https://www.notion.so/{database_id.replace('-', '')}",
            "title": title,
            "parent": {"type": "page_id", "page_id": parent},
            "data_sources": [{"id": source_id, "name": name}],
        }
        self.data_sources[source_id] = {
            "object": "data_source",
            "id": source_id,
            "parent": {"type": "database_id", "database_id": database_id},
            "title": title,
            "properties": _stored(body["initial_data_source"]["properties"]),
        }
        self.rows[source_id] = []
        return reply(200, self.databases[database_id])

    def _update_source(self, source_id: str | None, body: dict[str, Any]) -> httpx.Response:
        if source_id not in self.data_sources:
            return self._missing()
        source = self.data_sources[source_id]
        source["properties"] = {**source["properties"], **_stored(body["properties"])}
        return reply(200, source)

    def _create_page(self, body: dict[str, Any]) -> httpx.Response:
        source_id = normal(body["parent"]["data_source_id"])
        if source_id not in self.rows:
            return self._missing()
        page_id = self.new_id()
        self.pages[page_id] = {
            "object": "page",
            "id": page_id,
            "url": f"https://www.notion.so/{page_id.replace('-', '')}",
            "parent": {"type": "data_source_id", "data_source_id": source_id},
            "properties": _stored(body["properties"]),
        }
        self.markdown[page_id] = {
            "id": page_id,
            "markdown": body.get("markdown", ""),
            "truncated": False,
            "unknown_block_ids": [],
        }
        self.rows[source_id].append(page_id)
        return reply(200, self.pages[page_id])

    def _update_page(self, page_id: str | None, body: dict[str, Any]) -> httpx.Response:
        if page_id not in self.pages:
            return self._missing()
        page = self.pages[page_id]
        page["properties"] = {**page["properties"], **_stored(body["properties"])}
        return reply(200, page)


def _text(value: Any) -> str:
    return "".join(p.get("plain_text", "") for p in (value or {}).get("rich_text") or [])
