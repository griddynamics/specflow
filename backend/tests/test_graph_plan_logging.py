"""Plan logging: an offline plan of the shop logs its events with ids, codes and counts, never spec or model text."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel

from graph.core.errors import LLMError
from graph.plan.answers import NotBuildableAnswer, TasksAnswer
from graph.plan.diff import DiffInputs, diff
from graph.plan.files import open_plan_store
from graph.plan.stitch import stitch
from graph.plan.view import PlanView
from graph_plan_support import planned_shop, script_plan, shop_answers, shop_fixture_data, shop_view
from graph_support import ScriptedGateway
from logger.formatters import JsonFormatter
from logger.logger import INFO, get_logger, log_event
from logger_harness import assert_json_plain, fields

MARK: Final = "SECRET"
TEXT_FIELDS: Final = frozenset({"title", "description", "summary", "text", "note", "setup_summary", "closing_summary"})
INPUTS: Final = DiffInputs(ontology_version="2.0", plan_ontology_version="2.0", rules_version="2.0")
NOT_BUILDABLE: Final = "BR::RELEASE"
SHORT_NAME: Final = 12
"""Spec names shorter than this are only checked as whole field values (they may occur inside ids)."""
PLAIN_VALUE: Final = re.compile(r"^[A-Za-z0-9_.:#|\-]*$")
"""A logged string: an id, a code or an event name; never a sentence."""


def secret_texts(value: Any, name: str = "") -> Any:
    """A dumped answer with every free text replaced by a distinctive, numbered secret text."""

    if isinstance(value, dict):
        return {key: secret_texts(item, f"{name}-{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [secret_texts(item, f"{name}-{index}") for index, item in enumerate(value)]
    if isinstance(value, str) and name.rsplit("-", 1)[-1] in TEXT_FIELDS:
        return f"{MARK}{name.upper()} the planner wrote this"
    return value


def secret_answer[AnswerT: BaseModel](answer: AnswerT, name: str) -> AnswerT:
    return type(answer).model_validate(secret_texts(answer.model_dump(mode="json"), name))


def secret_answers(view: PlanView) -> dict[tuple[str, str], Any]:
    """The shop answers with secret texts, one not-buildable note, and three answers that fail."""

    answers: dict[tuple[str, str], Any] = {
        key: secret_answer(answer, f"-{key[0]}") for key, answer in shop_answers(view).items()
    }
    parts = [part.part_id for part in view.partition.parts]
    holder = next(part.part_id for part in view.partition.parts if NOT_BUILDABLE in part.members)
    held = answers[("tasks", holder)]
    assert isinstance(held, TasksAnswer)
    answers[("tasks", holder)] = held.model_copy(
        update={
            "tasks": [
                task.model_copy(update={"implements": [item for item in task.implements if item != NOT_BUILDABLE]})
                for task in held.tasks
            ],
            "not_buildable": [
                NotBuildableAnswer(spec_id=NOT_BUILDABLE, reason="release_gate", note=f"{MARK}-NOTE a release gate")
            ],
        }
    )
    stays_invalid, repaired = [part for part in parts if part != holder][:2]
    for part in (stays_invalid, repaired):
        valid = answers[("tasks", part)]
        assert isinstance(valid, TasksAnswer)
        broken = valid.model_copy(
            update={"tasks": [valid.tasks[0].model_copy(update={"phase": "deploy"}), *valid.tasks[1:]]}
        )
        answers[("tasks", part)] = broken if part == stays_invalid else [broken, valid]
    failing_area = view.active_areas[0].area_id
    answers[("describe", failing_area)] = LLMError("malformed")
    return answers


def spec_texts() -> tuple[set[str], set[str]]:
    """The fixture's names (short and long) and its statements, quotes, criteria, fragments and titles."""

    data = shop_fixture_data()
    names = {node["name"] for node in data["nodes"]}
    texts = {node["name"] for node in data["nodes"] if len(node["name"]) >= SHORT_NAME}
    for node in data["nodes"]:
        texts.update(item["text"] for item in node.get("statements") or ())
        texts.update(item["quote"] for item in node.get("provenance") or () if item.get("quote"))
        texts.update((node.get("attributes") or {}).get("acceptance_criteria") or ())
    texts.update(fragment["text"] for fragment in data["fragments"])
    texts.update(document["title"] for document in data["documents"])
    return {name for name in names if len(name) < SHORT_NAME}, texts


