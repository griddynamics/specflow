"""Shared fakes of the Atlassian importer tests (no tests in here): a Confluence Cloud REST v2 and Jira Cloud REST v3
behind an ``httpx.MockTransport`` (responses shaped like the real API's), the tokens of a member, the importer's
``Connections`` and a writer that records what an importer stores. Every title, text and id is made up."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from api.imports import ImportWriter
from api.imports import atlassian as atlassian_module
from jira.reader import AtlassianReader

SITE_HOST = "example.atlassian.net"
SITE = f"https://{SITE_HOST}"
CLOUD = "cloud-0001"
ACTING = uuid.UUID(int=77)


def adf(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(blocks)}


def paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def storage_page(page_id: str, title: str, storage: str, *, space_id: str = "10") -> dict[str, Any]:
    """A page of ``GET /wiki/api/v2/pages/{id}?body-format=storage``."""

    return {
        "id": page_id,
        "status": "current",
        "title": title,
        "spaceId": space_id,
        "parentId": None,
        "version": {"number": 3},
        "body": {"storage": {"representation": "storage", "value": storage}},
        "_links": {"webui": f"/spaces/ORD/pages/{page_id}", "base": f"{SITE}/wiki"},
    }


def jira_issue(
    key: str, summary: str, description: str, comments: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """An issue of ``GET /rest/api/3/issue/{key}?fields=...`` (ADF description, comments of the comment field)."""

    found = comments or []
    return {
        "id": "10001",
        "key": key,
        "fields": {
            "summary": summary,
            "description": adf(paragraph(description)),
            "status": {"name": "In Progress"},
            "issuetype": {"name": "Story"},
            "labels": ["checkout"],
            "comment": {"comments": found, "total": len(found), "startAt": 0, "maxResults": 100},
        },
    }


def jira_comment(author: str, text: str, created: str = "2026-03-01T10:00:00.000+0000") -> dict[str, Any]:
    return {"id": "9", "author": {"displayName": author}, "created": created, "body": adf(paragraph(text))}


class FakeTokens:
    """A member's tokens: ``t1`` first, ``t2`` after a refresh."""

    def __init__(self) -> None:
        self.refreshes: list[str] = []

    async def access_token(self) -> str:
        return "t2" if self.refreshes else "t1"

    async def refreshed(self, used: str) -> str:
        self.refreshes.append(used)
        return "t2"


