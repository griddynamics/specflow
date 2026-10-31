"""Agent QA of the MCP server with Claude Code (docs/mcp-server/testing.md, Agent QA with Claude Code).

Gated: ``SPECFLOW_TEST_MCP_CLAUDE=1`` (run with ``--env-file ../.env.local``); never in CI. It spends model tokens and
writes to the user's Claude Code configuration, so it needs the ``oidc`` stack, a seeded fixture, a signed-in `claude`
CLI and a free port 8765:

    SPECFLOW_TEST_MCP_CLAUDE=1 uv run --env-file ../.env.local pytest tests/test_mcp_claude_qa.py -q

One test is one full flow in a fresh temp git repository: the driver registers ``specflow-qa-<run id>`` (local scope,
in that folder), signs in over plain HTTP, runs the four steps of the workspace flow (bind P-Alpha, reuse it, a P-Beta
binding the agent must not use, switch to P-Empty) and, always, logs out, removes the server and deletes the folder; a
server left behind fails the run. ``SPECFLOW_QA_RUNS=3`` runs three flows (the gate: all pass); the default is 1.
Assertions read tool events and the binding file only, never the model's prose (which goes into the report).
A missing precondition fails with its reason; nothing here skips except the gate.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn

import pytest
import pytest_asyncio
from pydantic import SecretStr

from logger.logger import INFO, get_logger, log_event
from mcp_qa_lib import (
    CLAUDE_MODEL,
    LOCAL_DEFAULTS,
    FixtureIds,
    FlowContext,
    QaError,
    RunReport,
    StepRecord,
    TeardownVerdict,
    WorkspaceFlow,
    claude_signed_in,
    flow_failure,
    flow_workspace,
    new_run_id,
    parse_runs,
    port_is_free,
    require_env,
    save_report,
    seeded_ids,
    signed_in_claude,
    stack_mode,
    step_result,
)
from specflow_live import live_enabled, requires_live

pytestmark = [requires_live("MCP_CLAUDE"), pytest.mark.asyncio(loop_scope="module")]

logger = get_logger("tests.mcp_claude_qa")

REQUIRED_ENV: Final = ("KEYCLOAK_QA_PASSWORD", "KEYCLOAK_ADMIN_PASSWORD", "DATABASE_PASSWORD")
RUNS: Final = parse_runs(os.environ.get("SPECFLOW_QA_RUNS", "1")) if live_enabled("MCP_CLAUDE") else 1


@dataclass
class Stack:
    """What every run shares: the QA password, the seeded ids and the report built from all runs."""

    password: SecretStr
    ids: FixtureIds
    report: RunReport
    cost_usd: float = 0.0


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
async def stack(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Stack]:
    with pytest.MonkeyPatch.context() as patch:
        for name, value in LOCAL_DEFAULTS.items():
            if not os.environ.get(name, "").strip():
                patch.setenv(name, value)
        try:
            secrets_ = require_env(*REQUIRED_ENV)
        except SystemExit as missing:
            precondition(str(missing))
        await require_stack(tmp_path_factory.mktemp("claude-qa-probe"))  # only `claude auth status` runs here
        report = RunReport(new_run_id(), "agent-flow", [], {"model": CLAUDE_MODEL, "runs": RUNS})
        state = Stack(secrets_["KEYCLOAK_QA_PASSWORD"], await seeded_ids("keycloak"), report)
        failed = False
        try:
            yield state
        except BaseException:
            failed = True
            raise
        finally:
            report.metrics["cost_usd"] = round(state.cost_usd, 4)
            save_report(report, raise_on_refusal=not failed)


@pytest.mark.parametrize("run_number", range(1, RUNS + 1))
async def test_workspace_flow(run_number: int, stack: Stack) -> None:
    """One full flow in a fresh git repository: register, sign in, four steps, log out, remove, delete."""

    verdict, records = TeardownVerdict(), list[StepRecord]()
    try:
        async with (
            flow_workspace(verdict) as workspace,
            signed_in_claude("alice", stack.password, cwd=workspace, run_id=new_run_id(), verdict=verdict) as name,
        ):
            flow = WorkspaceFlow(FlowContext(name, workspace, stack.ids), model=CLAUDE_MODEL)
            try:
                await flow.run()
            finally:
                records = flow.records
    except QaError as failed:
        precondition(str(failed))
    finally:  # the report is built from what ran, also when a step or the teardown failed
        stack.report.results.extend(step_result(record, run_number) for record in records)
        stack.report.metrics[f"run{run_number}_teardown_clean"] = verdict.label
        stack.cost_usd += sum(record.cost_usd for record in records)
        log_event(logger, INFO, "mcp_claude_qa.flow", run=run_number, steps=len(records))
    reason = flow_failure(records)
    if reason is not None:
        pytest.fail(reason, pytrace=False)
