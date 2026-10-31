"""QA commands of the MCP server (docs/mcp-server/testing.md): seed, tokens, a smoke check, Claude Code prompts.

Run from backend/ with the local stack up (`make up WITH=keycloak`):

    uv run --env-file ../.env.local python examples/mcp_qa.py seed [--mode keycloak|dev]
    uv run --env-file ../.env.local python examples/mcp_qa.py token alice [--client mcp|web]
    uv run --env-file ../.env.local python examples/mcp_qa.py dev-token alice@example.com [--client mcp|web] [--headers]
    uv run --env-file ../.env.local python examples/mcp_qa.py check [--user alice|bob] [--latency-seconds N]
    uv run --env-file ../.env.local python examples/mcp_qa.py claude [--user alice] [--model sonnet] [--prompt TEXT ...]

`seed` prints ids and counts. `token` and `dev-token` are the only commands that print a token (nothing else on the
line), so `TOKEN=$(... token alice)` works; `dev-token --headers` prints the `headersHelper` JSON instead. Passwords,
secrets and emails are never printed.

`check` signs in with the MCP Python SDK (OAuth provider in `oidc` mode, a dev token in dev mode) and prints the mode,
tool names, project names and counts, and milliseconds (`--latency-seconds` adds an E14-style run on `P-Large`).
`claude` registers a temporary `specflow-qa-<run id>` server with the Claude Code CLI (`oidc` mode only), signs in,
runs the three persona prompts (or each `--prompt`) and prints our tool calls and results; it spends model tokens,
writes to the Claude configuration and always logs out and removes
the server; the report is written even when the run fails. Only `claude` writes a report (to `.reports/mcp-qa/`).
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession

from example_lib import run_example
from mcp_qa_lib import (
    CLAUDE_MAX_BUDGET_USD,
    CLAUDE_MODEL,
    QA_USERS,
    PromptRun,
    QaError,
    RunReport,
    ScenarioResult,
    StackMode,
    TeardownVerdict,
    all_projects,
    apply_local_defaults,
    call,
    claude_prompt_argv,
    claude_signed_in,
    claude_tool_names,
    dev_token,
    foreign_tool_calls,
    headers_json,
    mcp_session,
    new_run_id,
    oauth_provider,
    persona_prompts,
    port_is_free,
    report_note,
    require_env,
    run_latency,
    run_prompt,
    seed,
    seeded_ids,
    signed_in_claude,
    stack_mode,
    tool_json,
    user_token,
    write_report,
)


def _user_key(user: str) -> str:
    """The QA user key for `alice` or her email."""

    key = next((name for name, email in QA_USERS.items() if user in (name, email)), None)
    if key is None:
        raise SystemExit("unknown QA user (expected one of: " + ", ".join(QA_USERS) + ")")
    return key


async def seed_command(args: argparse.Namespace) -> int:
    report = await seed(args.mode)
    print("\n".join(report.lines()))
    return 0


async def token_command(args: argparse.Namespace) -> int:
    names = ("KEYCLOAK_QA_PASSWORD", *(("KEYCLOAK_WEB_CLIENT_SECRET",) if args.client == "web" else ()))
    secrets_ = require_env(*names)
    try:
        token = await user_token(
            _user_key(args.user),
            secrets_["KEYCLOAK_QA_PASSWORD"],
            client=args.client,
            web_secret=secrets_.get("KEYCLOAK_WEB_CLIENT_SECRET"),
        )
    except QaError as error:
        raise SystemExit(str(error)) from None
    print(token)
    return 0


async def dev_token_command(args: argparse.Namespace) -> int:
    signing_key = require_env("AUTH_DEV_SIGNING_KEY")["AUTH_DEV_SIGNING_KEY"]
    token = dev_token(args.email, signing_key, client=args.client)
    print(headers_json(token) if args.headers else token)
    return 0


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


async def _smoke(session: ClientSession, mode: StackMode) -> None:
    started = time.perf_counter()
    tools = [tool.name for tool in (await session.list_tools()).tools]
    print(f"tools {', '.join(tools)} ({_ms(started)} ms)")
    started = time.perf_counter()
    projects = await all_projects(session)
    print(f"projects {len(projects)} ({_ms(started)} ms)")
    if mode == "dev":
        return
    print("project names " + ", ".join(sorted(str(project["name"]) for project in projects)))
    alpha = next((project["project_id"] for project in projects if project["name"] == "P-Alpha"), None)
    if alpha is not None:
        outcome = await call(session, "list_features", {"project_id": alpha})
        print(f"P-Alpha features {outcome.data.get('total_count', 'error ' + str(outcome.code))}")


async def _bearer(mode: StackMode, key: str) -> str:
    if mode == "oidc":
        return await user_token(key, require_env("KEYCLOAK_QA_PASSWORD")["KEYCLOAK_QA_PASSWORD"])
    return dev_token(QA_USERS[key], require_env("AUTH_DEV_SIGNING_KEY")["AUTH_DEV_SIGNING_KEY"])


async def _latency(mode: StackMode, key: str, seconds: float) -> None:
    async with mcp_session(token=await _bearer(mode, key)) as session:
        large = next((p["project_id"] for p in await all_projects(session) if p["name"] == "P-Large"), None)
        if large is None:
            raise QaError("P-Large is not listed: run the seed")

        async def once() -> bool:
            return not (await call(session, "list_features", {"project_id": large})).is_error

        stats = await run_latency(once, duration_s=seconds)
    print(
        f"latency calls {stats.calls} errors {stats.errors} p50 {stats.p50_ms:.1f} p95 {stats.p95_ms:.1f}"
        f" max {stats.max_ms:.1f} ms"
    )


async def check_command(args: argparse.Namespace) -> int:
    key = _user_key(args.user)
    try:
        mode = await stack_mode()
        print(f"mode {mode}")
        if mode == "oidc":
            password = require_env("KEYCLOAK_QA_PASSWORD")["KEYCLOAK_QA_PASSWORD"]
            async with mcp_session(auth=oauth_provider(key, password)) as session:
                await _smoke(session, mode)
        else:
            async with mcp_session(token=await _bearer(mode, key)) as session:
                await _smoke(session, mode)
        if args.latency_seconds > 0:
            await _latency(mode, key, args.latency_seconds)
    except QaError as error:
        raise SystemExit(str(error)) from None
    return 0


def _print_run(label: str, run: PromptRun, tools: tuple[str, str]) -> None:
    print(f"{label}: exit {run.exit_code}{' (timed out)' if run.timed_out else ''}")
    for event in run.events:
        if event.name not in tools:
            continue
        short = str(event.name).rsplit("__", maxsplit=1)[-1]
        if event.kind == "call":
            arguments: dict[str, Any] = event.payload if isinstance(event.payload, dict) else {}
            cursor = "yes" if arguments.get("cursor") else "no"
            print(f"  call {short} project_id={arguments.get('project_id', '-')} cursor={cursor}")
            continue
        data = tool_json(event)
        error = data.get("error") if isinstance(data, dict) else None
        count = len(data.get("items", [])) if isinstance(data, dict) else 0
        print(f"  result {short} " + (f"error {error.get('code')}" if isinstance(error, dict) else f"items {count}"))
    result = run.result or {}
    print(f"  turns {result.get('num_turns', '-')} cost_usd {result.get('total_cost_usd', '-')}")
    print(f"  prose {report_note(run.prose, 2000)}")


async def _run_prompts(
    prompts: list[tuple[str, str]],
    name: str,
    cwd: Path,
    args: argparse.Namespace,
    *,
    results: list[ScenarioResult],
    metrics: dict[str, float | int | str],
) -> None:
    tools = claude_tool_names(name)
    cost, turns = 0.0, 0
    for label, text in prompts:
        started = time.perf_counter()
        run = await run_prompt(
            claude_prompt_argv(text, tools, model=args.model, max_budget_usd=args.max_budget_usd), cwd
        )
        _print_run(label, run, tools)
        foreign = sorted(set(foreign_tool_calls(run.events, tools)))
        if foreign:
            print(f"  tool calls outside our two tools: {foreign}")
        done = run.result is not None and not run.result.get("is_error") and not foreign
        cost += float((run.result or {}).get("total_cost_usd", 0) or 0)
        turns += int((run.result or {}).get("num_turns", 0) or 0)
        note = f"foreign tools {foreign} " if foreign else ""
        results.append(
            ScenarioResult(label, "passed" if done else "failed", _ms(started), note + report_note(run.prose))
        )
        metrics.update({"cost_usd": round(cost, 4), "turns": turns})


async def _claude_run(
    args: argparse.Namespace,
    run_id: str,
    results: list[ScenarioResult],
    metrics: dict[str, float | int | str],
) -> None:
    password = require_env("KEYCLOAK_QA_PASSWORD")["KEYCLOAK_QA_PASSWORD"]
    if await stack_mode() != "oidc":
        raise QaError("the claude command needs the oidc stack: run make up WITH=keycloak")
    if not port_is_free():
        raise QaError("port 8765 is in use: stop the process holding it")
    verdict = TeardownVerdict()
    cwd = Path(tempfile.mkdtemp(prefix="specflow-qa-claude-"))  # never the repo root: its tracked .mcp.json
    metrics["cwd"] = cwd.name  # the folder name only; it lives in the system temp folder
    try:
        if not await claude_signed_in(cwd):
            raise QaError("the claude CLI is not signed in")
        prompts = (
            [(f"manual-{n}", text) for n, text in enumerate(args.prompt, start=1)]
            if args.prompt
            else persona_prompts(await seeded_ids("keycloak"))
        )

        try:
            async with signed_in_claude(
                _user_key(args.user), password, cwd=cwd, run_id=run_id, verdict=verdict
            ) as name:
                await _run_prompts(prompts, name, cwd, args, results=results, metrics=metrics)
        finally:
            metrics["teardown_clean"] = verdict.label
    finally:
        if verdict.clean is not False:  # an unclean teardown keeps the folder: recovery runs from it
            shutil.rmtree(cwd, ignore_errors=True)


async def claude_command(args: argparse.Namespace) -> int:
    run_id = new_run_id()
    results: list[ScenarioResult] = []
    metrics: dict[str, float | int | str] = {"model": args.model}
    try:
        await _claude_run(args, run_id, results, metrics)
    except QaError as error:
        raise SystemExit(str(error)) from None
    finally:
        print(f"teardown clean {metrics.get('teardown_clean', 'not reached')}")
        if metrics.get("teardown_clean") == "no":
            print(
                f"recovery: server specflow-qa-{run_id}; from the temp folder {metrics['cwd']} run "
                f"`claude mcp logout specflow-qa-{run_id}` and `claude mcp remove specflow-qa-{run_id} --scope local`"
            )
        try:
            print(f"report {write_report(RunReport(run_id, 'agent-manual', results, metrics)).name}")
        except QaError as refused:
            print(str(refused))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)

    seeding = commands.add_parser("seed", help="idempotent seed of Keycloak, Postgres and Neo4j; prints ids and counts")
    seeding.add_argument("--mode", choices=("keycloak", "dev"), default="keycloak", help="subject kind of the users")
    seeding.set_defaults(handler=seed_command)

    token = commands.add_parser("token", help="print the access token of a QA user (authorization code with PKCE)")
    token.add_argument("user", help="alice, bob or carol (or the email)")
    token.add_argument("--client", choices=("mcp", "web"), default="mcp")
    token.set_defaults(handler=token_command)

    dev = commands.add_parser("dev-token", help="print an HS256 dev token (needs AUTH_DEV_SIGNING_KEY; works offline)")
    dev.add_argument("email")
    dev.add_argument("--client", choices=("mcp", "web"), default="mcp")
    dev.add_argument("--headers", action="store_true", help="print the headersHelper JSON instead of the token")
    dev.set_defaults(handler=dev_token_command)

    check = commands.add_parser(
        "check", help="smoke: sign in with the MCP SDK, list tools and projects (names, counts, ms)"
    )
    check.add_argument("--user", default="alice", help="alice or bob")
    check.add_argument("--latency-seconds", type=float, default=0, help="also run 10 list_features calls/s on P-Large")
    check.set_defaults(handler=check_command)

    agent = commands.add_parser("claude", help="run prompts through Claude Code against the oidc stack (spends tokens)")
    agent.add_argument("--user", default="alice", help="the QA user to sign in as")
    agent.add_argument("--model", default=CLAUDE_MODEL)
    agent.add_argument("--max-budget-usd", default=CLAUDE_MAX_BUDGET_USD, help="spend limit per prompt")
    agent.add_argument("--prompt", action="append", help="a prompt to run (repeatable); default: the three personas")
    agent.set_defaults(handler=claude_command)
    return parser.parse_args()


async def main() -> int:
    apply_local_defaults(os.environ)
    args = parse_args()
    return int(await args.handler(args))


if __name__ == "__main__":
    raise SystemExit(run_example(main))
