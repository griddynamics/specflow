"""The MCP contract gate (MCP-SPLIT N1): the surface an agent sees, frozen as golden files.

Everything is read the way a client reads it, over JSON-RPC and HTTP against the in-process app: ``tools/list``
(descriptions, input and output schemas, annotations), ``prompts/list``, the rendered ``select_project`` prompt, the
server instructions, both PRM documents, the probes, the ``401``/``403``/``404``/``405`` answers, one success result
per tool (structured content and text block), one error result per ``ErrorCode`` and the committed cursors. Nothing
asserts on server internals (audit events, ``FakeRepo`` calls, recorded queries), so the goldens hold when the data
behind the tools moves: the ``backend_double`` fixture of ``mcp_golden_seam.py`` is the ONE seam that provides that
data, and that module is the only one the split's later sessions edit (05: the import paths; 06: the data source, a
fake API). This module names no product module. The tool manifest stays with ``test_mcp_manifest.py``.

Goldens live in ``tests/fixtures/mcp/golden/``. A normal run only compares; it never writes and fails on a missing
file. Volatile values are normalised, not frozen: the run's IdP issuer, durations and the order of an ``Allow`` list.

Regenerate on purpose, after a deliberate contract change, and review the diff of the golden files before commit::

    cd backend && MCP_GOLDEN_UPDATE=1 uv run pytest tests/test_mcp_golden.py -q

``cursors.json`` is not regenerated: a committed cursor is a promise that later versions keep accepting it.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from mcp_golden_seam import ALPHA, BETA, UNKNOWN_PROJECT
from mcp_support import PRM_URL, rpc, suite_token
from specflow_api_support import token as web_token

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "mcp" / "golden"
UPDATE_ENV = "MCP_GOLDEN_UPDATE"
ALICE = suite_token("alice@example.com")
OUTSIDER = suite_token("carol@other.example")  # a verified token of a domain the policy refuses (403)
HEADERS_KEPT = ("www-authenticate", "allow", "content-type")

# The ErrorCodes of docs/mcp-server/tools.md, kept literal so nothing here imports the product; a new code must be
# added here with its case and golden.
ERROR_CODES = (
    "project_not_found",
    "invalid_cursor",
    "no_plan",
    "task_not_found",
    "feature_not_found",
    "graph_unavailable",
    "database_unavailable",
    "internal_error",
)


# --- golden files and normalisation -------------------------------------------------------------------------


def normalised(value: Any, issuer: str) -> Any:
    """``value`` with the volatile parts replaced: the run's issuer, ``duration_ms``."""

    if isinstance(value, dict):
        return {k: "<duration_ms>" if k == "duration_ms" else normalised(v, issuer) for k, v in value.items()}
    if isinstance(value, list):
        return [normalised(v, issuer) for v in value]
    return "<issuer>" if value == issuer else value


