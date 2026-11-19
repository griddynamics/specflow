"""The host's ``PlanView`` of ``graph.gap`` over the persisted plan (docs/graph/gap/api.md "Ports").

``graph.gap`` never imports ``graph.plan``; a gap run and the gap reads see the plan through this adapter: the tasks
that implement a spec node (``IMPLEMENTS``), with their story and status, and the stories of those tasks. Merged and
rekeyed ids resolve through the graph's aliases, so an old id finds the tasks of its survivor. Closed tasks are
listed too (their status says so). One read of the plan and one of the spec nodes per call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from graph.core.reader import GraphReader
from graph.gap import TaskRef
from graph.plan.changes import resolve_spec_id
from graph.plan.reader import PlanReader, spec_aliases


class PlanReaderView:
    """``PlanView`` over ``PlanReader`` (tasks) and ``GraphReader`` (aliases)."""

    def __init__(self, plan: PlanReader, graph: GraphReader) -> None:
        self._plan = plan
        self._graph = graph

    async def tasks_for(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, Sequence[TaskRef]]:
        """For each spec id asked, the tasks implementing it (or its survivor), by task id."""

        wanted = list(dict.fromkeys(spec_ids))
        if not wanted:
            return {}
        aliases = spec_aliases(await self._graph.nodes(project_id))
        tasks = await self._plan.tasks(project_id)
        implemented = [
            (
                TaskRef(task_id=task.task_id, story_id=task.story_id, status=task.status),
                _resolved(task.implements, aliases),
            )
            for task in tasks
        ]
        found: dict[str, tuple[TaskRef, ...]] = {}
        for spec_id in wanted:
            survivor = resolve_spec_id(spec_id, aliases)
            found[spec_id] = tuple(ref for ref, ids in implemented if survivor in ids)
        return found

    async def stories_for(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, Sequence[str]]:
        """For each spec id asked, the stories of the tasks implementing it, sorted."""

        tasks = await self.tasks_for(project_id, spec_ids)
        return {
            spec_id: tuple(sorted({ref.story_id for ref in refs if ref.story_id is not None}))
            for spec_id, refs in tasks.items()
        }


def _resolved(spec_ids: Sequence[str], aliases: Mapping[str, str]) -> frozenset[str]:
    return frozenset(resolve_spec_id(spec_id, aliases) for spec_id in spec_ids)
