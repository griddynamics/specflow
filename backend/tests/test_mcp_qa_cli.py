"""Offline tests of the MCP QA command line (`examples/mcp_qa.py`, docs/mcp-server/testing.md, MCP-M7).

The CLI is thin glue over `mcp_qa_lib`. Library seams that reach an external system (the MCP session, the sign-in,
the latency pacer, the `claude` runner, the stack probe) are replaced on the `mcp_qa` module; everything else (the
library's `call`, `all_projects`, `report_note`, `write_report`, `RunReport`, `dev_token`, ...) is the real code. The
report folder is a `tmp_path`. Nothing here needs the network, Keycloak, Docker or `claude`.
"""

from __future__ import annotations

import functools
import json
import shutil
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import TextContent
from pydantic import SecretStr

import mcp_qa
import mcp_qa_lib
from graph.core import load_ontology
from mcp_qa_lib import (
    CLAUDE_MAX_BUDGET_USD,
    CLAUDE_MODEL,
    REUSE_PROMPT,
    FixtureIds,
    LatencyStats,
    PromptRun,
    QaError,
    StepRecord,
    TeardownVerdict,
    ToolEvent,
    claude_tool_names,
    fixture_nodes,
    parse_stream_json,
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
            {"user": "alice", "model": CLAUDE_MODEL, "max_budget_usd": CLAUDE_MAX_BUDGET_USD, "runs": "1"},
        ),
        (
            [
                "claude",
                "--user",
                "bob",
                "--model",
                "opus",
                "--max-budget-usd",
                "0.25",
                "--runs",
                "3",
            ],
            mcp_qa.claude_command,
            {"user": "bob", "model": "opus", "max_budget_usd": "0.25", "runs": "3"},
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


# _print_step ------------------------------------------------------------------------------------------------------


def test_print_step_prints_outcome_mode_tools_and_a_safe_prose_line(capsys: pytest.CaptureFixture[str]) -> None:
    record = StepRecord(
        "2-reuse-alpha",
        "failed",
        ("Read", "list_features"),
        1.26,
        0.5,
        "-",
        "assertion: list_projects was called",
        "see https://example.com or a@b.c and Bearer abc",
    )

    mcp_qa._print_step(2, record)

    assert capsys.readouterr().out.splitlines() == [
        "run 2 2-reuse-alpha: failed (-) 1.3 s cost_usd 0.5000 tools Read,list_features",
        "  assertion: list_projects was called",
        "  prose see [url] or [email] and abc",
    ]


def test_print_step_marks_a_step_without_tools_or_note(capsys: pytest.CaptureFixture[str]) -> None:
    mcp_qa._print_step(1, StepRecord("1-bind-alpha", "passed", (), 0.0, 0.0, "slash"))

    assert capsys.readouterr().out.splitlines() == [
        "run 1 1-bind-alpha: passed (slash) 0.0 s cost_usd 0.0000 tools -",
        "  prose ",
    ]


# claude: the workspace flow, report in `finally` ------------------------------------------------------------------

EMPTY = str(uuid.uuid4())
SLASH = f"mcp__{SERVER}__select_project"


def message_init() -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "init",
        "mcp_servers": [{"name": SERVER, "status": "connected"}],
        "tools": list(TOOLS),
        "slash_commands": [SLASH],
    }


def message_call(use_id: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    block = {"type": "tool_use", "id": use_id, "name": tool, "input": arguments}
    return {"type": "assistant", "message": {"content": [block]}}


def message_result(use_id: str, data: dict[str, Any]) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": use_id, "content": [{"type": "text", "text": json.dumps(data)}]}
    return {"type": "user", "message": {"content": [block]}}


def message_done(prose: str = "done") -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "result": prose, "num_turns": 2, "total_cost_usd": 0.25}


def canned_run(*messages: dict[str, Any]) -> PromptRun:
    lines = [json.dumps(message) for message in messages]
    init = next(m for m in messages if m.get("subtype") == "init")
    result = next((m for m in reversed(messages) if m.get("type") == "result"), None)
    return PromptRun(
        0, timed_out=False, lines=lines, events=parse_stream_json(lines), init=init, result=result, stderr_tail=""
    )


