"""The `specflow` command: roles, --check-config, one logger close per command."""

from __future__ import annotations

import socket
import sys
import types

import pytest

import cli
from logger.correlation import current_request_id
from logger.logger import ERROR


class Spy:
    """Records logger closes and log_event calls made by the cli module."""

    def __init__(self) -> None:
        self.closed = 0
        self.events: list[tuple[int, str, dict]] = []


@pytest.fixture
def spy(monkeypatch):
    spy = Spy()

    def close():
        spy.closed += 1

    def log_event(_logger, level, event, message=None, /, **fields):
        spy.events.append((level, event, fields))

    monkeypatch.setattr(cli, "close_logger", close)
    monkeypatch.setattr(cli, "log_event", log_event)
    return spy


@pytest.fixture
def env(clean_specflow_env, valid_env):
    for name, value in valid_env.items():
        clean_specflow_env.setenv(name, value)
    return clean_specflow_env


@pytest.fixture
def work(monkeypatch):
    """Fake work modules; each records the call and returns its configured result."""

    calls: dict[str, tuple] = {}
    results: dict[str, object] = {}

    def install(module_name: str, key: str) -> None:
        module = types.ModuleType(module_name)

        def run(*args):
            calls[key] = args
            result = results.get(key)
            if isinstance(result, Exception):
                raise result
            return result

        vars(module)["run"] = run
        monkeypatch.setitem(sys.modules, module_name, module)

    install("api.server", "api")
    install("temporal.worker", "worker")
    install("db.migrate", "migrate")
    health = types.ModuleType("health")

    def run_checks(*args, **kwargs):
        calls["check"] = args
        calls["check_kwargs"] = kwargs
        return results.get("check")

    vars(health)["run_checks"] = run_checks
    monkeypatch.setitem(sys.modules, "health", health)
    return types.SimpleNamespace(calls=calls, results=results)


# --- parsing --------------------------------------------------------------


def test_help_lists_exactly_the_three_roles(capsys, spy):
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    assert raised.value.code == 0
    out = capsys.readouterr().out
    assert "{api,worker,migrate}" in out
    for other in ("check", "frontend", "all"):
        assert f"\n    {other} " not in out
    assert spy.closed == 1


@pytest.mark.parametrize(
    "argv",
    [[], ["check"], ["bogus"], ["worker", "--queue", "other"], ["migrate", "--check-config"]],
)
def test_bad_command_lines_exit_2(argv, spy, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)

    assert raised.value.code == 2


def test_worker_accepts_repeated_queues_and_check_flags(env, work, spy):
    parsed = cli._parser().parse_args(["worker", "--queue", "parse", "--queue", "agents", "--check", "--check-config"])

    assert (parsed.queue, parsed.check, parsed.check_config) == (
        ["parse", "agents"],
        True,
        True,
    )


# --- --check-config (AC-2.6, AC-1.6) --------------------------------------


@pytest.mark.parametrize("role", ["api", "worker"])
def test_check_config_exits_0_for_a_valid_environment(role, env, work, spy):
    assert cli.main([role, "--check-config"]) == 0

    assert work.calls == {}
    assert spy.events == []


@pytest.mark.parametrize("role", ["api", "worker"])
def test_check_config_makes_no_network_connection(role, env, work, spy, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access during --check-config")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)

    assert cli.main([role, "--check-config"]) == 0


@pytest.mark.parametrize("role", ["api", "worker"])
def test_check_config_exits_2_and_logs_the_variables(role, env, work, spy):
    env.setenv("APP_ENV", "prod")
    env.setenv("DATABASE_PASSWORD", "")

    assert cli.main([role, "--check-config"]) == 2

    [(level, event, fields)] = spy.events
    assert (level, event) == (ERROR, "config.validation.failed")
    assert fields["role"] == role
    assert "DATABASE_PASSWORD" in fields["variables"]
    assert work.calls == {}
    assert spy.closed == 1


def test_the_failure_log_names_variables_never_values(env, work, spy):
    env.setenv("AUTH_MODE", "dev")
    env.setenv("APP_ENV", "prod")
    env.setenv("API_PORT", "s3cr3t-port")

    assert cli.main(["api", "--check-config"]) == 2

    [(_, _, fields)] = spy.events
    assert set(fields) == {"role", "variables"}
    text = repr(fields)
    assert "s3cr3t-port" not in text and "k" * 40 not in text
    assert {"API_PORT", "AUTH_MODE", "AUTH_DEV_SIGNING_KEY"} <= set(fields["variables"])


def test_the_worker_check_config_honours_queue_options(env, work, spy):
    for name in ("NEO4J_URI", "AGENT_MODEL", "LLM_GATEWAY_URL", "DATABASE_AGENT_PASSWORD"):
        env.delenv(name)

    assert cli.main(["worker", "--queue", "parse", "--check-config"]) == 0
    assert cli.main(["worker", "--check-config"]) == 2


# --- running a role -------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "key"),
    [(["api"], "api"), (["worker"], "worker"), (["migrate"], "migrate")],
)
def test_a_role_runs_its_module_with_the_loaded_settings(argv, key, env, work, spy):
    assert cli.main(argv) == 0

    [settings] = work.calls[key]
    assert settings.role == argv[0]
    assert spy.closed == 1


