"""End-to-end scenarios E1 to E17 of the MCP server against the local stack (docs/mcp-server/testing.md).

Gated: ``SPECFLOW_TEST_MCP_E2E=1`` (run with ``--env-file ../.env.local``), with the stack up and seeded:

    make up && (cd backend && uv run --env-file ../.env.local python examples/mcp_qa.py seed)
    SPECFLOW_TEST_MCP_E2E=1 uv run --env-file ../.env.local pytest tests/test_mcp_e2e.py -q

Every project-scoped scenario reads its project id from a binding file the harness wrote in a temp workspace
(``E2e.bind``): written, found from a subfolder up, validated against the contract schema, then read.
Every scenario signs in through the local Keycloak; E13 checks that the API (web token) and the MCP server (MCP
token) see the same subject for the same user. Under ``make check`` nothing here does I/O: the module marker skips
every test.
Each run writes a report to ``.reports/mcp-qa/`` (never committed); E8 restores the membership it removes.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import jwt
import pytest
import pytest_asyncio
from pydantic import SecretStr

from logger.logger import INFO, get_logger, log_event
from mcp_qa_lib import (
    ISSUER,
    LOCAL_DEFAULTS,
    MCP_RESOURCE_URL,
    PRM_URL,
    QA_USER_IDS,
    FixtureIds,
    RunReport,
    ToolOutcome,
    all_projects,
    binding_text,
    bound_project_id,
    call,
    create_project_rest,
    fetch_prm,
    find_binding,
    make_workspace,
    mcp_session,
    new_run_id,
    oauth_provider,
    post_mcp,
    postgres_session,
    prompt_text,
    read_binding,
    remove_member,
    require_env,
    restore_member,
    run_latency_into,
    run_scenario,
    seeded_ids,
    user_token,
    write_binding,
    write_report,
)
from specflow_live import requires_live

pytestmark = requires_live("MCP_E2E")

logger = get_logger("tests.mcp_e2e")

STACK_ENV: Final = ("KEYCLOAK_QA_PASSWORD", "KEYCLOAK_WEB_CLIENT_SECRET", "DATABASE_PASSWORD")
LATENCY_CALLS: Final = 600
LATENCY_P95_LIMIT_MS: Final = 500
FEATURES: Final = 7
PAGE: Final = 5
BINDING_NAME: Final = ".specflow/config.json"
INSTRUCTIONS_LIMIT: Final = 2048
PROJECTS_BY_NAME: Final = ("P-Alpha", "P-Beta", "P-Empty", "P-Large")


@dataclass
class E2e:
    """What a scenario needs: the QA secrets, the run report and a temporary workspace."""

    env: dict[str, SecretStr]
    report: RunReport
    workspace: Path

    @property
    def password(self) -> SecretStr:
        return self.env["KEYCLOAK_QA_PASSWORD"]

    def alice(self) -> AbstractAsyncContextManager[Any]:
        return self.session("alice")

    def session(self, key: str) -> AbstractAsyncContextManager[Any]:
        """An SDK session signed in through the OAuth provider (pre-registered client, no registration request)."""

        return mcp_session(auth=oauth_provider(key, self.password))

    async def token(self, key: str, **kwargs: Any) -> str:
        return await user_token(key, self.password, **kwargs)

    @property
    def subfolder(self) -> Path:
        """Where the scenario "works": below the workspace root, so reading the file walks up."""

        return self.workspace / "src" / "app"

    def bind_id(self, project_id: uuid.UUID, project_name: str) -> uuid.UUID:
        """Write (or replace) the binding file; return the id read back from the subfolder (find, validate, read)."""

        write_binding(self.workspace, project_id, project_name)
        self.subfolder.mkdir(parents=True, exist_ok=True)
        return bound_project_id(self.subfolder)

    async def bind(self, project: str) -> uuid.UUID:
        """Bind the workspace to the seeded project of this name (`P-Alpha`) and return the id the file gives."""

        return self.bind_id((await self.ids()).projects[project], project)

    @staticmethod
    async def ids() -> FixtureIds:
        """Read the ids of the seeded fixture."""

        return await seeded_ids()


@pytest.fixture(scope="module")
def report() -> Iterator[RunReport]:
    run = RunReport(new_run_id(), "e2e", [], {})
    yield run
    path = write_report(run)
    log_event(logger, INFO, "mcp_e2e.report", report=path.name, scenarios=len(run.results))


@pytest_asyncio.fixture
async def e2e(report: RunReport, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[E2e]:
    for name, value in LOCAL_DEFAULTS.items():
        if not os.environ.get(name, "").strip():
            monkeypatch.setenv(name, value)
    try:
        env = require_env(*STACK_ENV)
    except SystemExit as missing:
        pytest.fail(str(missing), pytrace=False)
    workspace = make_workspace()
    try:
        yield E2e(env, report, workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def error_of(outcome: ToolOutcome) -> dict[str, Any]:
    assert outcome.is_error, "the call was expected to fail"
    error = outcome.data["error"]
    assert isinstance(error, dict)
    return error


def feature_ids(outcome: ToolOutcome) -> list[str]:
    assert not outcome.is_error, outcome.code
    return [str(item["spec_id"]) for item in outcome.data["items"]]


async def features(session: Any, project_id: uuid.UUID, **arguments: Any) -> ToolOutcome:
    return await call(session, "list_features", {"project_id": str(project_id), **arguments})


async def e1(_e2e: E2e) -> None:
    response = await post_mcp(None)
    assert response.status_code == 401
    assert f'resource_metadata="{PRM_URL}"' in response.headers["WWW-Authenticate"]
    prm = await fetch_prm()
    assert prm["resource"] == MCP_RESOURCE_URL
    assert prm["authorization_servers"] == [ISSUER]


async def e2(e2e: E2e) -> None:
    async with e2e.alice() as session:
        names = {project["name"] for project in await all_projects(session)}
    assert {"P-Alpha", "P-Empty", "P-Large"} <= names
    assert "P-Beta" not in names


async def e3(e2e: E2e) -> None:
    alpha = await e2e.bind("P-Alpha")
    async with e2e.alice() as session:
        first = await features(session, alpha, limit=PAGE)
        assert (first.data["total_count"], len(first.data["items"])) == (FEATURES, PAGE)
        assert first.data["next_cursor"]
        second = await features(session, alpha, limit=PAGE, cursor=first.data["next_cursor"])
    assert (second.data["total_count"], len(second.data["items"]), second.data["next_cursor"]) == (FEATURES, 2, None)
    seen = feature_ids(first) + feature_ids(second)
    assert len(set(seen)) == FEATURES
    assert all(spec_id.startswith("FEAT::") for spec_id in seen)


async def e4(e2e: E2e) -> None:
    beta = await e2e.bind("P-Beta")
    async with e2e.alice() as session:
        assert error_of(await features(session, beta))["code"] == "project_not_found"


async def e5(e2e: E2e) -> None:
    beta = await e2e.bind("P-Beta")
    async with e2e.alice() as session:
        foreign = error_of(await features(session, beta))
        unknown = error_of(await features(session, e2e.bind_id(uuid.uuid4(), "Unknown")))
    assert unknown["code"] == "project_not_found"
    assert unknown["message"] == foreign["message"]


async def e6(e2e: E2e) -> None:
    empty = await e2e.bind("P-Empty")
    async with e2e.alice() as session:
        outcome = await features(session, empty)
    assert (outcome.is_error, outcome.data["total_count"], outcome.data["items"]) == (False, 0, [])
    assert outcome.data["next_cursor"] is None


async def e7(e2e: E2e) -> None:
    async with e2e.session("bob") as session:
        assert {project["name"] for project in await all_projects(session)} == {"P-Beta"}


async def e8(e2e: E2e) -> None:
    alice = (await e2e.ids()).users["alice"]
    alpha = await e2e.bind("P-Alpha")
    try:
        async with e2e.alice() as session:
            assert not (await features(session, alpha)).is_error
            async with postgres_session() as database:
                assert await remove_member(database, alpha, alice)
            assert error_of(await features(session, alpha))["code"] == "project_not_found"
    finally:
        async with postgres_session() as database:  # idempotent: also repairs a half-done removal
            await restore_member(database, alpha, alice)
    async with e2e.alice() as session:
        assert (await features(session, alpha)).data["total_count"] == FEATURES


async def e9(e2e: E2e) -> None:
    web = await e2e.token("alice", client="web", web_secret=e2e.env["KEYCLOAK_WEB_CLIENT_SECRET"])
    assert (await post_mcp(web)).status_code == 401


async def e10(e2e: E2e) -> None:
    response = await post_mcp(await e2e.token("carol"))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "domain_not_allowed"
    assert "WWW-Authenticate" not in response.headers


async def e11(e2e: E2e) -> None:
    alpha = await e2e.bind("P-Alpha")
    forged = base64.urlsafe_b64encode(json.dumps({"v": 2, "after": "FEAT::X"}).encode()).rstrip(b"=").decode()
    async with e2e.alice() as session:
        assert error_of(await features(session, alpha, cursor=forged))["code"] == "invalid_cursor"
        assert error_of(await features(session, alpha, cursor="not*base64!"))["code"] == "invalid_cursor"


async def e12(e2e: E2e) -> None:
    assert (await post_mcp(await e2e.token("alice"), host="attacker.example")).status_code == 421


async def e14(e2e: E2e) -> None:
    large = await e2e.bind("P-Large")
    token = await e2e.token("alice")  # fresh: the 300 s life outlasts the 60 s run

    async with mcp_session(token=token) as session:

        async def once() -> bool:
            return not (await features(session, large)).is_error

        stats = await run_latency_into(e2e.report.metrics, once)
    assert (stats.samples, stats.errors) == (LATENCY_CALLS, 0)  # every planned call finished and none errored
    assert stats.p95_ms < LATENCY_P95_LIMIT_MS


def subject_of(token: str) -> str:
    """The `sub` claim of a token this run just received from Keycloak (read, not verified: the servers verify)."""

    return str(jwt.decode(token, options={"verify_signature": False})["sub"])


async def e13(e2e: E2e) -> None:
    web = await e2e.token("alice", client="web", web_secret=e2e.env["KEYCLOAK_WEB_CLIENT_SECRET"])
    mcp = await e2e.token("alice")
    assert subject_of(web) == subject_of(mcp) == QA_USER_IDS["alice"]  # one Keycloak user behind both clients
    created = await create_project_rest(f"QA-E13-{e2e.report.run_id}", web)
    async with mcp_session(token=mcp) as session:
        assert str(created) in {project["project_id"] for project in await all_projects(session)}
    assert (await post_mcp(web)).status_code == 401


async def e15(e2e: E2e) -> None:
    ids = await e2e.ids()
    async with e2e.alice() as session:
        text = session.instructions or ""
    assert MCP_RESOURCE_URL in text and BINDING_NAME in text
    assert len(text) < INSTRUCTIONS_LIMIT
    seeded = [*PROJECTS_BY_NAME, *(str(project_id) for project_id in ids.projects.values())]
    assert not [value for value in seeded if value.lower() in text.lower()], "the instructions carry project data"


async def e16(e2e: E2e) -> None:
    template = binding_text("<id>", "<name>")
    async with e2e.alice() as session:
        prompts = {prompt.name: prompt for prompt in (await session.list_prompts()).prompts}
        assert "select_project" in prompts
        assert [(arg.name, bool(arg.required)) for arg in prompts["select_project"].arguments or []] == [
            ("project", False)
        ]
        plain = prompt_text(await session.get_prompt("select_project"))
        named = prompt_text(await session.get_prompt("select_project", {"project": "P-Alpha"}))
    for text in (plain, named):
        assert BINDING_NAME in text and template in text and "list_projects" in text
    assert json.dumps("P-Alpha") in named and json.dumps("P-Alpha") not in plain
    for method, params in (("prompts/list", {}), ("prompts/get", {"name": "select_project"})):
        response = await post_mcp(None, method=method, params=params)
        assert response.status_code == 401, method
        assert f'resource_metadata="{PRM_URL}"' in response.headers["WWW-Authenticate"]


async def e17(e2e: E2e) -> None:
    alpha = await e2e.bind("P-Alpha")
    async with e2e.alice() as session:
        assert (await features(session, alpha)).data["total_count"] == FEATURES
        empty = await e2e.bind("P-Empty")  # selecting again replaces the whole file
        bound = read_binding(find_binding(e2e.subfolder) or e2e.workspace)
        assert bound == json.loads(binding_text(empty, "P-Empty")) and uuid.UUID(bound["project_id"]) != alpha
        assert (await features(session, empty)).data["total_count"] == 0
        foreign = await e2e.bind("P-Beta")
        assert error_of(await features(session, foreign))["code"] == "project_not_found"


# E8 runs after every reader of P-Alpha; E14 and E13 come last.
ORDER: Final = (
    *("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E9", "E10", "E11", "E12", "E15", "E16", "E17"),
    *("E8", "E14", "E13"),
)
SCENARIOS: Final[dict[str, Callable[[E2e], Awaitable[None]]]] = {
    "E1": e1,
    "E2": e2,
    "E3": e3,
    "E4": e4,
    "E5": e5,
    "E6": e6,
    "E7": e7,
    "E8": e8,
    "E9": e9,
    "E10": e10,
    "E11": e11,
    "E12": e12,
    "E13": e13,
    "E14": e14,
    "E15": e15,
    "E16": e16,
    "E17": e17,
}


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Collect the scenarios in `ORDER`."""

    if "scenario" in metafunc.fixturenames:
        metafunc.parametrize("scenario", list(ORDER))


async def test_the_stack_answers_with_the_resource_metadata() -> None:
    prm = await fetch_prm()
    assert (prm["resource"], prm["authorization_servers"]) == (MCP_RESOURCE_URL, [ISSUER])


async def test_scenario(scenario: str, e2e: E2e) -> None:
    await run_scenario(e2e.report, scenario, SCENARIOS[scenario](e2e))
