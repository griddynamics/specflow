"""What a restart or a passing failure leaves of the answers (answers.md "Applying answers"): ``pending_answers``
finds the answers still in their quiet period, the applies still waiting for their build and the amendment rows of
deleted clarifications; the writes after a committed answer or apply are retried (``graph.gap.retry``)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gap_answer_support import MEMBER, answer_world
from graph.core.errors import ConfigurationError
from graph.gap import retry
from graph.gap.models import AmendmentRow, AnswerInput


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "RETRY_DELAYS", (0.0, 0.0))


class Flaky:
    """Wraps one port method: raises ``error`` the first ``times`` calls, then calls through."""

    def __init__(self, call: Any, error: Exception, times: int = 1) -> None:
        self.call, self.error, self.left, self.calls = call, error, times, 0
        self.__name__ = getattr(call, "__name__", "call")

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise self.error
        return await self.call(*args, **kwargs)


async def test_an_answer_in_its_quiet_period_is_found_with_its_time() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    await rig.service.answer_gap(rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER))
    answered = (await rig.service.get_gap(rig.project_id, gap.gap_id)).clarification

    found = await rig.service.pending_answers(rig.project_id)

    assert answered is not None and found.answered_at == answered.answered_at
    assert (found.apply_requested_at, found.orphans) == (None, 0)


async def test_an_applied_answer_waits_for_its_build_and_not_for_its_quiet_period() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    await rig.service.answer_gap(rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER))
    await rig.service.apply_now(rig.project_id, by=MEMBER)

    found = await rig.service.pending_answers(rig.project_id)

    assert found.answered_at is None and found.apply_requested_at is not None


async def test_a_project_without_answers_has_nothing_pending() -> None:
    rig = await answer_world()

    found = await rig.service.pending_answers(rig.project_id)

    assert (found.answered_at, found.apply_requested_at, found.orphans) == (None, None, 0)


async def test_pending_rows_of_a_deleted_clarification_are_deleted() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    result = await rig.service.answer_gap(
        rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER)
    )
    (kept,) = await rig.world.amendments.for_clarification(rig.project_id, result.clr_id)
    now = datetime.now(UTC)
    orphan = AmendmentRow.model_validate(
        kept.model_dump() | {"amendment_id": "AMD::ORPHAN", "clr_id": "CLR::GONE", "created_at": now, "updated_at": now}
    )
    await rig.world.amendments.put(rig.project_id, [orphan])

    found = await rig.service.pending_answers(rig.project_id)

    assert found.orphans == 1
    assert set(rig.world.amendments.rows[rig.project_id]) == {kept.amendment_id}


async def test_a_passing_failure_of_the_event_log_after_an_answer_is_retried() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    flaky = Flaky(rig.world.records.log, ConnectionError("blip"))
    rig.service._records = _Records(rig.world.records, log=flaky)

    await rig.service.answer_gap(rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER))

    assert flaky.calls == 2
    assert [e.event for e in await rig.world.records.events(rig.project_id, gap.gap_id) if e.event == "answered"]


async def test_a_passing_failure_of_the_build_request_after_an_apply_is_retried() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    await rig.service.answer_gap(rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER))
    flaky = Flaky(rig.builds.request_update_build, ConnectionError("blip"), times=2)
    rig.builds.request_update_build = flaky

    result = await rig.service.apply_now(rig.project_id, by=MEMBER)

    assert (flaky.calls, result.build) == (3, "build-1")


async def test_a_refused_build_request_is_not_retried() -> None:
    rig = await answer_world()
    gap = await rig.cancel_gap()
    await rig.service.answer_gap(rig.project_id, AnswerInput(gap_id=gap.gap_id, option_rank=1, answered_by=MEMBER))
    flaky = Flaky(rig.builds.request_update_build, ConfigurationError("closed"), times=5)
    rig.builds.request_update_build = flaky

    with pytest.raises(ConfigurationError):
        await rig.service.apply_now(rig.project_id, by=MEMBER)

    assert flaky.calls == 1
    # The apply committed before the request: the answer now waits for its build, which ``recover`` requests.
    assert (await rig.service.pending_answers(rig.project_id)).apply_requested_at is not None


class _Records:
    """The records port with one method replaced."""

    def __init__(self, records: Any, **methods: Any) -> None:
        self._records, self._methods = records, methods

    def __getattr__(self, name: str) -> Any:
        return self._methods.get(name) or getattr(self._records, name)
