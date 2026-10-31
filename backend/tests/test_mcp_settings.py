"""``mcp`` role settings: defaults, bindings, each refusal naming its variable (FR-MCP-AUTH-09, FR-MCP-OPS-01)."""

from __future__ import annotations

import pytest

from mcp_support import MCP_URL, PRM_URL, mcp_env, oidc_env
from runtime_defaults_support import RUNTIME_DEFAULTS
from settings import ConfigurationError, load_settings


def refused(env: dict[str, str]) -> tuple[str, ...]:
    with pytest.raises(ConfigurationError) as raised:
        load_settings("mcp", env)
    return raised.value.variables


def deployed(valid_env: dict[str, str], **over: str) -> dict[str, str]:
    """A valid non-local ``mcp`` environment (oidc, https)."""

    base = {
        "APP_ENV": "dev",
        "AUTH_MODE": "oidc",
        "OIDC_ISSUER": "https://sso.example.test/realms/app",
        "MCP_RESOURCE_URL": "https://mcp.example.test/mcp",
    }
    env = mcp_env(valid_env, **{**base, **over})
    env.pop("AUTH_DEV_SIGNING_KEY")
    return env


# --- defaults and bindings (FR-MCP-OPS-01, FR-MCP-AUTH-09) ------------------


def test_the_defaults_of_the_mcp_settings(valid_env):
    settings = load_settings("mcp", mcp_env(valid_env))
    mcp = settings.mcp

    assert mcp.resource_url == MCP_URL
    assert mcp.audience == MCP_URL
    assert mcp.client_ids == ("specflow-mcp",)
    assert (mcp.allowed_hosts, mcp.allowed_origins) == ((), ())
    assert mcp.port == RUNTIME_DEFAULTS["MCP_PORT"]
    assert mcp.prm_url == PRM_URL


def test_the_mcp_role_builds_only_database_neo4j_auth_and_mcp(valid_env):
    """FR-MCP-OPS-01: no Temporal, storage, Langfuse, LLM or agent-database binding."""

    settings = load_settings("mcp", mcp_env(valid_env))

    assert (settings.database, settings.neo4j, settings.auth, settings.mcp) != (None,) * 4
    for absent in ("temporal", "storage", "langfuse", "agent", "agent_run", "graph_build", "agent_database"):
        assert getattr(settings, absent) is None, absent


def test_the_other_roles_have_no_mcp_binding(valid_env):
    assert load_settings("api", valid_env).mcp is None


def test_overrides_are_read(valid_env):
    env = mcp_env(
        valid_env,
        MCP_AUDIENCE="api://specflow-mcp",
        MCP_CLIENT_IDS="a, b",
        MCP_ALLOWED_HOSTS="x.example",
        MCP_ALLOWED_ORIGINS="https://o.example",
        MCP_PORT="9100",
    )
    mcp = load_settings("mcp", env).mcp

    assert (mcp.audience, mcp.client_ids, mcp.allowed_hosts, mcp.allowed_origins, mcp.port) == (
        "api://specflow-mcp",
        ("a", "b"),
        ("x.example",),
        ("https://o.example",),
        9100,
    )


# --- one refusal per variable (FR-MCP-AUTH-09) -------------------------------

