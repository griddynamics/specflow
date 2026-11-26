"""The Linear gap channel in the runtime (``api.linear_gaps.wiring``): built only with the member connections and gap
runs, registered as a gap-run notice, its catch-up read started and stopped with the exit stack."""

from __future__ import annotations

from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any

import pytest

from api.linear_gaps.channel import LinearGaps
from api.linear_gaps.wiring import add_linear_gaps
from linear_gaps_support import FakeLinearGapsDb, transaction


class Gaps:
    def __init__(self) -> None:
        self.notices: list[Any] = []

    def add_notice(self, notice: Any) -> None:
        self.notices.append(notice)


def runtime_with(*, linear: Any, gaps: Any) -> SimpleNamespace:
    return SimpleNamespace(linear=linear, gaps=gaps, linear_gaps=None, graph_store=object(), transaction=transaction)


async def test_it_is_built_with_connections_and_gap_runs_and_hears_of_every_gap_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeLinearGapsDb().install(monkeypatch)
    gaps = Gaps()
    runtime = runtime_with(linear=object(), gaps=gaps)

    async with AsyncExitStack() as stack:
        add_linear_gaps(runtime, stack)

        assert isinstance(runtime.linear_gaps, LinearGaps)
        assert gaps.notices == [runtime.linear_gaps]
        tasks = list(runtime.linear_gaps._tasks)  # the catch-up read of the start

    assert all(task.done() for task in tasks)
    assert not runtime.linear_gaps._tasks


async def test_it_is_not_built_without_the_connections_or_without_gap_runs() -> None:
    for linear, gaps in ((None, Gaps()), (object(), None)):
        runtime = runtime_with(linear=linear, gaps=gaps)

        async with AsyncExitStack() as stack:
            add_linear_gaps(runtime, stack)

        assert runtime.linear_gaps is None
        assert gaps is None or gaps.notices == []
