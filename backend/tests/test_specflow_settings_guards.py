"""AC-1.2 to AC-1.5: the startup guards, each with a positive and a negative case."""

from __future__ import annotations

import pytest

from settings import ConfigurationError, load_settings


def refused(role, env, **kwargs) -> tuple[str, ...]:
    with pytest.raises(ConfigurationError) as raised:
        load_settings(role, env, **kwargs)
    return raised.value.variables


def deployed(env: dict[str, str], app_env: str = "dev") -> dict[str, str]:
    """A valid non-local api/worker environment (oidc, https, real prefix)."""

    env.update(
        APP_ENV=app_env,
        AUTH_MODE="oidc",
        OIDC_ISSUER="https://sso.example.test/realms/app",
        STORAGE_PREFIX="dev/",
    )
    env.pop("AUTH_DEV_SIGNING_KEY")
    return env


# --- AC-1.2 ---------------------------------------------------------------


@pytest.mark.parametrize("app_env", ["local", "dev", "prod"])
def test_the_worker_refuses_the_agent_user_equal_to_the_database_user(app_env, valid_env):
    valid_env = deployed(valid_env, app_env)
    valid_env["DATABASE_AGENT_USER"] = "APP_user"

    assert refused("worker", valid_env) == ("DATABASE_AGENT_USER",)


def test_the_worker_accepts_distinct_database_users(valid_env):
    load_settings("worker", deployed(valid_env))


def test_the_agent_user_guard_is_a_worker_guard(valid_env):
    valid_env["DATABASE_AGENT_USER"] = "app_user"

    load_settings("api", valid_env)


# --- AC-1.3 ---------------------------------------------------------------


def test_a_valid_deployed_environment_passes(valid_env):
    for role in ("api", "worker", "migrate"):
        load_settings(role, deployed(dict(valid_env)))
    prod = deployed(dict(valid_env), "prod")
    prod["AUTH_JWKS_URL"] = "https://SSO.example.test/realms/app/certs"
    load_settings("api", prod)


@pytest.mark.parametrize("app_env", ["dev", "prod"])
def test_dev_auth_is_refused_outside_local(app_env, valid_env):
    valid_env["APP_ENV"] = app_env

    assert {"AUTH_MODE", "AUTH_DEV_SIGNING_KEY", "OIDC_ISSUER", "STORAGE_PREFIX"} <= set(refused("api", valid_env))


def test_dev_auth_is_accepted_locally(valid_env):
    assert load_settings("api", valid_env).auth.mode == "dev"


def test_a_dev_signing_key_is_refused_outside_local(valid_env):
    env = deployed(valid_env)
    env["AUTH_DEV_SIGNING_KEY"] = "k" * 40

    assert refused("api", env) == ("AUTH_DEV_SIGNING_KEY",)


@pytest.mark.parametrize("issuer", ["http://sso.example.test/realms/app", "specflow-dev-login"])
def test_a_non_https_issuer_is_refused_outside_local(issuer, valid_env):
    env = deployed(valid_env)
    env["OIDC_ISSUER"] = issuer

    assert refused("api", env) == ("OIDC_ISSUER",)


def test_a_non_https_issuer_is_accepted_locally(valid_env):
    valid_env["AUTH_MODE"] = "dev"
    valid_env["OIDC_ISSUER"] = "http://localhost:8180/realms/app"

    load_settings("api", valid_env)


@pytest.mark.parametrize(
    "jwks",
    [
        "https://other.example.test/certs",
        "http://sso.example.test/certs",
    ],
)
def test_a_jwks_url_on_another_host_or_over_http_is_refused_outside_local(jwks, valid_env):
    env = deployed(valid_env)
    env["AUTH_JWKS_URL"] = jwks

    assert refused("api", env) == ("AUTH_JWKS_URL",)


def test_a_jwks_url_on_the_issuer_host_is_accepted(valid_env):
    env = deployed(valid_env)
    env["AUTH_JWKS_URL"] = "https://sso.example.test/other/path/certs"

    load_settings("api", env)