def check_golden(name: str, actual: Any) -> None:
    """Compare ``actual`` with ``golden/<name>``; writes it only when ``MCP_GOLDEN_UPDATE=1``."""

    path = GOLDEN / name
    rendered = json.dumps(actual, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if os.environ.get(UPDATE_ENV) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    assert path.exists(), (
        f"golden file {name} is missing: regenerate it on purpose with {UPDATE_ENV}=1 (see the docstring)"
    )
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(rendered), f"{name} differs from the contract"


def committed_cursors() -> dict[str, str]:
    return json.loads((GOLDEN / "cursors.json").read_text(encoding="utf-8"))


def issuer_of(valid_env: dict[str, str]) -> str:
    return valid_env["OIDC_ISSUER"]


async def method(client: httpx.AsyncClient, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``result`` of one JSON-RPC call to ``/mcp`` as alice."""

    body = {"jsonrpc": "2.0", "id": 7, "method": name, **({"params": params} if params is not None else {})}
    response = await rpc(client, ALICE, body)
    assert response.status_code == 200, response.text
    return dict(response.json()["result"])


async def tool(client: httpx.AsyncClient, name: str, **arguments: Any) -> dict[str, Any]:
    return await method(client, "tools/call", {"name": name, "arguments": arguments})


def wire(response: httpx.Response) -> dict[str, Any]:
    """An HTTP answer as a client sees it: status, the headers that carry meaning, the body. ``Allow`` is a set of
    methods whose order Starlette leaves to hash randomisation: it is sorted, not frozen."""

    kept = {k: v for k, v in sorted(response.headers.items()) if k in HEADERS_KEPT}
    if "allow" in kept:
        kept["allow"] = ", ".join(sorted(m.strip() for m in kept["allow"].split(",")))
    return {
        "status": response.status_code,
        "headers": kept,
        "body": response.json() if response.content else None,
    }


# --- the protocol surface ---------------------------------------------------------------------------------


async def test_the_protocol_surface_is_the_golden_one(backend_double, valid_env):
    """tools/list (schemas, annotations), prompts/list, ``select_project`` both forms, instructions."""

    async with backend_double.serve() as client:
        initialize = await method(
            client,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "gate", "version": "1"}},
        )
        surface = {
            "initialize": {
                "instructions": initialize["instructions"],
                "capabilities": initialize["capabilities"],
                "server_name": initialize["serverInfo"]["name"],
            },
            "tools_list": await method(client, "tools/list"),
            "prompts_list": await method(client, "prompts/list"),
            "select_project_with_argument": await method(
                client, "prompts/get", {"name": "select_project", "arguments": {"project": "Alpha"}}
            ),
            "select_project_without_argument": await method(client, "prompts/get", {"name": "select_project"}),
        }
    check_golden("protocol.json", normalised(surface, issuer_of(valid_env)))


# --- HTTP: PRM, probes, guard answers, unknown paths ------------------------------------------------------


async def test_the_http_surface_is_the_golden_one(backend_double, valid_env):
    """Both PRM paths, ``/healthz``, ``/readyz`` ready and not ready, 401/403 with the challenge, 404 and 405 bodies."""

    bearer = {"Authorization": f"Bearer {ALICE}"}
    async with backend_double.serve() as client:
        http = {
            "prm_mcp_path": wire(await client.get("/.well-known/oauth-protected-resource/mcp")),
            "prm_root_path": wire(await client.get("/.well-known/oauth-protected-resource")),
            "healthz": wire(await client.get("/healthz")),
            "readyz_ready": wire(await client.get("/readyz")),
            "no_token_401": wire(await rpc(client)),
            "wrong_audience_token_401": wire(await rpc(client, web_token("alice@example.com"))),
            "refused_domain_403": wire(await rpc(client, OUTSIDER)),
            "unknown_path_404": wire(await client.get("/nope")),
            "trailing_slash_404": wire(await client.post("/mcp/", headers=bearer)),
            "get_on_mcp_405": wire(await client.get("/mcp", headers=bearer)),
            "post_on_healthz_405": wire(await client.post("/healthz")),
        }
    async with backend_double.serve("not_ready") as client:
        http["readyz_not_ready"] = wire(await client.get("/readyz"))
        http["healthz_while_not_ready"] = wire(await client.get("/healthz"))
    assert http["no_token_401"]["headers"]["www-authenticate"] == f'Bearer resource_metadata="{PRM_URL}"'
    check_golden("http.json", normalised(http, issuer_of(valid_env)))


# --- tool results --------------------------------------------------------------------------------------------

PAGE_ONE: dict[str, tuple[str, dict[str, Any]]] = {
    "list_projects": ("list_projects", {"limit": 2}),
    "list_features": ("list_features", {"project_id": ALPHA, "limit": 2}),
    "list_tasks": ("list_tasks", {"project_id": ALPHA, "limit": 2}),
    "list_feature_tasks": ("list_feature_tasks", {"project_id": ALPHA, "feature_id": "FEAT::PLACE", "limit": 2}),
    "get_task_path": ("get_task_path", {"project_id": ALPHA, "task_id": "TASK::ORDERS::C", "limit": 2}),
}
BRIEF = ("get_task_brief", {"project_id": ALPHA, "task_id": "TASK::ORDERS::B"})


async def test_every_tool_answers_the_golden_success_result_and_pages_on_the_committed_cursor(backend_double):
    """One success per tool (structured content and text block, ``next_cursor`` set), then page two of each paged tool
    from the COMMITTED cursor: later sessions must keep accepting it."""

    cursors = committed_cursors()
    results: dict[str, Any] = {}
    async with backend_double.serve() as client:
        for key, (name, arguments) in PAGE_ONE.items():
            first = await tool(client, name, **arguments)
            assert first["structuredContent"]["next_cursor"] == cursors[key], key
            second = await tool(client, name, **arguments, cursor=cursors[key])
            results[key] = {"page_1": first, "page_2_from_committed_cursor": second}
        results["get_task_brief"] = {"page_1": await tool(client, BRIEF[0], **BRIEF[1])}
    for key, entry in results.items():
        for page in entry.values():
            assert page.get("isError", False) is False, key
            assert json.loads(page["content"][0]["text"]) == page["structuredContent"], key
    check_golden("tool_results.json", results)


async def test_every_error_code_has_a_golden_error_result(backend_double):
    """One error result per ``ErrorCode``, each reached the way an agent reaches it."""

    cases: dict[str, tuple[str, str, dict[str, Any]]] = {
        "project_not_found": ("ok", "list_features", {"project_id": UNKNOWN_PROJECT}),
        "invalid_cursor": ("ok", "list_features", {"project_id": ALPHA, "cursor": "not-a-cursor"}),
        "no_plan": ("no_plan", "list_tasks", {"project_id": ALPHA}),
        "task_not_found": ("ok", "get_task_brief", {"project_id": ALPHA, "task_id": "TASK::NOPE"}),
        "feature_not_found": ("ok", "list_feature_tasks", {"project_id": ALPHA, "feature_id": "FEAT::NOPE"}),
        "graph_unavailable": ("graph_down", "list_features", {"project_id": ALPHA}),
        "database_unavailable": ("database_down", "list_projects", {}),
        "internal_error": ("bug", "list_features", {"project_id": ALPHA}),
    }
    assert set(cases) == set(ERROR_CODES), "the cases must be exactly the ErrorCodes of tools.md"
    errors: dict[str, Any] = {}
    for code, (scenario, name, arguments) in cases.items():
        async with backend_double.serve(scenario) as client:
            result = await tool(client, name, **arguments)
        assert result["isError"] is True and result["structuredContent"]["error"]["code"] == code
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        errors[code] = result
    check_golden("tool_errors.json", errors)


# --- the committed cursors -----------------------------------------------------------------------------------


def test_the_committed_cursors_are_canonical_base64url_and_name_the_fixture_positions():
    """``cursors.json`` is hand-committed, never regenerated, and read here without any product codec. Each is
    unpadded base64url of its exact bytes: ``{"v":1,"after":<id>}`` for the feature-style ones, ``<timestamp>|<id>`` of
    the second seeded project for ``list_projects``."""

    cursors = committed_cursors()
    assert set(cursors) == set(PAGE_ONE)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", c) for c in cursors.values())
    decoded = {k: base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)) for k, c in cursors.items()}
    assert decoded == {
        "list_projects": b"2026-03-02T09:30:00+00:00|" + BETA.encode(),
        "list_features": b'{"v":1,"after":"FEAT::F2"}',
        "list_tasks": b'{"v":1,"after":"TASK::ORDERS::D"}',
        "list_feature_tasks": b'{"v":1,"after":"TASK::ORDERS::B"}',
        "get_task_path": b'{"v":1,"after":"TASK::ORDERS::B"}',
    }
