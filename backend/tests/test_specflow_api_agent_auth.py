"""Agent route auth (docs/mcp-server/auth.md, Token validation and the audience matrix; plan 03 Do 5): each route group
accepts the tokens of its own audience only, in both audience modes.

Modes: ``resource`` (the audience is the MCP resource URL, ``AUTH_TOKEN_TYPE=keycloak``) and ``client`` (the audience
is a client id, ``AUTH_TOKEN_TYPE=claim``). Tokens: web/API, MCP, smoke client, ID token, tampered type claim,
expired, missing. Routes: one REST route (``GET /api/v1/projects``) and one agent route
(``GET /api/v1/agent/projects``).
The matrix runs in-process against the real verifiers; the web BFF proxies ``/api/backend/*`` to ``/api/v1/*``, so a
web token does reach the agent group and must be refused there.

Boundaries faked: Postgres (``FakeRepo``) and the graph; the JWKS endpoint is the run's loopback IdP.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from agent_contracts import AGENT_ROUTES
from settings import ConfigurationError, load_settings
from specflow_api_agent_support import (
    ALICE_EMAIL,
    ALICE_SUBJECT,
    CALLS,
    MCP_CLIENT,
    MCP_URL,
    SMOKE_CLIENT,
    USER_HEADER,
    access_token,
    agent_env,
    bearer_header,
    claim_mode_env,
    events,
    mcp_token,
    serve_agent,
    web_alice,
)
from specflow_api_support import token as web_token

REST = "/api/v1/projects"
AGENT = AGENT_ROUTES["list_projects"]
WEB_CLIENT = "specflow-web"
LONG_AGO = int(time.time()) - 7200


@dataclass(frozen=True)
class Cell:
    """One token of the matrix and the status each route group must answer it with."""

    make: Callable[[], str | None]
    rest: int
    agent: int


def resource_env(valid_env: dict[str, str]) -> dict[str, str]:
    return {
        **agent_env(valid_env, AUTH_API_CLIENT_IDS=f"{WEB_CLIENT},{SMOKE_CLIENT}"),
        "AUTH_TOKEN_TYPE": "keycloak",
    }


def expired(**over: Any) -> dict[str, Any]:
    return {"iat": LONG_AGO, "exp": LONG_AGO + 60, **over}


RESOURCE_MATRIX: dict[str, Cell] = {
    "web-api-token": Cell(web_alice, 200, 401),
    "mcp-token": Cell(lambda: mcp_token(ALICE_EMAIL), 401, 200),
    "smoke-client-token": Cell(lambda: web_token(ALICE_EMAIL, azp=SMOKE_CLIENT), 200, 401),
    "smoke-client-with-the-mcp-audience": Cell(lambda: mcp_token(ALICE_EMAIL, azp=SMOKE_CLIENT), 401, 401),
    "id-token-of-the-web-client": Cell(lambda: web_token(ALICE_EMAIL, aud=WEB_CLIENT, typ="ID"), 401, 401),
    "id-token-of-the-mcp-client": Cell(
        lambda: web_token(ALICE_EMAIL, aud=MCP_CLIENT, azp=MCP_CLIENT, typ="ID"),
        401,
        401,
    ),
    "tampered-type-claim-on-a-web-token": Cell(lambda: web_token(ALICE_EMAIL, typ="Refresh"), 401, 401),
    "tampered-type-claim-on-an-mcp-token": Cell(lambda: mcp_token(ALICE_EMAIL, typ="Refresh"), 401, 401),
    "missing-type-claim": Cell(lambda: mcp_token(ALICE_EMAIL, typ=None), 401, 401),
    "expired-web-token": Cell(lambda: web_token(ALICE_EMAIL, **expired()), 401, 401),
    "expired-mcp-token": Cell(lambda: mcp_token(ALICE_EMAIL, **expired()), 401, 401),
    "both-audiences-in-one-token": Cell(lambda: mcp_token(ALICE_EMAIL, aud=[MCP_URL, "specflow-api"]), 401, 401),
    "missing-token": Cell(lambda: None, 401, 401),
}

CLIENT_MATRIX: dict[str, Cell] = {
    "web-api-token": Cell(lambda: access_token(audience=WEB_CLIENT, client=WEB_CLIENT), 200, 401),
    "mcp-token": Cell(lambda: access_token(audience=MCP_CLIENT, client=MCP_CLIENT), 401, 200),
    "smoke-client-token": Cell(lambda: access_token(audience=WEB_CLIENT, client=SMOKE_CLIENT), 200, 401),
    "smoke-client-with-the-mcp-audience": Cell(
        lambda: access_token(audience=MCP_CLIENT, client=SMOKE_CLIENT), 401, 401
    ),
    "id-token-of-the-web-client": Cell(
        lambda: access_token(audience=WEB_CLIENT, client=WEB_CLIENT, kind="id"),
        401,
        401,
    ),
    "id-token-of-the-mcp-client": Cell(
        lambda: access_token(audience=MCP_CLIENT, client=MCP_CLIENT, kind="id"),
        401,
        401,
    ),
    "tampered-type-claim-on-a-web-token": Cell(
        lambda: access_token(audience=WEB_CLIENT, client=WEB_CLIENT, kind="ACCESS"),
        401,
        401,
    ),
    "tampered-type-claim-on-an-mcp-token": Cell(
        lambda: access_token(audience=MCP_CLIENT, client=MCP_CLIENT, kind="ACCESS"),
        401,
        401,
    ),
    "missing-type-claim": Cell(
        lambda: access_token(audience=MCP_CLIENT, client=MCP_CLIENT, kind=None),
        401,
        401,
    ),
    "expired-web-token": Cell(
        lambda: access_token(audience=WEB_CLIENT, client=WEB_CLIENT, **expired()),
        401,
        401,
    ),
    "expired-mcp-token": Cell(
        lambda: access_token(audience=MCP_CLIENT, client=MCP_CLIENT, **expired()),
        401,
        401,
    ),
    "resource-url-audience-under-client-mode": Cell(
        lambda: access_token(audience=MCP_URL, client=MCP_CLIENT), 401, 401
    ),
    "missing-token": Cell(lambda: None, 401, 401),
}

MODES: dict[str, tuple[Callable[[dict[str, str]], dict[str, str]], dict[str, Cell]]] = {
    "resource": (resource_env, RESOURCE_MATRIX),
    "client": (claim_mode_env, CLIENT_MATRIX),
}
CASES = [pytest.param(mode, name, id=f"{mode}-{name}") for mode, (_, matrix) in MODES.items() for name in matrix]


@pytest.mark.parametrize(("mode", "name"), CASES)
async def test_the_audience_matrix_a_token_passes_only_the_route_group_of_its_own_policy(
    monkeypatch, valid_env, mode, name
):
    """FR-MCP-AUTH-04/-05/-09: only API-policy tokens pass the REST route, only MCP-policy tokens the agent route;
    every refusal is ``401 invalid_token`` in the API envelope with the Bearer challenge and no user-id header."""

    make_env, matrix = MODES[mode]
    cell = matrix[name]
    raw = cell.make()
    async with serve_agent(monkeypatch, make_env(valid_env)) as rig:
        rest = await rig.client.get(REST, headers=bearer_header(raw))
        agent = await rig.client.get(AGENT, headers=bearer_header(raw))
    assert (rest.status_code, agent.status_code) == (cell.rest, cell.agent), (
        rest.text,
        agent.text,
    )
    for response, status in ((rest, cell.rest), (agent, cell.agent)):
        if status == 401:
            assert response.json()["error"]["code"] == "invalid_token"
            assert response.json()["error"]["message"] and response.headers["www-authenticate"].startswith("Bearer")
            assert USER_HEADER not in response.headers
    assert USER_HEADER not in rest.headers  # a REST response never names the user
    if cell.agent == 200:
        subject = ALICE_EMAIL if mode == "client" else ALICE_SUBJECT  # claim mode: the subject is the email
        assert agent.headers[USER_HEADER] == str(rig.repo.users[subject].id)


@pytest.mark.parametrize("mode", list(MODES))
async def test_a_web_token_is_refused_on_each_of_the_six_agent_routes_as_through_the_bff(monkeypatch, valid_env, mode):
    """The BFF forwards the session's web token to ``/api/v1/*``: every agent route must refuse it (and an MCP token
    on the REST routes the BFF fronts), not only the one of the matrix."""

    make_env, matrix = MODES[mode]
    web, mcp = matrix["web-api-token"].make(), matrix["mcp-token"].make()
    async with serve_agent(monkeypatch, make_env(valid_env)) as rig:
        agent = {name: await rig.call(name, web) for name in CALLS}
        rest = [await rig.client.get(path, headers=bearer_header(mcp)) for path in ("/api/v1/me", REST)]
    assert {name: r.status_code for name, r in agent.items()} == dict.fromkeys(CALLS, 401)
    assert all(USER_HEADER not in r.headers for r in agent.values())
    assert [r.status_code for r in rest] == [401, 401]


async def test_the_two_verifiers_share_one_jwks_cache(monkeypatch, valid_env, caplog):
    """Both verifiers read the same keys: one fetch serves a REST call and an agent call (one cache, not two)."""

    caplog.set_level("INFO")
    async with serve_agent(monkeypatch, agent_env(valid_env)) as rig:
        verifiers = (rig.app.state.token_verifier, rig.app.state.agent_token_verifier)
        assert verifiers[0] is not verifiers[1]
        # No public accessor exposes the cache; identity of the private one is the only direct proof of sharing.
        assert verifiers[0]._jwks is verifiers[1]._jwks
        before = len(events(caplog, "auth.jwks.refetch"))
        assert (await rig.client.get(REST, headers=bearer_header(web_alice()))).status_code == 200
        assert (await rig.client.get(AGENT, headers=bearer_header(mcp_token(ALICE_EMAIL)))).status_code == 200
    assert len(events(caplog, "auth.jwks.refetch")) - before <= 1


async def test_the_agent_verifier_is_its_own_app_state_attribute(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as rig:
        assert hasattr(rig.app.state, "token_verifier") and hasattr(rig.app.state, "agent_token_verifier")


# --- configs where one token would satisfy both policies: startup is refused ------------------------------------


def refused(env: dict[str, str]) -> tuple[str, ...]:
    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", env)
    return raised.value.variables


@pytest.mark.parametrize(
    ("over", "variables"),
    [
        pytest.param(
            {"MCP_AUDIENCE": "specflow-api"},
            ("MCP_AUDIENCE",),
            id="mcp-audience-is-the-rest-audience",
        ),
        pytest.param(
            {"MCP_AUDIENCE": WEB_CLIENT},
            ("MCP_AUDIENCE",),
            id="mcp-audience-is-a-rest-client",
        ),
        pytest.param(
            {"MCP_AUDIENCE": MCP_CLIENT},
            ("MCP_AUDIENCE",),
            id="client-audience-without-a-type-check",
        ),
        pytest.param(
            {"AUTH_IGNORED_AUDIENCES": "specflow-api"},
            ("AUTH_IGNORED_AUDIENCES",),
            id="ignored-rest-audience",
        ),
        pytest.param(
            {"MCP_AUDIENCE": "account", "AUTH_IGNORED_AUDIENCES": "account"},
            ("AUTH_IGNORED_AUDIENCES",),
            id="ignored-mcp-audience",
        ),
    ],
)
async def test_an_api_config_where_one_token_satisfies_both_policies_refuses_the_start(valid_env, over, variables):
    """Plan 03 Do 5 / A3: the api role runs the MCP audience guards once ``MCP_RESOURCE_URL`` is set."""

    assert refused(agent_env(valid_env, **over)) == variables


def test_a_client_audience_sharing_a_client_with_the_rest_surface_refuses_the_start(
    valid_env,
):
    env = claim_mode_env(valid_env)
    assert refused({**env, "MCP_CLIENT_IDS": f"{MCP_CLIENT},{WEB_CLIENT}"}) == ("MCP_CLIENT_IDS",)
    assert refused({**env, "MCP_AUDIENCE": WEB_CLIENT, "MCP_CLIENT_IDS": WEB_CLIENT}) == (
        "MCP_AUDIENCE",
        "MCP_CLIENT_IDS",
    )


def test_the_shipped_claim_mode_and_resource_mode_configs_of_the_matrix_are_valid(
    valid_env,
):
    for make_env in (resource_env, claim_mode_env):
        assert load_settings("api", make_env(valid_env)).mcp is not None


def test_a_deployed_api_refuses_a_plain_http_resource_url_and_the_smoke_client_as_an_mcp_client(
    valid_env,
):
    deployed = {
        "APP_ENV": "dev",
        "OIDC_ISSUER": "https://sso.example.test/realms/app",
        "STORAGE_PREFIX": "dev/team/",
    }
    base = agent_env(valid_env, **deployed)
    base.pop("AUTH_JWKS_URL")
    assert refused({**base, "MCP_RESOURCE_URL": "http://mcp.example.test/mcp"}) == ("MCP_RESOURCE_URL",)
    assert refused(
        {
            **base,
            "MCP_RESOURCE_URL": "https://mcp.example.test/mcp",
            "MCP_CLIENT_IDS": f"{MCP_CLIENT},{SMOKE_CLIENT}",
        }
    ) == ("MCP_CLIENT_IDS",)


# --- settings: the api role with and without MCP_RESOURCE_URL --------------------------------------------------


def test_the_api_role_has_no_mcp_settings_without_the_resource_url(valid_env):
    assert load_settings("api", valid_env).mcp is None


@pytest.mark.parametrize("blank", ["", " ", "   "])
def test_a_blank_resource_url_is_unset(valid_env, blank):
    assert load_settings("api", {**valid_env, "MCP_RESOURCE_URL": blank}).mcp is None


def test_other_mcp_variables_alone_do_not_switch_the_agent_routes_on(valid_env):
    """Without the resource URL nothing else of ``MCP_*`` is read (even a value that would be refused with it)."""

    env = {
        **valid_env,
        "MCP_AUDIENCE": "specflow-api",
        "MCP_CLIENT_IDS": WEB_CLIENT,
        "MCP_PORT": "not-a-port",
    }
    assert load_settings("api", env).mcp is None


def test_the_api_role_with_the_resource_url_builds_the_mcp_policy_with_its_defaults(
    valid_env,
):
    mcp = load_settings("api", agent_env(valid_env)).mcp
    assert mcp is not None
    assert (mcp.resource_url, mcp.audience, mcp.client_ids) == (
        MCP_URL,
        MCP_URL,
        (MCP_CLIENT,),
    )


def test_the_api_role_takes_an_audience_and_client_override(valid_env):
    mcp = load_settings("api", agent_env(valid_env, MCP_AUDIENCE=" api://mcp ", MCP_CLIENT_IDS="a, b")).mcp
    assert mcp is not None and (mcp.audience, mcp.client_ids) == (
        "api://mcp",
        ("a", "b"),
    )


# --- agent routes without MCP settings: 503 agent_unavailable -----------------------------------------------------


@pytest.mark.parametrize("resource_url", [None, "", "  "], ids=["unset", "empty", "blank"])
async def test_every_agent_route_answers_503_agent_unavailable_when_the_api_has_no_mcp_settings(
    monkeypatch, valid_env, resource_url
):
    env = dict(valid_env) if resource_url is None else {**valid_env, "MCP_RESOURCE_URL": resource_url}
    async with serve_agent(monkeypatch, env) as rig:
        assert not hasattr(rig.app.state, "agent_token_verifier")
        answers = {
            (name, kind): await rig.call(name, raw)
            for name in CALLS
            for kind, raw in (
                ("mcp", mcp_token(ALICE_EMAIL)),
                ("web", web_alice()),
                ("none", None),
            )
        }
        rest = await rig.client.get(REST, headers=bearer_header(web_alice()))
    for key, response in answers.items():
        assert response.status_code == 503, key
        assert response.json() == {
            "error": {
                "code": "agent_unavailable",
                "message": "The agent routes are not configured.",
                "details": {},
            }
        }
        assert USER_HEADER not in response.headers
    assert rest.status_code == 200  # the REST surface does not depend on the agent routes


async def test_a_deliberate_refusal_is_not_a_crash_the_unset_api_logs_no_error(monkeypatch, valid_env, caplog):
    caplog.set_level("DEBUG")
    async with serve_agent(monkeypatch, valid_env) as rig:
        await rig.call("list_projects")
    assert [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")] == []
