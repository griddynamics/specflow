"""Offline tests of the MCP QA command line (`examples/mcp_qa.py`, docs/mcp-server/testing.md, MCP-M7).

The CLI is thin glue over `mcp_qa_lib`. Library seams that reach an external system (the MCP session, the sign-in,
the latency pacer, the `claude` runner, the stack probe) are replaced on the `mcp_qa` module; everything else (the
library's `call`, `all_projects`, `report_note`, `write_report`, `RunReport`, `dev_token`, ...) is the real code. The
report folder is a `tmp_path`. Nothing here needs the network, Keycloak, Docker or `claude`.
"""

from __future__ import annotations

import functools
import json
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from mcp.types import TextContent
from pydantic import SecretStr

import mcp_qa
import mcp_qa_lib
from mcp_qa_lib import (
    CLAUDE_MAX_BUDGET_USD,
    CLAUDE_MODEL,
    FixtureIds,
    LatencyStats,
    PromptRun,
    QaError,
    TeardownVerdict,
    ToolEvent,
    claude_tool_names,
)

QA_CREDENTIAL = SecretStr("qa-credential-0001")
SIGNING_KEY = SecretStr("dev-signing-key-0000000000000000")
BEARER = SecretStr("bearer-token-0001")
RUN_ID = "run0001"
SERVER = f"specflow-qa-{RUN_ID}"
TOOLS = claude_tool_names(SERVER)
ALPHA, LARGE, BETA = (str(uuid.uuid4()) for _ in range(3))
SECRETS = tuple(secret.get_secret_value() for secret in (QA_CREDENTIAL, SIGNING_KEY, BEARER))
PASSWORD = QA_CREDENTIAL.get_secret_value()


# Fakes of the external boundaries ---------------------------------------------------------------------------------


class FakeResult:
    def __init__(self, structured: dict[str, Any], *, is_error: bool = False) -> None:
        self.structured_content = structured
        self.is_error = is_error
        self.content = [TextContent(type="text", text=json.dumps(structured))]


class FakeSession:
    """An MCP session over canned data: alice's projects (P-Beta is hers only in the foreign-name test)."""

    def __init__(self, projects: list[tuple[str, str]], *, features: int = 7) -> None:
        self.projects = projects
        self.features = features
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=[SimpleNamespace(name="list_projects"), SimpleNamespace(name="list_features")])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> FakeResult:
        self.calls.append((name, arguments))
        if name == "list_projects":
            items = [{"project_id": pid, "name": project} for pid, project in self.projects]
            return FakeResult({"items": items, "next_cursor": None})
        return FakeResult({"project_id": arguments["project_id"], "total_count": self.features, "items": []})