@pytest.mark.parametrize(
    "prefix",
    ["dev", "dev/local/", "dev/local/pytest/", "prod", "/dev/local/", "Dev/Local/", "//DEV/local/x/"],
)
@pytest.mark.parametrize("role", ["api", "worker"])
def test_a_bad_storage_prefix_is_refused_outside_local(role, prefix, valid_env):
    env = deployed(valid_env)
    env["STORAGE_PREFIX"] = prefix

    assert refused(role, env) == ("STORAGE_PREFIX",)


@pytest.mark.parametrize("prefix", ["dev/", "prod/", "dev/localized/"])
def test_a_good_storage_prefix_is_accepted_outside_local(prefix, valid_env):
    env = deployed(valid_env)
    env["STORAGE_PREFIX"] = prefix

    load_settings("worker", env)


@pytest.mark.parametrize("prefix", ["dev/local/", "dev", "x"])
def test_any_storage_prefix_is_accepted_locally(prefix, valid_env):
    valid_env["STORAGE_PREFIX"] = prefix

    load_settings("worker", valid_env)


@pytest.mark.parametrize(
    ("role", "variable"),
    [
        ("api", "NEO4J_PASSWORD"),
        ("worker", "NEO4J_PASSWORD"),
        ("worker", "LLM_GATEWAY_API_KEY"),
    ],
)
def test_an_empty_secret_is_refused_outside_local(role, variable, valid_env):
    env = deployed(valid_env)
    env[variable] = ""

    assert refused(role, env) == (variable,)


@pytest.mark.parametrize("variable", ["NEO4J_PASSWORD", "LLM_GATEWAY_API_KEY"])
def test_an_empty_secret_is_accepted_locally(variable, valid_env):
    valid_env[variable] = ""

    load_settings("worker", valid_env)


@pytest.mark.parametrize("variable", ["DATABASE_PASSWORD", "DATABASE_AGENT_PASSWORD"])
@pytest.mark.parametrize("app_env", ["local", "dev"])
def test_an_empty_database_secret_is_refused_everywhere(variable, app_env, valid_env):
    env = deployed(valid_env, app_env)
    env[variable] = ""

    assert refused("worker", env) == (variable,)


def test_the_smoke_client_is_refused_outside_local(valid_env):
    env = deployed(valid_env)
    env["AUTH_API_CLIENT_IDS"] = "specflow-web,specflow-smoke"

    assert refused("api", env) == ("AUTH_API_CLIENT_IDS",)


def test_the_smoke_client_is_accepted_locally(valid_env):
    valid_env["AUTH_API_CLIENT_IDS"] = "specflow-web,specflow-smoke"

    assert load_settings("api", valid_env).auth.api_client_ids == (
        "specflow-web",
        "specflow-smoke",
    )


def test_all_refusals_arrive_in_one_error(valid_env):
    env = deployed(valid_env)
    env.update(
        AUTH_MODE="dev",
        AUTH_DEV_SIGNING_KEY="k" * 40,
        OIDC_ISSUER="http://x.test",
        STORAGE_PREFIX="dev/local/",
        NEO4J_PASSWORD="",
        AUTH_API_CLIENT_IDS="specflow-smoke",
    )

    assert set(refused("api", env)) == {
        "AUTH_MODE",
        "AUTH_DEV_SIGNING_KEY",
        "OIDC_ISSUER",
        "STORAGE_PREFIX",
        "NEO4J_PASSWORD",
        "AUTH_API_CLIENT_IDS",
    }


# --- AC-1.4 ---------------------------------------------------------------


def test_dev_auth_needs_a_signing_key_of_at_least_32_bytes(valid_env):
    valid_env["AUTH_DEV_SIGNING_KEY"] = "k" * 32
    load_settings("api", valid_env)
    valid_env["AUTH_DEV_SIGNING_KEY"] = "k" * 31
    assert refused("api", valid_env) == ("AUTH_DEV_SIGNING_KEY",)
    del valid_env["AUTH_DEV_SIGNING_KEY"]
    assert refused("api", valid_env) == ("AUTH_DEV_SIGNING_KEY",)


def test_the_signing_key_length_counts_bytes_not_characters(valid_env):
    valid_env["AUTH_DEV_SIGNING_KEY"] = "é" * 16

    load_settings("api", valid_env)


