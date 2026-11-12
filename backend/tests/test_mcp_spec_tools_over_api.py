"""The agent reads of the spec graph (``get_story_brief``, ``trace``, ``get_node``, ``search_spec``, ``similar``,
``get_evidence``, ``path``, ``expand_brief``) through the MCP router over the REAL API app, in process (the pattern
of ``test_mcp_over_api.py``): the shop fixture behind the API (``spec_reads_support``), a recording transport
between the two services, the MCP SDK client in front.

Covers the tool against its API route (same structured content, one text block holding the same JSON, query as the
API reads it: lists repeated, booleans lower case), the audit (``item_count``, ids and codes only), the error paths
end to end (fixed messages, no details), and the input schemas the SDK enforces before any call.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from agent_contracts import AGENT_ROUTES
from graph.core import ConfigurationError, InfrastructureUnavailableError
from mcp_server.errors import MESSAGES
from mcp_server.registry import install
from mcp_support import ALICE, MCP_URL, build_app, call, completed, mcp_env, sdk_session, structured, text
from spec_reads_support import CHECKOUT, SpecRig, failing_reads, serve_spec
from specflow_api_agent_support import ALPHA, FOREIGN, UNKNOWN_PROJECT, agent_env

CALLS: dict[str, dict[str, Any]] = {
    "get_story_brief": {"project_id": ALPHA, "story_id": CHECKOUT, "neighbours": 5},
    "trace": {"project_id": ALPHA, "spec_ids": ["FEAT::CHECKOUT", "FEAT::CARD_PAYMENT"], "direction": "based_on"},
    "get_node": {"project_id": ALPHA, "spec_id": "MOD::STRIPE", "include": ["edges", "findings"], "edge_limit": 2},
    "search_spec": {"project_id": ALPHA, "query": "checkout", "near": CHECKOUT, "labels": ["Feature"], "limit": 3},
    "similar": {"project_id": ALPHA, "story_id": CHECKOUT, "min_score": 0.8},
    "get_evidence": {"project_id": ALPHA, "spec_id": "CON::EU_REGION", "context_lines": 1},
    "path": {"project_id": ALPHA, "from": "MOD::PAYMENTS_SERVICE", "to": "MOD::STRIPE", "max_hops": 3},
    "expand_brief": {"project_id": ALPHA, "story_id": CHECKOUT, "budget_tokens": 500},
}
ITEM_COUNT = {
    "get_story_brief": 1,
    "trace": 9,
    "get_node": 1,
    "search_spec": 2,
    "similar": 2,
    "get_evidence": 1,
    "path": 1,
    "expand_brief": 2,
}  # nodes of the trace; items of a search, a similar, an evidence, a path or an expansion


class Recorder(httpx.AsyncBaseTransport):
    """The API as the MCP client sees it, remembering every exchange."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.exchanges: list[tuple[httpx.Request, httpx.Response]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        self.exchanges.append((request, response))
        return response

    def agent_calls(self) -> list[tuple[httpx.Request, httpx.Response]]:
        return [(q, a) for q, a in self.exchanges if q.url.path in AGENT_ROUTES.values()]


@dataclass
class Stack:
    spec: SpecRig
    api: Recorder
    env: dict[str, str]

    def app(self) -> Any:
        return build_app(self.env, transport=self.api, tools=lambda server: install(server, resource_url=MCP_URL))


@asynccontextmanager
async def over_spec(
    monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str], *, has_plan: bool = True, fail: Exception | None = None
) -> AsyncIterator[Stack]:
    async with serve_spec(monkeypatch, agent_env(valid_env), has_plan=has_plan, fail=fail) as spec:
        recorder = Recorder(httpx.ASGITransport(app=spec.rig.app, raise_app_exceptions=False))
        yield Stack(spec, recorder, mcp_env(valid_env))


@pytest.mark.parametrize("tool", CALLS)
async def test_a_tool_returns_the_structured_content_of_its_route_in_one_json_text_block(monkeypatch, valid_env, tool):
    async with over_spec(monkeypatch, valid_env) as stack:
        async with sdk_session(stack.app(), ALICE) as session:
            result = await call(session, tool, **CALLS[tool])
        (request, answer), *_ = stack.api.agent_calls()

    assert not result.is_error and answer.status_code == 200
    assert (request.method, request.url.path) == ("GET", AGENT_ROUTES[tool])
    assert structured(result) == answer.json()
    assert len(result.content) == 1 and json.loads(text(result)) == structured(result)


