"""Recorded-style Linear GraphQL answers shared by the ``linear.http`` tests."""

from __future__ import annotations

from typing import Any

import httpx

CANARY = "lin_oauth_canary_9f3a7c1e5b"
LINEAR_WORDS = "Linear internal wording 77"
TEAM = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
ISSUE = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
STATE = "55555555-5555-4555-8555-555555555555"
LABEL = "66666666-6666-4666-8666-666666666666"
NEW_ID = "77777777-7777-4777-8777-777777777777"


def data(payload: dict[str, Any], **headers: str) -> httpx.Response:
    return httpx.Response(200, json={"data": payload}, headers=headers)


def failure(code: str, status: int = 200, **extensions: Any) -> httpx.Response:
    error = {"message": LINEAR_WORDS, "extensions": {"code": code, **extensions}}
    return httpx.Response(status, json={"errors": [error]})


def connection(nodes: list[dict[str, Any]], cursor: str | None = None) -> dict[str, Any]:
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def org() -> dict[str, str]:
    return {"id": OTHER, "name": "Graphico", "urlKey": "graphico-app"}


def label_node(label_id: str = LABEL, name: str = "frontend", **over: Any) -> dict[str, Any]:
    node = {"id": label_id, "name": name, "color": "#ff0000", "isGroup": False, "parent": None, "team": {"id": TEAM}}
    return {**node, **over}


def issue_node(identifier: str = "GRA-12", **over: Any) -> dict[str, Any]:
    node = {
        "id": ISSUE,
        "identifier": identifier,
        "url": f"https://linear.app/graphico-app/issue/{identifier}/a-title",
        "title": "A title",
        "description": "Body",
        "estimate": 3,
        "priority": 2,
        "createdAt": "2026-03-01T10:00:00.000Z",
        "updatedAt": "2026-03-02T10:00:00.000Z",
        "team": {"id": TEAM},
        "state": {"id": STATE, "name": "Todo", "type": "unstarted", "position": 1.5},
        "parent": None,
        "project": {"id": PROJECT},
        "projectMilestone": None,
        "assignee": {"name": "Ada Lovelace"},
        "labels": {"nodes": [label_node()]},
    }
    return {**node, **over}


def project_node(**over: Any) -> dict[str, Any]:
    node = {
        "id": PROJECT,
        "name": "Plan",
        "url": "https://linear.app/graphico-app/project/plan-abc123def456",
        "description": "Why",
        "teams": {"nodes": [{"id": TEAM}]},
    }
    return {**node, **over}


def comment_node(comment_id: str, created: str, user: dict[str, str] | None = None) -> dict[str, Any]:
    return {"id": comment_id, "body": f"body {comment_id}", "createdAt": created, "user": user, "issue": {"id": ISSUE}}
