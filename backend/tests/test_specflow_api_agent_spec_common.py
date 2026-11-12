"""What every agent read of the spec graph shares with the other agent routes (``specflow_api_agent_support``):
``503 agent_unavailable`` without MCP settings, the user-id header on every status, the adopted request id, a
malformed project id that runs no query and is never logged, the project row id handed to the libraries, and the
infrastructure codes (graph, database, bug) of each of the eight routes. The shop rig of ``spec_reads_support``.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_contracts import AGENT_ROUTES
from graph.core import ConfigurationError, InfrastructureUnavailableError
from spec_reads_support import CHECKOUT, SpecRig, failing_reads, recording_reads, serve_spec
from specflow_api_agent_support import (
    ALPHA,
    FOREIGN,
    USER_HEADER,
    agent_env,
    alice,
    bearer_header,
    log_haystack,
    mcp_token,
    serve_agent,
)

ROUTES: dict[str, dict[str, Any]] = {
    "get_story_brief": {"story_id": CHECKOUT},
    "trace": {"story_id": CHECKOUT, "direction": "affects"},
    "get_node": {"spec_id": "MOD::STRIPE"},
    "search_spec": {"query": "checkout"},
    "similar": {"story_id": CHECKOUT},
    "get_evidence": {"spec_id": "CON::EU_REGION"},
    "path": {"from": "MOD::PAYMENTS_SERVICE", "to": "MOD::STRIPE"},
    "expand_brief": {"story_id": CHECKOUT},
}
OUT_OF_RANGE: dict[str, dict[str, Any]] = {  # one argument the contract refuses (422) per route
    "get_story_brief": {"neighbours": -1},
    "trace": {"depth": 0},
    "get_node": {"edge_limit": 0},
    "search_spec": {"limit": 0},
    "similar": {"limit": 0},
    "get_evidence": {"context_lines": 6},
    "path": {"max_hops": 0},
    "expand_brief": {"budget_tokens": 1},
}
FAILURES = {
    "graph_down": (InfrastructureUnavailableError("neo4j_unreachable"), 503, "graph_unavailable"),
    "bug": (ConfigurationError("unknown_label", label="X"), 500, "internal_error"),
}


@pytest.fixture
async def spec(monkeypatch, valid_env):
    async with serve_spec(monkeypatch, agent_env(valid_env)) as rig:
        yield rig


async def call(spec: SpecRig, tool: str, headers: dict[str, str] | None = None, **over: Any) -> Any:
    params = {"project_id": ALPHA, **ROUTES[tool], **over}
    return await spec.rig.client.get(
        AGENT_ROUTES[tool], params=params, headers={**bearer_header(alice()), **(headers or {})}
    )


def code_of(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


@pytest.mark.parametrize("tool", ROUTES)
async def test_a_route_answers_503_agent_unavailable_when_the_api_has_no_mcp_settings(monkeypatch, valid_env, tool):
    async with serve_agent(monkeypatch, dict(valid_env)) as rig:
        response = await rig.get(AGENT_ROUTES[tool], mcp_token(), project_id=ALPHA, **ROUTES[tool])

    assert code_of(response) == (503, "agent_unavailable") and USER_HEADER not in response.headers


@pytest.mark.parametrize("tool", ROUTES)
async def test_the_user_id_header_is_on_every_status_of_a_route(spec, tool):
    answers = [
        await call(spec, tool),
        await call(spec, tool, project_id=FOREIGN),
        await call(spec, tool, **OUT_OF_RANGE[tool]),
    ]
    for name in FAILURES:
        failing_reads(spec, FAILURES[name][0])
        answers.append(await call(spec, tool))

    assert [answer.status_code for answer in answers] == [200, 404, 422, 503, 500]
    assert {answer.headers.get(USER_HEADER) for answer in answers} == {str(spec.alice)}


@pytest.mark.parametrize("tool", ROUTES)
async def test_a_safe_inbound_request_id_is_echoed_on_success_and_on_a_mapped_503(spec, tool):
    inbound = {"X-Request-ID": "agent-req-0123456789"}

    ok = await call(spec, tool, inbound)
    failing_reads(spec, FAILURES["graph_down"][0])
    down = await call(spec, tool, inbound)

    assert (ok.status_code, down.status_code) == (200, 503)
    assert ok.headers["x-request-id"] == down.headers["x-request-id"] == inbound["X-Request-ID"]


@pytest.mark.parametrize("tool", ROUTES)
async def test_a_malformed_project_id_runs_no_query_and_is_never_logged(spec, tool, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    asked: list[Any] = []
    original = spec.rig.repo.get_member_project

    async def spy(session: Any, user_id: Any, project_id: Any) -> Any:
        asked.append(project_id)
        return await original(session, user_id, project_id)

    monkeypatch.setattr("api.deps.repo.get_member_project", spy)
    marker = "id-marker-9d41c7/../x"

    response = await call(spec, tool, project_id=marker)

    assert code_of(response) == (404, "project_not_found")
    assert asked == [] and spec.executor.calls == [] and spec.memory.calls == []
    assert "id-marker-9d41c7" not in log_haystack(caplog)


@pytest.mark.parametrize("tool", ROUTES)
async def test_the_libraries_get_the_project_row_id_never_the_argument(spec, tool):
    seen = recording_reads(spec)

    response = await call(spec, tool, project_id=ALPHA.upper())  # the same UUID, another spelling

    assert response.status_code == 200, response.text
    assert seen and set(seen) == {ALPHA}


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("tool", ROUTES)
async def test_a_graph_outage_or_a_bug_below_a_route_keeps_its_code(spec, tool, failure):
    error, status, code = FAILURES[failure]
    failing_reads(spec, error)

    response = await call(spec, tool)

    assert code_of(response) == (status, code)
    assert response.json()["error"]["details"] == {} and "neo4j" not in response.text


@pytest.mark.parametrize("tool", ROUTES)
async def test_a_database_outage_at_the_membership_check_is_database_unavailable(spec, tool, monkeypatch):
    async def refused(*_: Any, **__: Any) -> Any:
        raise ConnectionRefusedError("[Errno 111] Connect call failed")

    monkeypatch.setattr("api.deps.repo.get_member_project", refused)

    response = await call(spec, tool)

    assert code_of(response) == (503, "database_unavailable") and spec.executor.calls == []


LOWER_BOUNDS = [
    ("trace", {"depth": 0}),
    ("trace", {"limit": 0}),
    ("similar", {"limit": 0}),
    ("similar", {"min_score": -0.1}),
    ("path", {"max_hops": 0}),
    ("get_node", {"spec_id": ""}),
    ("get_evidence", {"spec_id": ""}),
    ("get_evidence", {"context_lines": -1}),
    ("search_spec", {"near": ""}),
    ("expand_brief", {"story_id": ""}),
    ("get_story_brief", {"story_id": "S" * 201}),
    ("trace", {"relations": ["PART_OF"] * 51}),
    ("search_spec", {"labels": ["Feature"] * 51}),
]


@pytest.mark.parametrize(("tool", "params"), LOWER_BOUNDS)
async def test_a_route_refuses_an_argument_below_its_bound_before_any_read(spec, tool, params):
    over = {key: value for key, value in params.items() if value is not None}
    base = {key: value for key, value in ROUTES[tool].items() if key not in params}

    response = await spec.rig.client.get(
        AGENT_ROUTES[tool], params={"project_id": ALPHA, **base, **over}, headers=bearer_header(alice())
    )

    assert code_of(response) == (422, "validation_failed"), (tool, params)
    assert spec.executor.calls == [] and spec.memory.calls == []


async def test_a_path_from_a_node_to_itself_has_no_chain(spec):
    response = await call(spec, "path", to="MOD::PAYMENTS_SERVICE")

    assert response.status_code == 200
    assert (response.json()["direction"], response.json()["items"]) == (None, [])