async def test_the_query_the_api_reads_repeats_lists_and_spells_booleans_lower_case(monkeypatch, valid_env):
    async with over_spec(monkeypatch, valid_env) as stack:
        async with sdk_session(stack.app(), ALICE) as session:
            await call(session, "trace", **CALLS["trace"], include_owner=False, relations=["DEPENDS_ON", "PART_OF"])
            await call(session, "get_node", project_id=ALPHA, spec_id="MOD::STRIPE")
            await call(session, "path", **CALLS["path"])
            await call(session, "similar", project_id=ALPHA, spec_ids=["FEAT::CHECKOUT"], exclude_linked=False)
        (trace, _), (node, _), (path, _), (similar, _) = stack.api.agent_calls()

    assert trace.url.params.get_list("spec_ids") == ["FEAT::CHECKOUT", "FEAT::CARD_PAYMENT"]
    assert trace.url.params.get_list("relations") == ["DEPENDS_ON", "PART_OF"]
    assert trace.url.params["include_owner"] == "false" and trace.url.params["depth"] == "2"
    assert "story_id" not in trace.url.params  # None is left out
    assert node.url.params.get_list("include") == ["edges", "plan", "provenance", "findings"]  # the default, all four
    assert (path.url.params["from"], path.url.params["to"]) == ("MOD::PAYMENTS_SERVICE", "MOD::STRIPE")  # wire names
    assert similar.url.params["exclude_linked"] == "false" and similar.url.params["min_score"] == "0.75"


async def test_the_audit_counts_items_or_nodes_and_holds_ids_and_codes_only(monkeypatch, valid_env, central_log):
    central_log.configure()
    async with over_spec(monkeypatch, valid_env) as stack:
        async with sdk_session(stack.app(), ALICE) as session:
            for tool, arguments in CALLS.items():
                await call(session, tool, **arguments)
            await call(session, "get_node", project_id=ALPHA, spec_id="FEAT::NOPE")
        user_id = stack.api.agent_calls()[0][1].headers["x-specflow-user-id"]

    events = central_log.events("mcp.tool.completed")
    assert [(e["tool"], e["outcome"], e["item_count"]) for e in events[: len(CALLS)]] == [
        (tool, "ok", count) for tool, count in ITEM_COUNT.items()
    ]
    assert (events[len(CALLS)]["tool"], events[len(CALLS)]["outcome"]) == ("get_node", "node_not_found")
    for event in events:
        assert {"tool", "outcome", "item_count", "duration_ms", "request_id", "user_id", "project_id"} <= event.keys()
        assert event["user_id"] == user_id and event["project_id"] == ALPHA
    dump = json.dumps(events, default=str)
    for private in ("checkout", "Which currencies", "MOD::", "FEAT::", "EU region", "Alice", "Ben", CHECKOUT, ALICE):
        assert private not in dump, private


ERROR_CASES = [
    pytest.param("get_story_brief", {"project_id": FOREIGN, "story_id": CHECKOUT}, "project_not_found", id="foreign"),
    pytest.param(
        "trace",
        {"project_id": UNKNOWN_PROJECT, "story_id": CHECKOUT, "direction": "affects"},
        "project_not_found",
        id="unknown",
    ),
    pytest.param("get_story_brief", {"project_id": ALPHA, "story_id": "STORY::NOPE"}, "story_not_found", id="story"),
    pytest.param(
        "trace",
        {"project_id": ALPHA, "story_id": "STORY::NOPE", "direction": "affects"},
        "story_not_found",
        id="trace-story",
    ),
    pytest.param("get_node", {"project_id": ALPHA, "spec_id": "FEAT::NOPE"}, "node_not_found", id="node"),
    pytest.param("trace", {"project_id": ALPHA, "direction": "affects"}, "invalid_arguments", id="trace-neither"),
    pytest.param(
        "trace",
        {"project_id": ALPHA, "direction": "affects", "story_id": CHECKOUT, "spec_ids": ["A"]},
        "invalid_arguments",
        id="trace-both",
    ),
    pytest.param(
        "trace",
        {"project_id": ALPHA, "direction": "affects", "story_id": CHECKOUT, "relations": ["NOPE"]},
        "unknown_relation",
        id="relation",
    ),
    pytest.param(
        "search_spec", {"project_id": ALPHA, "query": "x", "labels": ["Nope"]}, "invalid_arguments", id="label"
    ),
    pytest.param("search_spec", {"project_id": ALPHA, "query": "x", "near": "NOPE"}, "invalid_arguments", id="near"),
    pytest.param("similar", {"project_id": ALPHA}, "invalid_arguments", id="similar-neither"),
    pytest.param("similar", {"project_id": ALPHA, "story_id": "STORY::NOPE"}, "story_not_found", id="similar-story"),
    pytest.param("get_evidence", {"project_id": ALPHA, "spec_id": "FEAT::NOPE"}, "node_not_found", id="evidence"),
    pytest.param(
        "path", {"project_id": ALPHA, "from": "FEAT::NOPE", "to": "FEAT::CHECKOUT"}, "node_not_found", id="path"
    ),
    pytest.param("expand_brief", {"project_id": ALPHA, "story_id": "STORY::NOPE"}, "story_not_found", id="expand"),
    pytest.param(
        "expand_brief", {"project_id": FOREIGN, "story_id": CHECKOUT}, "project_not_found", id="expand-foreign"
    ),
]