REFUSALS = [
    ("MCP_RESOURCE_URL", {"MCP_RESOURCE_URL": ""}, ("MCP_RESOURCE_URL",)),
    ("path", {"MCP_RESOURCE_URL": "http://localhost:8002/other"}, ("MCP_RESOURCE_URL",)),
    ("trailing slash", {"MCP_RESOURCE_URL": "http://localhost:8002/mcp/"}, ("MCP_RESOURCE_URL",)),
    ("query", {"MCP_RESOURCE_URL": "http://localhost:8002/mcp?x=1"}, ("MCP_RESOURCE_URL",)),
    ("fragment", {"MCP_RESOURCE_URL": "http://localhost:8002/mcp#f"}, ("MCP_RESOURCE_URL",)),
    ("userinfo", {"MCP_RESOURCE_URL": "http://u:p@localhost:8002/mcp"}, ("MCP_RESOURCE_URL",)),
    ("quote", {"MCP_RESOURCE_URL": 'http://localhost:8002/mcp"'}, ("MCP_RESOURCE_URL",)),
    ("scheme", {"MCP_RESOURCE_URL": "ftp://localhost/mcp"}, ("MCP_RESOURCE_URL",)),
    ("port", {"MCP_RESOURCE_URL": "http://localhost:99999/mcp"}, ("MCP_RESOURCE_URL",)),
    ("audience is a client", {"MCP_AUDIENCE": "specflow-mcp"}, ("MCP_AUDIENCE",)),
    ("audience is the REST audience", {"MCP_AUDIENCE": "specflow-api"}, ("MCP_AUDIENCE",)),
    ("empty client list", {"MCP_CLIENT_IDS": ","}, ("MCP_CLIENT_IDS",)),
    ("wildcard host", {"MCP_ALLOWED_HOSTS": "*.example"}, ("MCP_ALLOWED_HOSTS",)),
    ("wildcard origin", {"MCP_ALLOWED_ORIGINS": "https://*.example"}, ("MCP_ALLOWED_ORIGINS",)),
    ("whitespace in a host", {"MCP_ALLOWED_HOSTS": "a b"}, ("MCP_ALLOWED_HOSTS",)),
    ("ignored audience", {"MCP_AUDIENCE": "account", "AUTH_IGNORED_AUDIENCES": "account"}, ("AUTH_IGNORED_AUDIENCES",)),
    ("port range", {"MCP_PORT": "0"}, ("MCP_PORT",)),
]


@pytest.mark.parametrize(("overrides", "variables"), [case[1:] for case in REFUSALS], ids=[c[0] for c in REFUSALS])
def test_each_bad_value_is_refused_naming_its_variable(valid_env, overrides, variables):
    assert refused(mcp_env(valid_env, **{"MCP_CLIENT_IDS": "specflow-mcp", **overrides})) == variables


def test_the_api_audience_in_the_ignored_list_is_refused(valid_env):
    assert refused(mcp_env(valid_env, AUTH_IGNORED_AUDIENCES="specflow-api")) == ("AUTH_IGNORED_AUDIENCES",)


def test_dev_auth_is_refused_inside_kubernetes_for_the_mcp_role(valid_env):
    """FR-MCP-AUTH-09: ``AUTH_MODE=dev`` with ``KUBERNETES_SERVICE_HOST`` set, in a local environment."""

    load_settings("mcp", mcp_env(valid_env))

    assert refused(mcp_env(valid_env, KUBERNETES_SERVICE_HOST="10.0.0.1")) == ("AUTH_MODE",)


def test_oidc_is_accepted_inside_kubernetes_for_the_mcp_role(valid_env):
    load_settings("mcp", deployed(valid_env, KUBERNETES_SERVICE_HOST="10.0.0.1"))


@pytest.mark.parametrize(
    ("over", "refused_variables"),
    [
        ({"AUTH_MODE": "dev", "AUTH_DEV_SIGNING_KEY": "k" * 40}, ("AUTH_MODE", "AUTH_DEV_SIGNING_KEY")),
        ({"OIDC_ISSUER": ""}, ("OIDC_ISSUER",)),
        ({"AUTH_DEV_SIGNING_KEY": "k" * 40}, ("AUTH_DEV_SIGNING_KEY",)),
    ],
    ids=["dev mode when deployed", "no issuer", "dev key in oidc mode"],
)
def test_the_shared_auth_refusals_apply_to_the_mcp_role(valid_env, over, refused_variables):
    assert refused({**deployed(valid_env), **over}) == refused_variables


def test_every_bad_variable_is_named_at_once(valid_env):
    """Field errors and guards together: one refusal lists the resource URL, the hosts, the port and the auth mode."""

    env = deployed(
        valid_env,
        AUTH_MODE="dev",
        AUTH_DEV_SIGNING_KEY="k" * 40,
        MCP_RESOURCE_URL=MCP_URL.replace("/mcp", "/other"),
        MCP_ALLOWED_HOSTS="*",
        MCP_PORT="0",
    )

    assert set(refused(env)) == {
        "AUTH_MODE",
        "AUTH_DEV_SIGNING_KEY",
        "MCP_RESOURCE_URL",
        "MCP_ALLOWED_HOSTS",
        "MCP_PORT",
    }


