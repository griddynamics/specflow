"""What a plan destination's pass reads from the knowledge graph (the GitHub Projects and Notion destinations share it):
the current plan of a project, and the current id and name of the spec nodes its stories name."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from api.jira.desired import SpecRef
from graph.core import load_ontology
from graph.core.reader import GraphReader
from graph.core.store import as_executor
from graph.plan.reader import LoadedPlan, load_plan


class PlanGraph:
    """The reads of one graph store."""

    def __init__(self, graph_store: Any) -> None:
        self._graph = graph_store

    async def load_plan(self, project_id: str) -> LoadedPlan:
        return await load_plan(as_executor(self._graph), project_id)

    async def spec_names(self, project_id: str, ids: Sequence[str]) -> Mapping[str, SpecRef]:
        """Each asked spec id (an alias too) to the current id and name of its node; an unknown id is left out."""

        ontology = await asyncio.to_thread(load_ontology)
        found = await GraphReader(as_executor(self._graph), ontology).nodes_by_spec_ids(project_id, ids)
        refs = {
            name: SpecRef(node.spec_id, node.name)
            for node in found.values()
            for name in (node.spec_id, *node.spec_id_aliases)
        }
        return {name: refs[name] for name in ids if name in refs}
