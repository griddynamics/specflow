"""Tier 2 with the real model: AC-9.5 (5 gateway runs) and AC-11.3 (Langfuse).

Gates: ``SPECFLOW_TEST_LLM_GATEWAY=1`` plus ``SPECFLOW_TEST_TEMPORAL/POSTGRES=1``
(``uv run --env-file ../.env.local``). Each test costs gateway agent runs
(AC-9.5: 5, AC-11.3: 2); run this file on purpose only.

Tracing is configured ONCE per process from the real env (Langfuse local):
the OTel global tracer provider can be set only once. Per-run outcomes (no
content) are appended as JSON lines to ``SPECFLOW_RUN_LOG`` when it is set.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry import trace as otel_trace
from temporalio.client import WorkflowFailureError

from agent_runtime import tracing
from settings import load_settings
from specflow_live import requires_live
from specflow_summarize_live_support import (
    history_events,
    record_fixture,
    scheduled_activities,
    secret_values,
    summarize_stack,
    synthetic_markdown,
)
from temporal.agents.tools import READ_DOCUMENT, SUBMIT_SUMMARY

pytest_plugins = ["specflow_db_live_support"]
LANGFUSE_POLL_SECONDS = 3
pytestmark = [
    requires_live("LLM_GATEWAY"),
    requires_live("TEMPORAL"),
    requires_live("POSTGRES"),
]

PUBLIC_MARKDOWN = Path(__file__).resolve().parent / "data" / "specs" / "extraction.md"
V2_OBSERVATIONS = "/api/public/v2/observations"
TOOLS = {READ_DOCUMENT, SUBMIT_SUMMARY}
_TRACING: dict[str, Any] = {}


def _settings():
    env = {
        **os.environ,
        "TEMPORAL_NAMESPACE": "specflow-local",
        "WORKER_QUEUES": "agents",
    }
    return load_settings("worker", env)


def _configure_tracing_once(settings) -> bool:
    if "handle" not in _TRACING:
        _TRACING["handle"] = tracing.configure(settings)
    return _TRACING["handle"].enabled


def _flush() -> None:
    provider = otel_trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()


def _log_run(entry: dict[str, Any]) -> None:
    path = os.environ.get("SPECFLOW_RUN_LOG")
    if path:
        with Path(path).open("a") as handle:
            handle.write(json.dumps(entry) + "\n")


@pytest.fixture
def live_args(temporal_live, migrated_database, agent_role):
    settings = _settings()
    otel = _configure_tracing_once(settings)
    return {
        "settings": settings,
        "temporal_live": temporal_live,
        "database": migrated_database,
        "agent_role": agent_role,
        "model_provider": None,  # the real LLM gateway provider from the settings
        "otel": otel,
    }


async def _run_once(stack, seeded, *, label: str, sensitive: bool) -> dict[str, Any]:
    started = time.monotonic()
    handle = await stack.start(
        seeded,
        model=stack_model(),
        trace_sensitive_data=sensitive,
        name=f"{label}-{seeded.document_id.hex[:8]}",
    )
    entry: dict[str, Any] = {"label": label, "workflow_id": handle.id}
    try:
        result = await asyncio.wait_for(handle.result(), 15 * 60)
        entry.update(
            outcome="succeeded",
            summary_chars=result.summary_chars,
            requests=result.usage.requests,
        )
    except WorkflowFailureError as error:
        entry.update(
            outcome="failed",
            error=getattr(error.cause, "message", type(error.cause).__name__),
        )
    events = await history_events(handle)
    entry["tools"] = [name for name, _ in scheduled_activities(events) if name != "invoke_model_activity"]
    entry["model_calls"] = sum(1 for name, _ in scheduled_activities(events) if name == "invoke_model_activity")
    entry["seconds"] = round(time.monotonic() - started, 1)
    row = await stack.document_row(seeded)
    entry["db_status"] = row.summarization_status
    entry["db_summary_non_empty"] = bool(row.summarization)
    entry["handle"] = handle
    return entry


def stack_model() -> str:
    return os.environ["AGENT_MODEL"]


async def test_ac_9_5_five_real_runs_at_least_four_succeed(live_args):
    """C27: fewer than 4 of 5 -> the step stops and escalates (no tuning)."""

    markdown = PUBLIC_MARKDOWN.read_text()
    outcomes = []
    async with summarize_stack(**live_args) as stack:
        for index in range(5):
            seeded = await stack.seed(markdown=markdown, filename="extraction.pdf")
            entry = await _run_once(stack, seeded, label=f"ac95-{index + 1}", sensitive=True)
            handle = entry.pop("handle")
            if index == 0 and entry["outcome"] == "succeeded":
                await record_fixture(handle, "llm_gateway_success")
            _log_run(entry)
            outcomes.append(entry)
    _flush()
    good = [
        e
        for e in outcomes
        if e["outcome"] == "succeeded"
        and e["db_status"] == "succeeded"
        and e["db_summary_non_empty"]
        and e["seconds"] < 15 * 60
    ]
    assert len(good) >= 4, [{k: v for k, v in e.items() if k != "workflow_id"} for e in outcomes]


# ---------------------------------------------------------------- AC-11.3
def _langfuse_get(params: dict[str, Any]) -> dict[str, Any]:
    base = os.environ["LANGFUSE_BASE_URL"].rstrip("/")
    auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    response = httpx.get(f"{base}{V2_OBSERVATIONS}", params=params, auth=auth, timeout=30)
    assert response.status_code == 200, response.status_code
    return response.json()


async def _root_observation(workflow_id: str, since: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while True:
        body = _langfuse_get(
            {
                "isRootObservation": "true",
                "fromStartTime": since,
                "fields": "core,basic,trace_context",
                "limit": 100,
            }
        )
        for row in body.get("data", []):
            if row.get("sessionId") == workflow_id and row.get("name") == "summarize-document":
                return row
        assert time.monotonic() < deadline, "no root observation within 60 s"
        await asyncio.sleep(LANGFUSE_POLL_SECONDS)


@pytest.mark.parametrize("sensitive", [True, False], ids=["sensitive-true", "sensitive-false"])
async def test_ac_11_3_langfuse_trace_for_both_sensitive_data_values(live_args, sensitive):
    assert live_args["otel"] is True, "Langfuse must be configured in the env"
    label = f"ac113-{str(sensitive).lower()}"
    since = (datetime.now(UTC) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    async with summarize_stack(**live_args) as stack:
        seeded = await stack.seed(markdown=synthetic_markdown(label, chars=12_000))
        entry = await _run_once(stack, seeded, label=label, sensitive=sensitive)
        entry.pop("handle")
    _flush()

    root = await _root_observation(entry["workflow_id"], since)
    assert root["type"] == "AGENT"
    rows = _langfuse_get(
        {
            "traceId": root["traceId"],
            "fromStartTime": since,
            "fields": "core,basic,model,usage",
            "limit": 200,
        }
    )["data"]
    generations = [r for r in rows if r.get("type") == "GENERATION"]
    tool_names = {r.get("name") for r in rows if r.get("type") == "TOOL"}
    agents = {r.get("name") for r in rows if r.get("type") == "AGENT"}
    io_body = json.dumps(
        _langfuse_get(
            {
                "traceId": root["traceId"],
                "fromStartTime": since,
                "fields": "core,basic,model,usage,io",
                "limit": 200,
            }
        )
    )
    canary_present = f"CANARY-{label}" in io_body
    entry.update(
        generations=len(generations),
        tool_names=sorted(n for n in tool_names if n),
        canary_in_io=canary_present,
        trace_found=True,
    )
    _log_run(entry)

    assert entry["outcome"] == "succeeded"
    assert any((g.get("usageDetails") or {}).get("input", 0) > 0 for g in generations)
    assert tool_names >= TOOLS
    assert {"summarize-document", "document-summarizer"} <= agents
    for value in secret_values():
        assert value not in io_body
    if not sensitive:
        assert canary_present is False