class ClaudeHarness:
    """The `claude` command with its external seams faked: stack probe, sign-in, runner, workspace, ids."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.reports = tmp_path / "reports"
        self.root = tmp_path / "workspaces"
        self.root.mkdir()
        self.workspaces: list[Path] = []
        self.handlers: list[Callable[[Path], PromptRun | Exception]] = []
        self.argvs: list[list[str]] = []
        self.sessions: list[dict[str, Any]] = []
        self.teardown_clean: bool | None = True
        self.body_error: Exception | None = None
        self.signed_in = True
        self.mode = "oidc"
        self.port_free = True
        self.alpha_features = {
            node.spec_id for node in fixture_nodes("P-Alpha", ALPHA, load_ontology()) if node.label == "Feature"
        }
        patch = functools.partial(monkeypatch.setattr, mcp_qa)
        patch("new_run_id", lambda: RUN_ID)
        patch("write_report", functools.partial(mcp_qa_lib.write_report, directory=self.reports))
        patch("port_is_free", lambda: self.port_free)
        patch("stack_mode", self._stack_mode)
        patch("claude_signed_in", self._signed_in)
        patch("seeded_ids", self._ids)
        patch("run_prompt", self._run_prompt)
        patch("signed_in_claude", self._signed_in_claude)
        patch("flow_workspace", self._flow_workspace)
        self.handlers = self.good_flow()

    @property
    def cwd(self) -> Path:
        return self.workspaces[-1]

    def good_flow(self) -> list[Callable[[Path], PromptRun | Exception]]:
        def binds(project_id: str, name: str) -> Callable[[Path], PromptRun | Exception]:
            def handler(cwd: Path) -> PromptRun:
                path = mcp_qa_lib.write_binding(cwd, project_id, name)
                args = {"file_path": str(path), "content": "{}"}
                listed = {"items": [{"project_id": project_id, "name": name}], "next_cursor": None}
                return canned_run(
                    message_init(),
                    message_call("p1", TOOLS[0], {}),
                    message_result("p1", listed),
                    message_call("w1", "Write", args),
                    message_done(),
                )

            return handler

        def features(project_id: str, items: list[dict[str, Any]], total: int, error: str | None = None):
            def handler(_cwd: Path) -> PromptRun:
                page = (
                    {"error": {"code": error}} if error else {"total_count": total, "items": items, "next_cursor": None}
                )
                return canned_run(
                    message_init(),
                    message_call("u1", TOOLS[1], {"project_id": project_id}),
                    message_result("u1", page),
                    message_done(),
                )

            return handler

        alpha_items = [{"spec_id": spec_id} for spec_id in sorted(self.alpha_features)]
        return [
            binds(ALPHA, "P-Alpha"),
            features(ALPHA, alpha_items, len(alpha_items)),
            features(BETA, [], 0, "project_not_found"),
            binds(EMPTY, "P-Empty"),
            features(EMPTY, [], 0),
        ]

    async def _stack_mode(self) -> str:
        return self.mode

    async def _signed_in(self, cwd: Path) -> bool:
        assert cwd in self.workspaces
        return self.signed_in

    async def _ids(self, mode: str) -> FixtureIds:
        assert mode == "keycloak"
        projects = {"P-Alpha": uuid.UUID(ALPHA), "P-Beta": uuid.UUID(BETA), "P-Empty": uuid.UUID(EMPTY)}
        return FixtureIds({"alice": uuid.uuid4()}, projects)

    async def _run_prompt(self, argv: list[str], cwd: Path) -> PromptRun:
        assert cwd == self.cwd
        self.argvs.append(argv)
        if not self.handlers:
            raise AssertionError("no canned run left")
        outcome = self.handlers.pop(0)(cwd)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    @asynccontextmanager
    async def _flow_workspace(self, verdict: TeardownVerdict) -> AsyncIterator[Path]:
        cwd = self.root / f"specflow-qa-ws-{len(self.workspaces) + 1}"
        cwd.mkdir()
        self.workspaces.append(cwd)
        try:
            yield cwd
        finally:
            if verdict.clean is not False:  # the real context manager keeps an unclean folder
                shutil.rmtree(cwd, ignore_errors=True)

    @asynccontextmanager
    async def _signed_in_claude(self, key: str, password: Any, **kwargs: Any) -> AsyncIterator[str]:
        self.sessions.append({"key": key, "password": password.get_secret_value(), "cwd": kwargs["cwd"]})
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


def claude_args(runs: str = "1", user: str = "alice") -> SimpleNamespace:
    return SimpleNamespace(user=user, model="sonnet", max_budget_usd="0.50", runs=runs)


STEPS = ["1-bind-alpha", "2-reuse-alpha", "3-foreign-beta", "4a-switch-empty", "4b-reuse-empty"]


async def test_claude_runs_the_flow_in_a_fresh_workspace_and_writes_the_report_with_a_clean_teardown(
    harness: ClaudeHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await mcp_qa.claude_command(claude_args()) == 0

    out = capsys.readouterr().out
    report = harness.report()
    assert (report["run_id"], report["kind"]) == (RUN_ID, "agent-flow")
    assert [(r["scenario"], r["outcome"]) for r in report["results"]] == [(f"run1-{step}", "passed") for step in STEPS]
    assert report["metrics"] == {
        "model": "sonnet",
        "runs": "1",
        "cwd": harness.cwd.name,
        "teardown_clean": "yes",
        "cost_usd": 1.25,
    }
    assert "teardown clean yes" in out and f"report {RUN_ID}.json" in out and "recovery" not in out
    assert "run 1 1-bind-alpha: passed (slash)" in out and "run 1 4b-reuse-empty: passed (-)" in out
    assert [(s["key"], s["password"], s["cwd"]) for s in harness.sessions] == [("alice", PASSWORD, harness.cwd)]
    assert harness.argvs[0][2] == f"/mcp__{SERVER}__select_project P-Alpha"
    assert harness.argvs[3][2] == f"/mcp__{SERVER}__select_project P-Empty"
    assert [argv[2] for argv in harness.argvs[1:3]] == [REUSE_PROMPT, REUSE_PROMPT]
    assert all(argv[argv.index("--max-budget-usd") + 1] == "0.50" for argv in harness.argvs)
    assert not harness.cwd.exists()  # a clean teardown removes the workspace
    assert_no_secret(out + json.dumps(report))


async def test_claude_runs_each_requested_flow_in_its_own_workspace_and_registration(
    harness: ClaudeHarness,
) -> None:
    harness.handlers = [*harness.good_flow(), *harness.good_flow()]

    assert await mcp_qa.claude_command(claude_args(runs="2")) == 0

    report = harness.report()
    assert [r["scenario"] for r in report["results"]] == [f"run{n}-{step}" for n in (1, 2) for step in STEPS]
    assert len(harness.sessions) == 2 and len(set(harness.workspaces)) == 2
    assert report["metrics"]["cost_usd"] == 2.5 and report["metrics"]["runs"] == "2"
    assert not any(path.exists() for path in harness.workspaces)


async def test_claude_exits_1_when_a_step_fails_and_names_it_but_still_tears_down(
    harness: ClaudeHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.handlers[2] = lambda _cwd: canned_run(message_init(), message_done())  # step 3 never calls list_features

    assert await mcp_qa.claude_command(claude_args()) == 1

    out = capsys.readouterr().out
    report = harness.report()
    assert [(r["scenario"], r["outcome"]) for r in report["results"]] == [
        *((f"run1-{step}", "passed") for step in STEPS[:2]),
        ("run1-3-foreign-beta", "failed"),
    ]
    assert "run 1 failed: 3-foreign-beta: assertion: list_features was never called" in out
    assert report["metrics"]["teardown_clean"] == "yes" and not harness.cwd.exists()
    assert len(harness.argvs) == 3  # nothing after the failed step


async def test_claude_runs_the_remaining_flows_after_a_failed_one_and_still_exits_1(harness: ClaudeHarness) -> None:
    def no_write(_cwd: Path) -> PromptRun:
        listed = {"items": [], "next_cursor": None}
        calls = [message_call("p1", TOOLS[0], {}), message_result("p1", listed)]
        return canned_run(message_init(), *calls, message_done())

    harness.handlers = [no_write, *harness.good_flow()]

    assert await mcp_qa.claude_command(claude_args(runs="2")) == 1

    outcomes = [(r["scenario"], r["outcome"]) for r in harness.report()["results"]]
    assert outcomes[0] == ("run1-1-bind-alpha", "failed") and outcomes[-1] == ("run2-4b-reuse-empty", "passed")


async def test_claude_fails_a_step_whose_run_used_a_tool_d7_does_not_allow(harness: ClaudeHarness) -> None:
    first = harness.handlers[0]

    def with_bash(cwd: Path) -> PromptRun:
        run = first(cwd)
        assert isinstance(run, PromptRun)
        return replace(run, events=[*run.events, ToolEvent("call", "b1", "Bash", {"command": "ls"}, is_error=False)])

    harness.handlers[0] = with_bash

    assert await mcp_qa.claude_command(claude_args()) == 1

    (result,) = harness.report()["results"]
    assert result["outcome"] == "failed" and "Bash is not allowed" in result["note"]


async def test_claude_writes_the_report_and_the_recovery_hint_and_keeps_the_workspace_on_an_unclean_teardown(
    harness: ClaudeHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.teardown_clean = False
    harness.body_error = QaError("claude teardown left a specflow-qa- server")

    with pytest.raises(SystemExit, match="claude teardown left"):
        await mcp_qa.claude_command(claude_args())

    out = capsys.readouterr().out
    report = harness.report()
    assert report["metrics"]["teardown_clean"] == "no"
    assert [r["outcome"] for r in report["results"]] == ["passed"] * 5  # the steps ran; only the teardown failed
    assert "teardown clean no" in out
    assert f"from the temp folder {harness.cwd.name} run `claude mcp list`" in out
    assert str(harness.cwd.parent) not in out  # the folder name only, never the path
    assert harness.cwd.exists()  # kept: the recovery runs from it


async def test_claude_writes_the_report_when_the_run_fails_before_a_teardown_is_reached(
    harness: ClaudeHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.mode = "dev"

    with pytest.raises(SystemExit, match="needs the oidc stack"):
        await mcp_qa.claude_command(claude_args())

    assert "teardown clean not reached" in capsys.readouterr().out
    report = harness.report()
    assert report["results"] == [] and "teardown_clean" not in report["metrics"]
    assert harness.workspaces == [] and harness.sessions == []


async def test_claude_refuses_a_busy_callback_port_before_touching_the_claude_configuration(
    harness: ClaudeHarness,
) -> None:
    harness.port_free = False

    with pytest.raises(SystemExit, match="port 8765 is in use"):
        await mcp_qa.claude_command(claude_args())

    assert harness.sessions == [] and harness.workspaces == []
    assert harness.report()["kind"] == "agent-flow"


@pytest.mark.parametrize("runs", ["0", "two", "-1"])
async def test_claude_refuses_a_run_count_that_is_not_a_positive_integer(harness: ClaudeHarness, runs: str) -> None:
    with pytest.raises(SystemExit, match="positive integer"):
        await mcp_qa.claude_command(claude_args(runs=runs))

    assert harness.sessions == [] and harness.workspaces == []


async def test_claude_stops_when_the_cli_is_not_signed_in_and_removes_the_workspace(harness: ClaudeHarness) -> None:
    harness.signed_in = False

    with pytest.raises(SystemExit, match="the claude CLI is not signed in"):
        await mcp_qa.claude_command(claude_args())

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
    harness.handlers[1] = lambda _cwd: failure  # the second prompt's runner raises

    with pytest.raises(escapes_as, match=str(failure)):
        await mcp_qa.claude_command(claude_args())

    out = capsys.readouterr().out
    report = harness.report()
    assert [(r["scenario"], r["outcome"]) for r in report["results"]] == [("run1-1-bind-alpha", "passed")]
    assert report["metrics"]["teardown_clean"] == "yes"  # the fake teardown ran in its own `finally`
    assert "teardown clean yes" in out and f"report {RUN_ID}.json" in out
    assert not harness.cwd.exists()
