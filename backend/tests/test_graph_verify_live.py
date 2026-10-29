"""Live Neo4j verification with repair on the shop corpus: injected faults, their repairs, the
deletion valve, foreign data, read-only ``verify_project`` and the build result (plan 4.T2).

FR-VER-01..04, FR-BLD-15; analysis.md "Verification"; m4-spec-edits G5 (valve max(1, 5%), edges
deleted with their node counted), G6 (a follow-up leaves the problem in place), G8 (a retried lineage
re-extracts never-extracted and unknown-status fragments), G9 (spec_id forms, repair deletions in
``spec_changes``), G10 (an edge without ``basis`` is stated).

Builds use the hand-written shop answers (``ShopAnswers``) and, where inference runs, a hand-written
judge rule (``judge`` below: every orphan goes to its first candidate, quoting its first shown
fragment; nothing else is linked). Faults are raw Cypher, as a bug or another writer would leave
them: between builds, inside a build right after the infer step (``Shop.build(fault=...)``, so the
build's own verify step meets them), or before a re-run of the verify step of a finished build.

Gate: ``SPECFLOW_TEST_NEO4J=1`` and ``uv run --env-file ../.env.local``. Every test builds into its
own ``pytest-graph-<hex>`` project ids; ``neo4j_project`` deletes them afterwards and checks that
nothing outside them changed.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import graph_build as example
from graph.build import (
    BuildContext,
    BuildResult,
    BuildSettings,
    open_file_stores,
    run_build,
    steps,
    verify_project,
)
from graph.build import run as runner
from graph.build import verify as verification
from graph.build.graphiti import NODES_PROMPT
from graph.build.models import (
    InferenceReport,
    PlanArtifact,
    Repair,
    SpecChanges,
    TargetGraph,
    VerificationReport,
)
from graph.build.verify import READ_BASES, verify_build
from graph_support import (
    Neo4jProject,
    ScriptedGateway,
    ScriptedRequest,
    live_llm_config,
    shipped_ontology,
    shop_answers,
    shop_corpus,
    spine_unreachable_count,
    untrusted_blocks,
)
from specflow_live import requires_live

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")

ANSWERS = shop_answers(1)
ONTOLOGY = shipped_ontology()
JUDGE = {rule.template: rule.prompt for rule in ONTOLOGY.inference.rules}
CLEAN_STATUS = "partially_succeeded"  # the shop answers skip one orders.md fragment (FR-BLD-11)
DROPPED = {"RELATES_TO": 1}  # the illegal "Authentication RELATES_TO Audit logging" of the answers
OBLIGATIONS = "ord.obligations"  # a fragment that is one node window of its own (8 entities)
DETAILS3 = "ord.details3"  # one node window of its own, no entity

Fault = Callable[[Neo4jProject, str], Awaitable[None]]


def judge(request: ScriptedRequest) -> dict[str, Any]:
    """Hand-written judge rule: attach every orphan to its first candidate, quoting the first shown
    fragment; decline every endpoints and realization subject."""

    blocks = dict(untrusted_blocks(request.user))
    payload = json.loads(blocks.pop("payload"))
    if request.prompt == JUDGE["attach_orphan"]:
        fragment_id = min(blocks)
        quote = " ".join(blocks[fragment_id].split())[:40]
        evidence = [{"fragment_id": fragment_id, "quote": quote}]
        return {"confidence": 0.9, "evidence": evidence, "target": payload["candidates"][0]["id"]}
    declined = {JUDGE["endpoints"]: {"links": []}, JUDGE["realization"]: {"targets": []}}
    return {"confidence": 0.9, "evidence": [], **declined[request.prompt]}


@dataclass
class Built:
    ctx: BuildContext
    result: BuildResult
    gateway: ScriptedGateway

    async def artifact(self, step: str, model: Any) -> Any:
        payload = await self.ctx.builds.get_artifact(self.ctx.project_id, self.result.build_id, step)
        assert payload is not None, step
        return model.model_validate(payload)

    async def report(self) -> VerificationReport:
        return await self.artifact("verify", VerificationReport)

    async def handles(self) -> dict[str, str]:
        return ANSWERS.fragment_ids(await self.artifact("plan", PlanArtifact))


class Shop:
    """Builds of one project from the shop snapshots, sharing file stores and the corpus index."""

    def __init__(self, project: Neo4jProject, gateways: Callable[..., ScriptedGateway], root: Path) -> None:
        self.project = project
        self.gateways = gateways
        self.root = root
        self.project_id = project.project_id
        self.stores = open_file_stores(root)

    async def build(
        self,
        snapshot: int = 1,
        *,
        fault: Fault | None = None,
        retry: tuple[str, ...] = (),
        inference: bool = False,
    ) -> Built:
        gateway = shop_answers(snapshot).install(self.gateways(live_llm_config()))
        if inference:
            for prompt in JUDGE.values():
                gateway.handle(prompt, judge)
        ctx = BuildContext(
            project_id=self.project_id,
            ontology=self.project.ontology,
            driver=self.project.driver,
            gateway=gateway,
            registry=self.stores.registry,
            corpus=shop_corpus(snapshot, self.root),
            extraction=self.stores.extraction,
            builds=self.stores.builds,
            options=ANSWERS.options,
            settings=BuildSettings(inference=inference),
            retry_lineages=retry,
            database=self.project.database,
        )
        with pytest.MonkeyPatch.context() as patch:
            if fault is not None:
                infer = runner.STEP_FUNCTIONS["infer"]

                async def infer_then_fault(context: BuildContext) -> None:
                    await infer(context)
                    await fault(self.project, self.project_id)

                patch.setitem(runner.STEP_FUNCTIONS, "infer", infer_then_fault)
            result = await run_build(ctx)
        assert result.failed_step is None, (result.failed_step, result.error_code)
        return Built(ctx.with_build_id(result.build_id), result, gateway)


@pytest.fixture
def shop(neo4j_project: Neo4jProject, scripted_gateway: Any, tmp_path: Path) -> Shop:
    return Shop(neo4j_project, scripted_gateway, tmp_path / "stores")


# Graph probes and faults (raw Cypher)


async def one(project: Neo4jProject, text: str, **params: Any) -> dict[str, Any]:
    rows = await project.run(text, p=project.project_id, **params)
    assert len(rows) == 1, rows
    return rows[0]


async def spec_ids(project: Neo4jProject) -> set[str]:
    rows = await project.run("MATCH (n:SpecNode) WHERE n.group_id = $p RETURN n.spec_id AS s", p=project.project_id)
    return {row["s"] for row in rows}


async def count(project: Neo4jProject, text: str, **params: Any) -> int:
    return int((await one(project, text, **params))["n"])


ILLEGAL_EDGE = (
    "MATCH (m:Module), (r:FunctionalRequirement)-[:REFERENCED_BY]->(f:Fragment) "
    "WHERE m.group_id = $p AND r.group_id = $p AND f.status = 'active' "
    "WITH m, r, f ORDER BY m.spec_id, r.spec_id, f.fragment_id LIMIT 1 "
    "CREATE (m)-[:PART_OF {group_id: $p, edge_key: 'fault|illegal', uuid: $uuid, basis: 'stated', "
    "source_fragment_ids: [f.fragment_id]}]->(r) "
    "RETURN m.spec_id + '|PART_OF|' + r.spec_id AS subject"
)


async def illegal_edge(project: Neo4jProject, uuid: str = "fault-illegal") -> str:
    """A legal-looking, grounded ``Module PART_OF FunctionalRequirement`` (a pair the ontology forbids)."""

    return str((await one(project, ILLEGAL_EDGE, uuid=uuid))["subject"])


async def first_node(project: Neo4jProject, label: str, skip: int = 0) -> dict[str, Any]:
    return await one(
        project,
        f"MATCH (n:{label}) WHERE n.group_id = $p WITH n ORDER BY n.spec_id SKIP $skip LIMIT 1 "
        "RETURN n.uuid AS uuid, n.spec_id AS spec_id, n.identity_key AS key, n.spec_id_aliases AS aliases",
        skip=skip,
    )


async def stray_node(project: Neo4jProject, *, grounded: bool, spec_id: str | None, label: str = "Module") -> None:
    """A spec node no build wrote: grounded in the first active fragment, or not at all."""

    await project.run(
        f"CREATE (n:SpecNode:{label} {{group_id: $p, uuid: 'fault-node', name: 'Payment Gateway', "
        "identity_key: 'Module|name|payment gateway (fault)'}) SET n.spec_id = $spec_id "
        "WITH n MATCH (f:Fragment) WHERE f.group_id = $p AND f.status = 'active' AND $grounded "
        "WITH n, f ORDER BY f.fragment_id LIMIT 1 "
        "CREATE (n)-[:REFERENCED_BY {group_id: $p, line_start: 1, line_end: 1}]->(f)",
        p=project.project_id,
        spec_id=spec_id,
        grounded=grounded,
    )


def edge_ends(subject: str) -> tuple[str, str, str]:
    parts = subject.split("|")
    assert len(parts) == 3, subject
    return parts[0], parts[1], parts[2]


async def assert_spec_id_forms(project: Neo4jProject, *payloads: Any, known: set[str] = frozenset()) -> None:
    """NFR-SEC-01, G9: results and reports name edges ``<source spec_id>|<RELATION>|<target spec_id>``
    and hold no identity key, edge key or document text."""

    pid = project.project_id
    nodes = await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p "
        "RETURN n.spec_id AS spec_id, n.identity_key AS key, coalesce(n.identity_aliases, []) AS aliases",
        p=pid,
    )
    edge_keys = await project.run(
        "MATCH ()-[r]->() WHERE r.group_id = $p AND r.edge_key IS NOT NULL RETURN r.edge_key AS key", p=pid
    )
    texts = await project.run("MATCH (f:Fragment) WHERE f.group_id = $p RETURN f.text AS text", p=pid)
    secrets = {row["key"] for row in nodes if row["key"]} | {alias for row in nodes for alias in row["aliases"]}
    secrets |= {row["key"] for row in edge_keys}
    lines = {line.strip() for row in texts for line in (row["text"] or "").splitlines() if len(line.strip()) >= 30}
    assert secrets and lines
    ids = {row["spec_id"] for row in nodes} | set(known)
    for payload in payloads:
        data = payload.model_dump(mode="json")
        text = json.dumps(data, ensure_ascii=False)
        assert not [secret for secret in secrets if secret in text]
        assert not [line for line in lines if line in text]
        named: list[str] = []
        if isinstance(payload, VerificationReport):
            named = [item["subject"] for item in (*data["problems"], *data["repairs"])]
            named += [*data["removed_inferred_edges"]]
        if isinstance(payload, BuildResult):
            named = [*data["spec_changes"]["inferred_added"], *data["spec_changes"]["inferred_removed"]]
        for subject in (item for item in named if "|" in item):
            source, relation, target = edge_ends(subject)
            assert relation in ONTOLOGY.relations and {source, target} <= ids, subject


# FR-BLD-15: every field of the build result, on first, update and no-change builds


async def test_clean_builds_report_every_result_field(shop: Shop, neo4j_project: Neo4jProject) -> None:
    first = await shop.build(1)
    first_graph = await graph_counts(neo4j_project)
    update = await shop.build(2)
    update_graph = await graph_counts(neo4j_project)
    unchanged = await shop.build(2)
    unchanged_graph = await graph_counts(neo4j_project)
    assert (first.result.mode, update.result.mode, unchanged.result.mode) == ("first", "update", "update")
    for built, graph in ((first, first_graph), (update, update_graph), (unchanged, unchanged_graph)):
        result = built.result
        plan = await built.artifact("plan", PlanArtifact)
        conform = await built.artifact("conform", TargetGraph)
        report = await built.report()
        assert (result.project_id, result.failed_step, result.error_code) == (shop.project_id, None, None)
        assert result.started_at <= result.finished_at and result.settled
        assert result.status == ("partially_succeeded" if result.skipped_fragments else "succeeded")
        assert set(result.skipped_fragments) <= {fragment.fragment_id for fragment in plan.fragments}
        # Metrics: the target graph is what was persisted; units add up to the plan's.
        metrics = result.metrics
        assert metrics.nodes_by_label == graph["nodes"] and metrics.edges_by_relation == graph["edges"]
        assert metrics.units_extracted + metrics.units_reused == len(plan.units)
        assert sum(metrics.fragments_by_status.values()) == len(plan.fragments) + len(plan.retired_ids)
        assert metrics.fragments_by_status.get("skipped", 0) == len(result.skipped_fragments)
        assert metrics.exclusive_conflicts == conform.counters.exclusive_conflicts
        assert set(metrics.duration_s) >= {"plan", "persist", "infer", "verify"}
        # Verification summary: the stored report, the graph as Cypher counts it.
        summary = result.verification
        assert summary is not None and summary.ok
        assert (summary.found, summary.problems, summary.repairs, summary.rounds, summary.blocked_rounds) == (
            {},
            {},
            0,
            0,
            0,
        )
        assert summary.follow_up_lineages == () and summary.metrics == report.metrics
        assert (
            report.ok
            and not report.read_only
            and (report.spec_nodes, report.domain_edges)
            == (
                sum(graph["nodes"].values()),
                sum(graph["edges"].values()),
            )
        )
        unreached = await spine_unreachable_count(neo4j_project, shop.project_id)
        assert summary.metrics.spine_unreachable == summary.metrics.spine_unreachable_before == unreached
        assert metrics.spine_unreachable == unreached  # no inference: before and after agree
        assert summary.metrics.exclusive_conflicts == conform.counters.exclusive_conflicts
        assert summary.metrics.dropped_edges == {
            relation: sum(reasons.values())
            for relation, reasons in conform.counters.dropped_edges.items()
            if sum(reasons.values())
        }
        assert sum(metrics.dropped_edges.values()) == sum(summary.metrics.dropped_edges.values())
        await assert_spec_id_forms(neo4j_project, result, report)
    assert first.result.status == CLEAN_STATUS and first.result.verification.metrics.dropped_edges == DROPPED
    assert len(first.result.spec_changes.added) == sum(first_graph["nodes"].values())
    assert first.result.metrics.units_reused == 0 and first.result.metrics.llm
    assert update.result.spec_changes != SpecChanges() and update.result.metrics.units_reused > 0
    assert unchanged.result.spec_changes == SpecChanges()
    assert unchanged.result.metrics.units_extracted == 0
    assert [call for call in unchanged.gateway.calls if call.call_class != "embedding"] == []


async def graph_counts(project: Neo4jProject) -> dict[str, dict[str, int]]:
    """Spec nodes per domain label and domain edges per relation, as persisted."""

    labels = sorted(label for label in ONTOLOGY.labels if ONTOLOGY.owner_of(label) == "build")
    nodes = await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p UNWIND [l IN labels(n) WHERE l IN $labels] AS label "
        "RETURN label, count(*) AS n ORDER BY label",
        p=project.project_id,
        labels=labels,
    )
    edges = await project.run(
        "MATCH (:SpecNode)-[r]->(:SpecNode) WHERE r.group_id = $p AND type(r) IN $relations "
        "RETURN type(r) AS relation, count(*) AS n ORDER BY relation",
        p=project.project_id,
        relations=sorted(ONTOLOGY.relations),
    )
    return {"nodes": {row["label"]: row["n"] for row in nodes}, "edges": {row["relation"]: row["n"] for row in edges}}


# FR-VER-01: an illegal edge written by a fault is deleted and the run is ok


async def test_illegal_edge_is_deleted_and_the_run_is_ok(shop: Shop, neo4j_project: Neo4jProject) -> None:
    seen: dict[str, Any] = {}

    async def fault(project: Neo4jProject, pid: str) -> None:
        seen["before"] = await project.snapshot()
        seen["subject"] = await illegal_edge(project)

    built = await shop.build(1, fault=fault)
    report = await built.report()
    assert report.found == {"illegal_edge": 1} and report.problems == ()
    assert report.repairs == (Repair(round=1, check="illegal_edge", action="delete_edge", subject=seen["subject"]),)
    assert report.ok and report.rounds == 1 and report.blocked_rounds == 0
    assert await neo4j_project.snapshot() == seen["before"]
    # Verification never changes the status; nothing deleted was a node or an inferred edge.
    result = built.result
    assert result.status == CLEAN_STATUS and result.settled
    assert result.verification is not None and (result.verification.ok, result.verification.repairs) == (True, 1)
    assert result.spec_changes.removed == () and result.spec_changes.inferred_removed == ()
    await assert_spec_id_forms(neo4j_project, result, report)


# node_label


async def test_label_repairs_are_consistent_and_the_next_build_changes_nothing(
    shop: Shop, neo4j_project: Neo4jProject
) -> None:
    await shop.build(1)
    conform_keys = {node.identity_key: node for node in (await shop_conform(shop)).nodes}
    two = await first_node(neo4j_project, "FunctionalRequirement")
    bare = await first_node(neo4j_project, "Feature")
    # Between builds: a second label (Diff reads it); in the next build: a lost label and a node
    # whose spec_id names no label.
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: $u}) SET n:Feature", p=shop.project_id, u=two["uuid"]
    )

    async def fault(project: Neo4jProject, pid: str) -> None:
        await project.run("MATCH (n:SpecNode {group_id: $p, uuid: $u}) REMOVE n:Feature", p=pid, u=bare["uuid"])
        await stray_node(project, grounded=True, spec_id="ZZZ::STRAY", label="Reviewed")

    second = await shop.build(1, fault=fault)
    report = await second.report()
    assert report.ok and report.rounds == 1, report
    assert report.problems == ()
    assert [(r.check, r.action, r.subject, r.value) for r in report.repairs if r.check == "node_label"] == [
        ("node_label", "set_label", bare["spec_id"], "Feature"),
        ("node_label", "set_label", two["spec_id"], "FunctionalRequirement"),
        ("node_label", "delete_node", "ZZZ::STRAY", None),
    ]
    rows = await neo4j_project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p AND n.uuid IN $uuids "
        "RETURN n.uuid AS uuid, n.identity_key AS key, labels(n) AS labels, n.label_disagreement AS disagreement",
        p=shop.project_id,
        uuids=[two["uuid"], bare["uuid"], "fault-node"],
    )
    labels = {row["uuid"]: sorted(row["labels"]) for row in rows}
    assert labels == {two["uuid"]: ["FunctionalRequirement", "SpecNode"], bare["uuid"]: ["Feature", "SpecNode"]}
    # label_disagreement as Diff computes it: the target node's (none of the shop's).
    for row in rows:
        assert row["disagreement"] == conform_keys[row["key"]].label_disagreement
    stored = await count(
        neo4j_project,
        "MATCH (n:SpecNode) WHERE n.group_id = $p AND n.label_disagreement IS NOT NULL RETURN count(n) AS n",
    )
    assert report.metrics.label_disagreement == stored == 0
    assert "ZZZ::STRAY" in second.result.spec_changes.removed  # G9: repair deletions in spec_changes
    third = await shop.build(1)
    assert third.result.spec_changes == SpecChanges()
    assert third.result.verification is not None and third.result.verification.found == {}
    assert third.result.verification.metrics.label_disagreement == 0


async def shop_conform(shop: Shop) -> TargetGraph:
    record = await shop.stores.builds.latest_build(shop.project_id, finished_only=True)
    assert record is not None
    payload = await shop.stores.builds.get_artifact(shop.project_id, record.build_id, "conform")
    return TargetGraph.model_validate(payload)


# spec_id


async def test_spec_id_repairs_restore_issued_ids_keep_aliases_and_survive_a_resume(
    shop: Shop, neo4j_project: Neo4jProject
) -> None:
    built = await shop.build(1)
    pid = shop.project_id
    clean = await neo4j_project.snapshot()
    victim = await first_node(neo4j_project, "FunctionalRequirement")
    missing = await first_node(neo4j_project, "FunctionalRequirement", skip=1)
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: $u}) SET n.spec_id = 'bad id'", p=pid, u=victim["uuid"]
    )
    await neo4j_project.run("MATCH (n:SpecNode {group_id: $p, uuid: $u}) REMOVE n.spec_id", p=pid, u=missing["uuid"])
    await stray_node(neo4j_project, grounded=True, spec_id=None)
    checked = await verify_project(built.ctx)
    assert {(p.check, p.subject, p.code) for p in checked.problems} == {
        ("spec_id", "bad id", "malformed"),
        ("spec_id", "fault-node", "missing"),
        ("spec_id", missing["uuid"], "missing"),
    }
    await steps.verify(built.ctx)
    report = await built.report()
    assert report.ok and report.rounds == 1 and report.found == {"spec_id": 3}
    assigned = {r.subject: r.value for r in report.repairs if r.action == "assign_spec_id"}
    assert assigned == {
        "bad id": victim["spec_id"],
        missing["uuid"]: missing["spec_id"],
        "fault-node": "MOD::PAYMENT_GATEWAY",
    }
    rows = {
        row["uuid"]: row
        for row in await neo4j_project.run(
            "MATCH (n:SpecNode) WHERE n.group_id = $p AND n.uuid IN $uuids "
            "RETURN n.uuid AS uuid, n.spec_id AS spec_id, n.spec_id_aliases AS aliases",
            p=pid,
            uuids=[victim["uuid"], missing["uuid"], "fault-node"],
        )
    }
    assert (rows[victim["uuid"]]["spec_id"], rows[victim["uuid"]]["aliases"]) == (victim["spec_id"], ["bad id"])
    assert (rows[missing["uuid"]]["spec_id"], rows[missing["uuid"]]["aliases"]) == (missing["spec_id"], None)
    assert rows["fault-node"]["spec_id"] == "MOD::PAYMENT_GATEWAY"
    taken = await shop.stores.registry.taken(pid)
    assert (taken["bad id"].state, taken["bad id"].uuid) == ("alias", victim["uuid"])
    assert (taken["MOD::PAYMENT_GATEWAY"].state, taken["MOD::PAYMENT_GATEWAY"].uuid) == ("active", "fault-node")

    # A round that crashes after the registry write: the resumed run still keeps the alias.
    second = await first_node(neo4j_project, "FunctionalRequirement", skip=2)
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: $u}) SET n.spec_id = 'bad id 2'", p=pid, u=second["uuid"]
    )

    async def crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("crash after the registry write")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(verification, "apply_round", crash)
        with pytest.raises(RuntimeError):
            await steps.verify(built.ctx)
    assert (await shop.stores.registry.taken(pid))["bad id 2"].state == "alias"
    await steps.verify(built.ctx)
    assert (await built.report()).ok
    row = await one(
        neo4j_project,
        "MATCH (n:SpecNode {group_id: $p, uuid: $u}) RETURN n.spec_id AS s, n.spec_id_aliases AS a",
        u=second["uuid"],
    )
    assert (row["s"], row["a"]) == (second["spec_id"], ["bad id 2"])
    # Undo what the faults left (aliases, the stray node): the graph is the clean one.
    await neo4j_project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p AND n.uuid IN $uuids REMOVE n.spec_id_aliases",
        p=pid,
        uuids=[victim["uuid"], second["uuid"]],
    )
    await neo4j_project.run("MATCH (n:SpecNode {group_id: $p, uuid: 'fault-node'}) DETACH DELETE n", p=pid)
    assert await neo4j_project.snapshot() == clean


# ungrounded_node and ungrounded_edge: restored from the build's target graph, else deleted


async def test_grounding_is_restored_from_the_target_graph_else_deleted(
    shop: Shop, neo4j_project: Neo4jProject
) -> None:
    await shop.build(1)
    seen: dict[str, Any] = {}

    async def fault(project: Neo4jProject, pid: str) -> None:
        # A no-change update writes nothing, so its target graph is the only evidence to restore from.
        seen["before"] = await project.snapshot()
        seen["nodes"] = [
            row["s"]
            for row in await project.run(
                "MATCH (n:SpecNode) WHERE n.group_id = $p WITH n ORDER BY n.spec_id LIMIT 5 "
                "MATCH (n)-[r:REFERENCED_BY]->() DELETE r RETURN DISTINCT n.spec_id AS s",
                p=pid,
            )
        ]
        seen["edge"] = (
            await one(
                project,
                "MATCH (s:SpecNode)-[r]->(t:SpecNode) WHERE r.group_id = $p AND r.basis = 'stated' "
                "WITH s, r, t ORDER BY r.edge_key LIMIT 1 SET r.source_fragment_ids = [] "
                "RETURN s.spec_id + '|' + type(r) + '|' + t.spec_id AS subject",
            )
        )["subject"]
        # No basis (read as stated) and no evidence anywhere: deleted.
        seen["stray_edge"] = (
            await one(
                project,
                "MATCH (a:FunctionalRequirement), (b:FunctionalRequirement) WHERE a.group_id = $p AND b.group_id = $p "
                "AND a.spec_id < b.spec_id AND NOT (a)-[:RELATES_TO]-(b) WITH a, b ORDER BY a.spec_id, b.spec_id "
                "LIMIT 1 "
                "CREATE (a)-[:RELATES_TO {group_id: $p, edge_key: 'fault|stray', uuid: 'fault-stray'}]->(b) "
                "RETURN a.spec_id + '|RELATES_TO|' + b.spec_id AS subject",
            )
        )["subject"]
        await stray_node(project, grounded=False, spec_id="MOD::STRAY")

    built = await shop.build(1, fault=fault)
    report = await built.report()
    assert report.found == {"ungrounded_node": 6, "ungrounded_edge": 2} and report.ok and report.rounds == 1
    assert {(r.check, r.action, r.subject) for r in report.repairs} == {
        *(("ungrounded_node", "restore_evidence", spec_id) for spec_id in seen["nodes"]),
        ("ungrounded_node", "delete_node", "MOD::STRAY"),
        ("ungrounded_edge", "restore_evidence", seen["edge"]),
        ("ungrounded_edge", "delete_edge", seen["stray_edge"]),
    }
    assert len(seen["nodes"]) == 5 and await neo4j_project.snapshot() == seen["before"]
    assert report.removed_spec_ids == ("MOD::STRAY",) and report.removed_inferred_edges == ()
    assert built.result.spec_changes == SpecChanges(removed=("MOD::STRAY",))
    assert built.result.status == CLEAN_STATUS and built.result.settled
    await assert_spec_id_forms(neo4j_project, built.result, report, known={"MOD::STRAY"})


# inferred_edge: deleted; the next inference adds it again


async def test_inferred_edge_without_confidence_is_deleted_and_inference_adds_it_again(
    shop: Shop, neo4j_project: Neo4jProject
) -> None:
    first = await shop.build(1, inference=True)
    inferred = await neo4j_project.run(
        "MATCH (s:SpecNode)-[r]->(t:SpecNode) WHERE r.group_id = $p AND r.basis = 'inferred' "
        "RETURN s.spec_id + '|' + type(r) + '|' + t.spec_id AS subject, r.uuid AS uuid ORDER BY subject",
        p=shop.project_id,
    )
    assert inferred, "the judge rule attaches orphans"
    assert first.result.spec_changes.inferred_added == tuple(row["subject"] for row in inferred)
    victim = inferred[0]
    infer_report = await first.artifact("infer", InferenceReport)
    metrics = first.result.verification.metrics
    # Spine before and after inference (the same walk as inference's).
    assert metrics.spine_unreachable_before == infer_report.spine_unreachable_before
    assert metrics.spine_unreachable < metrics.spine_unreachable_before
    assert metrics.spine_unreachable == await spine_unreachable_count(neo4j_project, shop.project_id)
    assert metrics.spine_unreachable_before == await spine_unreachable_count(neo4j_project, shop.project_id, READ_BASES)

    hits: list[int] = []

    async def fault(project: Neo4jProject, pid: str) -> None:
        rows = await project.run(
            "MATCH ()-[r {uuid: $u}]->() WHERE r.group_id = $p REMOVE r.confidence RETURN count(r) AS n",
            p=pid,
            u=victim["uuid"],
        )
        hits.append(rows[0]["n"])

    second = await shop.build(1, inference=True, fault=fault)
    report = await second.report()
    assert hits == [1], await second.artifact("infer", InferenceReport)
    assert report.found == {"inferred_edge": 1} and report.ok
    assert [(r.check, r.action, r.subject) for r in report.repairs] == [
        ("inferred_edge", "delete_edge", victim["subject"])
    ]
    assert report.removed_inferred_edges == (victim["subject"],)
    assert second.result.spec_changes == SpecChanges(inferred_removed=(victim["subject"],))
    assert (
        await count(
            neo4j_project, "MATCH ()-[r {uuid: $u}]->() WHERE r.group_id = $p RETURN count(r) AS n", u=victim["uuid"]
        )
        == 0
    )
    await assert_spec_id_forms(neo4j_project, second.result, report)

    third = await shop.build(1, inference=True)
    assert third.result.spec_changes == SpecChanges(inferred_added=(victim["subject"],))
    assert third.result.verification is not None and third.result.verification.found == {}
    again = await neo4j_project.run(
        "MATCH (s:SpecNode)-[r]->(t:SpecNode) WHERE r.group_id = $p AND r.basis = 'inferred' "
        "RETURN s.spec_id + '|' + type(r) + '|' + t.spec_id AS subject, r.confidence AS confidence ORDER BY subject",
        p=shop.project_id,
    )
    assert [row["subject"] for row in again] == [row["subject"] for row in inferred]
    assert all(isinstance(row["confidence"], float) for row in again)


# fragment_state: a follow-up is requested; the follow-up update with the retried lineage settles it


async def test_unextracted_and_unknown_fragments_request_a_follow_up_that_settles_them(
    shop: Shop, neo4j_project: Neo4jProject
) -> None:

    handles: dict[str, str] = {}

    async def unextracted(project: Neo4jProject, pid: str) -> None:
        plan = await shop.stores.builds.latest_build(pid)
        assert plan is not None
        payload = await shop.stores.builds.get_artifact(pid, plan.build_id, "plan")
        handles.update(ANSWERS.fragment_ids(PlanArtifact.model_validate(payload)))
        await project.run(
            "MATCH (f:Fragment {group_id: $p, fragment_id: $f}) SET f.extracted_hash = null",
            p=pid,
            f=handles[OBLIGATIONS],
        )

    first = await shop.build(1, fault=unextracted)
    fid = handles[OBLIGATIONS]
    nodes = await spec_ids(neo4j_project)
    report = await first.report()
    assert [(p.check, p.subject, p.code) for p in report.problems] == [("fragment_state", fid, "not_extracted")]
    assert [(r.action, r.subject, r.value) for r in report.repairs] == [("request_follow_up", fid, "orders.md")]
    assert (report.ok, report.rounds, report.follow_up_lineages) == (False, 0, ("orders.md",))
    assert first.result.status == CLEAN_STATUS and not first.result.settled  # G6
    assert first.result.verification is not None and first.result.verification.follow_up_lineages == ("orders.md",)
    # verify_project lists the problem but requests nothing.
    checked = await verify_project(first.ctx)
    assert [(p.check, p.code) for p in checked.problems] == [("fragment_state", "not_extracted")]
    assert checked.follow_up_lineages == () and checked.repairs == ()

    # The example takes the follow-ups of the latest finished build (--retry-follow-ups).
    args = argparse.Namespace(retry_follow_ups=True, retry_lineage=[])
    retry = await example.retry_lineages(shop.stores, shop.project_id, args)
    assert retry == ("orders.md",)
    second = await shop.build(1, retry=retry)
    assert (OBLIGATIONS,) in set(second.gateway.keys_for(NODES_PROMPT))
    assert await fragment(neo4j_project, fid) == {"status": "active", "extracted": True}
    assert second.result.settled and second.result.verification is not None and second.result.verification.ok
    assert await spec_ids(neo4j_project) == nodes

    # An unknown status: it grounds nothing (verify_project reports what only it grounds), the
    # next build reads it as active and never extracted (its nodes stay) and a retry settles it.
    await neo4j_project.run(
        "MATCH (f:Fragment {group_id: $p, fragment_id: $f}) SET f.status = 'weird'", p=shop.project_id, f=fid
    )
    checked = await verify_project(second.ctx)
    only_here = await neo4j_project.run(
        "MATCH (n:SpecNode)-[:REFERENCED_BY]->(f:Fragment {fragment_id: $f}) WHERE n.group_id = $p "
        "AND NOT EXISTS { MATCH (n)-[:REFERENCED_BY]->(g:Fragment) WHERE g <> f AND g.status IN ['active', "
        "'skipped'] } "
        "RETURN n.spec_id AS s",
        p=shop.project_id,
        f=fid,
    )
    assert only_here
    assert [(p.subject, p.code) for p in checked.problems if p.check == "fragment_state"] == [(fid, "unknown_status")]
    assert {p.subject for p in checked.problems if p.check == "ungrounded_node"} == {row["s"] for row in only_here}
    assert {p.check for p in checked.problems} == {"fragment_state", "ungrounded_node", "ungrounded_edge"}
    third = await shop.build(1)
    assert not third.result.settled and third.result.verification is not None
    assert third.result.verification.follow_up_lineages == ("orders.md",)
    assert third.result.verification.found == third.result.verification.problems == {"fragment_state": 1}
    assert await fragment(neo4j_project, fid) == {"status": "active", "extracted": None}
    assert await spec_ids(neo4j_project) == nodes
    retry = await example.retry_lineages(shop.stores, shop.project_id, args)
    fourth = await shop.build(1, retry=retry)
    assert (OBLIGATIONS,) in set(fourth.gateway.keys_for(NODES_PROMPT))
    assert await fragment(neo4j_project, fid) == {"status": "active", "extracted": True}
    assert fourth.result.settled and fourth.result.verification is not None and fourth.result.verification.ok
    assert await spec_ids(neo4j_project) == nodes


async def fragment(project: Neo4jProject, fragment_id: str) -> dict[str, Any]:
    return await one(
        project,
        "MATCH (f:Fragment {group_id: $p, fragment_id: $f}) "
        "RETURN f.status AS status, f.extracted_hash = f.content_hash AS extracted",
        f=fragment_id,
    )


# FR-VER-02: the valve, max(1, 5%) of spec nodes and of domain edges


async def test_half_the_nodes_ungrounded_deletes_nothing(shop: Shop, neo4j_project: Neo4jProject) -> None:
    await shop.build(1)
    pid = shop.project_id
    nodes = await count(neo4j_project, "MATCH (n:SpecNode) WHERE n.group_id = $p RETURN count(n) AS n")
    edges = await count(
        neo4j_project,
        "MATCH (:SpecNode)-[r]->(:SpecNode) WHERE r.group_id = $p AND r.edge_key IS NOT NULL RETURN count(r) AS n",
    )
    half = nodes // 2
    await neo4j_project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p WITH n ORDER BY n.spec_id LIMIT $k MATCH (n)-[r:REFERENCED_BY]->() "
        "DELETE r",
        p=pid,
        k=half,
    )
    touching = await count(
        neo4j_project,
        "MATCH (n:SpecNode)-[r]-(:SpecNode) WHERE n.group_id = $p AND r.edge_key IS NOT NULL "
        "AND NOT (n)-[:REFERENCED_BY]->() RETURN count(DISTINCT r) AS n",
    )
    faulty = await neo4j_project.snapshot()
    # No target graph to restore from: every planned repair is a delete.
    report = await verify_build(
        neo4j_project.executor, pid, ONTOLOGY, build_id="b-valve", registry=shop.stores.registry, target=None, diff=None
    )
    assert report.found == {"ungrounded_node": half} and len(report.problems) == half
    assert (report.ok, report.rounds, report.blocked_rounds) == (False, 0, 1)
    assert report.valve == {"nodes": half, "edges": touching, "spec_nodes": nodes, "domain_edges": edges}
    assert report.repairs == () and report.removed_spec_ids == ()
    assert await neo4j_project.snapshot() == faulty


async def test_valve_blocks_inside_a_build_without_changing_its_status(shop: Shop, neo4j_project: Neo4jProject) -> None:
    seen: dict[str, Any] = {}

    async def fault(project: Neo4jProject, pid: str) -> None:
        await project.run(
            "MATCH (f:Fragment) WHERE f.group_id = $p AND f.status = 'active' SET f.status = 'retired'", p=pid
        )
        seen["faulty"] = await project.snapshot()

    built = await shop.build(1, fault=fault)
    report = await built.report()
    assert (report.ok, report.rounds, report.blocked_rounds) == (False, 0, 1)
    assert report.valve["nodes"] == report.spec_nodes and report.repairs == ()
    assert await neo4j_project.snapshot() == seen["faulty"]
    assert built.result.status == CLEAN_STATUS and built.result.verification is not None
    assert (built.result.verification.ok, built.result.verification.blocked_rounds) == (False, 1)
    assert built.result.spec_changes.removed == ()


async def seed_small_project(project: Neo4jProject) -> None:
    """5 modules and 10 requirements in one active fragment, each requirement PART_OF a module: 15
    spec nodes, 10 domain edges."""

    pid = project.project_id
    await project.run(
        "CREATE (:Fragment {group_id: $p, fragment_id: 'f1', document_id: 'd1.md', ordinal: 0, status: 'active', "
        "content_hash: 'h', extracted_hash: 'h'})",
        p=pid,
    )
    nodes = [("Module", f"M{i}") for i in range(5)] + [("FunctionalRequirement", f"R{i}") for i in range(10)]
    await project.run(
        "MATCH (f:Fragment {group_id: $p, fragment_id: 'f1'}) UNWIND $rows AS row "
        "CREATE (n:SpecNode {group_id: $p, uuid: row.key, spec_id: row.spec_id, name: row.key, identity_key: 'k|' + "
        "row.key}) "
        "SET n:$(row.label) CREATE (n)-[:REFERENCED_BY {group_id: $p, line_start: 1, line_end: 1}]->(f)",
        p=pid,
        rows=[{"key": key, "label": label, "spec_id": f"{ONTOLOGY.id_prefix(label)}{key}"} for label, key in nodes],
    )
    await project.run(
        "UNWIND range(0, 9) AS i MATCH (s:SpecNode {group_id: $p, uuid: 'R' + toString(i)}), "
        "(t:SpecNode {group_id: $p, uuid: 'M' + toString(i % 5)}) "
        "CREATE (s)-[:PART_OF {group_id: $p, edge_key: 'k|' + s.uuid + '|PART_OF', uuid: 'e' + toString(i), "
        "basis: 'stated', source_fragment_ids: ['f1']}]->(t)",
        p=pid,
    )


async def small_verify(project: Neo4jProject, tmp_path: Path) -> VerificationReport:
    return await verify_build(
        project.executor,
        project.project_id,
        ONTOLOGY,
        build_id="b-small",
        registry=open_file_stores(tmp_path / "small").registry,
        target=None,
        diff=None,
    )


async def bad_part_of(project: Neo4jProject, source: str, target: str, uuid: str) -> None:
    await project.run(
        "MATCH (s:SpecNode {group_id: $p, uuid: $s}), (t:SpecNode {group_id: $p, uuid: $t}) "
        "CREATE (s)-[:PART_OF {group_id: $p, edge_key: $u, uuid: $u, basis: 'stated', source_fragment_ids: "
        "['f1']}]->(t)",
        p=project.project_id,
        s=source,
        t=target,
        u=uuid,
    )


async def test_valve_repairs_one_illegal_edge_in_a_small_project_and_blocks_two(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    await seed_small_project(neo4j_project)
    await bad_part_of(neo4j_project, "M0", "R0", "bad1")  # 11 edges: max(1, 0.55) = 1 delete allowed
    report = await small_verify(neo4j_project, tmp_path)
    assert report.found == {"illegal_edge": 1} and (report.spec_nodes, report.domain_edges) == (15, 11)
    assert (report.ok, report.rounds, report.blocked_rounds) == (True, 1, 0)
    assert [(r.check, r.action, r.subject) for r in report.repairs] == [
        ("illegal_edge", "delete_edge", "MOD::M0|PART_OF|REQ::FR::R0")
    ]
    assert await count(neo4j_project, "MATCH ()-[r {uuid: 'bad1'}]->() WHERE r.group_id = $p RETURN count(r) AS n") == 0
    await bad_part_of(neo4j_project, "M0", "R0", "bad1")
    await bad_part_of(neo4j_project, "M1", "R1", "bad2")  # 2 of 12: over max(1, 0.6)
    faulty = await neo4j_project.snapshot()
    report = await small_verify(neo4j_project, tmp_path)
    assert (report.ok, report.rounds, report.blocked_rounds) == (False, 0, 1)
    assert report.valve == {"nodes": 0, "edges": 2, "spec_nodes": 15, "domain_edges": 12}
    assert await neo4j_project.snapshot() == faulty


async def test_valve_counts_the_edges_deleted_with_a_node(neo4j_project: Neo4jProject, tmp_path: Path) -> None:
    await seed_small_project(neo4j_project)
    pid = neo4j_project.project_id
    # An ungrounded module with no evidence anywhere: one node delete (allowed), with its edges.
    await neo4j_project.run(
        "CREATE (:SpecNode:Module {group_id: $p, uuid: 'M9', spec_id: 'MOD::M9', name: 'M9', identity_key: 'k|M9'})",
        p=pid,
    )
    await bad_part_of(neo4j_project, "R0", "M9", "to-m9-a")
    await bad_part_of(neo4j_project, "R1", "M9", "to-m9-b")  # 2 edges of 12 go with it: over max(1, 0.6)
    faulty = await neo4j_project.snapshot()
    report = await small_verify(neo4j_project, tmp_path)
    assert report.found == {"ungrounded_node": 1}
    assert (report.ok, report.blocked_rounds) == (False, 1)
    assert report.valve == {"nodes": 1, "edges": 2, "spec_nodes": 16, "domain_edges": 12}
    assert await neo4j_project.snapshot() == faulty
    await neo4j_project.run("MATCH ()-[r {uuid: 'to-m9-b'}]->() WHERE r.group_id = $p DELETE r", p=pid)
    report = await small_verify(neo4j_project, tmp_path)  # one edge with the node: within the limit
    assert (report.ok, report.rounds, report.removed_spec_ids) == (True, 1, ("MOD::M9",))
    assert await count(neo4j_project, "MATCH (n:SpecNode {group_id: $p, uuid: 'M9'}) RETURN count(n) AS n") == 0
    assert (
        await count(neo4j_project, "MATCH ()-[r {uuid: 'to-m9-a'}]->() WHERE r.group_id = $p RETURN count(r) AS n") == 0
    )


# At most 2 repair rounds


async def test_at_most_two_repair_rounds_when_a_fault_comes_back(shop: Shop, neo4j_project: Neo4jProject) -> None:
    built = await shop.build(1)
    subject = await illegal_edge(neo4j_project)
    real = verification.apply_round

    async def apply_then_fault(*args: Any, **kwargs: Any) -> None:
        await real(*args, **kwargs)
        await illegal_edge(neo4j_project)  # another writer puts it back

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(verification, "apply_round", apply_then_fault)
        await steps.verify(built.ctx)
    report = await built.report()
    assert (report.ok, report.rounds, report.blocked_rounds) == (False, 2, 0)
    assert [(r.round, r.action, r.subject) for r in report.repairs] == [
        (1, "delete_edge", subject),
        (2, "delete_edge", subject),
    ]
    assert [(p.check, p.subject) for p in report.problems] == [("illegal_edge", subject)]


# FR-VER-03: foreign nodes, relationships, properties and labels are never reported or touched


async def plant_foreign(project: Neo4jProject) -> dict[str, str]:
    pid = project.project_id
    a = await first_node(project, "FunctionalRequirement")
    b = await first_node(project, "Module")
    await project.run(
        "MATCH (a:SpecNode {group_id: $p, uuid: $a}), (b:SpecNode {group_id: $p, uuid: $b}) "
        "CREATE (g:GapFinding {group_id: $p, finding_id: 'g1'})-[:FLAGS {group_id: $p}]->(a) "
        "CREATE (a)-[:SIMILAR_TO {group_id: $p, score: 0.5}]->(b) "
        "CREATE (b)-[:HOSTED_ON]->(:HostThing {group_id: $p, name: 'vm-1'}) "
        "SET a.reviewed_by = 'someone', a:Reviewed, b.host_score = 3, b:Pinned",
        p=pid,
        a=a["uuid"],
        b=b["uuid"],
    )
    stated = await one(
        project,
        "MATCH (s:SpecNode {uuid: $a})-[r]->(:SpecNode) WHERE r.group_id = $p AND r.basis = 'stated' "
        "WITH r ORDER BY r.edge_key LIMIT 1 SET r.host_note = 'kept' RETURN r.uuid AS uuid",
        a=a["uuid"],
    )
    return {"a": a["uuid"], "b": b["uuid"], "edge": stated["uuid"]}


async def test_foreign_data_is_never_reported_or_touched_even_while_repairing(
    shop: Shop, neo4j_project: Neo4jProject
) -> None:
    built = await shop.build(1)
    pid = shop.project_id
    foreign = await plant_foreign(neo4j_project)
    with_foreign = await neo4j_project.snapshot()
    checked = await verify_project(built.ctx)
    assert checked.ok and checked.found == {} and checked.problems == ()
    await steps.verify(built.ctx)
    report = await built.report()
    assert report.ok and report.found == {} and report.repairs == ()
    assert await neo4j_project.snapshot() == with_foreign

    # Faults on the very nodes and edge that carry foreign data: repaired, the foreign data stays.
    await neo4j_project.run("MATCH (n:SpecNode {group_id: $p, uuid: $u}) SET n:Feature", p=pid, u=foreign["a"])
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: $u})-[r:REFERENCED_BY]->() DELETE r", p=pid, u=foreign["b"]
    )
    await neo4j_project.run(
        "MATCH ()-[r {uuid: $u}]->() WHERE r.group_id = $p SET r.source_fragment_ids = []", p=pid, u=foreign["edge"]
    )
    await illegal_edge(neo4j_project)
    await steps.verify(built.ctx)
    report = await built.report()
    assert report.found == {"node_label": 1, "ungrounded_node": 1, "ungrounded_edge": 1, "illegal_edge": 1}
    assert report.ok and report.rounds == 1
    assert await neo4j_project.snapshot() == with_foreign
    reported = json.dumps(report.model_dump(mode="json"))
    assert not [
        word
        for word in ("GapFinding", "SIMILAR_TO", "HOSTED_ON", "Reviewed", "Pinned", "host_", "g1")
        if word in reported
    ]


# FR-VER-04: verify_project reports problems and changes nothing


async def test_verify_project_reports_problems_and_changes_nothing(shop: Shop, neo4j_project: Neo4jProject) -> None:
    built = await shop.build(1)
    pid = shop.project_id
    handles = await built.handles()
    await illegal_edge(neo4j_project)
    victim = await first_node(neo4j_project, "FunctionalRequirement", skip=1)  # not an end of the illegal edge
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: $u}) SET n.spec_id = 'bad id', n:Feature", p=pid, u=victim["uuid"]
    )
    await neo4j_project.run(
        "MATCH (n:Module) WHERE n.group_id = $p WITH n ORDER BY n.spec_id LIMIT 1 MATCH (n)-[r:REFERENCED_BY]->() "
        "DELETE r",
        p=pid,
    )
    await neo4j_project.run(
        "MATCH (f:Fragment {group_id: $p, fragment_id: $f}) SET f.extracted_hash = null", p=pid, f=handles[OBLIGATIONS]
    )
    await neo4j_project.run(
        "MATCH (f:Fragment {group_id: $p, fragment_id: $f}) SET f.status = 'weird'", p=pid, f=handles[DETAILS3]
    )
    snapshot = await neo4j_project.snapshot()
    registry = await shop.stores.registry.taken(pid)
    stored = await built.report()
    latest = await shop.stores.builds.latest_build(pid)
    # Bound to the build, or not (then the latest finished build gives the dropped edges).
    for ctx in (built.ctx, dataclasses.replace(built.ctx, build_id=None)):
        report = await verify_project(ctx)
        assert report.found == {
            "node_label": 1,
            "spec_id": 1,
            "illegal_edge": 1,
            "ungrounded_node": 1,
            "fragment_state": 2,
        }
        assert not report.ok and report.read_only
        assert (report.rounds, report.blocked_rounds, report.repairs, report.follow_up_lineages) == (0, 0, (), ())
        assert {(p.check, p.code) for p in report.problems if p.check == "fragment_state"} == {
            ("fragment_state", "not_extracted"),
            ("fragment_state", "unknown_status"),
        }
        assert report.metrics.dropped_edges == DROPPED
        assert await neo4j_project.snapshot() == snapshot  # byte-identical graph
        assert await shop.stores.registry.taken(pid) == registry
        assert await built.report() == stored
        assert await shop.stores.builds.latest_build(pid) == latest
    await assert_spec_id_forms(neo4j_project, report, known={"bad id"})


# Metrics, checked against Cypher counts


async def test_metrics_match_independent_counts(shop: Shop, neo4j_project: Neo4jProject) -> None:
    built = await shop.build(1)
    pid = shop.project_id
    metrics = (await built.report()).metrics
    lonely = (
        "MATCH (f:Fragment) WHERE f.group_id = $p AND f.status = 'active' "
        "AND NOT EXISTS { MATCH (n:SpecNode)-[:REFERENCED_BY]->(f) WHERE n.group_id = $p } RETURN count(f) AS n"
    )
    assert metrics.fragments_without_nodes == await count(neo4j_project, lonely) > 0
    assert (
        metrics.spine_unreachable
        == metrics.spine_unreachable_before
        == await spine_unreachable_count(neo4j_project, pid)
    )
    assert metrics.exclusive_conflicts == 0
    by_reason = built.result.metrics.dropped_edges
    assert metrics.dropped_edges == DROPPED and sum(by_reason.values()) == sum(DROPPED.values())
    assert all(key.startswith("RELATES_TO:") for key in by_reason)

    # Two extra features for a requirement that has one: two exclusive conflicts (every extra target).
    owner = await one(
        neo4j_project,
        "MATCH (r:FunctionalRequirement)-[:PART_OF]->(f:Feature) WHERE r.group_id = $p "
        "WITH r, count(f) AS features WHERE features = 1 WITH r ORDER BY r.spec_id LIMIT 1 "
        "MATCH (r)-[:REFERENCED_BY]->(g:Fragment) WITH r, g ORDER BY g.fragment_id LIMIT 1 "
        "MATCH (f:Feature) WHERE f.group_id = $p AND NOT (r)-[:PART_OF]->(f) WITH r, g, f ORDER BY f.spec_id LIMIT 2 "
        "CREATE (r)-[:PART_OF {group_id: $p, edge_key: 'fault|x|' + f.spec_id, uuid: 'fault-x-' + f.spec_id, "
        "basis: 'stated', source_fragment_ids: [g.fragment_id]}]->(f) RETURN count(*) AS n",
    )
    assert owner["n"] == 2
    await neo4j_project.run(
        "CREATE (:Fragment {group_id: $p, fragment_id: 'extra.md#lonely', document_id: 'extra.md', status: 'active', "
        "content_hash: 'h', extracted_hash: 'h'})",
        p=pid,
    )
    report = await verify_project(built.ctx)
    assert report.ok, report.problems  # metrics are reported, never blocking
    assert report.metrics.exclusive_conflicts == 2
    assert report.metrics.fragments_without_nodes == metrics.fragments_without_nodes + 1