def _flatten(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _flatten(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _flatten(item)
    elif value is not None:
        yield str(value)


def record_strings(record: logging.LogRecord) -> list[str]:
    """The message, every structured field and the formatted line of a record."""

    return [record.getMessage(), *_flatten(fields(record)), JsonFormatter().format(record)]


def plan_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if str(getattr(record, "event", "")).startswith("graph.plan.")]


@pytest.fixture
async def records(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> list[logging.LogRecord]:
    """Every plan record of an offline shop plan: Describe, Project, Tasks, Stitch and Diff."""

    view = await shop_view()
    gateway = script_plan(ScriptedGateway(), secret_answers(view))
    with caplog.at_level(logging.DEBUG, logger="graph.plan"):
        shop = await planned_shop(view, open_plan_store(tmp_path / "plans"), gateway)
        stitched = stitch(view, shop.project, shop.tasks)
        diff(view, describe=shop.describe, project=shop.project, tasks=shop.tasks, stitch=stitched, inputs=INPUTS)
    assert any(MARK in task.title for task in shop.tasks.tasks)
    assert any(MARK in item.note for notes in shop.tasks.not_buildable.values() for item in notes)
    return plan_records(caplog)


def test_planner_events_are_logged_with_codes_and_counts(records: list[logging.LogRecord]) -> None:
    by_event: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_event.setdefault(str(getattr(record, "event", "")), []).append(fields(record))

    invalid = by_event["graph.plan.planner_invalid"]
    assert len(invalid) == 3
    assert all(item["planner"] == "tasks" and item["problems"] >= 1 for item in invalid)
    fallbacks = {(item["planner"], item["cause"]): item for item in by_event["graph.plan.planner_fallback"]}
    assert set(fallbacks) == {("tasks", "invalid_answer"), ("describe", "llm_error")}
    assert fallbacks[("tasks", "invalid_answer")]["calls"] == 2
    assert fallbacks[("tasks", "invalid_answer")]["invalid"] == 2
    assert fallbacks[("describe", "llm_error")]["code"] == LLMError("malformed").code
    assert fallbacks[("describe", "llm_error")]["key"].startswith("AREA::")


def leaks(record: logging.LogRecord) -> list[str]:
    """What a record carries that is not an id, a code or a count: free text, spec names or planner text."""

    short_names, texts = spec_texts()
    found: list[str] = []
    if record.getMessage() != getattr(record, "event", None):
        found.append(f"message {record.getMessage()!r}")
    for value in fields(record).values():
        if isinstance(value, str) and not PLAIN_VALUE.fullmatch(value):
            found.append(f"free text {value!r}")
        if isinstance(value, str) and value in short_names:
            found.append(f"spec name {value!r}")
    for text in record_strings(record):
        if MARK in text:
            found.append(f"planner text in {text!r}")
        found.extend(f"spec text {item!r}" for item in texts if item in text)
    return found


def test_the_check_finds_planted_text(caplog: pytest.LogCaptureFixture) -> None:
    logger = get_logger("graph.plan")
    with caplog.at_level(logging.DEBUG, logger="graph.plan"):
        log_event(logger, INFO, "graph.plan.planted", key="Order")
        log_event(logger, INFO, "graph.plan.planted", key="AREA::WEB_SHOP", title=f"{MARK}-TITLE")
        log_event(logger, INFO, "graph.plan.planted", "Customer can place an order")
    planted = plan_records(caplog)

    assert len(planted) == 3 and all(leaks(record) for record in planted)


def test_no_plan_record_carries_spec_or_model_text(records: list[logging.LogRecord]) -> None:
    assert records
    for record in records:
        assert_json_plain(record)
        assert not leaks(record), (getattr(record, "event", None), leaks(record))
