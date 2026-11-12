"""Findings on the live store (every store the live switch selects): the library contract, rebuild effects on a finding,
and the blocking link on a persisted shop plan including a plan run after the block."""

from __future__ import annotations

from pathlib import Path

import pytest

from graph.core import NodeRef
from graph.core.ontology import load_ontology
from graph.gap import FindingError, FindingFlow, Findings, NewFinding
from graph.plan import PlanService, TaskStateError
from graph.plan.files import open_plan_store
from graph.plan.lifecycle import TaskLifecycle
from graph.plan.reader import load_plan
from graph.plan.run import StepFunctions, run_with
from graph_plan_support import (
    live_context,
    plan_ontology,
    plan_rules,
    planned_live_shop,
    script_plan,
    shop_answers,
    shop_view,
)
from graph_probes import requires_live_graph
from graph_probes.base import select
from graph_support import GraphProject, ScriptedGateway

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

COMMIT = "ab" * 20
CARD, FEATURE, MODULE, REFUND = (
    "REQ::FR::CARD_PAYMENT",
    "FEAT::CARD_PAYMENT",
    "MOD::PAYMENTS_SERVICE",
    "REQ::FR::REFUND",
)
WORKSPACE, TOOLCHAIN = "TASK::PROJECT::WORKSPACE", "TASK::PROJECT::TOOLCHAIN"
UNSHARED_TASK = "TASK::PROJECT::CONTRACT_UNSHARED"
EXPORT, EXPORT_TASK = "STORY::ORDERS_SERVICE::ORDER_HISTORY_EXPORT", "TASK::ORDERS_SERVICE::LOGIC_ORDER_HISTORY_EXPORT"


async def seed_specs(project: GraphProject, project_id: str) -> None:
    writer = project.writer(project_id=project_id, ontology=load_ontology())
    for spec_id, label in (
        (CARD, "FunctionalRequirement"),
        (FEATURE, "Feature"),
        (MODULE, "Module"),
        (REFUND, "FunctionalRequirement"),
    ):
        await writer.upsert_node(label, f"{project_id}-{spec_id}", {"spec_id": spec_id, "name": spec_id})


async def test_the_findings_contract_on_the_live_store(graph_project: GraphProject) -> None:
    # Scenario: add (references shared between findings), repeat, list, count, close, withdraw.
    pid = graph_project.project_id
    await seed_specs(graph_project, pid)
    findings = Findings(graph_project.store, load_ontology())

    first = await findings.add(
        pid,
        created_by="u1",
        kind="gap",
        text="Refunds are not specified.",
        about=[FEATURE, CARD],
        refers_to=[("commit", "ABCDEF1"), ("pr", "7")],
    )
    again = await findings.add(
        pid, created_by="u1", kind="gap", text=" refunds are  NOT specified. ", about=[CARD, FEATURE]
    )
    second = await findings.add(
        pid, created_by="u2", kind="question", text="Which currency?", about=[CARD], refers_to=[("commit", "abcdef1")]
    )

    assert first.created and not again.created and again.finding.finding_id == first.finding.finding_id
    assert first.finding.about_spec_ids == (FEATURE, CARD) and first.finding.origin == "agent"
    assert [(r.type, r.value) for r in first.finding.refers_to] == [("commit", "abcdef1"), ("pr", "7")]
    assert len(await graph_project.nodes("ExternalRef", project_id=pid)) == 2  # the commit is one shared node
    assert len(await graph_project.edges("ABOUT", project_id=pid)) == 3
    assert len(await graph_project.edges("REFERS_TO", project_id=pid)) == 3
    page = await findings.findings(pid)
    assert [item.finding_id for item in page.findings] == [second.finding.finding_id, first.finding.finding_id]
    assert (await findings.open_counts(pid, [CARD, FEATURE, MODULE])) == {CARD: 2, FEATURE: 1, MODULE: 0}

    closed = await findings.resolve(pid, first.finding.finding_id, by="lead", note="Decided.")
    assert (closed.status, closed.resolution_note) == ("resolved", "Decided.")
    with pytest.raises(FindingError) as refused:
        await findings.dismiss(pid, first.finding.finding_id, by="lead")
    assert refused.value.code == "wrong_state"
    assert (await findings.open_counts(pid, [CARD]))[CARD] == 1

    third = await findings.add(
        pid, created_by="u3", kind="note", text="A page.", about=[MODULE], refers_to=[("url", "https://example.com/a")]
    )
    assert len(await graph_project.nodes("ExternalRef", project_id=pid)) == 3
    assert await findings.withdraw(pid, third.finding.finding_id) is True  # its own reference goes with it
    assert await findings.withdraw(pid, second.finding.finding_id) is True  # the commit is still cited by the first
    assert len(await graph_project.nodes("ExternalRef", project_id=pid)) == 2
    assert await findings.withdraw(pid, first.finding.finding_id) is False  # closed findings are history
    with pytest.raises(FindingError) as unknown:
        await findings.add(pid, created_by="u1", kind="gap", text="x", about=["REQ::FR::NOPE"])
    assert unknown.value.spec_ids == ("REQ::FR::NOPE",)


