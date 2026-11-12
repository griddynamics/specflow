"""``PlanService.story_findings``: the open findings per story for the story lists, through the ``FindingsReads`` port
(api.md "story_findings"), on the in-memory plan of ``mini_plan``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from graph.plan import PlanReadError, PlanService
from graph_plan_support import plan_ontology, plan_rules
from plan_claims_support import mini_plan

API, UI = "STORY::A::API", "STORY::A::UI"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def finding(
    finding_id: str, about: Sequence[str], *, blocking: bool = False, minutes: int = 0, story_id: str | None = None
) -> Any:
    return SimpleNamespace(
        finding_id=finding_id,
        kind="question",
        text=f"text of {finding_id}",
        blocking=blocking,
        about_spec_ids=tuple(about),
        story_id=story_id,
        created_by="u1",
        created_at=T0 + timedelta(minutes=minutes),
        orphaned=False,
    )


class Port:
    """What ``Findings`` answers: the open findings whose ids meet the asked ids; records every ask."""

    def __init__(self, *found: Any) -> None:
        self.found = found
        self.asked: list[tuple[str, ...]] = []

    async def open_counts(self, _project_id: str, spec_ids: Sequence[str]) -> Mapping[str, int]:
        return dict.fromkeys(spec_ids, 0)

    async def findings(self, _project_id: str, *, spec_ids: Sequence[str] | None = None, limit: int = 50) -> Any:
        self.asked.append(tuple(spec_ids or ()))
        wanted = set(spec_ids or ())
        return SimpleNamespace(findings=[item for item in self.found if wanted & set(item.about_spec_ids)][:limit])


def service(port: Port | None) -> PlanService:
    memory = mini_plan()
    memory.set_task("TASK::A::API_CONTRACT", implements=["FEAT::API", "REQ::API"])
    memory.set_task("TASK::A::UI_SCREEN", implements=["FEAT::UI"])
    memory.set_story(UI, anchor="FEAT::UI_ANCHOR")
    ontology = plan_ontology()
    return PlanService(memory, ontology, plan_rules(ontology), findings=port)


async def test_a_story_counts_its_open_findings_once_and_names_the_oldest_blocking_one() -> None:
    port = Port(
        finding("F1", ["FEAT::API", "REQ::API"], minutes=3),  # about two nodes of the story: one finding
        finding("F2", ["REQ::API"], blocking=True, minutes=2, story_id=API),
        finding("F3", ["FEAT::API"], blocking=True, minutes=1, story_id=API),
        finding("F4", ["FEAT::UI"], minutes=4),
        finding("F5", ["FEAT::ELSEWHERE"]),
    )

    found = await service(port).story_findings("shop", [API, UI])

    by_id = {item.story_id: item for item in found}
    assert [item.story_id for item in found] == [API, UI]
    assert by_id[API].count == 3 and by_id[API].question is not None and by_id[API].question.finding_id == "F3"
    assert by_id[UI].count == 1 and by_id[UI].question is None


async def test_the_anchor_of_the_story_counts_too() -> None:
    port = Port(finding("F9", ["FEAT::UI_ANCHOR"], blocking=True, story_id=UI))

    (found,) = await service(port).story_findings("shop", [UI])

    assert found.count == 1 and found.question is not None and found.question.finding_id == "F9"


async def test_the_question_is_a_blocking_finding_raised_on_a_story_of_the_unit_only() -> None:
    # A shared node: F1 (oldest) was raised on UI, F2 without a story, F3 on API; API counts all three, asks F3.
    port = Port(
        finding("F1", ["FEAT::API"], blocking=True, minutes=1, story_id=UI),
        finding("F2", ["FEAT::API"], blocking=True, minutes=2),
        finding("F3", ["FEAT::API"], blocking=True, minutes=3, story_id=API),
    )

    (found,) = await service(port).story_findings("shop", [API])

    assert found.count == 3 and found.question is not None and found.question.finding_id == "F3"


async def test_a_blocking_finding_raised_on_another_story_of_the_cycle_is_the_units_question() -> None:
    port = Port(finding("F1", ["FEAT::X"], blocking=True, story_id="STORY::A::Y"))
    memory = mini_plan()
    memory.set_task("TASK::A::X_CONTRACT", implements=["FEAT::X"])
    ontology = plan_ontology()

    (found,) = await PlanService(memory, ontology, plan_rules(ontology), findings=port).story_findings(
        "shop", ["STORY::A::X"]
    )

    assert found.question is not None and found.question.finding_id == "F1"


async def test_a_duplicate_id_is_answered_once_and_an_unknown_or_retired_story_counts_zero() -> None:
    port = Port(finding("F1", ["FEAT::API"]))
    memory_service = service(port)

    found = await memory_service.story_findings("shop", [API, API, "STORY::NOPE"])

    assert [(item.story_id, item.count) for item in found] == [(API, 1), ("STORY::NOPE", 0)]
    assert len(port.asked) == 1  # one read per story with nodes; none for the unknown one


async def test_without_a_port_every_story_has_none_and_a_story_without_nodes_reads_nothing() -> None:
    found = await service(None).story_findings("shop", [API, "STORY::A::LATE"])
    port = Port(finding("F1", ["FEAT::API"]))
    await service(port).story_findings("shop", ["STORY::A::LATE"])

    assert [(item.count, item.question) for item in found] == [(0, None), (0, None)]
    assert port.asked == []


async def test_a_project_without_a_plan_is_no_plan() -> None:
    memory = mini_plan()
    memory.plan = None
    ontology = plan_ontology()
    with pytest.raises(PlanReadError) as refused:
        await PlanService(memory, ontology, plan_rules(ontology), findings=Port()).story_findings("shop", [API])
    assert refused.value.code == "no_plan"