def test_the_audience_guards_all_report_together(valid_env):
    env = mcp_env(valid_env, MCP_AUDIENCE="specflow-mcp", AUTH_IGNORED_AUDIENCES="specflow-mcp")

    assert set(refused(env)) == {"MCP_AUDIENCE", "AUTH_IGNORED_AUDIENCES"}


def test_a_deployed_environment_needs_https_and_no_smoke_client(valid_env):
    """FR-MCP-AUTH-09: https outside local; SEC4: the QA direct-grant client is not an MCP client."""

    load_settings("mcp", deployed(valid_env))
    assert refused(deployed(valid_env, MCP_RESOURCE_URL="http://mcp.example.test/mcp")) == ("MCP_RESOURCE_URL",)
    assert refused(deployed(valid_env, MCP_CLIENT_IDS="specflow-mcp,specflow-smoke")) == ("MCP_CLIENT_IDS",)


# --- SEC1: the audience is compared as the policy sees it -------------------


@pytest.mark.parametrize("audience", ["specflow-mcp ", " specflow-mcp", " specflow-api "])
def test_an_audience_with_whitespace_is_refused_as_its_stripped_value(valid_env, audience):
    assert refused(mcp_env(valid_env, MCP_AUDIENCE=audience, MCP_CLIENT_IDS="specflow-mcp")) == ("MCP_AUDIENCE",)


def test_the_audience_is_stripped_and_blank_means_unset(valid_env):
    assert load_settings("mcp", mcp_env(valid_env, MCP_AUDIENCE=" api://mcp ")).mcp.audience == "api://mcp"
    assert load_settings("mcp", mcp_env(valid_env, MCP_AUDIENCE="  ")).mcp.audience == MCP_URL


# --- the canonical resource URL (R3, SEC2) ----------------------------------

NON_CANONICAL = [
    "HTTP://localhost:8002/mcp",
    "http://LocalHost:8002/mcp",
    "https://mcp.example.test:443/mcp",
    "http://mcp.example.test:80/mcp",
    "http://localhost:/mcp",
    "https://s.example.com./mcp",
    "https://s.example.com,a=b/mcp",
    "https://s_x.example.com/mcp",
    "http://[::zz]/mcp",
]


@pytest.mark.parametrize("url", NON_CANONICAL)
def test_a_non_canonical_resource_url_is_refused(valid_env, url):
    assert refused(mcp_env(valid_env, MCP_RESOURCE_URL=url)) == ("MCP_RESOURCE_URL",)


@pytest.mark.parametrize(
    "url", [MCP_URL, "https://s.example.com/mcp", "http://[::1]:8002/mcp", "http://10.0.0.5:8080/mcp"]
)
def test_a_canonical_resource_url_is_accepted(valid_env, url):
    assert load_settings("mcp", mcp_env(valid_env, MCP_RESOURCE_URL=url)).mcp.resource_url == url


# --- the Host allowlist derivation (FR-MCP-OPS-04) ---------------------------


@pytest.mark.parametrize(
    ("url", "extra", "expected"),
    [
        (MCP_URL, "", ("localhost:8002",)),
        ("https://mcp.example.test/mcp", "", ("mcp.example.test",)),
        ("http://[::1]:8002/mcp", "", ("[::1]:8002",)),
        (
            "https://mcp.example.test:8443/mcp",
            "alt.example.test, mcp.example.test",
            ("mcp.example.test:8443", "alt.example.test", "mcp.example.test"),
        ),
    ],
)
def test_transport_hosts_are_the_resource_host_as_clients_send_it_plus_the_extras(valid_env, url, extra, expected):
    env = mcp_env(valid_env, MCP_RESOURCE_URL=url, MCP_ALLOWED_HOSTS=extra)

    assert load_settings("mcp", env).mcp.transport_hosts == expected


def test_oidc_mode_settings_accept_the_loopback_stub(valid_env, stub_issuer):
    assert load_settings("mcp", oidc_env(valid_env, stub_issuer)).auth.mode == "oidc"