async def test_a_build_that_merges_or_drops_a_node_is_followed_by_refresh(graph_project: GraphProject) -> None:
    # Scenario: a finding about REFUND (merged into CARD by a build: alias on the survivor, foreign edges moved) and one
    # about FEATURE (deleted by a build); refresh follows the alias, orphans the lost node and restores it.
    pid = graph_project.project_id
    await seed_specs(graph_project, pid)
    ontology = load_ontology()
    findings = Findings(graph_project.store, ontology)
    merged = (await findings.add(pid, created_by="u1", kind="gap", text="merged", about=[REFUND])).finding
    lost = (await findings.add(pid, created_by="u1", kind="gap", text="lost", about=[FEATURE, MODULE])).finding
    build = graph_project.writer(project_id=pid, ontology=ontology)
    await graph_project.update_nodes(select("SpecNode", spec_id=CARD), {"spec_id_aliases": [REFUND]}, project_id=pid)
    await build.move_foreign_relationships(
        NodeRef("FunctionalRequirement", f"{pid}-{REFUND}"), NodeRef("FunctionalRequirement", f"{pid}-{CARD}")
    )
    await build.delete_node("FunctionalRequirement", f"{pid}-{REFUND}")
    await build.delete_node("Feature", f"{pid}-{FEATURE}")

    result = await findings.refresh(pid)

    assert (result.refreshed, result.orphaned) == (2, 1)
    current = await findings.finding(pid, merged.finding_id)
    assert current is not None and current.about_spec_ids == (CARD,)
    orphan = await findings.finding(pid, lost.finding_id)
    assert orphan is not None and orphan.orphaned and orphan.about_spec_ids == (FEATURE, MODULE)
    kept = {
        (edge.source.props["finding_id"], edge.target.props["spec_id"])
        for edge in await graph_project.edges("ABOUT", project_id=pid)
    }
    assert kept == {(merged.finding_id, CARD), (lost.finding_id, MODULE)}
    assert (await findings.open_counts(pid, [REFUND, CARD]))[REFUND] == 1

    await build.upsert_node("Feature", f"{pid}-{FEATURE}", {"spec_id": FEATURE, "name": FEATURE})
    again = await findings.refresh(pid)

    assert (again.refreshed, again.orphaned) == (2, 0)
    current = await findings.finding(pid, lost.finding_id)
    assert current is not None and not current.orphaned
    assert (lost.finding_id, FEATURE) in {
        (edge.source.props["finding_id"], edge.target.props["spec_id"])
        for edge in await graph_project.edges("ABOUT", project_id=pid)
    }


def _service(project: GraphProject, findings: Findings | None = None) -> PlanService:
    ontology = plan_ontology()
    return PlanService(project.store, ontology, plan_rules(ontology), findings=findings)


async def _ready(project: GraphProject, tmp_path: Path) -> tuple[str, Findings, PlanService, FindingFlow]:
    project_id, _ = await planned_live_shop(project, tmp_path)
    life = TaskLifecycle(project.store, plan_ontology(), plan_rules(plan_ontology()))
    for task_id in (WORKSPACE, TOOLCHAIN, UNSHARED_TASK):
        claim = await life.claim_task(project_id, task_id, claimed_by="setup")
        await life.complete_task(project_id, task_id, claim_token=claim.claim_token, commit=COMMIT)
    findings = Findings(project.store, plan_ontology())
    service = _service(project, findings)
    return project_id, findings, service, FindingFlow(findings, service)