def test_the_worker_passes_its_queue_options_to_the_settings(env, work, spy):
    for name in ("NEO4J_URI", "AGENT_MODEL", "LLM_GATEWAY_URL", "DATABASE_AGENT_PASSWORD"):
        env.delenv(name)

    assert cli.main(["worker", "--queue", "parse"]) == 0

    assert work.calls["worker"][0].temporal.worker_queues == ("parse",)


@pytest.mark.parametrize(("result", "code"), [(None, 0), (0, 0), (True, 0), (3, 3), (False, 1)])
def test_the_exit_code_follows_the_run_result(result, code, env, work, spy):
    work.results["api"] = result

    assert cli.main(["api"]) == code


def test_an_async_run_is_awaited(env, work, spy, monkeypatch):
    async def finished():
        return 4

    work.results["migrate"] = None
    monkeypatch.setattr(sys.modules["db.migrate"], "run", lambda _settings: finished())

    assert cli.main(["migrate"]) == 4


@pytest.mark.parametrize("argv", [["api"], ["worker"], ["migrate"]])
def test_a_refused_configuration_never_imports_the_work_module(argv, env, work, spy):
    env.setenv("DATABASE_HOST", "")

    assert cli.main(argv) == 2

    assert work.calls == {}
    assert spy.closed == 1
    assert spy.events[0][1] == "config.validation.failed"


def test_worker_check_runs_the_health_checks(env, work, spy):
    work.results["check"] = True
    assert cli.main(["worker", "--check"]) == 0
    work.results["check"] = False
    assert cli.main(["worker", "--check"]) == 1

    assert work.calls["check"] == ("worker",)
    assert "worker" not in work.calls


def test_worker_check_receives_the_parsed_settings_with_the_queue_flag(env, work, spy):
    """R-code LOW 7: --queue must reach the checks, not be lost by a reload from the env."""

    for name in ("NEO4J_URI", "AGENT_MODEL", "LLM_GATEWAY_URL", "DATABASE_AGENT_PASSWORD"):
        env.delenv(name)  # a parse-only environment: the reload would refuse it
    work.results["check"] = True

    assert cli.main(["worker", "--queue", "parse", "--check"]) == 0

    settings = work.calls["check_kwargs"]["settings"]
    assert settings.role == "worker"
    assert settings.temporal.worker_queues == ("parse",)


# --- missing modules and failures ----------------------------------------


@pytest.mark.parametrize(
    ("argv", "constant"),
    [
        (["api"], "_API_MODULE"),
        (["worker"], "_WORKER_MODULE"),
        (["migrate"], "_MIGRATE_MODULE"),
        (["worker", "--check"], "_HEALTH_MODULE"),
    ],
)
def test_a_missing_work_module_is_a_clear_error_not_a_crash(argv, constant, env, spy, monkeypatch):
    monkeypatch.setattr(cli, constant, "specflow_not_built_yet.server")

    assert cli.main(argv) == 1

    [(level, event, fields)] = spy.events
    assert (level, event) == (ERROR, "cli.module.unavailable")
    assert fields["target_module"] == "specflow_not_built_yet.server"
    assert spy.closed == 1


def test_an_import_error_inside_a_module_is_not_swallowed(env, work, spy, monkeypatch):
    def broken(name):
        raise ModuleNotFoundError("No module named 'dependency'", name="dependency")

    monkeypatch.setattr(cli.importlib, "import_module", broken)

    with pytest.raises(ModuleNotFoundError):
        cli.main(["api"])

    assert spy.closed == 1


# --- logger ownership (AC-2.5) -------------------------------------------


@pytest.mark.parametrize("argv", [["api"], ["worker"], ["migrate"], ["api", "--check-config"]])
def test_each_command_closes_the_logger_exactly_once(argv, env, work, spy):
    cli.main(argv)

    assert spy.closed == 1


def test_the_logger_is_closed_once_when_the_role_raises(env, work, spy):
    work.results["api"] = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        cli.main(["api"])

    assert spy.closed == 1


def test_each_command_runs_in_its_own_request_context(env, work, spy, monkeypatch):
    seen = []

    def run(settings):
        seen.append(current_request_id())

    monkeypatch.setattr(sys.modules["api.server"], "run", run)

    cli.main(["api"])
    cli.main(["api"])

    assert seen[0] and seen[1] and seen[0] != seen[1]


class Check:
    def __init__(self, *, ok: bool) -> None:
        self.ok = ok


@pytest.mark.parametrize(
    ("result", "code"),
    [
        ({"db": Check(ok=True), "temporal": Check(ok=True)}, 0),
        ({"db": Check(ok=True), "temporal": Check(ok=False)}, 1),
        ({"db": True, "storage": True}, 0),
        ({"db": True, "storage": False}, 1),
        ({}, 1),
    ],
)
def test_a_mapping_of_checks_exits_0_only_when_all_are_ok(result, code):
    assert cli._exit_code(result) == code


def test_check_config_refuses_an_api_audience_equal_to_a_client_id(env, work, spy):
    """FR-MCP-AUTH-12: ``specflow api --check-config`` fails naming AUTH_API_AUDIENCE."""

    env.setenv("AUTH_API_AUDIENCE", "specflow-web")

    assert cli.main(["api", "--check-config"]) == cli.EXIT_CONFIGURATION

    [(level, event, fields)] = spy.events
    assert (level, event) == (ERROR, "config.validation.failed")
    assert fields["variables"] == ["AUTH_API_AUDIENCE"]
    assert work.calls == {}