@pytest.mark.parametrize(("tool", "arguments", "code"), ERROR_CASES)
async def test_an_api_error_is_the_fixed_mcp_message_for_its_code_and_none_of_the_api_text(
    monkeypatch, valid_env, tool, arguments, code
):
    async with over_spec(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        result = await call(session, tool, **arguments)

    assert result.is_error
    assert structured(result) == {"error": {"code": code, "message": MESSAGES[code]}}  # no details key
    assert text(result) == json.dumps(structured(result), separators=(",", ":"))


@pytest.mark.parametrize(
    ("tool", "arguments", "has_plan", "code"),
    [
        ("get_story_brief", CALLS["get_story_brief"], False, "no_plan"),
        ("trace", {"project_id": ALPHA, "story_id": CHECKOUT, "direction": "affects"}, False, "no_plan"),
        ("similar", {"project_id": ALPHA, "story_id": CHECKOUT}, False, "no_plan"),
        ("expand_brief", {"project_id": ALPHA, "story_id": CHECKOUT}, False, "no_plan"),
    ],
)
async def test_a_story_tool_on_a_project_without_a_plan_is_no_plan(
    monkeypatch, valid_env, *, tool, arguments, has_plan, code
):
    async with (
        over_spec(monkeypatch, valid_env, has_plan=has_plan) as stack,
        sdk_session(stack.app(), ALICE) as session,
    ):
        result = await call(session, tool, **arguments)

    assert result.is_error and structured(result)["error"]["code"] == code


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (InfrastructureUnavailableError("neo4j_unreachable"), "graph_unavailable"),
        (ConfigurationError("unknown_label", label="X"), "internal_error"),
    ],
)
@pytest.mark.parametrize("tool", CALLS)
async def test_a_graph_outage_and_an_api_bug_keep_their_codes_through_the_router(
    monkeypatch, valid_env, tool, error, code
):
    async with over_spec(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        failing_reads(stack.spec, error)  # every library call below the route raises
        result = await call(session, tool, **CALLS[tool])

    assert result.is_error and structured(result) == {"error": {"code": code, "message": MESSAGES[code]}}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_story_brief", {"project_id": ALPHA, "story_id": CHECKOUT, "neighbours": 101}),
        ("get_story_brief", {"project_id": ALPHA}),  # story_id missing
        ("trace", {"project_id": ALPHA, "story_id": CHECKOUT}),  # direction missing
        ("trace", {"project_id": ALPHA, "story_id": CHECKOUT, "direction": "affects", "depth": 4}),
        ("trace", {"project_id": ALPHA, "story_id": CHECKOUT, "direction": "affects", "limit": 201}),
        ("trace", {"project_id": ALPHA, "spec_ids": [], "direction": "affects"}),
        ("trace", {"project_id": ALPHA, "spec_ids": [f"A{n}" for n in range(21)], "direction": "affects"}),
        ("trace", {"project_id": ALPHA, "story_id": CHECKOUT, "direction": "affects", "basis": "all"}),
        ("get_node", {"project_id": ALPHA, "spec_id": "X", "edge_limit": 51}),
        ("get_node", {"project_id": ALPHA, "spec_id": "X", "include": ["history"]}),
        ("get_node", {"project_id": ALPHA, "spec_id": "X", "include": []}),
        ("get_node", {"project_id": ALPHA, "spec_id": ""}),
        ("trace", {"project_id": ALPHA, "story_id": CHECKOUT, "direction": "affects", "relations": ["R"] * 51}),
        ("path", {"project_id": ALPHA, "from": "X" * 201, "to": "Y"}),
        ("search_spec", {"project_id": ALPHA, "query": ""}),
        ("search_spec", {"project_id": ALPHA, "query": "x" * 501}),
        ("search_spec", {"project_id": ALPHA, "query": "x", "limit": 21}),
        ("similar", {"project_id": ALPHA, "story_id": CHECKOUT, "limit": 21}),
        ("similar", {"project_id": ALPHA, "story_id": CHECKOUT, "min_score": 1.5}),
        ("similar", {"project_id": ALPHA, "spec_ids": []}),
        ("get_evidence", {"project_id": ALPHA, "spec_id": "X", "context_lines": 6}),
        ("path", {"project_id": ALPHA, "from": "X"}),  # to missing
        ("path", {"project_id": ALPHA, "from": "X", "to": "Y", "max_hops": 7}),
        ("expand_brief", {"project_id": ALPHA, "story_id": CHECKOUT, "budget_tokens": 499}),
        ("expand_brief", {"project_id": ALPHA, "story_id": CHECKOUT, "budget_tokens": 8001}),
    ],
)
async def test_an_input_schema_refusal_makes_no_api_call_and_writes_no_audit_event(
    monkeypatch, valid_env, mcp_log, tool, arguments
):
    async with over_spec(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        result = await call(session, tool, **arguments)

    assert result.is_error
    assert stack.api.agent_calls() == []
    assert completed(mcp_log) == []


async def test_the_input_schemas_publish_the_bounds_the_routes_enforce(monkeypatch, valid_env):
    async with over_spec(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        schemas = {t.name: t.input_schema for t in (await session.list_tools()).tools}

    trace = schemas["trace"]["properties"]
    assert schemas["trace"]["required"] == ["project_id", "direction"]
    assert (trace["depth"]["minimum"], trace["depth"]["maximum"], trace["depth"]["default"]) == (1, 3, 2)
    assert (trace["limit"]["minimum"], trace["limit"]["maximum"], trace["limit"]["default"]) == (1, 200, 100)
    assert trace["direction"]["enum"] == ["based_on", "affects"] and trace["basis"]["default"] == "trusted"
    assert trace["include_owner"]["default"] is True
    ids = next(branch for branch in trace["spec_ids"]["anyOf"] if branch.get("type") == "array")
    assert (ids["minItems"], ids["maxItems"]) == (1, 20)
    node = schemas["get_node"]["properties"]
    assert schemas["get_node"]["required"] == ["project_id", "spec_id"]
    assert (node["edge_limit"]["minimum"], node["edge_limit"]["maximum"], node["edge_limit"]["default"]) == (1, 50, 25)
    assert node["include"]["items"]["enum"] == ["edges", "plan", "provenance", "findings"]
    assert (
        node["include"]["default"] == ["edges", "plan", "provenance", "findings"] and node["include"]["minItems"] == 1
    )
    assert (node["spec_id"]["minLength"], node["spec_id"]["maxLength"]) == (1, 200)
    search = schemas["search_spec"]["properties"]
    assert schemas["search_spec"]["required"] == ["project_id", "query"]
    assert (search["query"]["minLength"], search["query"]["maxLength"]) == (1, 500)
    assert (search["limit"]["minimum"], search["limit"]["maximum"], search["limit"]["default"]) == (1, 20, 10)
    brief = schemas["get_story_brief"]["properties"]
    assert (brief["neighbours"]["minimum"], brief["neighbours"]["maximum"], brief["neighbours"]["default"]) == (
        0,
        100,
        30,
    )
    similar = schemas["similar"]["properties"]
    assert schemas["similar"]["required"] == ["project_id"]
    assert (similar["limit"]["minimum"], similar["limit"]["maximum"], similar["limit"]["default"]) == (1, 20, 5)
    assert (similar["min_score"]["minimum"], similar["min_score"]["maximum"], similar["min_score"]["default"]) == (
        0,
        1,
        0.75,
    )
    assert similar["exclude_linked"]["default"] is True
    evidence = schemas["get_evidence"]["properties"]["context_lines"]
    assert schemas["get_evidence"]["required"] == ["project_id", "spec_id"]
    assert (evidence["minimum"], evidence["maximum"], evidence["default"]) == (0, 5, 2)
    path = schemas["path"]
    assert path["required"] == ["project_id", "from", "to"] and list(path["properties"]) == [
        "project_id",
        "from",
        "to",
        "max_hops",
    ]
    hops = path["properties"]["max_hops"]
    assert (hops["minimum"], hops["maximum"], hops["default"]) == (1, 6, 4)
    budget = schemas["expand_brief"]["properties"]["budget_tokens"]
    assert schemas["expand_brief"]["required"] == ["project_id", "story_id"]
    assert (budget["minimum"], budget["maximum"], budget["default"]) == (500, 8000, 3000)
    for name in CALLS:
        project_id = schemas[name]["properties"]["project_id"]
        assert project_id["type"] == "string" and not {"format", "pattern", "maxLength", "minLength"} & set(project_id)