def install_session(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> list[dict[str, Any]]:
    """Replace `mcp_session`; the returned list records how each session was opened (auth or token)."""

    opened: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_session(**kwargs: Any) -> AsyncIterator[FakeSession]:
        opened.append(kwargs)
        yield session

    monkeypatch.setattr(mcp_qa, "mcp_session", fake_session)
    return opened


def install_stack(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    async def fake_mode() -> str:
        return mode

    monkeypatch.setattr(mcp_qa, "stack_mode", fake_mode)


@pytest.fixture(autouse=True)
def qa_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_QA_PASSWORD", PASSWORD)
    monkeypatch.setenv("AUTH_DEV_SIGNING_KEY", SIGNING_KEY.get_secret_value())


def assert_no_secret(output: str) -> None:
    assert not [secret for secret in SECRETS if secret in output]
    assert "@" not in output
    assert "eyJ" not in output


def check_args(user: str = "alice", seconds: float = 0) -> SimpleNamespace:
    return SimpleNamespace(user=user, latency_seconds=seconds)


# parse_args -------------------------------------------------------------------------------------------------------


def parse(monkeypatch: pytest.MonkeyPatch, *argv: str) -> Any:
    monkeypatch.setattr(sys, "argv", ["mcp_qa.py", *argv])
    return mcp_qa.parse_args()


@pytest.mark.parametrize(
    ("argv", "handler", "expected"),
    [
        (["seed"], mcp_qa.seed_command, {"mode": "keycloak"}),
        (["seed", "--mode", "dev"], mcp_qa.seed_command, {"mode": "dev"}),
        (["token", "bob"], mcp_qa.token_command, {"user": "bob", "client": "mcp"}),
        (["token", "alice", "--client", "web"], mcp_qa.token_command, {"user": "alice", "client": "web"}),
        (
            ["dev-token", "alice@example.com"],
            mcp_qa.dev_token_command,
            {"email": "alice@example.com", "client": "mcp", "headers": False},
        ),
        (
            ["dev-token", "bob@example.com", "--client", "web", "--headers"],
            mcp_qa.dev_token_command,
            {"email": "bob@example.com", "client": "web", "headers": True},
        ),
        (["check"], mcp_qa.check_command, {"user": "alice", "latency_seconds": 0}),
        (
            ["check", "--user", "bob", "--latency-seconds", "2.5"],
            mcp_qa.check_command,
            {"user": "bob", "latency_seconds": 2.5},
        ),
        (
            ["claude"],
            mcp_qa.claude_command,
            {"user": "alice", "model": CLAUDE_MODEL, "max_budget_usd": CLAUDE_MAX_BUDGET_USD, "prompt": None},
        ),
        (
            [
                "claude",
                "--user",
                "bob",
                "--model",
                "opus",
                "--max-budget-usd",
                "0.50",
                "--prompt",
                "a",
                "--prompt",
                "b",
            ],
            mcp_qa.claude_command,
            {"user": "bob", "model": "opus", "max_budget_usd": "0.50", "prompt": ["a", "b"]},
        ),
    ],
    ids=[
        "seed",
        "seed-dev",
        "token",
        "token-web",
        "dev-token",
        "dev-token-flags",
        "check",
        "check-flags",
        "claude",
        "claude-flags",
    ],
)
def test_parse_args_reads_every_command_and_flag(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], handler: Callable[..., Awaitable[int]], expected: dict[str, Any]
) -> None:
    args = parse(monkeypatch, *argv)

    assert args.command == argv[0]
    assert args.handler is handler
    assert {name: getattr(args, name) for name in expected} == expected


@pytest.mark.parametrize(
    "argv",
    [[], ["nope"], ["seed", "--mode", "other"], ["token"], ["token", "alice", "--client", "x"], ["dev-token"]],
    ids=["no-command", "unknown-command", "bad-mode", "token-needs-user", "bad-client", "dev-token-needs-email"],
)
def test_parse_args_refuses_bad_input(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        parse(monkeypatch, *argv)

    assert stopped.value.code == 2


def test_user_key_accepts_a_key_or_an_email_and_refuses_the_rest() -> None:
    assert (mcp_qa._user_key("alice"), mcp_qa._user_key("bob@example.com")) == ("alice", "bob")
    with pytest.raises(SystemExit, match="unknown QA user"):
        mcp_qa._user_key("mallory")


# check: NFR-MCP-01 smoke and latency -------------------------------------------------------------------------------


async def test_check_in_oidc_mode_signs_in_with_the_provider_and_prints_names_counts_and_ms(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession([(ALPHA, "P-Alpha"), (LARGE, "P-Large")])
    opened = install_session(monkeypatch, session)
    install_stack(monkeypatch, "oidc")
    providers: list[tuple[str, str]] = []

    def fake_provider(key: str, password: Any) -> str:
        providers.append((key, password.get_secret_value()))
        return "provider"

    monkeypatch.setattr(mcp_qa, "oauth_provider", fake_provider)

    assert await mcp_qa.check_command(check_args()) == 0

    out = capsys.readouterr().out.splitlines()
    assert providers == [("alice", PASSWORD)]
    assert opened == [{"auth": "provider"}]
    assert out[0] == "mode oidc"
    assert out[1].startswith("tools list_projects, list_features (") and out[1].endswith(" ms)")
    assert out[2].startswith("projects 2 (")
    assert out[3] == "project names P-Alpha, P-Large"
    assert out[4] == "P-Alpha features 7"
    assert len(out) == 5
    assert_no_secret("\n".join(out))
    assert ("list_features", {"project_id": ALPHA}) in session.calls


async def test_check_in_dev_mode_uses_a_dev_token_header_and_prints_no_project_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened = install_session(monkeypatch, FakeSession([(ALPHA, "P-Alpha")]))
    install_stack(monkeypatch, "dev")

    assert await mcp_qa.check_command(check_args("bob")) == 0

    out = capsys.readouterr().out
    assert [line.split(" (")[0] for line in out.splitlines()] == [
        "mode dev",
        "tools list_projects, list_features",
        "projects 1",
    ]
    assert list(opened[0]) == ["token"] and opened[0]["token"].count(".") == 2
    assert opened[0]["token"] not in out
    assert_no_secret(out)


async def test_check_reports_a_missing_p_alpha_without_a_feature_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession([(LARGE, "P-Large")])
    install_session(monkeypatch, session)
    install_stack(monkeypatch, "oidc")
    monkeypatch.setattr(mcp_qa, "oauth_provider", lambda _key, _password: "provider")

    await mcp_qa.check_command(check_args())

    assert "P-Alpha features" not in capsys.readouterr().out
    assert [name for name, _ in session.calls] == ["list_projects"]


async def test_check_latency_prints_the_e14_numbers_and_paces_list_features_on_p_large_nfr_mcp_01(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # E14 / NFR-MCP-01: the CLI hands run_latency a call that lists P-Large's features, then prints the stats only.
    session = FakeSession([(ALPHA, "P-Alpha"), (LARGE, "P-Large")])
    install_session(monkeypatch, session)
    install_stack(monkeypatch, "dev")
    paced: list[dict[str, Any]] = []

    async def fake_latency(call_once: Callable[[], Awaitable[bool]], **kwargs: Any) -> LatencyStats:
        paced.append(kwargs)
        assert await call_once() is True
        return LatencyStats(calls=600, errors=1, p50_ms=27.64, p95_ms=37.04, max_ms=54.95, duration_s=60.0)

    monkeypatch.setattr(mcp_qa, "run_latency", fake_latency)

    await mcp_qa.check_command(check_args(seconds=60))

    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "latency calls 600 errors 1 p50 27.6 p95 37.0 max 55.0 ms"
    assert paced == [{"duration_s": 60}]
    assert session.calls[-1] == ("list_features", {"project_id": LARGE})
    assert_no_secret("\n".join(out))


async def test_check_latency_needs_the_seeded_p_large(monkeypatch: pytest.MonkeyPatch) -> None:
    install_session(monkeypatch, FakeSession([(ALPHA, "P-Alpha")]))
    install_stack(monkeypatch, "dev")

    with pytest.raises(SystemExit, match="P-Large is not listed: run the seed"):
        await mcp_qa.check_command(check_args(seconds=1))


async def test_check_turns_a_library_error_into_a_clean_exit_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unreachable() -> str:
        raise QaError("MCP endpoint not reachable: run make up")

    monkeypatch.setattr(mcp_qa, "stack_mode", unreachable)

    with pytest.raises(SystemExit, match="MCP endpoint not reachable"):
        await mcp_qa.check_command(check_args())


# _print_run -------------------------------------------------------------------------------------------------------


def stream_event(
    kind: Literal["call", "result"], name: str | None, payload: Any, *, is_error: bool = False
) -> ToolEvent:
    return ToolEvent(kind, f"id-{name}", name, payload, is_error)


def prompt_run(events: list[ToolEvent], result: dict[str, Any] | None, *, timed_out: bool = False) -> PromptRun:
    return PromptRun(None if timed_out else 0, timed_out, [], events, None, result, "")


def test_print_run_prints_tool_names_counts_codes_and_a_safe_prose_line(capsys: pytest.CaptureFixture[str]) -> None:
    list_features, list_projects = TOOLS[1], TOOLS[0]
    items = {"items": [{"name": "FEAT secret feature name"}, {"name": "other"}]}
    events = [
        stream_event("call", list_projects, {}),
        stream_event("result", list_projects, json.dumps({"items": [{"name": "P-Alpha"}]})),
        stream_event("call", list_features, {"project_id": ALPHA, "cursor": "opaque-cursor-value"}),
        stream_event("result", list_features, [{"type": "text", "text": json.dumps(items)}]),
        stream_event("call", list_features, {"project_id": BETA}),
        stream_event("result", list_features, json.dumps({"error": {"code": "project_not_found"}}), is_error=True),
        stream_event("call", "mcp__other__tool", {"project_id": "not-ours"}),
    ]
    leaked = BEARER.get_secret_value()
    result = {
        "num_turns": 4,
        "total_cost_usd": 0.0123,
        "result": f"Your projects: alice@example.com see https://x.example/?code=abc {leaked} Bearer abc",
    }

    mcp_qa._print_run("agent-1", prompt_run(events, result), TOOLS)

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[:6] == [
        "agent-1: exit 0",
        "  call list_projects project_id=- cursor=no",
        "  result list_projects items 1",
        f"  call list_features project_id={ALPHA} cursor=yes",
        "  result list_features items 2",
        f"  call list_features project_id={BETA} cursor=no",
    ]
    assert lines[6] == "  result list_features error project_not_found"
    assert lines[7] == "  turns 4 cost_usd 0.0123"
    assert lines[8].startswith("  prose Your projects: ")
    assert len(lines) == 9
    for hidden in (
        "secret feature name",
        "opaque-cursor-value",
        "not-ours",
        "alice@example.com",
        "x.example",
        "code=",
        "Bearer",
    ):
        assert hidden not in out


def test_print_run_marks_a_timeout_and_a_run_without_a_result(capsys: pytest.CaptureFixture[str]) -> None:
    mcp_qa._print_run("agent-2", prompt_run([], None, timed_out=True), TOOLS)

    assert capsys.readouterr().out.splitlines() == [
        "agent-2: exit None (timed out)",
        "  turns - cost_usd -",
        "  prose ",
    ]


# claude: agent tier, report in `finally` ---------------------------------------------------------------------------


class ClaudeHarness:
    """The `claude` command with its external seams faked: stack probe, sign-in, runner, subprocess, temp folder."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.reports = tmp_path / "reports"
        self.cwd = tmp_path / "specflow-qa-claude-test"
        self.runs: list[PromptRun] = []
        self.argvs: list[list[str]] = []
        self.sessions: list[dict[str, Any]] = []
        self.teardown_clean: bool | None = True
        self.body_error: Exception | None = None
        self.prompt_error: Exception | None = None
        self.signed_in = True
        self.mode = "oidc"
        self.port_free = True
        patch = functools.partial(monkeypatch.setattr, mcp_qa)
        patch("new_run_id", lambda: RUN_ID)
        patch("write_report", functools.partial(mcp_qa_lib.write_report, directory=self.reports))
        patch("port_is_free", lambda: self.port_free)
        patch("stack_mode", self._stack_mode)
        patch("claude_signed_in", self._signed_in)
        patch("seeded_ids", self._ids)
        patch("run_prompt", self._run_prompt)
        patch("signed_in_claude", self._signed_in_claude)
        patch("tempfile", SimpleNamespace(mkdtemp=self._mkdtemp))

    def _mkdtemp(self, prefix: str) -> str:
        assert prefix == "specflow-qa-claude-"
        self.cwd.mkdir()
        return str(self.cwd)

    async def _stack_mode(self) -> str:
        return self.mode

    async def _signed_in(self, cwd: Path) -> bool:
        assert cwd == self.cwd
        return self.signed_in

    async def _ids(self, mode: str) -> FixtureIds:
        assert mode == "keycloak"
        return FixtureIds({"alice": uuid.uuid4()}, {"P-Beta": uuid.UUID(BETA)})

    async def _run_prompt(self, argv: list[str], cwd: Path) -> PromptRun:
        assert cwd == self.cwd
        self.argvs.append(argv)
        if not self.runs:
            raise self.prompt_error or AssertionError("no canned run left")
        return self.runs.pop(0)

    @asynccontextmanager
    async def _signed_in_claude(self, key: str, password: Any, **kwargs: Any) -> AsyncIterator[str]:
        self.sessions.append({"key": key, "password": password.get_secret_value(), "run_id": kwargs["run_id"]})
        verdict: TeardownVerdict = kwargs["verdict"]
        try:
            yield SERVER
            if self.body_error is not None:
                raise self.body_error
        finally:
            verdict.clean = self.teardown_clean

    def report(self) -> dict[str, Any]:
        return json.loads((self.reports / f"{RUN_ID}.json").read_text(encoding="utf-8"))


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ClaudeHarness:
    return ClaudeHarness(monkeypatch, tmp_path)


def claude_args(*prompts: str, user: str = "alice") -> SimpleNamespace:
    return SimpleNamespace(user=user, model="sonnet", max_budget_usd="1.00", prompt=list(prompts) or None)


def good_run(items: int = 1) -> PromptRun:
    events = [
        stream_event("call", TOOLS[0], {}),
        stream_event("result", TOOLS[0], json.dumps({"items": [{}] * items})),
    ]
    return prompt_run(events, {"is_error": False, "num_turns": 2, "total_cost_usd": 0.25, "result": "done"})


async def test_claude_records_a_passed_prompt_and_writes_the_report_with_a_clean_teardown(
    harness: ClaudeHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.runs = [good_run(), good_run()]

    assert await mcp_qa.claude_command(claude_args("first prompt", "second prompt")) == 0

    out = capsys.readouterr().out
    report = harness.report()
    assert (report["run_id"], report["kind"]) == (RUN_ID, "agent-manual")
    assert [(r["scenario"], r["outcome"]) for r in report["results"]] == [
        ("manual-1", "passed"),
        ("manual-2", "passed"),
    ]
    assert report["metrics"] == {
        "model": "sonnet",
        "cwd": harness.cwd.name,
        "teardown_clean": "yes",
        "cost_usd": 0.5,
        "turns": 4,
    }
    assert "teardown clean yes" in out and f"report {RUN_ID}.json" in out and "recovery" not in out
    assert harness.sessions == [{"key": "alice", "password": PASSWORD, "run_id": RUN_ID}]
    assert "first prompt" in harness.argvs[0] and "second prompt" in harness.argvs[1]
    assert not harness.cwd.exists()  # a clean teardown removes the temp folder
    assert_no_secret(out + json.dumps(report))


async def test_claude_without_prompts_runs_the_three_personas_with_the_seeded_beta_id(
    harness: ClaudeHarness,
) -> None:
    harness.runs = [good_run(), good_run(), good_run()]

    await mcp_qa.claude_command(claude_args())

    assert [r["scenario"] for r in harness.report()["results"]] == ["agent-1", "agent-2", "agent-3"]
    assert BETA in harness.argvs[2][2]  # the prompt text of the third persona


async def test_claude_marks_an_errored_result_and_a_missing_result_as_failed(harness: ClaudeHarness) -> None:
    errored = prompt_run([], {"is_error": True, "num_turns": 1, "total_cost_usd": 0.1, "result": "boom"})
    harness.runs = [errored, prompt_run([], None, timed_out=True)]

    await mcp_qa.claude_command(claude_args("a", "b"))

    assert [r["outcome"] for r in harness.report()["results"]] == ["failed", "failed"]


async def test_claude_marks_foreign_tool_calls_as_failed_and_names_them(
    harness: ClaudeHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    run = good_run()
    run.events.append(stream_event("call", "Bash", {"command": "ls"}))
    harness.runs = [run]

    await mcp_qa.claude_command(claude_args("a"))

    (result,) = harness.report()["results"]
    assert result["outcome"] == "failed"
    assert result["note"].startswith("foreign tools ['Bash']")
    assert "tool calls outside our two tools: ['Bash']" in capsys.readouterr().out


async def test_claude_writes_the_report_and_the_recovery_hint_and_keeps_the_folder_on_an_unclean_teardown(
    harness: ClaudeHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.runs = [good_run()]
    harness.teardown_clean = False
    harness.body_error = QaError("claude teardown left a specflow-qa- server")

    with pytest.raises(SystemExit, match="claude teardown left"):
        await mcp_qa.claude_command(claude_args("a"))

    out = capsys.readouterr().out
    assert harness.report()["metrics"]["teardown_clean"] == "no"
    assert "teardown clean no" in out
    assert (
        f"recovery: server {SERVER}; from the temp folder {harness.cwd.name} run "
        f"`claude mcp logout {SERVER}` and `claude mcp remove {SERVER} --scope local`"
    ) in out
    assert str(harness.cwd.parent) not in out  # the folder name only, never the path
    assert harness.cwd.exists()  # kept: the recovery runs from it


async def test_claude_writes_the_report_when_the_run_fails_before_a_teardown_is_reached(
    harness: ClaudeHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.mode = "dev"

    with pytest.raises(SystemExit, match="needs the oidc stack"):
        await mcp_qa.claude_command(claude_args("a"))

    assert "teardown clean not reached" in capsys.readouterr().out
    report = harness.report()
    assert report["results"] == [] and "teardown_clean" not in report["metrics"]
    assert not harness.cwd.exists() and harness.sessions == []


async def test_claude_refuses_a_busy_callback_port_before_touching_the_claude_configuration(
    harness: ClaudeHarness,
) -> None:
    harness.port_free = False

    with pytest.raises(SystemExit, match="port 8765 is in use"):
        await mcp_qa.claude_command(claude_args("a"))

    assert harness.sessions == [] and not harness.cwd.exists()
    assert harness.report()["kind"] == "agent-manual"


async def test_claude_stops_when_the_cli_is_not_signed_in_and_removes_the_folder(harness: ClaudeHarness) -> None:
    harness.signed_in = False

    with pytest.raises(SystemExit, match="the claude CLI is not signed in"):
        await mcp_qa.claude_command(claude_args("a"))

    assert harness.sessions == [] and not harness.cwd.exists()
    assert harness.report()["results"] == []


@pytest.mark.parametrize(
    ("failure", "escapes_as"),
    [(QaError("claude is not on PATH"), SystemExit), (OSError("spawn failed"), OSError)],
    ids=["qa-error-exits-cleanly", "os-error-propagates"],
)
async def test_claude_keeps_the_first_result_and_still_tears_down_when_run_prompt_raises_mid_run(
    harness: ClaudeHarness, capsys: pytest.CaptureFixture[str], failure: Exception, escapes_as: type[BaseException]
) -> None:
    harness.runs = [good_run()]  # the second prompt finds no canned run and raises `failure`
    harness.prompt_error = failure

    with pytest.raises(escapes_as, match=str(failure)):
        await mcp_qa.claude_command(claude_args("first", "second"))

    out = capsys.readouterr().out
    report = harness.report()
    assert [(r["scenario"], r["outcome"]) for r in report["results"]] == [("manual-1", "passed")]
    assert report["metrics"]["teardown_clean"] == "yes"  # the fake teardown ran in its own `finally`
    assert "teardown clean yes" in out and f"report {RUN_ID}.json" in out
    assert not harness.cwd.exists()