async def test_a_blocking_finding_blocks_the_story_and_resolving_it_returns_the_tasks(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, findings, service, flow = await _ready(graph_project, tmp_path)
    claim = await service.claim_story(project_id, EXPORT, claimed_by="u1")
    task = (await load_plan(graph_project.store.executor(), project_id)).tasks[EXPORT_TASK]
    about = task.implements[:1]

    question = NewFinding(
        created_by="u1", kind="question", text="Which format?", about=about, story_id=EXPORT, blocking=True
    )
    added = await flow.add_finding(project_id, question, claim_token=claim.claim_token)
    repeat = await flow.add_finding(project_id, question.model_copy(update={"text": "which format?"}))

    assert added.created and added.blocked == (EXPORT_TASK,) and not repeat.created
    stored = (await load_plan(graph_project.store.executor(), project_id)).tasks[EXPORT_TASK]
    assert (stored.status, stored.block_reason, stored.blocked_by) == (
        "blocked",
        "spec_ambiguous",
        added.finding.finding_id,
    )
    preview = await service.story_preview(project_id, EXPORT)
    assert preview.state == "blocked" and [f.finding_id for f in preview.findings] == [added.finding.finding_id]
    brief = await service.story_brief(project_id, EXPORT)
    assert [(f.finding_id, f.blocking) for f in brief.findings] == [(added.finding.finding_id, True)]
    with pytest.raises(TaskStateError) as refused:
        await flow.add_finding(
            project_id,
            NewFinding(created_by="u1", kind="gap", text="another", about=about, story_id=EXPORT, blocking=True),
            claim_token=claim.claim_token,
        )
    assert refused.value.code == "claim_mismatch" and len((await findings.findings(project_id)).findings) == 1

    closed = await flow.close_finding(project_id, added.finding.finding_id, by="lead")

    assert closed.unblocked == (EXPORT_TASK,)
    stored = (await load_plan(graph_project.store.executor(), project_id)).tasks[EXPORT_TASK]
    assert (stored.status, stored.blocked_by) == ("pending", None)


async def test_a_plan_run_that_resets_a_blocked_task_leaves_the_finding_open(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    # Scenario: a blocking finding blocks EXPORT; the implemented requirement changes meaning and a replan resets the
    # task (block and finding link cleared); the finding is still open and closing it unblocks nothing.
    project_id, findings, service, flow = await _ready(graph_project, tmp_path)
    claim = await service.claim_story(project_id, EXPORT, claimed_by="u1")
    task = (await load_plan(graph_project.store.executor(), project_id)).tasks[EXPORT_TASK]
    missing = NewFinding(
        created_by="u1", kind="gap", text="Missing limit.", about=task.implements[:1], story_id=EXPORT, blocking=True
    )
    added = await flow.add_finding(project_id, missing, claim_token=claim.claim_token)
    for spec_id in task.implements:
        await graph_project.update_nodes(
            select("SpecNode", spec_id=spec_id), {"last_build_id": "build-2"}, project_id=project_id
        )
    replan = live_context(
        graph_project,
        project_id,
        open_plan_store(tmp_path),
        script_plan(ScriptedGateway(), shop_answers(await shop_view())),
        build_id="build-2",
        replan=True,
    )

    result = await run_with(replan, StepFunctions())

    assert result.checks is not None and result.checks.ok, result.checks
    reset = (await load_plan(graph_project.store.executor(), project_id)).tasks[EXPORT_TASK]
    assert (reset.status, reset.blocked_by, reset.block_reason) == ("pending", None, None)
    current = await findings.finding(project_id, added.finding.finding_id)
    assert current is not None and current.status == "open"
    closed = await flow.close_finding(project_id, added.finding.finding_id, by="lead")
    assert closed.unblocked == () and closed.finding.status == "resolved"