def test_dev_auth_is_refused_when_running_in_kubernetes(valid_env):
    load_settings("api", valid_env)
    valid_env["KUBERNETES_SERVICE_HOST"] = "10.0.0.1"

    assert refused("api", valid_env) == ("AUTH_MODE",)


def test_oidc_auth_is_accepted_in_kubernetes(valid_env):
    env = deployed(valid_env)
    env["KUBERNETES_SERVICE_HOST"] = "10.0.0.1"

    load_settings("api", env)


@pytest.mark.parametrize("app_env", ["local", "dev"])
def test_oidc_with_a_dev_signing_key_is_refused_everywhere(app_env, valid_env):
    valid_env.update(APP_ENV=app_env, AUTH_MODE="oidc")
    if app_env == "dev":
        valid_env.update(OIDC_ISSUER="https://sso.example.test", STORAGE_PREFIX="dev/")

    assert refused("api", valid_env) == ("AUTH_DEV_SIGNING_KEY",)


def test_oidc_without_a_dev_signing_key_is_accepted_locally(valid_env):
    valid_env["AUTH_MODE"] = "oidc"
    del valid_env["AUTH_DEV_SIGNING_KEY"]

    assert load_settings("api", valid_env).auth.mode == "oidc"


# --- AC-1.5 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("parse", ("parse",)),
        ("agents", ("agents",)),
        ("parse,agents", ("parse", "agents")),
        (" agents , parse ", ("agents", "parse")),
        ("parse,parse", ("parse",)),
    ],
)
def test_worker_queues_accepts_parse_and_agents_or_a_subset(raw, expected, valid_env):
    valid_env["WORKER_QUEUES"] = raw

    assert load_settings("worker", valid_env).temporal.worker_queues == expected


@pytest.mark.parametrize("raw", ["other", "parse,other", "specflow-parse", ",", "PARSE"])
def test_worker_queues_refuses_anything_else(raw, valid_env):
    valid_env["WORKER_QUEUES"] = raw

    assert refused("worker", valid_env) == ("WORKER_QUEUES",)


@pytest.mark.parametrize(
    "jwks",
    [
        "https://sso.example.test:443/certs",
        "https://SSO.Example.test/certs",
    ],
)
def test_a_jwks_url_with_the_default_port_spelled_out_is_accepted(jwks, valid_env):
    env = deployed(valid_env)
    env["AUTH_JWKS_URL"] = jwks

    load_settings("api", env)


def test_an_explicit_default_port_on_the_issuer_matches_a_bare_jwks_url(valid_env):
    env = deployed(valid_env)
    env["OIDC_ISSUER"] = "https://sso.example.test:443/realms/app"
    env["AUTH_JWKS_URL"] = "https://sso.example.test/certs"

    load_settings("api", env)


@pytest.mark.parametrize("jwks", ["https://sso.example.test:8443/certs", "https://sso.example.test:444/certs"])
def test_a_jwks_url_on_another_port_is_refused(jwks, valid_env):
    env = deployed(valid_env)
    env["AUTH_JWKS_URL"] = jwks

    assert refused("api", env) == ("AUTH_JWKS_URL",)


def test_the_same_non_default_port_on_both_is_accepted(valid_env):
    env = deployed(valid_env)
    env["OIDC_ISSUER"] = "https://sso.example.test:8443/realms/app"
    env["AUTH_JWKS_URL"] = "https://sso.example.test:8443/certs"

    load_settings("api", env)


# --- FR-MCP-AUTH-12 and the claim profile (FR-MCP-AUTH-07) ---------------------------


@pytest.mark.parametrize("app_env", ["local", "dev"])
def test_the_api_refuses_an_audience_equal_to_a_client_id(app_env, valid_env):
    """FR-MCP-AUTH-12: names AUTH_API_AUDIENCE, locally and deployed."""

    env = valid_env if app_env == "local" else deployed(valid_env, app_env)
    env["AUTH_API_AUDIENCE"] = "specflow-web"

    assert refused("api", env) == ("AUTH_API_AUDIENCE",)


