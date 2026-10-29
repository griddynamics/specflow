"""Tier 1: offline replay of histories recorded from tier-2 runs (AC-8.1, AC-8.4).

The fixtures in ``fixtures/temporal/parse_*.json`` are produced by
``SPECFLOW_PARSE_RECORD=1`` live runs of ``test_specflow_parse_live.py`` and
scrubbed by ``scrub_history`` (ids, queues, identities, stack traces).
"""

from __future__ import annotations

import base64
import json
import re

import pytest
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer

from specflow_parse_support import FIXED_WORKFLOW_ID, FIXTURE_DIR, find_leaks, scrub_history
from temporal.documents.workflows import ParseDocumentWorkflow
from test_specflow_parse_variants import ReorderedParseWorkflow

FIXTURES = ("parse_success.json", "parse_failed.json", "parse_not_found.json")


def load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


def replayer(workflow=ParseDocumentWorkflow) -> Replayer:
    return Replayer(workflows=[workflow], data_converter=pydantic_data_converter)


def test_the_committed_fixtures_exist():
    assert {p.name for p in FIXTURE_DIR.glob("parse_*.json")} >= set(FIXTURES)


@pytest.mark.parametrize("name", FIXTURES)
async def test_a_recorded_history_replays_without_nondeterminism(name):
    history = WorkflowHistory.from_json(FIXED_WORKFLOW_ID, load(name))

    await replayer().replay_workflow(history)


async def test_replaying_all_fixtures_in_one_call():
    async def histories():
        for name in FIXTURES:
            yield WorkflowHistory.from_json(FIXED_WORKFLOW_ID, load(name))

    results = await replayer().replay_workflows(histories())  # raises on the first failure

    assert not results.replay_failures


async def test_a_changed_workflow_fails_the_replay():
    """Control: the replay really checks commands against the recorded history."""

    history = WorkflowHistory.from_json(FIXED_WORKFLOW_ID, load("parse_success.json"))

    with pytest.raises(Exception, match=r"(?i)nondeterminism|non-deterministic"):
        await replayer(ReorderedParseWorkflow).replay_workflow(history)


@pytest.mark.parametrize("name", FIXTURES)
def test_fixtures_hold_no_paths_filenames_secrets_or_markdown(name):
    text = load(name)

    assert find_leaks(text) == []
    assert len(text) < 2 * 1024 * 1024
    payloads = [base64.b64decode(blob).decode() for blob in re.findall(r'"data":\s*"([A-Za-z0-9+/=]+)"', text)]
    assert not any('markdown"' in p and "markdown_chars" not in p for p in payloads)
    assert "stackTrace" not in text or '"stackTrace": ""' in text


def test_the_failed_history_carries_only_fixed_codes():
    events = json.loads(load("parse_failed.json"))["events"]
    messages = {
        e[key]["failure"]["message"]
        for e in events
        for key in e
        if isinstance(e[key], dict) and isinstance(e[key].get("failure"), dict)
        if "message" in e[key]["failure"]
    }

    assert messages <= {"pdf_parse_failed", "Activity task failed"}
    assert "pdf_parse_failed" in messages


def test_scrub_replaces_ids_queues_identities_and_stack_traces():
    raw = json.dumps(
        {
            "events": [
                {
                    "workflowExecutionStartedEventAttributes": {
                        "workflowId": "pytest-ab-doc-parse-123e4567-e89b-12d3-a456-426614174000",
                        "identity": "4242@my-laptop.local",
                        "taskQueue": {"name": "pytest-ab-1"},
                        "failure": {"stackTrace": "File /Users/me/x.py, line 1"},
                        "input": {
                            "payloads": [
                                {
                                    "metadata": {"encoding": "anNvbi9wbGFpbg=="},
                                    "data": base64.b64encode(
                                        b'{"document_id":"123e4567-e89b-12d3-a456-426614174001"}'
                                    ).decode(),
                                }
                            ]
                        },
                    }
                }
            ]
        }
    )

    out = scrub_history(
        raw, workflow_id="pytest-ab-doc-parse-123e4567-e89b-12d3-a456-426614174000", task_queues=["pytest-ab-1"]
    )
    text = json.dumps(out)

    for leaked in ("my-laptop", "/Users/me", "pytest-ab", "123e4567"):
        assert leaked not in text
    assert find_leaks(text) == []
