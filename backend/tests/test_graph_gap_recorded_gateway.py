"""The recorded gateway fakes of the gap roles (gap_acceptance_support.RecordedGapGateway): replay by input hash,
recorded failures, and a test failure for every call nobody recorded."""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from gap_acceptance_support import (
    RECORDINGS_DIR,
    RecordedGapGateway,
    UnrecordedCallError,
    assert_all_recorded,
    gateway_role,
    input_hash,
    load_recordings,
)
from graph.core.errors import LLMError
from graph.gap.names import GATEWAY_ROLES
from graph_support import UnscriptedRequestError


class Verdict(BaseModel):
    relevance: str


MESSAGES = [{"role": "system", "content": "Classify each section."}, {"role": "user", "content": "<data>...</data>"}]


async def _ask(gateway: RecordedGapGateway, prompt: str = "gap_relevance", messages: Any = None) -> Verdict:
    return await gateway.structured("small", None, messages or MESSAGES, Verdict, prompt=prompt)


@pytest.mark.parametrize(
    ("prompt", "role"),
    [
        ("gap_relevance", "gap_relevance"),
        ("gap_investigate.round", "gap_investigate"),
        ("gap.judge", "gap_judge"),
        ("gap.edits.v2", "gap_edits"),
        ("plan.describe.1", None),
        ("gap_investigator", None),
    ],
)
def test_a_prompt_name_maps_to_its_gap_role(prompt: str, role: str | None) -> None:
    assert gateway_role(prompt) == role


def test_the_input_hash_is_stable_and_covers_role_and_every_message() -> None:
    digest = input_hash("gap_judge", MESSAGES)
    assert digest == input_hash("gap_judge", [dict(reversed(list(m.items()))) for m in MESSAGES])
    assert digest != input_hash("gap_edits", MESSAGES)
    assert digest != input_hash("gap_judge", [*MESSAGES[:1], {"role": "user", "content": "<data>..</data>"}])


def test_the_shipped_recordings_load_for_every_role() -> None:
    assert {path.stem for path in RECORDINGS_DIR.glob("*.json")} == set(GATEWAY_ROLES)
    assert set(load_recordings()) == set(GATEWAY_ROLES)


async def test_a_recorded_call_is_replayed_as_the_output_model() -> None:
    gateway = RecordedGapGateway({}).record(
        "gap_relevance", input_hash("gap_relevance", MESSAGES), {"relevance": "spec"}
    )

    assert await _ask(gateway) == Verdict(relevance="spec")
    assert len(gateway.calls_of("gap_relevance")) == 1
    assert [recording.role for recording in gateway.replayed] == ["gap_relevance"]
    assert_all_recorded(gateway)


async def test_a_recorded_failure_raises_the_gateway_error() -> None:
    gateway = RecordedGapGateway({}).record("gap_judge", input_hash("gap_judge", MESSAGES), error="truncated")

    with pytest.raises(LLMError) as raised:
        await _ask(gateway, "gap.judge")
    assert raised.value.reason == "truncated"


async def test_an_unrecorded_call_fails_and_names_its_role_and_hash() -> None:
    gateway = RecordedGapGateway({})

    with pytest.raises(UnrecordedCallError, match=input_hash("gap_investigate", MESSAGES)):
        await _ask(gateway, "gap_investigate.round")
    (missing,) = gateway.unrecorded
    assert (missing.role, missing.output_type) == ("gap_investigate", "Verdict")


async def test_an_unrecorded_call_the_library_swallowed_still_fails_the_test() -> None:
    gateway = RecordedGapGateway({})
    with contextlib.suppress(UnrecordedCallError):  # what a run that defers a failed site does
        await _ask(gateway)

    with pytest.raises(pytest.fail.Exception, match="unrecorded gap gateway calls"):
        assert_all_recorded(gateway)


async def test_a_failing_role_fails_every_call_of_that_role() -> None:
    gateway = RecordedGapGateway({}, fail_roles=GATEWAY_ROLES)

    for prompt in ("gap_relevance", "gap_investigate", "gap_judge", "gap_edits"):
        with pytest.raises(LLMError):
            await _ask(gateway, prompt)
    assert gateway.unrecorded == []


async def test_a_handler_takes_precedence_and_other_prompts_stay_scripted() -> None:
    gateway = RecordedGapGateway({})
    gateway.handle("gap_relevance", lambda _request: {"relevance": "mixed"})

    assert await _ask(gateway) == Verdict(relevance="mixed")
    with pytest.raises(UnscriptedRequestError):
        await _ask(gateway, "plan.describe.1")


@pytest.mark.parametrize(
    ("calls", "problem"),
    [
        ([{"input_hash": "0" * 64, "note": "x"}], "exactly one of answer or error"),
        ([{"input_hash": "0" * 64, "note": "x", "answer": {}, "error": "fatal"}], "exactly one of answer or error"),
        ([{"input_hash": "abc", "note": "x", "answer": {}}], "64 lowercase hex"),
        ([{"input_hash": "0" * 64, "note": " ", "answer": {}}], "needs a note"),
        ([{"input_hash": "0" * 64, "note": "x", "error": "timeout"}], "error must be one of"),
        ([{"input_hash": "0" * 64, "note": "x", "answer": {}}] * 2, "recorded twice"),
        ([{"input_hash": "0" * 64, "note": "x", "answer": {}, "extra": 1}], "has the keys"),
    ],
)
def test_an_invalid_recording_is_refused(tmp_path: Path, calls: list[dict[str, Any]], problem: str) -> None:
    (tmp_path / "gap_judge.json").write_text(json.dumps({"role": "gap_judge", "calls": calls}), encoding="utf-8")

    with pytest.raises(ValueError, match=problem):
        load_recordings(tmp_path)


def test_a_recordings_file_of_another_role_is_refused(tmp_path: Path) -> None:
    (tmp_path / "gap_edits.json").write_text(json.dumps({"role": "gap_judge", "calls": []}), encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape("gap_edits.json")):
        load_recordings(tmp_path)
