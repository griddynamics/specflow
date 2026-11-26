"""The backend-double seam of the golden gate of the MCP tooling tools (``test_mcp_golden_tooling.py``, D29).

Like ``mcp_golden_seam`` (the gate of the MCP split, whose goldens stay frozen) but behind the router is the API over
the shop graph of ``spec_reads_support``: the real readers and libraries over a fixture graph, a plan, findings and the
project's members. The golden tests import only ``tooling_double`` and ``BackendDouble.serve(scenario)``.

Scenarios: ``ok``, ``no_plan``, ``graph_down``, ``database_down``, ``bug``; and, with the plan in the in-memory
executor so the story calls can write: ``free`` (as it is), ``claimed`` (Checkout held by Ben), ``applying`` (a plan
persist running), ``held_applying`` (Checkout held by Alice, token 1, and a plan persist running); ``gaps``: the API
over the shop gap corpus after a gap run and the mini plan of ``gap_agent_support`` (open gaps in briefs,
``list_gaps``, the rows of ``browse_gaps`` and the card of ``open_gap``, which link
to the web app at ``https://app.example.test``); ``jira`` (the shop API with a real ``JiraSyncRunner`` over the fake
Jira, the fake Jira tables and a stub ``sync_once`` that never ends: Alpha has an enabled target and one finished pass
with six items, Beta has no target) and ``jira_off`` (``runtime.jira`` is ``None``); ``linear`` and ``linear_off`` are
their Linear twins (``LinearPlanRunner`` with five items; ``runtime.linear_plan`` is ``None``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from api.jira.runner import JiraRunnerDependencies, JiraSyncRunner
from api.jira.sync import SyncResult
from api.linear_plan.runner import LinearPlanRunner
from api.linear_plan.sync import PushResult
from db.jira import JiraTargetView
from db.models import JiraIssue, LinearIssue
from gap_agent_support import serve_gaps
from graph.core import ConfigurationError, InfrastructureUnavailableError
from jira_db_fakes import FakeJiraDb
from jira_fake_support import FakeJiraGateway
from linear.fake import FakeLinear
from linear_plan_support import FakePlanDb, target_view
from mcp_golden_seam import ALPHA, UNKNOWN_PROJECT
from mcp_server.registry import install
from mcp_support import MCP_URL, build_app, mcp_env, serving
from spec_reads_support import CHECKOUT, serve_spec
from specflow_api_agent_support import agent_env, refuse_database

__all__ = ["ALPHA", "CHECKOUT", "SCENARIOS", "UNKNOWN_PROJECT", "BackendDouble", "tooling_double"]

GAP_RUN_INSTANT = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
JIRA_INSTANT = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
JIRA_SITE = "https://example.atlassian.net"
JIRA_ITEMS = (  # node id, kind, Jira key, removed, error code
    ("EPIC", "epic", "ABC-1", False, None),
    ("STORY::PAYMENTS_SERVICE::CARD_PAYMENT", "story", "ABC-2", False, None),
    ("STORY::WEB_SHOP::CHECKOUT", "story", "ABC-3", False, "jira_invalid_field"),
    ("TASK::WEB_SHOP::CONTRACT_CHECKOUT", "subtask", "ABC-4", False, None),
    ("TASK::WEB_SHOP::LOGIC_CHECKOUT", "subtask", "ABC-5", True, None),
    ("TASK::WEB_SHOP::UI_CHECKOUT", "subtask", None, False, None),
)

LINEAR_INSTANT = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
LINEAR_ITEMS = (  # node id, kind, identifier, removed, error code
    ("STORY::PAYMENTS_SERVICE::CARD_PAYMENT", "story", "ENG-1", False, None),
    ("STORY::WEB_SHOP::CHECKOUT", "story", "ENG-2", False, "linear_invalid"),
    ("TASK::WEB_SHOP::CONTRACT_CHECKOUT", "subtask", "ENG-3", False, None),
    ("TASK::WEB_SHOP::LOGIC_CHECKOUT", "subtask", "ENG-4", True, None),
    ("TASK::WEB_SHOP::UI_CHECKOUT", "subtask", None, False, None),
)

SCENARIOS = (
    "ok",
    "no_plan",
    "graph_down",
    "database_down",
    "bug",
    "free",
    "claimed",
    "applying",
    "held_applying",
    "gaps",
    "jira",
    "jira_off",
    "linear",
    "linear_off",
)
FAILURES: dict[str, Exception] = {
    "graph_down": InfrastructureUnavailableError("neo4j_unreachable"),
    "bug": ConfigurationError("unknown_label", label="X"),
}

WRITABLE: dict[str, str] = {name: name for name in ("free", "claimed", "applying", "held_applying")}

Serve = Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]


@dataclass
class BackendDouble:
    """``serve(scenario)`` yields a client of the running MCP app over the API over the shop graph."""

    serve: Serve


@pytest.fixture
def tooling_double(monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str]) -> BackendDouble:
    @asynccontextmanager
    async def serve(scenario: str = "ok") -> AsyncIterator[httpx.AsyncClient]:
        assert scenario in SCENARIOS, scenario
        if scenario == "gaps":
            # One instant for the whole gap run: the gaps' order and cursors depend on their creation times.
            monkeypatch.setattr("graph.gap.run._now", lambda: GAP_RUN_INSTANT)
            async with (
                serve_gaps(monkeypatch, agent_env(valid_env, PUBLIC_URL="https://app.example.test")) as gaps,
                serving(_mcp_app(valid_env, gaps.rig.app)) as client,
            ):
                yield client
            return
        async with serve_spec(
            monkeypatch,
            agent_env(valid_env),
            has_plan=scenario != "no_plan",
            fail=FAILURES.get(scenario),
            writable=WRITABLE.get(scenario),
        ) as spec:
            if scenario == "jira":
                runner = _jira_runner(spec.rig.runtime, monkeypatch)
                try:
                    async with serving(_mcp_app(valid_env, spec.rig.app)) as client:
                        yield client
                finally:
                    await runner.aclose()
                return
            if scenario == "linear":
                linear = _linear_runner(spec.rig.runtime, monkeypatch)
                try:
                    async with serving(_mcp_app(valid_env, spec.rig.app)) as client:
                        yield client
                finally:
                    await linear.aclose()
                return
            if scenario == "database_down":  # after the rig installed its FakeRepo, which it would replace
                refuse_database(monkeypatch)
            async with serving(_mcp_app(valid_env, spec.rig.app)) as client:
                yield client

    return BackendDouble(serve=serve)


def _mcp_app(valid_env: dict[str, str], api: Any) -> Any:
    """The MCP app whose API calls go to the in-process ``api`` app."""

    transport = httpx.ASGITransport(app=api, raise_app_exceptions=False)

    def tools(server: MCPServer) -> None:
        install(server, resource_url=MCP_URL)

    return build_app(mcp_env(valid_env), transport=transport, tools=tools)


def _jira_runner(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> JiraSyncRunner:
    """Alpha: an enabled target ``ABC`` and one finished pass with the six ``JIRA_ITEMS``; a pass started now never
    ends (the stub waits), so the answer of ``sync_to_jira`` is the same every run. Beta has no target."""

    project_id = uuid.UUID(ALPHA)
    db = FakeJiraDb()
    db.install(monkeypatch)
    db.targets[project_id] = JiraTargetView(
        project_id=project_id,
        project_name="Alpha",
        jira_project="ABC",
        epic_key=None,
        enabled=True,
        updated_at=JIRA_INSTANT,
        last_status="succeeded",
        last_trigger="plan_run",
        last_started_at=JIRA_INSTANT,
        last_finished_at=JIRA_INSTANT,
        last_error_code=None,
        last_counts={"created": 6, "links_added": 2},
    )
    for node_id, kind, key, removed, error_code in JIRA_ITEMS:
        db.issues[project_id, node_id] = JiraIssue(
            project_id=project_id,
            node_id=node_id,
            kind=kind,
            jira_key=key,
            requested_at=JIRA_INSTANT,
            removed_at=JIRA_INSTANT if removed else None,
            error_code=error_code,
        )

    async def never_ends(_project_id: str, _deps: Any) -> SyncResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    runner = JiraSyncRunner(
        FakeJiraGateway(),
        transaction=runtime.transaction,
        store=None,
        public_url=None,
        site_url=JIRA_SITE,
        dependencies=JiraRunnerDependencies(sync_once=never_ends, now=lambda: JIRA_INSTANT),
    )
    runtime.jira = runner
    return runner


def _linear_runner(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> LinearPlanRunner:
    """Alpha: an enabled target in team ``ENG`` and one finished pass with the five ``LINEAR_ITEMS``; a pass started now
    never ends (the stub waits), so the answer of ``sync_to_linear`` is the same every run. Beta has no target."""

    project_id = uuid.UUID(ALPHA)
    db = FakePlanDb()
    db.install(monkeypatch)
    fake = FakeLinear()
    team = fake.add_team("ENG")
    db.targets[project_id] = replace(
        target_view(
            project_id, team, "9d4b2a64-0000-4000-8000-000000000001", url="https://linear.app/acme/project/alpha-1"
        ),
        linear_project_name="Alpha",
        last_status="succeeded",
        last_trigger="plan_run",
        last_started_at=LINEAR_INSTANT,
        last_finished_at=LINEAR_INSTANT,
        last_counts={"created": 5, "relations_added": 2},
    )
    db.items[project_id] = [
        LinearIssue(
            project_id=project_id,
            node_id=node_id,
            kind=kind,
            identifier=identifier,
            requested_at=LINEAR_INSTANT,
            removed_at=LINEAR_INSTANT if removed else None,
            error_code=error_code,
        )
        for node_id, kind, identifier, removed, error_code in LINEAR_ITEMS
    ]

    async def never_ends(_project_id: str, _deps: Any) -> PushResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    @asynccontextmanager
    async def gateway(_user_id: uuid.UUID) -> AsyncIterator[FakeLinear]:
        yield fake

    runner = LinearPlanRunner(
        gateway, transaction=runtime.transaction, graph_store=None, push=never_ends, now=lambda: LINEAR_INSTANT
    )
    runtime.linear_plan = runner
    return runner