def test_the_api_audience_guard_holds_in_dev_mode(valid_env):
    """FR-MCP-AUTH-12: every mode (the fixture is ``AUTH_MODE=dev``; the dev verifier uses the audience too)."""

    assert valid_env["AUTH_MODE"] == "dev"
    valid_env["AUTH_API_AUDIENCE"] = "specflow-web"

    assert refused("api", valid_env) == ("AUTH_API_AUDIENCE",)


def test_the_api_audience_guard_checks_every_client_id(valid_env):
    """FR-MCP-AUTH-12: an audience equal to the second of several client ids is refused."""

    valid_env["AUTH_API_CLIENT_IDS"] = "specflow-web,specflow-api"

    assert refused("api", valid_env) == ("AUTH_API_AUDIENCE",)


def test_the_api_refuses_ignored_audiences_holding_the_api_audience(valid_env):
    """FR-MCP-AUTH-12."""

    valid_env["AUTH_IGNORED_AUDIENCES"] = "account, specflow-api"

    assert refused("api", valid_env) == ("AUTH_IGNORED_AUDIENCES",)


def test_both_api_audience_guards_fire_at_once(valid_env):
    """FR-MCP-AUTH-12: every bad name is reported together."""

    valid_env["AUTH_API_AUDIENCE"] = "specflow-web"
    valid_env["AUTH_IGNORED_AUDIENCES"] = "specflow-web"

    assert set(refused("api", valid_env)) == {
        "AUTH_API_AUDIENCE",
        "AUTH_IGNORED_AUDIENCES",
    }


def test_the_api_audience_guards_do_not_apply_to_the_worker(valid_env):
    """FR-MCP-AUTH-12: an api-role guard."""

    valid_env["AUTH_API_AUDIENCE"] = "specflow-web"

    load_settings("worker", valid_env)


@pytest.mark.parametrize(
    ("name", "field", "default"),
    [
        ("AUTH_SUBJECT_CLAIM", "subject_claim", "sub"),
        ("AUTH_CLIENT_ID_CLAIM", "client_id_claims", ("azp", "client_id")),
        ("AUTH_EMAIL_CLAIM", "email_claim", "email"),
        ("AUTH_TOKEN_TYPE", "token_type", "none"),
        ("AUTH_IGNORED_AUDIENCES", "ignored_audiences", ("account",)),
    ],
)
def test_a_blank_claim_setting_falls_back_to_its_default(name, field, default, valid_env):
    """FR-MCP-AUTH-07: blank is unset for these five."""

    valid_env[name] = "  "

    assert getattr(load_settings("api", valid_env).auth, field) == default


def test_a_blank_email_verified_claim_stays_blank_and_means_no_check(valid_env):
    """FR-MCP-AUTH-07: the one claim setting whose empty value has a meaning."""

    valid_env["AUTH_EMAIL_VERIFIED_CLAIM"] = ""

    assert load_settings("api", valid_env).auth.email_verified_claim == ""
    assert load_settings("api", {**valid_env, "AUTH_EMAIL_VERIFIED_CLAIM": " xms_edov "}).auth.email_verified_claim == (
        "xms_edov"
    )


def test_the_email_verified_claim_defaults_to_email_verified(valid_env):
    """FR-MCP-AUTH-07."""

    assert load_settings("api", valid_env).auth.email_verified_claim == "email_verified"


@pytest.mark.parametrize("value", ["Keycloak", "bogus", "RFC9068"])
def test_an_unknown_token_type_is_refused(value, valid_env):
    """FR-MCP-AUTH-07: exact lowercase values only."""

    valid_env["AUTH_TOKEN_TYPE"] = value

    assert refused("api", valid_env) == ("AUTH_TOKEN_TYPE",)


@pytest.mark.parametrize("value", ["keycloak", "rfc9068", "none"])
def test_the_token_types_are_accepted(value, valid_env):
    """FR-MCP-AUTH-07."""

    valid_env["AUTH_TOKEN_TYPE"] = value

    assert load_settings("api", valid_env).auth.token_type == value


def test_the_client_id_claim_list_is_parsed_stripped_and_deduplicated(valid_env):
    """FR-MCP-AUTH-07: order is kept (first present wins)."""

    valid_env["AUTH_CLIENT_ID_CLAIM"] = " cid , azp,, cid "

    assert load_settings("api", valid_env).auth.client_id_claims == ("cid", "azp")
