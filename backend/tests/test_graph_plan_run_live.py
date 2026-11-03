"""Live Neo4j plan runs on the shop fixture: first run, unchanged update, resume, graph change, persist resume."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from graph.core.db import Executor
from graph.core.errors import ConfigurationError, InfrastructureUnavailableError, LLMFatalError
from graph.plan.errors import TaskStateError
from graph.plan.files import FilePlanStore, open_plan_store
from graph.plan.lifecycle import TaskLifecycle
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
from logger_harness import fields
from specflow_live import requires_live

pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")

PLACE = "TASK::ORDERS_SERVICE::LOGIC_PLACE_ORDER"
REPLAN_COMMIT = "56" * 20
PRIVATE_AGENT_NOTE = "AGENT-NOTE the tests are red"


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


async def test_a_run_interrupted_in_persist_finishes_on_resume_even_after_a_spec_write(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id = await _project(neo4j_project)
    store = open_plan_store(tmp_path)
    gateway = script_plan(ScriptedGateway(), await _answers())
    # Batch 5 is the first relationship batch: areas, stories and tasks are written, Plan.applying is set.
    failing = _FailingExecutor(neo4j_project.driver, neo4j_project.database, fail_at=5)
    context = _context(neo4j_project, project_id, store, gateway, query_executor=failing)

    with pytest.raises(InfrastructureUnavailableError):
        await run_with(context, StepFunctions())
    run = await store.latest_run(project_id)
    assert run is not None and run.progress["persist"] == "failed" and not run.finished
    ontology = plan_ontology()
    life = TaskLifecycle(neo4j_project.driver, ontology, plan_rules(ontology), database=neo4j_project.database)
    assert await life.ready_tasks(project_id) == []
    with pytest.raises(TaskStateError) as applying:
        await life.claim_task(project_id, "TASK::PROJECT::WORKSPACE", claimed_by="agent")
    assert applying.value.code == "plan_applying"
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, spec_id: 'REQ::FR::CHECKOUT'}) SET n.last_build_id = 'build-9'",
        p=project_id,
    )

    result = await run_with(
        _context(neo4j_project, project_id, store, gateway).with_run_id(run.run_id), StepFunctions()
    )

    current = await read_current_plan(Executor(neo4j_project.driver, neo4j_project.database), project_id)
    assert result.status == "succeeded" and result.failure is None
    assert current.plan is not None and not current.plan.applying
    assert result.checks is not None and result.checks.metrics.outdated is True
    assert [task.task_id for task in await life.ready_tasks(project_id)] == ["TASK::PROJECT::WORKSPACE"]


async def _snapshot(neo4j_project: Neo4jProject, project_id: str) -> tuple[Any, ...]:
    current = await read_current_plan(Executor(neo4j_project.driver, neo4j_project.database), project_id)
    return (
        sorted((task.task_id, task.status, task.story_id, task.implements) for task in current.tasks),
        sorted((link.type, link.source, link.target) for link in current.links),
        sorted((story.story_id, story.status) for story in current.stories),
    )


async def test_a_resumed_run_plans_exactly_what_an_uninterrupted_run_plans(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    answers = dict(await _answers())
    whole = await _project(neo4j_project)
    await run_with(
        _context(neo4j_project, whole, open_plan_store(tmp_path / "whole"), script_plan(ScriptedGateway(), answers)),
        StepFunctions(),
    )
    resumed = await _project(neo4j_project)
    store = open_plan_store(tmp_path / "resumed")
    failing = script_plan(ScriptedGateway(), {**answers, ("tasks", "AREA::ORDERS_SERVICE#1"): LLMFatalError()})
    with pytest.raises(LLMFatalError):
        await run_with(_context(neo4j_project, resumed, store, failing), StepFunctions())
    run = await store.latest_run(resumed)
    assert run is not None
    await run_with(
        _context(neo4j_project, resumed, store, script_plan(ScriptedGateway(), answers)).with_run_id(run.run_id),
        StepFunctions(),
    )

    assert await _snapshot(neo4j_project, resumed) == await _snapshot(neo4j_project, whole)


async def test_a_replan_keeps_completed_work_and_its_commit(neo4j_project: Neo4jProject, tmp_path: Path) -> None:
    project_id = await _project(neo4j_project)
    store = open_plan_store(tmp_path)
    answers = await _answers()
    await run_with(_context(neo4j_project, project_id, store, script_plan(ScriptedGateway(), answers)), StepFunctions())
    await neo4j_project.run(
        "MATCH (t:Task {group_id: $p}) WHERE t.task_id IN ['TASK::PROJECT::WORKSPACE', $place] "
        "SET t.status = 'completed', t.commit = $c",
        p=project_id,
        place=PLACE,
        c=REPLAN_COMMIT,
    )

    result = await run_with(
        _context(neo4j_project, project_id, store, script_plan(ScriptedGateway(), answers), replan=True),
        StepFunctions(),
    )

    current = await read_current_plan(Executor(neo4j_project.driver, neo4j_project.database), project_id)
    kept = {task.task_id: task for task in current.tasks}
    assert result.mode == "replan" and result.status == "succeeded"
    assert result.changes.tasks_superseded == ()
    assert (kept[PLACE].status, kept[PLACE].commit) == ("completed", REPLAN_COMMIT)


async def test_logs_and_the_result_of_a_run_and_task_work_carry_no_text(
    neo4j_project: Neo4jProject, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    project_id = await _project(neo4j_project)
    result = await run_with(
        _context(
            neo4j_project, project_id, open_plan_store(tmp_path), script_plan(ScriptedGateway(), await _answers())
        ),
        StepFunctions(),
    )
    ontology = plan_ontology()
    life = TaskLifecycle(neo4j_project.driver, ontology, plan_rules(ontology), database=neo4j_project.database)
    claim = await life.claim_task(project_id, "TASK::PROJECT::WORKSPACE", claimed_by="agent")
    await life.fail_task(project_id, "TASK::PROJECT::WORKSPACE", claim_token=claim.claim_token, note=PRIVATE_AGENT_NOTE)
    with pytest.raises(TaskStateError):
        await life.claim_task(project_id, PLACE, claimed_by="agent")

    texts = {node.name for node in await shop_graph().nodes(project_id)} | {PRIVATE_AGENT_NOTE, claim.claim_token}
    records = [record for record in caplog.records if str(getattr(record, "event", "")).startswith("graph.plan.")]
    seen = " ".join(
        " ".join(str(value) for value in (record.getMessage(), *fields(record).values())) for record in records
    )
    assert {str(getattr(record, "event", "")) for record in records} >= {
        "graph.plan.run_started",
        "graph.plan.step_done",
        "graph.plan.persist_batch",
        "graph.plan.check_done",
        "graph.plan.run_finished",
        "graph.plan.task_claimed",
        "graph.plan.task_transition",
        "graph.plan.claim_refused",
    }
    assert not [text for text in texts if text in seen]
    dumped = result.model_dump_json()
    assert not [text for text in texts if text in dumped]
