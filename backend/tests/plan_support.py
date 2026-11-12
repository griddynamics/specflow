"""The plan service over an in-memory plan, shared by the plan tool tests and the API agent route tests (neutral: it
depends on no MCP or route test module, so either side can be rewritten without the other)."""

from __future__ import annotations

from graph.plan import PlanReadError, PlanService, TaskBrief
from graph.plan.models import DoneWhen
from graph.plan.reader import LoadedPlan
from graph_plan_support import plan_ontology, plan_rules
from graph_support import OfflineExecutor
from plan_reads_support import ShopGraph, shop_plan


class FakePlanService(PlanService):
    """The real service over ``shop_plan`` and ``ShopGraph``, recording the projects it loads and the briefs asked."""

    def __init__(self, *, has_plan: bool = True) -> None:
        self.projects: list[str] = []
        self.briefs: list[tuple[str, str, int]] = []
        self.loaded = shop_plan(has_plan=has_plan)

        async def load(project_id: str) -> LoadedPlan:
            self.projects.append(project_id)
            return self.loaded

        ontology = plan_ontology()
        super().__init__(
            OfflineExecutor(),
            ontology,
            plan_rules(ontology),
            graph=ShopGraph(),
            load=load,
        )

    async def task_brief(self, project_id: str, task_id: str, *, neighbours: int = 30) -> TaskBrief:
        await self._plan(project_id)
        self.briefs.append((project_id, task_id, neighbours))
        task = self.loaded.tasks.get(task_id)
        if task is None or task.status == "superseded":
            raise PlanReadError("task_not_found")
        return TaskBrief(
            task=self.loaded.plan_task(task),
            done_when=(DoneWhen(text="An order is stored.", source="spec"),),
            profile={"languages": ["python"]},
            spec_changed=True,
            missing=("REQ::GONE",),
        )
