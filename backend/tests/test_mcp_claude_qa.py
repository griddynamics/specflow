"""Agent QA of the MCP server with Claude Code (docs/mcp-server/testing.md, Agent QA with Claude Code).

Gated: ``SPECFLOW_TEST_MCP_CLAUDE=1`` (run with ``--env-file ../.env.local``); never in CI. It spends model tokens and
writes to the user's Claude Code configuration, so it needs the ``oidc`` stack, a seeded fixture, a signed-in `claude`
CLI and a free port 8765:

    SPECFLOW_TEST_MCP_CLAUDE=1 uv run --env-file ../.env.local pytest tests/test_mcp_claude_qa.py -q

The driver registers ``specflow-qa-<run id>`` (local scope, in a temporary folder), signs in over plain HTTP, runs the
three persona prompts and, in a ``finally``, logs out and removes the server; a server left behind fails the module.
Assertions read the tool events of our two tools only, never the model's prose (which goes into the report).
A missing precondition fails with its reason; nothing here skips except the gate.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, NoReturn

import pytest
import pytest_asyncio

from logger.logger import INFO, get_logger, log_event
from mcp_qa_lib import (
    CLAUDE_MODEL,
    LOCAL_DEFAULTS,
    PERSONA_CHECKS,
    PERSONAS,
    FixtureIds,
    PromptRun,
    QaError,
    RunPreconditionError,
    RunReport,
    TeardownVerdict,
    check_run,
    claude_prompt_argv,
    claude_signed_in,
    claude_tool_names,
    new_run_id,
    persona_prompts,
    port_is_free,
    report_note,
    require_env,
    run_prompt,
    run_scenario,
    save_report,
    seeded_ids,
    signed_in_claude,
    stack_mode,
)
from specflow_live import requires_live

pytestmark = [requires_live("MCP_CLAUDE"), pytest.mark.asyncio(loop_scope="module")]

logger = get_logger("tests.mcp_claude_qa")

REQUIRED_ENV: Final = ("KEYCLOAK_QA_PASSWORD", "KEYCLOAK_ADMIN_PASSWORD", "DATABASE_PASSWORD")


@dataclass
class Agent:
    """The registered, signed-in server and the run state the report is built from."""

    name: str
    cwd: Path
    ids: FixtureIds
    report: RunReport
    cost_usd: float = 0.0
    turns: int = 0

    @property
    def tools(self) -> tuple[str, str]:
        return claude_tool_names(self.name)


def precondition(reason: str) -> NoReturn:
    pytest.fail(reason, pytrace=False)


async def require_stack(cwd: Path) -> None:
    """Fail (never skip) with the reason when the CLI, its sign-in, the stack mode or the callback port is not ready."""

    if shutil.which("claude") is None:
        precondition("the claude CLI is not on PATH")
    if not await claude_signed_in(cwd):
        precondition("the claude CLI is not signed in (run `claude auth login`)")
    if await stack_mode() != "oidc":
        precondition("the stack is not in oidc mode (run `make up WITH=keycloak`)")
    if not port_is_free():
        precondition("port 8765 is in use (stop the process holding it)")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def agent(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Agent]:
    with pytest.MonkeyPatch.context() as patch:
        for name, value in LOCAL_DEFAULTS.items():
            if not os.environ.get(name, "").strip():
                patch.setenv(name, value)
        try:
            secrets_ = require_env(*REQUIRED_ENV)
        except SystemExit as missing:
            precondition(str(missing))
        cwd = tmp_path_factory.mktemp("claude-qa")  # outside the repository: its tracked .mcp.json is not ours
        await require_stack(cwd)
        ids = await seeded_ids("keycloak")
        run_id = new_run_id()
        report = RunReport(run_id, "agent", [], {"model": CLAUDE_MODEL, "cwd": cwd.name})  # the folder name only
        state: Agent | None = None
        verdict = TeardownVerdict()
        failed = False
        try:
            async with signed_in_claude(
                "alice", secrets_["KEYCLOAK_QA_PASSWORD"], cwd=cwd, run_id=run_id, verdict=verdict
            ) as name:
                state = Agent(name, cwd, ids, report)
                yield state
        except BaseException:
            failed = True
            raise
        finally:
            report.metrics["teardown_clean"] = verdict.label
            if state is not None:
                report.metrics.update({"cost_usd": round(state.cost_usd, 4), "turns": state.turns})
            save_report(report, raise_on_refusal=not failed)


def check(agent: Agent, run: PromptRun, persona: str) -> None:
    """The shared preconditions, then the persona's assertions (both in the library, unit-tested offline)."""

    try:
        totals = check_run(run, agent.name, agent.tools)
    except RunPreconditionError as reason:
        precondition(str(reason))
    agent.cost_usd += totals.cost_usd
    agent.turns += totals.turns
    try:
        PERSONA_CHECKS[persona](run, agent.tools, agent.ids)
    except QaError as failed:
        pytest.fail(str(failed), pytrace=False)


@pytest.mark.parametrize("persona", [key for key, _ in PERSONAS])
async def test_persona(persona: str, agent: Agent) -> None:
    prompt = dict(persona_prompts(agent.ids))[persona]
    run: PromptRun | None = None

    async def work() -> None:
        nonlocal run
        started = time.perf_counter()
        run = await run_prompt(claude_prompt_argv(prompt, agent.tools), agent.cwd)
        log_event(
            logger, INFO, "mcp_claude_qa.prompt", persona=persona, seconds=round(time.perf_counter() - started, 1)
        )
        check(agent, run, persona)

    try:
        await run_scenario(agent.report, persona, work())
    finally:
        if run is not None:  # the model's prose is recorded, never asserted
            last = agent.report.results[-1]
            agent.report.results[-1] = replace(last, note=f"{last.note} {report_note(run.prose)}".strip())
