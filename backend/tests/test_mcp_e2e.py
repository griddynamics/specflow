"""End-to-end scenarios E1 to E14 of the MCP server against the local stack (docs/mcp-server/testing.md).

Gated: ``SPECFLOW_TEST_MCP_E2E=1`` (run with ``--env-file ../.env.local``), with the stack up and seeded:

    make up WITH=keycloak && (cd backend && uv run --env-file ../.env.local python examples/mcp_qa.py seed)
    SPECFLOW_TEST_MCP_E2E=1 uv run --env-file ../.env.local pytest tests/test_mcp_e2e.py -q

The tier detects the stack mode from the protected resource metadata: E1 to E12 and E14 run in ``oidc`` mode only,
E13 in dev mode only. Scenarios of the other mode are not collected (no skip, no empty parameter set). Under
``make check`` nothing here does I/O: the module marker skips every test and the mode is not detected.
Each run writes a report to ``.reports/mcp-qa/`` (never committed); E8 restores the membership it removes.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Final

import pytest
import pytest_asyncio
from pydantic import SecretStr

from logger.logger import INFO, get_logger, log_event
from mcp_qa_lib import (
    ISSUER,
    LOCAL_DEFAULTS,
    MCP_RESOURCE_URL,
    PRM_URL,
    QA_USERS,
    FixtureIds,
    RunReport,
    StackMode,
    ToolOutcome,
    all_projects,
    call,
    create_project_rest,
    dev_token,
    fetch_prm,
    mcp_session,
    new_run_id,
    oauth_provider,
    post_mcp,
    postgres_session,
    remove_member,
    require_env,
    restore_member,
    run_latency_into,
    run_scenario,
    seeded_ids,
    stack_mode,
    user_token,
    write_report,
)
from specflow_live import live_enabled, requires_live

pytestmark = requires_live("MCP_E2E")

logger = get_logger("tests.mcp_e2e")

OIDC_ENV: Final = ("KEYCLOAK_QA_PASSWORD", "KEYCLOAK_ADMIN_PASSWORD", "KEYCLOAK_WEB_CLIENT_SECRET", "DATABASE_PASSWORD")
DEV_ENV: Final = ("AUTH_DEV_SIGNING_KEY",)
LATENCY_CALLS: Final = 600
LATENCY_P95_LIMIT_MS: Final = 500
FEATURES: Final = 7
PAGE: Final = 5


@functools.cache
def detected_mode() -> StackMode:
    """The mode of the running stack; not detected (no I/O) unless the tier is enabled."""

    return asyncio.run(stack_mode()) if live_enabled("MCP_E2E") else "oidc"


@dataclass
class E2e:
    """What a scenario needs: the mode, the QA secrets of that mode and the run report."""

    mode: StackMode
    env: dict[str, SecretStr]
    report: RunReport

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

    @staticmethod
    async def ids() -> FixtureIds:
        """Read the ids of the seeded fixture (a Keycloak recreate changes them: never remember them)."""

        return await seeded_ids("keycloak")


@pytest.fixture(scope="module")
def report() -> Iterator[RunReport]:
    mode = detected_mode()
    run = RunReport(new_run_id(), f"e2e-{mode}", [], {"mode": mode})
    yield run
    path = write_report(run)
    log_event(logger, INFO, "mcp_e2e.report", report=path.name, scenarios=len(run.results))


@pytest_asyncio.fixture
async def e2e(report: RunReport, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[E2e]:
    mode = detected_mode()
    for name, value in LOCAL_DEFAULTS.items():
        if not os.environ.get(name, "").strip():
            monkeypatch.setenv(name, value)
    try:
        env = require_env(*(OIDC_ENV if mode == "oidc" else DEV_ENV))
    except SystemExit as missing:
        pytest.fail(str(missing), pytrace=False)
    yield E2e(mode, env, report)


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
    alpha = (await e2e.ids()).projects["P-Alpha"]
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
    beta = (await e2e.ids()).projects["P-Beta"]
    async with e2e.alice() as session:
        assert error_of(await features(session, beta))["code"] == "project_not_found"


async def e5(e2e: E2e) -> None:
    beta = (await e2e.ids()).projects["P-Beta"]
    async with e2e.alice() as session:
        foreign = error_of(await features(session, beta))
        unknown = error_of(await features(session, uuid.uuid4()))
    assert unknown["code"] == "project_not_found"
    assert unknown["message"] == foreign["message"]


async def e6(e2e: E2e) -> None:
    empty = (await e2e.ids()).projects["P-Empty"]
    async with e2e.alice() as session:
        outcome = await features(session, empty)
    assert (outcome.is_error, outcome.data["total_count"], outcome.data["items"]) == (False, 0, [])
    assert outcome.data["next_cursor"] is None


async def e7(e2e: E2e) -> None:
    async with e2e.session("bob") as session:
        assert {project["name"] for project in await all_projects(session)} == {"P-Beta"}


async def e8(e2e: E2e) -> None:
    ids = await e2e.ids()
    alpha, alice = ids.projects["P-Alpha"], ids.users["alice"]
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
    alpha = (await e2e.ids()).projects["P-Alpha"]
    forged = base64.urlsafe_b64encode(json.dumps({"v": 2, "after": "FEAT::X"}).encode()).rstrip(b"=").decode()
    async with e2e.alice() as session:
        assert error_of(await features(session, alpha, cursor=forged))["code"] == "invalid_cursor"
        assert error_of(await features(session, alpha, cursor="not*base64!"))["code"] == "invalid_cursor"


async def e12(e2e: E2e) -> None:
    assert (await post_mcp(await e2e.token("alice"), host="attacker.example")).status_code == 421


async def e14(e2e: E2e) -> None:
    large = (await e2e.ids()).projects["P-Large"]
    token = await e2e.token("alice")  # fresh: the 300 s life outlasts the 60 s run

    async with mcp_session(token=token) as session:

        async def once() -> bool:
            return not (await features(session, large)).is_error

        stats = await run_latency_into(e2e.report.metrics, once)
    assert (stats.samples, stats.errors) == (LATENCY_CALLS, 0)  # every planned call finished and none errored
    assert stats.p95_ms < LATENCY_P95_LIMIT_MS


async def e13(e2e: E2e) -> None:
    signing_key, email = e2e.env["AUTH_DEV_SIGNING_KEY"], QA_USERS["alice"]
    web = dev_token(email, signing_key, client="web")
    created = await create_project_rest(f"QA-E13-{e2e.report.run_id}", web)
    async with mcp_session(token=dev_token(email, signing_key)) as session:
        assert str(created) in {project["project_id"] for project in await all_projects(session)}
    assert (await post_mcp(web)).status_code == 401


# E8 runs after every reader of P-Alpha; E14 is the last of oidc mode.
ORDER: Final = ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E9", "E10", "E11", "E12", "E8", "E14", "E13")
SCENARIOS: Final[dict[str, tuple[StackMode, Callable[[E2e], Awaitable[None]]]]] = {
    "E1": ("oidc", e1),
    "E2": ("oidc", e2),
    "E3": ("oidc", e3),
    "E4": ("oidc", e4),
    "E5": ("oidc", e5),
    "E6": ("oidc", e6),
    "E7": ("oidc", e7),
    "E8": ("oidc", e8),
    "E9": ("oidc", e9),
    "E10": ("oidc", e10),
    "E11": ("oidc", e11),
    "E12": ("oidc", e12),
    "E13": ("dev", e13),
    "E14": ("oidc", e14),
}


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Collect only the scenarios of the detected mode, in `ORDER`."""

    if "scenario" in metafunc.fixturenames:
        mode = detected_mode()
        metafunc.parametrize("scenario", [name for name in ORDER if SCENARIOS[name][0] == mode])


async def test_the_stack_answers_in_the_mode_it_was_detected_in(e2e: E2e) -> None:
    if e2e.mode == "oidc":
        prm = await fetch_prm()
        assert (prm["resource"], prm["authorization_servers"]) == (MCP_RESOURCE_URL, [ISSUER])
    else:
        assert await stack_mode() == "dev"  # the protected resource metadata answers 404
        response = await post_mcp(None)
        assert response.status_code == 401
        assert "resource_metadata" not in response.headers.get("WWW-Authenticate", "")


async def test_scenario(scenario: str, e2e: E2e) -> None:
    await run_scenario(e2e.report, scenario, SCENARIOS[scenario][1](e2e))
