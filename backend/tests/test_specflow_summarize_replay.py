"""Tier 1 (offline): replay of recorded summarize histories (AC-8.1, AC-8.4).

The fixtures ``fixtures/temporal/summarize_*.json`` are histories recorded from
the tier-2 runs (``SPECFLOW_RECORD_FIXTURES=1``), scrubbed (worker identities)
and checked for secrets when written. A history above the fixture size ceiling
is stored gzip-compressed as ``summarize_*.json.gz`` and decompressed on read. Replay needs no server and no model:
the plugin only supplies its interceptors and payload converter.
"""

from __future__ import annotations

import base64
import json
import re
import socket
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import WorkflowHistory
from temporalio.openai_agents.testing import (
    ResponseBuilders,
    TestModel,
    TestModelProvider,
)
from temporalio.worker import Replayer

from agent_runtime.plugin import build_plugin
from settings import load_settings
from specflow_summarize_live_support import (
    FIXED_WORKER_IDENTITY,
    FIXTURES,
    WORKER_IDENTITY,
    fixture_name,
    read_fixture_text,
    scrub_history,
    summarize_fixture_path,
)
from temporal.agents.workflows import SummarizeDocumentWorkflow

# success and max_turns were re-recorded without the graph tools (A1).
# llm_gateway_success is not recorded: the host summarizer's model (gpt-5.6-luna)
# rejects function tools together with reasoning_effort on Chat Completions, so
# the llm_gateway_live recording fails. It stays in EXPECTED and skips until it is
# re-recorded with SPECFLOW_RECORD_FIXTURES=1 (llm_gateway_live).
EXPECTED = ("success", "tool_failed", "max_turns", "llm_gateway_success")
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


def _fixture(name: str) -> Path:
    return summarize_fixture_path(name)


def _all_fixtures() -> list[Any]:
    found = {fixture_name(p) for p in FIXTURES.glob("summarize_*.json*")}
    return [
        pytest.param(
            name,
            marks=pytest.mark.skipif(
                not _fixture(name).exists(),
                reason=f"summarize_{name}.json not recorded yet (tier 2 with SPECFLOW_RECORD_FIXTURES=1)",
            ),
        )
        for name in sorted(found | set(EXPECTED))
    ]


@pytest.mark.parametrize("name", _all_fixtures())
async def test_the_recorded_history_replays(name, valid_env):
    path = _fixture(name)
    history = WorkflowHistory.from_json(f"summarize-{name}", read_fixture_text(path))
    plugin = build_plugin(
        load_settings("worker", valid_env),
        otel=False,
        model_provider=TestModelProvider(TestModel.returning_responses([ResponseBuilders.output_message("unused")])),
    )
    result = await Replayer(workflows=[SummarizeDocumentWorkflow], plugins=[plugin]).replay_workflow(
        history, raise_on_replay_failure=False
    )
    assert result.replay_failure is None, repr(result.replay_failure)[:500]


@pytest.mark.parametrize("name", _all_fixtures())
def test_the_fixture_is_scrubbed_and_small(name):
    data = json.loads(read_fixture_text(_fixture(name)))
    text = json.dumps(data)
    identities = {value for key, value in _items(data) if key == "identity" and isinstance(value, str)}
    assert identities <= {"pytest-worker"}
    assert all(value == "" for key, value in _items(data) if key == "stackTrace")
    assert "/" + "Users/" not in text and "/home/" not in text
    assert len(text.encode()) < 8 * MAX_PAYLOAD_BYTES
    for marker in ("PASSWORD", "api_key", "Bearer ", "sk-lf-", "pk-lf-"):
        assert marker not in text


def _items(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _items(value)
    elif isinstance(node, list):
        for item in node:
            yield from _items(item)


def _decoded_texts(data):
    for key, value in _items(data):
        if key == "data" and isinstance(value, str):
            try:
                yield base64.b64decode(value, validate=True).decode()
            except ValueError:
                continue


@pytest.mark.parametrize("name", _all_fixtures())
def test_no_email_or_url_user_info_is_left_in_a_fixture(name):
    """F4: payloads are base64, so they are decoded before looking."""

    data = json.loads(read_fixture_text(_fixture(name)))
    texts = [json.dumps(data), *_decoded_texts(data)]
    for text in texts:
        found = [
            e
            # The lookbehind starts a match only at the start of a run, so long base64 runs
            # are scanned once (the unanchored pattern is quadratic on large fixtures).
            for e in re.findall(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
            if e != "redacted@example.invalid"
        ]
        assert found == []
        assert not re.search(r"://(?!redacted@)[^\s/\"]*@", text)


def test_scrub_history_masks_emails_and_url_user_info_inside_payloads():
    private_text = "Jane.Doe+x@university.example.org see https://bob:pw@host.example/a"
    payload = {
        "data": base64.b64encode(json.dumps({"t": private_text}).encode()).decode(),
        "metadata": {"encoding": "anNvbi9wbGFpbg=="},
    }
    out = json.loads(scrub_history(json.dumps({"events": [{"payloads": [payload]}]})))
    [clean] = _decoded_texts(out)
    assert "Jane.Doe" not in clean and "bob" not in clean and "pw@" not in clean
    assert "redacted@example.invalid" in clean and "https://redacted@host.example/a" in clean
    assert json.loads(clean)  # still valid JSON


PARSE_FIXTURES = sorted(FIXTURES.glob("parse_*.json"))


def _fixture_paths():
    return [*sorted(FIXTURES.glob("summarize_*.json*")), *PARSE_FIXTURES]


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_no_fixture_carries_a_pid_at_host_worker_identity(path):
    """M3: ``<pid>@<host>`` (also inside payloads) is always the fixed ``1@worker``."""

    data = json.loads(read_fixture_text(path))
    for text in [json.dumps(data), *_decoded_texts(data)]:
        assert set(WORKER_IDENTITY.findall(text)) <= {FIXED_WORKER_IDENTITY}


def test_scrub_history_normalises_worker_identities_in_strings_and_payloads():
    host = socket.gethostname().split(".")[0]
    sticky = "4242@pytest-host-0bb9cc82d118469bb7513d10db338ad6"
    payload = {
        "data": base64.b64encode(json.dumps({"w": f"77@{host}-ab12cd34ef"}).encode()).decode(),
        "metadata": {"encoding": "anNvbi9wbGFpbg=="},
    }
    history = {
        "events": [
            {"attributes": {"stickyTaskQueue": {"name": f"{sticky}-q"}, "identity": "9@h"}},
            {"payloads": [payload]},
        ]
    }
    out = json.loads(scrub_history(json.dumps(history)))
    text = json.dumps(out)
    assert "4242@" not in text and sticky not in text
    assert f'"name": "{FIXED_WORKER_IDENTITY}-q"' in text
    assert '"identity": "pytest-worker"' in text
    [clean] = _decoded_texts(out)
    assert json.loads(clean) == {"w": FIXED_WORKER_IDENTITY}
