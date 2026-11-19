"""Development gaps over the agent routes (docs/graph/gap/lifecycle.md "Development gaps", "Plan link"): repeats, the
refusal of a held story, the full answer path through the close check, blocker holds and migrated finding ids. The
acceptance tests of FR-GDEV-01 to 06 and FR-GPLN-03/04 cover the rest; the rig is
``gap_development_support.dev_rig``."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from gap_acceptance_support import Planted
from gap_close_support import apply_build, seed_investigator
from gap_development_support import (
    ADD,
    ALPHA,
    API_CONTRACT,
    API_LOGIC,
    API_STORY,
    CLAIM,
    MEMBER,
    QUESTION,
    OneReport,
    claim,
    dev_rig,
    report,
    report_blocking,
    task,
)
from graph.core.errors import InfrastructureUnavailableError
from graph.gap import AnswerInput, NewGap, run_gap
from graph.gap.gaps import gap_uuid
from graph.gap.models import InvestigationResult


@pytest.fixture
async def dev(monkeypatch, valid_env):
    async with dev_rig(monkeypatch, valid_env) as rig:
        yield rig


async def test_a_blocking_question_becomes_a_development_gap_that_holds_the_story_and_gets_options_at_once(dev) -> None:
    token = await claim(dev)

    added = await report_blocking(dev, token)

    gap_id = added["finding"]["finding_id"]
    assert gap_id.startswith("GAP::") and added["created"] is True
    assert added["finding"]["kind"] == "question" and added["finding"]["text"] == QUESTION
    assert added["blocked"] == [API_CONTRACT, API_LOGIC]
    for task_id in (API_CONTRACT, API_LOGIC):
        assert (task(dev, task_id)["status"], task(dev, task_id)["blocked_by"]) == ("blocked", gap_id)
    assert dev.store.stories[API_STORY]["claimed_by"] is None  # the claim ended (FR-GDEV-02)
    assert dev.runner.sites == [(ALPHA, gap_id)]  # investigated at once (FR-GDEV-03)
    gap = await dev.gaps.get_gap(ALPHA, gap_id)
    assert (gap.kind, gap.origin, gap.status, gap.question) == ("missing", "development", "open", QUESTION)
    assert [option.label for option in gap.options] == ["5 business days", "10 business days"]
    assert gap.route == "ask"  # a minor development gap with every gate holding is still asked (FR-GDEV-05)
    (created,) = [event for event in gap.history.events if event.event == "created"] if gap.history else []
    assert created.detail["refs"] == [{"type": "commit", "value": "abcdef1"}]


async def test_the_held_story_cannot_be_claimed_and_the_refusal_names_the_gap(dev) -> None:
    gap_id = (await report_blocking(dev, await claim(dev)))["finding"]["finding_id"]

    refused = await dev.rig.post(CLAIM, {"story_id": API_STORY}, project_id=ALPHA)

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "not_available"
    assert [item["finding_id"] for item in refused.json()["error"]["details"]["findings"]] == [gap_id]


async def test_a_repeat_returns_the_open_gap_and_holds_nothing_more(dev) -> None:
    token = await claim(dev)
    first = await report_blocking(dev, token)

    again = await dev.rig.post(
        ADD,
        {"kind": "question", "text": QUESTION, "about": [Planted.PLACE_ORDER], "story_id": API_STORY},
        project_id=ALPHA,
    )

    assert again.status_code == 200
    assert again.json()["finding"]["finding_id"] == first["finding"]["finding_id"] and again.json()["created"] is False
    assert again.json()["blocked"] == [] and len(dev.runner.sites) == 1


async def test_an_answer_to_a_holding_development_gap_applies_at_once_and_the_close_check_releases_the_story(
    dev,
) -> None:
    gap_id = (await report_blocking(dev, await claim(dev)))["finding"]["finding_id"]

    answered = await dev.gaps.answer_gap(ALPHA, AnswerInput(gap_id=gap_id, option_rank=1, answered_by=MEMBER))

    assert answered.immediate is True
    assert dev.builds.requests[-1][1:] == ("gap_answers", True)
    assert task(dev, API_LOGIC)["status"] == "blocked"
    rebuilt = await apply_build(dev.world)
    seed_investigator(dev.world)
    context = dev.world.context(changes=[rebuilt.report], build=rebuilt.build)

    result = await run_gap(dataclasses.replace(context, holds=dev.holds))

    gap = await dev.gaps.get_gap(ALPHA, gap_id)
    assert (gap.status, gap.resolution) == ("resolved", "answered")
    assert result.counts["close"]["resolved"] >= 1
    assert task(dev, API_LOGIC)["status"] == "pending" and task(dev, API_CONTRACT)["status"] == "pending"
    assert (await dev.rig.post(CLAIM, {"story_id": API_STORY}, project_id=ALPHA)).status_code == 200


async def test_an_open_blocker_gap_holds_the_tasks_of_its_nodes_until_it_is_dismissed(dev) -> None:
    raised = await dev.gaps.raise_gap(
        ALPHA,
        NewGap(
            kind="missing",
            question="Which payment methods may a customer place an order with?",
            about=(Planted.PLACE_ORDER,),
            severity="blocker",
            origin="person",
            created_by=MEMBER,
        ),
    )

    assert raised.held == (API_LOGIC,)
    refused = await dev.rig.post(CLAIM, {"story_id": API_STORY}, project_id=ALPHA)
    assert refused.status_code == 409
    assert [item["finding_id"] for item in refused.json()["error"]["details"]["findings"]] == [raised.gap_id]

    released = await dev.gaps.dismiss_gap(ALPHA, raised.gap_id, reason="Decided elsewhere.", by=MEMBER)

    assert released == (API_LOGIC,)
    assert (await dev.rig.post(CLAIM, {"story_id": API_STORY}, project_id=ALPHA)).status_code == 200


async def test_a_migrated_findings_old_id_still_names_its_gap_and_its_held_tasks_are_released_with_it(dev) -> None:
    # A first-slice finding migrated to a gap keeps its FND:: id as an alias; the plan's task still names it.
    gap_id = dev.question_gap()
    node = dev.world.executor.nodes[("Gap", gap_uuid(ALPHA, gap_id))]
    node["aliases"] = ["FND::OLD"]
    dev.store.set_task(API_LOGIC, status="blocked", block_reason="spec_ambiguous", blocked_by="FND::OLD")

    assert (await dev.gaps.get_gap(ALPHA, "FND::OLD")).gap_id == gap_id
    refused = await dev.rig.post(CLAIM, {"story_id": API_STORY}, project_id=ALPHA)
    assert [item["finding_id"] for item in refused.json()["error"]["details"]["findings"]] == [gap_id]

    released = await dev.gaps.dismiss_gap(ALPHA, gap_id, reason="Answered in the call.", by=MEMBER)

    assert released == (API_LOGIC,) and task(dev, API_LOGIC)["status"] == "pending"


async def test_a_held_task_a_plan_run_reset_is_held_again_before_the_next_claim(dev) -> None:
    raised = await dev.gaps.raise_gap(
        ALPHA,
        NewGap(
            kind="missing",
            question="Which payment methods may a customer place an order with?",
            about=(Planted.PLACE_ORDER,),
            severity="blocker",
            origin="person",
            created_by=MEMBER,
        ),
    )
    dev.store.set_task(API_LOGIC, status="pending", block_reason=None, blocked_by=None)  # a plan run's reset

    refused = await dev.rig.post(CLAIM, {"story_id": API_STORY}, project_id=ALPHA)

    assert refused.status_code == 409
    assert [item["finding_id"] for item in refused.json()["error"]["details"]["findings"]] == [raised.gap_id]
    assert (task(dev, API_LOGIC)["status"], task(dev, API_LOGIC)["blocked_by"]) == ("blocked", raised.gap_id)


async def test_the_site_run_of_a_raised_gap_never_shows_the_gap_as_another_open_gap(dev) -> None:
    # A real model once answered the agent's question "duplicate of" the gap it was investigating: the pack listed it.
    seen: list[Any] = []

    class Seeing(OneReport):
        async def investigate(self, pack: Any, reads: Any, budget: Any) -> InvestigationResult:
            seen.append(pack)
            return await super().investigate(pack, reads, budget)

    dev.world.investigator = Seeing(report())

    gap_id = (await report_blocking(dev, await claim(dev)))["finding"]["finding_id"]

    (pack,) = seen
    assert gap_id not in {item["gap_id"] for item in pack.context["gaps"]}
    assert len((await dev.gaps.get_gap(ALPHA, gap_id)).options) == 2


async def test_a_not_spec_development_gap_whose_note_failed_is_closed_and_released_when_its_run_resumes(
    dev, monkeypatch
) -> None:
    dev.world.investigator = OneReport(report(verdict="not_spec", not_spec_reason="A library bug.", options=()))
    records = dev.world.records
    put_notes, failed = records.put_notes, []

    async def down_once(project_id: str, run_id: str | None, notes: Any, *, once: str | None = None) -> None:
        if not failed:
            failed.append(run_id)
            raise InfrastructureUnavailableError("db_down")
        await put_notes(project_id, run_id, notes, once=once)

    monkeypatch.setattr(records, "put_notes", down_once)
    start = dev.runner.start_site

    async def stops(project_id: str, gap_id: str) -> None:
        with pytest.raises(InfrastructureUnavailableError, match="db_down"):
            await start(project_id, gap_id)

    monkeypatch.setattr(dev.runner, "start_site", stops)
    text = "The HTTP client library times out on every retry?"
    gap_id = (await report_blocking(dev, await claim(dev), kind="gap", text=text))["finding"]["finding_id"]
    record = await dev.world.store.latest_run(ALPHA)
    assert record is not None and not record.finished and gap_id not in dev.world.gap_nodes()

    context = dataclasses.replace(dev.world.context(run_id=record.run_id), holds=dev.holds)
    result = await run_gap(context, mode="site", site_ids=(gap_id,))

    assert result.counts["outcomes"]["not_spec"] == 1
    assert not [row for row in records.relevance_rows[ALPHA].values() if "not_spec" in row.signals]
    assert task(dev, API_LOGIC)["status"] == "pending" and task(dev, API_CONTRACT)["status"] == "pending"
    assert [note.text for note in records.notes[ALPHA]].count(text) == 1
    closed = [row for row in records.event_rows[ALPHA] if row.gap_id == gap_id and row.event == "obsolete"]
    assert len(closed) == 1