@dataclass
class FakeAtlassian:
    """Confluence (``/ex/confluence/{cloud}``) and Jira (``/ex/jira/{cloud}``) of one site; ``requests`` is every call.
    ``status`` forces an answer for a path (``/wiki/api/v2/pages/5``); ``expire_t1`` answers 401 to the first token."""

    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    below: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # page id -> descendants (any type)
    spaces: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> space
    space_pages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # space id -> pages
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    extra_comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # beyond the issue's own
    status: dict[str, int] = field(default_factory=dict)
    page_size: int = 2
    expire_t1: bool = False
    requests: list[httpx.Request] = field(default_factory=list)
    auths: list[str] = field(default_factory=list)  # the Authorization header of each call, as sent

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self) -> list[str]:
        return [request.url.path.split(f"/{CLOUD}", 1)[1] for request in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.auths.append(request.headers.get("Authorization", ""))
        if self.expire_t1 and request.headers.get("Authorization") == "Bearer t1":
            return httpx.Response(401, json={"message": "expired"})
        path = request.url.path.split(f"/{CLOUD}", 1)[1]
        if path in self.status:
            return httpx.Response(self.status[path], json={"message": "refused"})
        params = request.url.params
        if path.startswith("/wiki/api/v2/"):
            return self._confluence(path.removeprefix("/wiki/api/v2"), params)
        return self._jira(request, path)

    def _confluence(self, path: str, params: httpx.QueryParams) -> httpx.Response:
        if path == "/spaces":
            space = self.spaces.get(params.get("keys", ""))
            return httpx.Response(200, json={"results": [space] if space else []})
        if path == "/pages":
            found = [
                page
                for page in self.pages.values()
                if page["title"] == params.get("title") and page["spaceId"] == params.get("space-id")
            ]
            return httpx.Response(200, json={"results": [{"id": page["id"]} for page in found[:1]]})
        match = re.fullmatch(r"/spaces/(\d+)/pages", path)
        if match:
            return self._paged(path, params, self.space_pages.get(match[1], []))
        match = re.fullmatch(r"/pages/(\d+)/descendants", path)
        if match:
            return self._paged(path, params, self.below.get(match[1], []))
        match = re.fullmatch(r"/pages/(\d+)", path)
        if match and match[1] in self.pages:
            return httpx.Response(200, json=self.pages[match[1]])
        return httpx.Response(404, json={"message": "no such page"})

    def _paged(self, path: str, params: httpx.QueryParams, items: list[dict[str, Any]]) -> httpx.Response:
        start = int(params.get("cursor", "0"))
        chunk = items[start : start + self.page_size]
        body: dict[str, Any] = {"results": chunk, "_links": {"base": SITE}}
        if start + self.page_size < len(items):
            body["_links"]["next"] = f"/wiki/api/v2{path}?cursor={start + self.page_size}&limit=250"
        return httpx.Response(200, json=body)

    def _jira(self, request: httpx.Request, path: str) -> httpx.Response:
        match = re.fullmatch(r"/rest/api/3/issue/([A-Z0-9-]+)/comment", path)
        if match:
            more = self.extra_comments.get(match[1], [])
            start = int(request.url.params.get("startAt", "0"))
            return httpx.Response(200, json={"comments": more[start : start + 1], "total": len(more)})
        match = re.fullmatch(r"/rest/api/3/issue/([A-Z0-9-]+)", path)
        if match and match[1] in self.issues:
            return httpx.Response(200, json=self.issues[match[1]])
        if request.method == "POST" and path == "/rest/api/3/search/jql":
            return self._search(json.loads(request.content))
        if request.method == "POST" and path == "/rest/api/3/search/approximate-count":
            return httpx.Response(200, json={"count": len(self.epic_children(json.loads(request.content)["jql"]))})
        return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})

    def epic_children(self, jql: str) -> list[dict[str, Any]]:
        match = re.search(r'parent = "([A-Z0-9-]+)"', jql)
        key = match[1] if match else ""
        return [issue for issue in self.issues.values() if issue.get("parent") == key]

    def _search(self, body: dict[str, Any]) -> httpx.Response:
        found = self.epic_children(body["jql"])
        start = int(body.get("nextPageToken") or 0)
        chunk = found[start : start + self.page_size]
        page: dict[str, Any] = {"issues": [_without_parent(issue) for issue in chunk]}
        if start + self.page_size < len(found):
            page["nextPageToken"] = str(start + self.page_size)
        return httpx.Response(200, json=page)


def _without_parent(issue: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in issue.items() if key != "parent"}


async def no_sleep(_seconds: float) -> None:
    return None


class FakeConnections:
    """The ``Connections`` the importers use: one connection, of ``user_id`` (``ACTING`` by default)."""

    def __init__(self, *, status: str = "active", site_url: str = SITE, user_id: uuid.UUID = ACTING) -> None:
        self.site_url = site_url
        self.user_id = user_id
        self.status = status
        self.tokens = FakeTokens()

    async def get(self, user_id: uuid.UUID) -> SimpleNamespace | None:
        if user_id != self.user_id:
            return None
        return SimpleNamespace(status=self.status, cloud_id=CLOUD)

    def token_source(self, _user_id: uuid.UUID) -> FakeTokens:
        return self.tokens


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeAtlassian, tokens: FakeTokens | None = None) -> None:
    """Make every reader of the importers talk to ``fake`` (``tokens`` replace the member's)."""

    def reader_for(base_url: str, member_tokens: Any) -> AtlassianReader:
        return AtlassianReader(base_url, tokens or member_tokens, transport=fake.transport(), sleep=no_sleep)

    monkeypatch.setattr(atlassian_module, "reader_for", reader_for)


class RecordingWriter:
    """What an importer stores, in order: ``declared``, ``added`` (external id -> name, markdown, original) and
    ``failed`` (external id, reason, step)."""

    def __init__(self) -> None:
        self.declared: list[tuple[str, str]] = []
        self.added: dict[str, tuple[str, str, bytes]] = {}
        self.failed: list[tuple[str, str, str]] = []

    async def declare(self, external_id: str, name: str) -> None:
        self.declared.append((external_id, name))

    async def add(self, external_id: str, *, name: str, original: bytes, markdown: str, **_: Any) -> str:
        self.added[external_id] = (name, markdown, original)
        return "added"

    async def fail(self, external_id: str, _name: str, *, reason: str, step: str) -> None:
        self.failed.append((external_id, reason, step))


def as_writer(writer: RecordingWriter) -> ImportWriter:
    """The recorder where an importer expects its ``ImportWriter`` (the same methods; the database is the boundary)."""

    return cast(ImportWriter, writer)
