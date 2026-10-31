"""QA commands of the MCP server (docs/mcp-server/testing.md): seed, tokens, a smoke check, Claude Code prompts.

Run from backend/ with the local stack up (`make up WITH=keycloak`):

    uv run --env-file ../.env.local python examples/mcp_qa.py seed [--mode keycloak|dev]
    uv run --env-file ../.env.local python examples/mcp_qa.py token alice [--client mcp|web]
    uv run --env-file ../.env.local python examples/mcp_qa.py dev-token alice@example.com [--client mcp|web] [--headers]
    uv run --env-file ../.env.local python examples/mcp_qa.py check [--user alice|bob] [--latency-seconds N]
    uv run --env-file ../.env.local python examples/mcp_qa.py claude [--user alice] [--model sonnet] [--runs 3]

`seed` prints ids and counts. `token` and `dev-token` are the only commands that print a token (nothing else on the
line), so `TOKEN=$(... token alice)` works; `dev-token --headers` prints the `headersHelper` JSON instead. Passwords,
secrets and emails are never printed.

`check` signs in with the MCP Python SDK (OAuth provider in `oidc` mode, a dev token in dev mode) and prints the mode,
tool names, project names and counts, and milliseconds (`--latency-seconds` adds an E14-style run on `P-Large`).
`claude` runs the workspace flow of the agent tier `--runs` times (default 1; the gate is 3). Each run is a fresh
temporary git repository with a temporary `specflow-qa-<run id>` server registered with the Claude Code CLI (`oidc`
mode only), a sign-in and four steps (bind P-Alpha, reuse it, a P-Beta binding the agent must not use, switch to
P-Empty). It prints each step's outcome, tool calls, duration, cost and `slash` or `plain`; it spends model tokens,
writes to the Claude configuration and always logs out, removes the server and deletes the repository. The exit
status is 1 when a step fails. The report is written even when a run fails. Only `claude` writes a report (to
`.reports/mcp-qa/`).
"""

from __future__ import annotations

import argparse
import os
import time

from mcp.client.session import ClientSession
from pydantic import SecretStr

from example_lib import run_example
from mcp_qa_lib import (
    CLAUDE_MAX_BUDGET_USD,
    CLAUDE_MODEL,
    QA_USERS,
    SERVER_PREFIX,
    FixtureIds,
    FlowContext,
    QaError,
    RunReport,
    ScenarioResult,
    StackMode,
    StepRecord,
    TeardownVerdict,
    WorkspaceFlow,
    all_projects,
    apply_local_defaults,
    call,
    claude_signed_in,
    dev_token,
    flow_failure,
    flow_workspace,
    headers_json,
    mcp_session,
    new_run_id,
    oauth_provider,
    parse_runs,
    port_is_free,
    report_note,
    require_env,
    run_latency,
    run_prompt,
    seed,
    seeded_ids,
    signed_in_claude,
    stack_mode,
    step_result,
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


def _print_step(run_number: int, record: StepRecord) -> None:
    tools = ",".join(record.tool_calls) or "-"
    print(
        f"run {run_number} {record.step}: {record.outcome} ({record.mode}) {record.duration_s:.1f} s "
        f"cost_usd {record.cost_usd:.4f} tools {tools}"
    )
    if record.note:
        print(f"  {report_note(record.note)}")
    print(f"  prose {report_note(record.prose, 2000)}")


async def _one_flow(
    args: argparse.Namespace,
    password: SecretStr,
    ids: FixtureIds,
    *,
    run_number: int,
    records: list[StepRecord],
    metrics: dict[str, float | int | str],
) -> None:
    """One flow in a fresh repository; `records` fills as steps finish, so a failed teardown keeps them."""

    verdict = TeardownVerdict()
    try:
        async with flow_workspace(verdict) as workspace:
            if metrics.get("teardown_clean") != "no":
                metrics["cwd"] = workspace.name  # the folder name only; it lives in the system temp folder
            if not await claude_signed_in(workspace):
                raise QaError("the claude CLI is not signed in")
            async with signed_in_claude(
                _user_key(args.user), password, cwd=workspace, run_id=new_run_id(), verdict=verdict
            ) as name:
                context = FlowContext(name, workspace, ids)
                flow = WorkspaceFlow(context, run_prompt, model=args.model, max_budget_usd=args.max_budget_usd)
                try:
                    await flow.run()
                finally:
                    records.extend(flow.records)
    finally:
        unclean = verdict.label == "no" or metrics.get("teardown_clean") == "no"
        metrics["teardown_clean"] = "no" if unclean else verdict.label
        for record in records:
            _print_step(run_number, record)


async def _claude_run(
    args: argparse.Namespace, results: list[ScenarioResult], metrics: dict[str, float | int | str]
) -> bool:
    """Run the flows; True when every step of every run passed."""

    password = require_env("KEYCLOAK_QA_PASSWORD")["KEYCLOAK_QA_PASSWORD"]
    if await stack_mode() != "oidc":
        raise QaError("the claude command needs the oidc stack: run make up WITH=keycloak")
    if not port_is_free():
        raise QaError("port 8765 is in use: stop the process holding it")
    ids, cost, passed = await seeded_ids("keycloak"), 0.0, True
    for run_number in range(1, parse_runs(str(args.runs)) + 1):
        records: list[StepRecord] = []
        try:
            await _one_flow(args, password, ids, run_number=run_number, records=records, metrics=metrics)
        finally:
            results.extend(step_result(record, run_number) for record in records)
            cost += sum(record.cost_usd for record in records)
            metrics["cost_usd"] = round(cost, 4)
        reason = flow_failure(records)
        if reason is not None:
            print(f"run {run_number} failed: {report_note(reason)}")
            passed = False
    return passed


async def claude_command(args: argparse.Namespace) -> int:
    run_id = new_run_id()
    results: list[ScenarioResult] = []
    metrics: dict[str, float | int | str] = {"model": args.model, "runs": args.runs}
    passed = False
    try:
        passed = await _claude_run(args, results, metrics)
    except QaError as error:
        raise SystemExit(str(error)) from None
    finally:
        print(f"teardown clean {metrics.get('teardown_clean', 'not reached')}")
        if metrics.get("teardown_clean") == "no":
            print(
                f"recovery: a server named {SERVER_PREFIX}<run id> may be left; from the temp folder {metrics['cwd']} "
                "run `claude mcp list`, then `claude mcp logout <name>` and `claude mcp remove <name> --scope local`"
            )
        try:
            print(f"report {write_report(RunReport(run_id, 'agent-flow', results, metrics)).name}")
        except QaError as refused:
            print(str(refused))
    return 0 if passed else 1


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
    agent.add_argument("--runs", default="1", help="full flows to run, each in a fresh repository (the gate: 3)")
    agent.set_defaults(handler=claude_command)
    return parser.parse_args()


async def main() -> int:
    apply_local_defaults(os.environ)
    args = parse_args()
    return int(await args.handler(args))


if __name__ == "__main__":
    raise SystemExit(run_example(main))
