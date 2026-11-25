"""The Jira gap channel in the runtime (``api.runtime._add_jira_gaps``): built only with the users' Atlassian
connections and gap runs, registered as a gap-run notice, its poller started and stopped with the exit stack."""

from __future__ import annotations

from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any

from api.jira_gaps import JiraGaps
from api.runtime import _add_jira_gaps


class Gaps:
    def __init__(self) -> None:
        self.notices: list[Any] = []

    def add_notice(self, notice: Any) -> None:
        self.notices.append(notice)


def runtime_with(*, jira: Any, gaps: Any) -> SimpleNamespace:
    return SimpleNamespace(jira=jira, gaps=gaps, jira_gaps=None, graph_store=object(), session_factory=object())


async def test_it_is_built_with_oauth_and_gap_runs_and_hears_of_every_gap_run() -> None:
    gaps = Gaps()
    runtime = runtime_with(jira=SimpleNamespace(connections=object()), gaps=gaps)

    async with AsyncExitStack() as stack:
        _add_jira_gaps(runtime, stack)

        assert isinstance(runtime.jira_gaps, JiraGaps)
        assert gaps.notices == [runtime.jira_gaps]
        task = runtime.jira_gaps.poller._task
        assert task is not None and not task.done()

    assert task.done()  # the exit stack stopped the poller


async def test_it_is_not_built_without_the_users_connections_or_without_gap_runs() -> None:
    for jira, gaps in (
        (None, Gaps()),
        (SimpleNamespace(connections=None), Gaps()),
        (SimpleNamespace(connections=1), None),
    ):
        runtime = runtime_with(jira=jira, gaps=gaps)

        async with AsyncExitStack() as stack:
            _add_jira_gaps(runtime, stack)

        assert runtime.jira_gaps is None
        assert gaps is None or gaps.notices == []
