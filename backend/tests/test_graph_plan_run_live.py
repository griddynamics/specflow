"""Live Neo4j plan runs on the shop fixture: first run, unchanged update, resume, graph change, persist resume."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from graph.core.db import Executor
from graph.core.errors import ConfigurationError, InfrastructureUnavailableError, LLMFatalError
from graph.plan.files import FilePlanStore, open_plan_store
from graph.plan.models import BuildChangeReport, DiffArtifact
from graph.plan.options import PlanContext
from graph.plan.persist import persist
from graph.plan.prompts import gateway_prompt
from graph.plan.run import StepFunctions, run_with
from graph.plan.snapshot import read_current_plan
from graph_plan_support import (
    plan_ontology,
    plan_rules,
    script_plan,
    shop_answers,
    shop_graph,
    shop_view,
    write_spec_graph,
)
from graph_support import Neo4jProject, ScriptedGateway
from specflow_live import requires_live

pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")


async def _project(neo4j_project: Neo4jProject) -> str:
    project_id = neo4j_project.new_project()
    await write_spec_graph(neo4j_project, project_id, shop_graph(), plan_ontology())
    return project_id


def _context(
    neo4j_project: Neo4jProject,
    project_id: str,
    store: FilePlanStore,
    gateway: ScriptedGateway,
    **fields: Any,
) -> PlanContext:
    ontology = plan_ontology()
    values: dict[str, Any] = {
        "project_id": project_id,
        "ontology": ontology,
        "rules": plan_rules(ontology),
        "driver": neo4j_project.driver,
        "gateway": gateway,
        "store": store,
        "build_id": "build-1",
        "database": neo4j_project.database,
        **fields,
    }
    return PlanContext(**values)


async def _answers() -> Mapping[tuple[str, str], Any]:
    return shop_answers(await shop_view())


async def test_a_first_run_persists_a_plan_that_passes_every_check(neo4j_project: Neo4jProject, tmp_path: Path) -> None:
    project_id = await _project(neo4j_project)
    gateway = script_plan(ScriptedGateway(), await _answers())

    result = await run_with(_context(neo4j_project, project_id, open_plan_store(tmp_path), gateway), StepFunctions())

    assert result.status == "succeeded" and result.mode == "first"
    assert result.checks is not None and result.checks.ok, result.checks
    assert result.checks.metrics.ready == 1  # only the setup root is ready on a fresh plan
    current = await read_current_plan(Executor(neo4j_project.driver, neo4j_project.database), project_id)
    assert current.plan is not None and current.plan.last_build_id == "build-1" and not current.plan.applying
    assert {task.status for task in current.tasks} == {"pending"}
    assert len(current.tasks) == len(result.changes.tasks_added)
    assert result.planning.review_failed is False
    assert [call.call_class for call in gateway.calls if call.prompt == gateway_prompt("review")] == ["judge"]


async def test_an_unchanged_update_makes_no_call_and_persists_only_the_consumed_build(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id = await _project(neo4j_project)
    store = open_plan_store(tmp_path)
    await run_with(
        _context(neo4j_project, project_id, store, script_plan(ScriptedGateway(), await _answers())), StepFunctions()
    )
    executor = Executor(neo4j_project.driver, neo4j_project.database)
    before = await read_current_plan(executor, project_id)
    gateway = ScriptedGateway()

    result = await run_with(
        _context(
            neo4j_project,
            project_id,
            store,
            gateway,
            build_id="build-2",
            changes=(BuildChangeReport(build_id="build-2", previous_build_id="build-1"),),
        ),
        StepFunctions(),
    )

    after = await read_current_plan(executor, project_id)
    assert result.mode == "update" and result.unchanged and result.status == "succeeded"
    assert gateway.calls == []
    assert after.plan is not None and after.plan.last_build_id == "build-2"
    assert after.tasks == before.tasks and after.links == before.links
    assert (await store.get_revisions(project_id)).consumed == ("build-1", "build-2")


async def test_a_failed_run_resumes_without_repeating_a_planner_call(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id = await _project(neo4j_project)
    store = open_plan_store(tmp_path)
    answers = dict(await _answers())
    failing = script_plan(ScriptedGateway(), {**answers, ("tasks", "AREA::WEB_SHOP#1"): LLMFatalError()})
    context = _context(neo4j_project, project_id, store, failing)

    with pytest.raises(LLMFatalError):
        await run_with(context, StepFunctions())
    run = await store.latest_run(project_id)
    assert run is not None and not run.finished and run.progress["tasks"] == "failed"

    with pytest.raises(ConfigurationError) as refused:
        await run_with(context, StepFunctions())
    assert refused.value.code == "unfinished_run"

    resumed = script_plan(ScriptedGateway(), answers)
    result = await run_with(
        _context(neo4j_project, project_id, store, resumed).with_run_id(run.run_id), StepFunctions()
    )

    assert result.status == "succeeded" and result.run_id == run.run_id
    prompts = [call.prompt for call in resumed.calls]
    assert gateway_prompt("describe") not in prompts and gateway_prompt("project") not in prompts
    assert prompts.count(gateway_prompt("tasks")) >= 1


async def test_a_run_resumed_after_a_spec_write_ends_with_graph_changed(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id = await _project(neo4j_project)
    store = open_plan_store(tmp_path)
    context = _context(neo4j_project, project_id, store, script_plan(ScriptedGateway(), await _answers()))
    functions = StepFunctions()
    run_id = await functions.snapshot(context)
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, spec_id: 'REQ::FR::CHECKOUT'}) SET n.last_build_id = 'build-9'",
        p=project_id,
    )

    result = await run_with(context.with_run_id(run_id), functions)

    assert result.status == "failed" and result.failure == "graph_changed"


class _FailingExecutor(Executor):
    """An executor whose n-th write fails as an unreachable database would."""

    def __init__(self, driver: Any, database: str | None, fail_at: int) -> None:
        super().__init__(driver, database)
        self.writes = 0
        self.fail_at = fail_at

    async def write(self, query: Any, project_id: str, /, **params: Any) -> list[Any]:
        self.writes += 1
        if self.writes == self.fail_at:
            raise InfrastructureUnavailableError("neo4j_unavailable")
        return await super().write(query, project_id, **params)


async def test_an_interrupted_persist_resumes_at_its_cursor_with_the_same_result(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id = await _project(neo4j_project)
    store = open_plan_store(tmp_path)
    context = _context(neo4j_project, project_id, store, script_plan(ScriptedGateway(), await _answers()))
    functions = StepFunctions()
    bound = context.with_run_id(await functions.snapshot(context))
    for name in ("partition", "describe", "project", "tasks", "stitch", "review", "diff"):
        await getattr(functions, name)(bound)
    stored = await store.get_artifact(project_id, _run_id(bound), "diff")
    assert stored is not None
    planned = DiffArtifact.model_validate(stored)
    failing = _FailingExecutor(neo4j_project.driver, neo4j_project.database, fail_at=3)

    with pytest.raises(InfrastructureUnavailableError):
        await persist(failing, store, planned, project_id=project_id, run_id=_run_id(bound), now=_now)
    stopped = await store.get_artifact(project_id, _run_id(bound), "persist")
    assert stopped is not None and stopped["cursor"] == 2

    executor = Executor(neo4j_project.driver, neo4j_project.database)
    done = await persist(executor, store, planned, project_id=project_id, run_id=_run_id(bound), now=_now)
    current = await read_current_plan(executor, project_id)

    assert done.cursor == done.batches and done.conflicts == ()
    assert current.plan is not None and not current.plan.applying
    assert {task.task_id for task in current.tasks} == {op.id for op in planned.tasks}
    assert len(current.links) == len(planned.links_create)


def _now() -> str:
    return "2026-01-01T00:00:00+00:00"


def _run_id(context: PlanContext) -> str:
    assert context.run_id is not None
    return context.run_id
