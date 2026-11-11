"""Agent route failures and logs (docs/mcp-server/architecture.md, Errors; plan 04 Rules; review fixes of M1).

Covers: the ``agent_error`` mapping and its order, what an agent-route bug logs (the class only, never a traceback or
text; REST keeps its traceback), the lazy load's failures (an ontology without ``feature``, bad plan rules, an
unreadable file) answering ``500 internal_error`` while the API keeps serving, and S2: no token, header, email, claim,
caller text or query text in any log record of the agent routes.

Boundaries faked: Postgres (``FakeRepo``), the graph (a scripted executor), the plan (the plan library over memory).
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

import api.agent_reads as agent_reads_module
from agent_contracts import AGENT_ROUTES
from api.errors import ApiError
from api.runtime import agent_error
from graph.core import ConfigurationError, InfrastructureUnavailableError, OntologyError
from graph.plan import PlanRulesError
from logger.logger import get_logger
from specflow_api_agent_support import (
    ALICE_EMAIL,
    ALPHA,
    CALLS,
    PROJECT_ROUTES,
    USER_HEADER,
    agent_env,
    alice,
    events,
    log_haystack,
    mcp_token,
    serve_agent,
    web_alice,
)

NO_FEATURE_ONTOLOGY = types.SimpleNamespace(node_types={"module": 1})


@pytest.fixture
def env(valid_env: dict[str, str]) -> dict[str, str]:
    return agent_env(valid_env)


# --- agent_error: the mapping and its order ---------------------------------------------------------------------


class GraphDownAndOsError(InfrastructureUnavailableError, OSError):
    """An outage that is also an ``OSError`` (``is_database_unreachable`` counts every ``OSError`` as the database)."""


def mapped(error: Exception) -> tuple[int, str] | None:
    result = agent_error(error)
    return None if result is None else (result.status, result.code)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            InfrastructureUnavailableError("neo4j_unreachable"),
            (503, "graph_unavailable"),
            id="graph-outage",
        ),
        pytest.param(
            GraphDownAndOsError("neo4j_unreachable"),
            (503, "graph_unavailable"),
            id="graph-before-database",
        ),
        pytest.param(
            ConnectionRefusedError("[Errno 111]"),
            (503, "database_unavailable"),
            id="connection-refused",
        ),
        pytest.param(TimeoutError("slow"), (503, "database_unavailable"), id="timeout"),
        pytest.param(
            OSError("any os error counts as the database"),
            (503, "database_unavailable"),
            id="os-error",
        ),
        pytest.param(
            OperationalError("select 1", {}, Exception("down")),
            (503, "database_unavailable"),
            id="operational",
        ),
        pytest.param(RuntimeError("a bug"), None, id="bug"),
        pytest.param(ConfigurationError("unknown_label", label="X"), None, id="configuration-bug"),
        pytest.param(OntologyError(["no feature type"]), None, id="ontology-fault"),
        pytest.param(KeyError("x"), None, id="key-error"),
    ],
)
def test_agent_error_maps_infrastructure_failures_and_leaves_a_bug_to_the_generic_500(error, expected):
    assert mapped(error) == expected


def test_a_graph_outage_is_checked_before_is_database_unreachable():
    """Plan 04 Rules: ``InfrastructureUnavailableError`` first (a lower layer's outage that is also an ``OSError``)."""

    both = GraphDownAndOsError("neo4j_unreachable")
    assert isinstance(both, OSError)
    result = agent_error(both)
    assert isinstance(result, ApiError) and (result.status, result.code) == (
        503,
        "graph_unavailable",
    )


def test_the_mapped_error_carries_fixed_text_and_no_exception_text():
    result = agent_error(InfrastructureUnavailableError("secret-host.internal:7687"))
    assert result is not None
    assert "secret-host" not in result.message and not result.details
    assert (
        agent_error(ConnectionRefusedError("password=hunter2")).message
        == "The database is unavailable. Try again later."
    )


async def test_a_graph_outage_through_the_route_is_graph_unavailable_not_database_unavailable(monkeypatch, env):
    async with serve_agent(monkeypatch, env, scenario="graph_down") as rig:
        response = await rig.call("list_features")
    assert (response.status_code, response.json()["error"]["code"]) == (
        503,
        "graph_unavailable",
    )


async def test_an_os_error_from_a_read_is_a_database_outage_by_design(monkeypatch, env):
    """The documented consequence of the order: an ``OSError`` that is not a graph outage maps to the database."""

    async with serve_agent(monkeypatch, env) as rig:
        rig.executor.answers["count_nodes"] = _raising(ConnectionResetError("reset"))
        response = await rig.call("list_features")
    assert (response.status_code, response.json()["error"]["code"]) == (
        503,
        "database_unavailable",
    )


def _raising(error: Exception) -> Any:
    def raise_it(_: Any) -> list[dict[str, Any]]:
        raise error

    return raise_it


# --- what a failure logs ----------------------------------------------------------------------------------------


async def test_an_agent_route_bug_logs_api_request_failed_with_the_class_only(monkeypatch, env, caplog):
    """Review fix 1: no ``exc_info``, no exception text (it can hold feature text) for an agent-route bug."""

    caplog.set_level("DEBUG")
    async with serve_agent(monkeypatch, env, scenario="bug") as rig:
        response = await rig.call("list_features")
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
    (record,) = events(caplog, "api.request.failed")
    assert record.error_type == "ConfigurationError"
    assert record.path == AGENT_ROUTES["list_features"]
    assert not record.exc_info and not record.exc_text
    assert "unknown_label" not in log_haystack(caplog) and "Traceback" not in log_haystack(caplog)


async def test_a_rest_route_bug_keeps_its_traceback(monkeypatch, env, caplog):
    """The REST surface is unchanged: ``api.request.failed`` still carries ``exc_info``."""

    caplog.set_level("DEBUG")
    async with serve_agent(monkeypatch, env) as rig:

        async def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("rest-bug-text")

        monkeypatch.setattr("db.repositories.list_projects", broken)
        response = await rig.client.get("/api/v1/projects", headers={"Authorization": f"Bearer {web_alice()}"})
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
    (record,) = events(caplog, "api.request.failed")
    assert record.error_type == "RuntimeError" and record.exc_info
    assert USER_HEADER not in response.headers


async def test_a_mapped_infrastructure_failure_logs_one_event_with_the_code_and_class_only(monkeypatch, env, caplog):
    caplog.set_level("DEBUG")
    async with serve_agent(monkeypatch, env, scenario="graph_down") as rig:
        await rig.call("list_features")
    (record,) = events(caplog, "api.agent.failed")
    assert (record.code, record.error_type) == (
        "graph_unavailable",
        "InfrastructureUnavailableError",
    )
    assert not record.exc_info
    assert "neo4j_unreachable" not in log_haystack(caplog)


# --- the lazy load fails: 500 internal_error, the API keeps serving -------------------------------------------------


def broken_ontology(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_reads_module.graph_core, "load_ontology", lambda: NO_FEATURE_ONTOLOGY)


def broken_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise PlanRulesError(["the rules file must be a mapping"])

    monkeypatch.setattr(agent_reads_module, "load_plan_rules", refuse)


def unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> Any:
        raise PermissionError(13, "Permission denied", "/etc/specflow/ontology.yaml")

    monkeypatch.setattr(agent_reads_module.graph_core, "load_ontology", refuse)


FAULTS = {
    "ontology-without-a-feature-type": broken_ontology,
    "bad-plan-rules": broken_rules,
    "unreadable-file-oserror": unreadable,
}
READ_ROUTES = (
    "list_features",
    "list_tasks",
    "list_feature_tasks",
    "get_task_path",
    "get_task_brief",
)


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("tool", READ_ROUTES)
async def test_a_failing_lazy_load_is_500_internal_error_never_503_and_never_a_failed_start(
    monkeypatch, env, tool, fault, caplog
):
    """A6 and review fix 2: ontology without ``feature``, bad plan rules, an ``OSError`` reading a file -> 500
    ``internal_error`` with the user-id header and a logged event; not ``database_unavailable``."""

    caplog.set_level("DEBUG")
    FAULTS[fault](monkeypatch)
    async with serve_agent(monkeypatch, env, lazy=True) as rig:  # the app starts although the load would fail
        response = await rig.call(tool)
    assert (response.status_code, response.json()["error"]["code"]) == (
        500,
        "internal_error",
    ), response.text
    assert response.json()["error"]["message"] == "An internal error occurred."
    assert response.headers[USER_HEADER] == rig.alice_id
    (record,) = events(caplog, "api.request.failed")
    assert not record.exc_info
    assert "Permission denied" not in log_haystack(caplog) and "ontology.yaml" not in response.text


@pytest.mark.parametrize("fault", FAULTS)
async def test_the_api_keeps_serving_after_a_failed_load_and_a_failure_is_not_cached(monkeypatch, env, fault):
    """Never fail the API start (A6): probes, REST, ``list_projects`` and the next agent read still answer; once the
    fault is gone the next read loads and succeeds (a failed load is retried, not kept)."""

    async with serve_agent(monkeypatch, env, lazy=True) as rig:
        faulty = pytest.MonkeyPatch()  # the fault alone, so it can be lifted again
        FAULTS[fault](faulty)
        try:
            failed = await rig.call("list_features")
            assert failed.status_code == 500 and rig.runtime.agent_reads is None
            health = await rig.client.get("/healthz")
            projects = await rig.call("list_projects")
            rest = await rig.client.get("/api/v1/projects", headers={"Authorization": f"Bearer {web_alice()}"})
            assert (health.status_code, projects.status_code, rest.status_code) == (
                200,
                200,
                200,
            )
        finally:
            faulty.undo()
        again = await rig.call("list_features")
    assert again.status_code == 200, again.text
    assert [i["spec_id"] for i in again.json()["items"]] == [
        "FEAT::F1",
        "FEAT::F2",
    ]  # page one (limit 2)
    assert rig.runtime.agent_reads is not None


@pytest.mark.parametrize("tool", ["list_projects"])
async def test_list_projects_needs_no_ontology_and_ignores_a_broken_one(monkeypatch, env, tool):
    broken_ontology(monkeypatch)
    async with serve_agent(monkeypatch, env, lazy=True) as rig:
        response = await rig.call(tool)
    assert response.status_code == 200 and rig.runtime.agent_reads is None


# --- S2: nothing the caller or the token holds reaches a log ------------------------------------------------------

MARKERS = {
    "project": "project-marker-3b7d",
    "cursor": "cursor-marker-71aa",
    "task": "TASK::marker-88b1",
    "feature": "FEAT::marker-0c52",
    "status": "status-marker-6e19",
    "claim_name": "Claim-Marker-5c2e",
    "claim_extra": "note-marker-1d9f",
    "other_email": "carol@other.example",
}


def rich_token(email: str = ALICE_EMAIL, **over: Any) -> str:
    return mcp_token(email, name=MARKERS["claim_name"], extra_note=MARKERS["claim_extra"], **over)


async def traffic(rig: Any) -> list[int]:
    """Every route with caller text in every argument, rejected tokens included; the statuses answered."""

    good = rich_token()
    cases = [
        (good, over)
        for over in (
            {},
            {"project_id": MARKERS["project"]},
            {"cursor": MARKERS["cursor"]},
            {"task_id": MARKERS["task"], "feature_id": MARKERS["feature"]},
            {"status": MARKERS["status"]},
            {"limit": 0},
        )
    ] + [  # 401 (none), 401 (web), 403 (domain), 401 (audience)
        (raw, {})
        for raw in (
            None,
            web_alice(),
            rich_token(MARKERS["other_email"]),
            rich_token(aud="https://other.example/mcp"),
        )
    ]
    return [(await rig.call(name, raw, **over)).status_code for name in CALLS for raw, over in cases]


def leaks(haystack: str, token: str) -> list[str]:
    head, payload, signature = token.split(".")
    secrets = [
        token,
        head,
        payload,
        signature,
        "Bearer ",
        ALICE_EMAIL,
        *MARKERS.values(),
    ]
    return [secret for secret in secrets if secret in haystack]


@pytest.mark.parametrize("scenario", ["ok", "graph_down", "bug", "no_plan"])
async def test_s2_no_token_header_email_claim_or_caller_text_in_any_log_record_of_the_agent_routes(
    monkeypatch, env, caplog, scenario
):
    """S2: at DEBUG, across every route, success and every error path (``httpx``/``httpcore`` records are the test
    client's own request lines and are left out: the product's routes use neither)."""

    caplog.set_level("DEBUG")
    async with serve_agent(monkeypatch, env, scenario=scenario, has_plan=scenario != "no_plan") as rig:
        statuses = await traffic(rig)
    assert {401, 403, 404, 400, 422} <= set(statuses), statuses
    haystack = log_haystack(caplog)
    assert leaks(haystack, alice()) == []
    assert "authorization" not in haystack.lower().replace("www-authenticate", "")
    assert [r.getMessage() for r in caplog.records if r.exc_info] == []  # no traceback from an agent route, bug or not


def test_s2_the_leak_check_finds_what_it_looks_for(caplog):
    """A control: a record that holds a token, the email or caller text is caught by the same check."""

    caplog.set_level("DEBUG")
    get_logger("api.control").warning("seen %s", MARKERS["cursor"], extra={"who": ALICE_EMAIL})
    assert {MARKERS["cursor"], ALICE_EMAIL} <= set(leaks(log_haystack(caplog), alice()))


async def test_s2_a_rejected_token_logs_only_the_reason_status_and_code(monkeypatch, env, caplog):
    caplog.set_level("DEBUG")
    async with serve_agent(monkeypatch, env) as rig:
        await rig.call("list_projects", rich_token(aud="https://other.example/mcp"))
        await rig.call("list_projects", web_alice())
    rejected = events(caplog, "auth.token.rejected")
    assert [(r.reason, r.status, r.code) for r in rejected] == [("audience", 401, "invalid_token")] * 2
    assert ALICE_EMAIL not in log_haystack(caplog)


async def test_s2_the_project_routes_log_nothing_of_a_foreign_denial(monkeypatch, env, caplog):
    """A denial (foreign or malformed id) is not an event: it is the same quiet 404 as an unknown project."""

    caplog.set_level("DEBUG")
    async with serve_agent(monkeypatch, env) as rig:
        for tool in PROJECT_ROUTES:
            await rig.call(tool, alice(), project_id=MARKERS["project"])
            await rig.call(tool, alice(), project_id="00000000-0000-4000-8000-000000000004")  # bob's
    haystack = log_haystack(caplog)
    assert MARKERS["project"] not in haystack and "00000000-0000-4000-8000-000000000004" not in haystack
    assert ALPHA not in haystack
