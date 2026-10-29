"""Live e2e of the graph builder with the REAL model (LLM gateway) and the real Neo4j (tech-specs 9.2).

Builds shop 1, updates to 2, updates to 3 and runs two no-change updates, wiring the library like
``examples/graph_build.py`` (host-style configs from the environment). Only what is stable with a
real model is asserted; calls and tokens per build are printed, asserted only as the update/first
ratio of snapshot 2 and 0 calls for a no-change update. Gate: ``SPECFLOW_TEST_NEO4J=1`` and
``SPECFLOW_TEST_LLM_GATEWAY=1`` with
``uv run --env-file ../.env.local`` (never part of ``make check``). It costs real tokens and minutes.

One scratch corpus folder whose contents are replaced between builds, file stores under ``tmp_path``,
a unique ``pytest-graph-`` project id; ``neo4j_project`` deletes the project by ``group_id`` afterwards.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from graph.build import (
    BuildContext,
    BuildResult,
    FileStores,
    open_file_stores,
    run_build,
)
from graph.build.models import DiffArtifact, NodesArtifact, PlanArtifact, SpecChanges
from graph.core import Gateway, LLMConfig
from graph_support import SHOP_DIR, Neo4jProject
from specflow_live import requires_live

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

LLM_ENVIRONMENT = (
    "LLM_GATEWAY_URL",
    "LLM_GATEWAY_API_KEY",
    "GRAPH_LLM_MODEL",
    "GRAPH_SMALL_MODEL",
    "GRAPH_EMBEDDING_MODEL",
    "GRAPH_EMBEDDING_DIM",
)


def missing_llm_environment() -> list[str]:
    return [name for name in LLM_ENVIRONMENT if not os.environ.get(name, "").strip()]


pytestmark = [
    requires_live("NEO4J"),
    requires_live("LLM_GATEWAY"),
    pytest.mark.skipif(
        bool(missing_llm_environment()),
        reason="missing environment variables: " + ", ".join(missing_llm_environment()),
    ),
]
OLD_NAME = "Orders service"
NEW_NAME = "Order management service"
SETTLED_STATUSES = ("succeeded", "partially_succeeded")
# Nodes grounded only in unchanged fragments keep their spec_id in units reused as stored (no model
# call) almost always. In units re-extracted anyway the model may cut the text differently, so their
# retention is reported, never asserted.
RETENTION_FLOORS: dict[str, float | None] = {"reused": 0.95, "reextracted": None}
UPDATE_TO_FIRST_CALLS = 0.5


def show(text: str) -> None:
    """Show a progress line of the live run on stdout (pytest -s)."""

    sys.stdout.write(text + "\n")


def real_llm_config() -> LLMConfig:
    env = {name: os.environ[name].strip() for name in LLM_ENVIRONMENT}
    return LLMConfig(
        endpoint=env["LLM_GATEWAY_URL"],
        api_key=SecretStr(env["LLM_GATEWAY_API_KEY"]),
        main_model=env["GRAPH_LLM_MODEL"],
        small_model=env["GRAPH_SMALL_MODEL"],
        embedding_model=env["GRAPH_EMBEDDING_MODEL"],
        embedding_dim=int(env["GRAPH_EMBEDDING_DIM"]),
    )


def squash(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True)
class Usage:
    calls: int
    input_tokens: int
    output_tokens: int

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def usage_of(result: BuildResult) -> Usage:
    counts = list(result.metrics.llm.values())
    return Usage(
        calls=sum(c.calls for c in counts),
        input_tokens=sum(c.input_tokens for c in counts),
        output_tokens=sum(c.output_tokens for c in counts),
    )


@dataclass(frozen=True)
class Snapshot:
    """The spec nodes (by ``spec_id``) and fragments of the project after one build."""

    # spec_id -> uuid, name, labels, evidence fragment ids
    nodes: dict[str, dict[str, Any]]
    fragments: dict[str, dict[str, Any]]  # fragment_id -> document_id, status, text

    def active_fragments(self) -> set[str]:
        return {fid for fid, row in self.fragments.items() if row["status"] == "active"}


def unchanged_fragments(before: Snapshot, after: Snapshot) -> set[str]:
    """Fragments active with the same id in both snapshots and the same text (whitespace squashed)."""

    return {
        fid
        for fid in before.active_fragments() & after.active_fragments()
        if squash(before.fragments[fid]["text"] or "") == squash(after.fragments[fid]["text"] or "")
    }


@dataclass
class Built:
    result: BuildResult
    usage: Usage
    snapshot: Snapshot
    seconds: float
    plan: PlanArtifact
    nodes_artifact: NodesArtifact
    diff_operations: int | None = None


def load_corpus(folder: Path, snapshot: int) -> None:
    """Replace the contents of the scratch corpus folder with ``shop/<snapshot>/``."""

    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(SHOP_DIR / str(snapshot), folder)


async def take_snapshot(project: Neo4jProject) -> Snapshot:
    p = project.project_id
    node_rows = await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p "
        "OPTIONAL MATCH (n)-[:REFERENCED_BY]->(f:Fragment) WHERE f.group_id = $p "
        "RETURN n.spec_id AS spec_id, n.uuid AS uuid, n.name AS name, [l IN labels(n) WHERE l <> 'SpecNode'] AS "
        "labels, "
        "collect(f.fragment_id) AS fragments",
        p=p,
    )
    fragment_rows = await project.run(
        "MATCH (f:Fragment) WHERE f.group_id = $p "
        "RETURN f.fragment_id AS id, f.document_id AS document_id, f.status AS status, f.text AS text",
        p=p,
    )
    return Snapshot(
        nodes={
            row["spec_id"]: {
                "uuid": row["uuid"],
                "name": row["name"],
                "labels": row["labels"],
                "fragments": set(row["fragments"]),
            }
            for row in node_rows
        },
        fragments={
            row["id"]: {
                "document_id": row["document_id"],
                "status": row["status"],
                "text": row["text"],
            }
            for row in fragment_rows
        },
    )


class RunLog:
    """One small JSON file per build in a temp dir (no document text); failures are written before raising."""

    def __init__(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="graph-real-live-"))
        self.builds: dict[str, dict[str, Any]] = {}

    def record(self, build: str, **data: Any) -> None:
        self.builds.setdefault(build, {"build": build, "failures": []}).update(data)
        self.flush(build)

    def flush(self, build: str) -> None:
        path = self.directory / f"{build}.json"
        path.write_text(
            json.dumps(self.builds[build], indent=2, sort_keys=True, default=sorted),
            encoding="utf-8",
        )

    def ensure(self, build: str, message: str, *, condition: bool) -> None:
        """Assert with a message naming the build, the measured value and the threshold."""

        if condition:
            return
        message = f"[{build}] {message}"
        self.builds.setdefault(build, {"build": build, "failures": []})["failures"].append(message)
        self.flush(build)
        raise AssertionError(message)


async def build_once(
    project: Neo4jProject,
    stores: FileStores,
    llm: LLMConfig,
    expected_mode: str,
    name: str,
    *,
    log: RunLog,
) -> Built:
    assert stores.corpus is not None
    gateway = Gateway(llm)
    started = time.monotonic()
    try:
        ctx = BuildContext(
            project_id=project.project_id,
            ontology=project.ontology,
            driver=project.driver,
            gateway=gateway,
            registry=stores.registry,
            corpus=stores.corpus,
            extraction=stores.extraction,
            builds=stores.builds,
            database=project.database,
        )
        result = await run_build(ctx)
    finally:
        await gateway.aclose()
    seconds = time.monotonic() - started
    usage = usage_of(result)
    log.record(
        name,
        mode=result.mode,
        status=result.status,
        settled=result.settled,
        failed_step=result.failed_step,
        error_code=result.error_code,
        calls=usage.calls,
        tokens_in=usage.input_tokens,
        tokens_out=usage.output_tokens,
        units_extracted=result.metrics.units_extracted,
        units_reused=result.metrics.units_reused,
        skipped_fragments=result.skipped_fragments,
        spec_changes={k: len(v) for k, v in result.spec_changes.model_dump().items()},
        seconds=round(seconds, 1),
    )
    log.ensure(
        name,
        f"mode {result.mode!r} != expected {expected_mode!r}",
        condition=result.mode == expected_mode,
    )
    log.ensure(
        name,
        f"status {result.status!r} not in {SETTLED_STATUSES} (failed_step={result.failed_step}, "
        f"error_code={result.error_code})",
        condition=result.status in SETTLED_STATUSES,
    )
    log.ensure(name, "build not settled", condition=result.settled)
    plan = PlanArtifact.model_validate(await stores.builds.get_artifact(project.project_id, result.build_id, "plan"))
    nodes = NodesArtifact.model_validate(
        await stores.builds.get_artifact(project.project_id, result.build_id, "extract_nodes")
    )
    diff_payload = await stores.builds.get_artifact(project.project_id, result.build_id, "diff")
    diff_operations = None if diff_payload is None else len(DiffArtifact.model_validate(diff_payload).operations)
    log.record(name, diff_operations=diff_operations)
    snapshot = await take_snapshot(project)
    log.record(
        name,
        nodes=len(snapshot.nodes),
        fragments_by_status=dict(Counter(f["status"] for f in snapshot.fragments.values())),
    )
    await check_invariants(project, name, log)
    await check_provenance(project, name, log)
    return Built(result, usage, snapshot, seconds, plan, nodes, diff_operations)


# D1-D4 (data-model.md) by Cypher


async def check_invariants(project: Neo4jProject, name: str, log: RunLog) -> None:
    p = project.project_id
    domain_labels = set(project.ontology.labels)

    # D1: SpecNode plus exactly one domain label
    nodes = await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p RETURN n.spec_id AS spec_id, labels(n) AS labels",
        p=p,
    )
    d1 = sorted(
        row["spec_id"]
        for row in nodes
        if not (len(set(row["labels"]) - {"SpecNode"}) == 1 and set(row["labels"]) - {"SpecNode"} <= domain_labels)
    )

    # D2: every domain edge has a declared type on a legal pair
    legal = project.ontology.edge_type_map()
    edges = await project.run(
        "MATCH (s:SpecNode)-[r]->(t:SpecNode) WHERE s.group_id = $p "
        "RETURN type(r) AS relation, [l IN labels(s) WHERE l <> 'SpecNode'][0] AS source, "
        "[l IN labels(t) WHERE l <> 'SpecNode'][0] AS target, r.group_id AS group_id, r.uuid AS uuid, "
        "r.edge_key AS edge_key, r.basis AS basis, r.source_fragment_ids AS fragments",
        p=p,
    )
    d2 = sorted(
        row["edge_key"]
        for row in edges
        if row["relation"] not in legal.get((row["source"], row["target"]), ()) or row["group_id"] != p
    )
    edge_keys = Counter(row["edge_key"] for row in edges)
    edge_uuids = Counter(row["uuid"] for row in edges)

    # D3: spec_id, uuid and identity key unique per project
    counts = (
        await project.run(
            "MATCH (n:SpecNode) WHERE n.group_id = $p "
            "RETURN count(n) AS nodes, count(DISTINCT n.spec_id) AS spec_ids, count(DISTINCT n.uuid) AS uuids, "
            "count(DISTINCT n.identity_key) AS identity_keys",
            p=p,
        )
    )[0]

    # D4: every spec node grounded in an active or skipped fragment; every stated or implied edge has
    # evidence fragments that exist in the project
    ungrounded = sorted(
        row["spec_id"]
        for row in await project.run(
            "MATCH (n:SpecNode) WHERE n.group_id = $p AND NOT EXISTS { "
            "MATCH (n)-[:REFERENCED_BY]->(f:Fragment) WHERE f.group_id = $p AND f.status IN ['active', 'skipped'] } "
            "RETURN n.spec_id AS spec_id",
            p=p,
        )
    )
    fragment_ids = {
        row["id"]
        for row in await project.run("MATCH (f:Fragment) WHERE f.group_id = $p RETURN f.fragment_id AS id", p=p)
    }
    no_evidence = sorted(
        row["edge_key"]
        for row in edges
        if row["basis"] in ("stated", "implied") and not (row["fragments"] and set(row["fragments"]) <= fragment_ids)
    )
    log.record(
        name,
        d1_spec_nodes=len(nodes),
        d1_violations=d1,
        d2_edges=len(edges),
        d2_violations=d2,
        d3_counts=dict(counts),
        d3_duplicate_edge_keys=sorted(k for k, c in edge_keys.items() if c > 1),
        d3_duplicate_edge_uuids=sorted(k for k, c in edge_uuids.items() if c > 1),
        d4_ungrounded_nodes=ungrounded,
        d4_edges_without_evidence=no_evidence,
    )
    log.ensure(name, "D1: the build produced 0 spec nodes", condition=bool(nodes))
    log.ensure(
        name,
        f"D1: {len(d1)} nodes without exactly one domain label (limit 0): {d1[:5]}",
        condition=not d1,
    )
    log.ensure(
        name,
        f"D2: {len(d2)} edges on an illegal pair or foreign group (limit 0): {d2[:5]}",
        condition=not d2,
    )
    n = counts["nodes"]
    log.ensure(
        name,
        f"D3: node/spec_id/uuid/identity_key counts differ (must all be equal): {dict(counts)}",
        condition=dict(counts) == {"nodes": n, "spec_ids": n, "uuids": n, "identity_keys": n},
    )
    log.ensure(
        name,
        f"D3: {len(edges)} edges but {len(edge_keys)} distinct edge_keys and {len(edge_uuids)} distinct uuids",
        condition=len(edge_keys) == len(edges) and len(edge_uuids) == len(edges),
    )
    log.ensure(
        name,
        f"D4: {len(ungrounded)} nodes not grounded in an active/skipped fragment (limit 0): {ungrounded[:5]}",
        condition=not ungrounded,
    )
    log.ensure(
        name,
        f"D4: {len(no_evidence)} stated/implied edges without existing evidence fragments (limit 0): {no_evidence[:5]}",
        condition=not no_evidence,
    )


async def check_provenance(project: Neo4jProject, name: str, log: RunLog) -> None:
    """Every node quote and edge fact is a substring (whitespace squashed) of its evidence fragment text."""

    p = project.project_id
    quotes = await project.run(
        "MATCH (n:SpecNode)-[:REFERENCED_BY]->(f:Fragment) WHERE n.group_id = $p AND n.quote IS NOT NULL "
        "RETURN n.spec_id AS spec_id, n.quote AS quote, collect(f.text) AS texts, collect(f.fragment_id) AS fragments",
        p=p,
    )
    bad_quotes = sorted(
        (row["spec_id"], row["fragments"])
        for row in quotes
        if not any(squash(row["quote"]) in squash(text) for text in row["texts"])
    )
    facts = await project.run(
        "MATCH (s:SpecNode)-[r]->(:SpecNode) WHERE s.group_id = $p AND r.fact IS NOT NULL "
        "MATCH (f:Fragment) WHERE f.group_id = $p AND f.fragment_id IN r.source_fragment_ids "
        "RETURN r.edge_key AS edge_key, r.fact AS fact, collect(f.text) AS texts, collect(f.fragment_id) AS fragments",
        p=p,
    )
    bad_facts = sorted(
        (row["edge_key"], row["fragments"])
        for row in facts
        if not any(squash(row["fact"]) in squash(text) for text in row["texts"])
    )
    log.record(
        name,
        quotes_checked=len(quotes),
        quotes_not_in_evidence=bad_quotes,
        facts_checked=len(facts),
        facts_not_in_evidence=bad_facts,
    )
    log.ensure(name, "provenance: no node quote found to check (expected > 0)", condition=bool(quotes))
    log.ensure(name, "provenance: no edge fact found to check (expected > 0)", condition=bool(facts))
    log.ensure(
        name,
        f"provenance: {len(bad_quotes)}/{len(quotes)} node quotes not in their evidence fragment text (limit 0): "
        f"{bad_quotes[:5]}",
        condition=not bad_quotes,
    )
    log.ensure(
        name,
        f"provenance: {len(bad_facts)}/{len(facts)} edge facts not in their evidence fragment text (limit 0): "
        f"{bad_facts[:5]}",
        condition=not bad_facts,
    )


# Update checks


def retention(before: Snapshot, after: Snapshot, built: Built) -> dict[str, tuple[list[str], list[str]]]:
    """Per class, (spec_ids of ``before`` grounded only in unchanged fragments (same id, active and
    the same text in both snapshots), the ones lost). ``reused``: every evidence fragment is in a unit not
    re-extracted in
    ``built``; ``reextracted``: some evidence fragment is in a re-extracted unit (model variance)."""

    unchanged = unchanged_fragments(before, after)
    reextracted_units = {raw.unit_id for raw in built.nodes_artifact.raw_nodes if not raw.reused}
    reextracted_fragments = {
        fid for unit in built.plan.units if unit.unit_id in reextracted_units for fid in unit.fragment_ids
    }
    groups: dict[str, list[str]] = {"reused": [], "reextracted": []}
    for sid, node in before.nodes.items():
        if node["fragments"] and node["fragments"] <= unchanged:
            kind = "reextracted" if node["fragments"] & reextracted_fragments else "reused"
            groups[kind].append(sid)
    return {kind: (sids, [sid for sid in sids if sid not in after.nodes]) for kind, sids in groups.items()}


def extracted_documents(built: Built) -> set[str]:
    """Documents of the units whose nodes were extracted (not reused) in this build."""

    unit_documents = {unit.unit_id: unit.document_id for unit in built.plan.units}
    return {unit_documents[raw.unit_id] for raw in built.nodes_artifact.raw_nodes if not raw.reused}


def module_spec_ids(snapshot: Snapshot, name: str) -> list[str]:
    return [
        sid
        for sid, node in snapshot.nodes.items()
        if "Module" in node["labels"] and squash(node["name"]).casefold() == name.casefold()
    ]


def check_retention(
    log: RunLog, name: str, before: Snapshot, after: Snapshot, built: Built, *, require_stable: bool = True
) -> None:
    """``require_stable`` False: the snapshot may leave no node grounded only in unchanged fragments
    (shop 3 renames a module in every spec document; only the chatter of ``notes.md`` stays)."""

    groups = retention(before, after, built)
    log.record(name, retention_floors=RETENTION_FLOORS)
    if require_stable:
        log.ensure(
            name,
            "retention: 0 nodes grounded only in unchanged fragments (expected > 0)",
            condition=any(sids for sids, _ in groups.values()),
        )
    for kind, (stable, lost) in groups.items():
        kept = len(stable) - len(lost)
        floor = RETENTION_FLOORS[kind]
        ratio = f"{kept / len(stable):.1%}" if stable else "n/a (no such nodes)"
        bound = "report only" if floor is None else f"floor {floor:.0%}"
        show(f"[{name}] spec_id retention, {kind} units: {kept}/{len(stable)} = {ratio} ({bound})")
        documents = {
            sid: sorted(
                {before.fragments[f]["document_id"] for f in before.nodes[sid]["fragments"] if f in before.fragments}
            )
            for sid in lost
        }
        log.record(
            name,
            **{
                f"retention_{kind}_kept": kept,
                f"retention_{kind}_total": len(stable),
                f"retention_{kind}_lost": sorted(lost),
                f"retention_{kind}_lost_documents": documents,
            },
        )
        if stable and floor is not None:
            log.ensure(
                name,
                f"retention in {kind} units {kept}/{len(stable)} = {kept / len(stable):.1%} < {floor:.0%}; "
                f"lost spec_ids: {sorted(lost)[:8]}",
                condition=kept >= floor * len(stable),
            )


def report(label: str, built: Built, first: Built) -> None:
    ratio_calls = built.usage.calls / first.usage.calls
    ratio_tokens = built.usage.tokens / first.usage.tokens
    changes = {k: len(v) for k, v in built.result.spec_changes.model_dump().items() if v}
    show(
        f"\n[{label}] mode={built.result.mode} status={built.result.status} "
        f"calls={built.usage.calls} tokens_in={built.usage.input_tokens} tokens_out={built.usage.output_tokens} "
        f"vs_first calls={ratio_calls:.1%} tokens={ratio_tokens:.1%} "
        f"units extracted/reused={built.result.metrics.units_extracted}/{built.result.metrics.units_reused} "
        f"nodes={len(built.snapshot.nodes)} spec_changes={changes} time={built.seconds:.0f}s"
    )


def check_no_change(
    log: RunLog,
    name: str,
    built: Built,
    previous: Built,
    *,
    max_calls: int,
    require_no_diff: bool,
) -> None:
    """A no-change update: at most ``max_calls`` model calls (0 output tokens), empty ``spec_changes``,
    the same node set; with ``require_no_diff`` also 0 diff operations."""

    changes = {k: sorted(v) for k, v in built.result.spec_changes.model_dump().items() if v}
    gained = sorted(set(built.snapshot.nodes) - set(previous.snapshot.nodes))
    lost = sorted(set(previous.snapshot.nodes) - set(built.snapshot.nodes))
    log.record(
        name,
        nonempty_spec_changes=changes,
        max_calls=max_calls,
        nodes_gained=gained,
        nodes_lost=lost,
    )
    show(
        f"[{name}] calls={built.usage.calls} tokens_in={built.usage.input_tokens} "
        f"tokens_out={built.usage.output_tokens} diff_operations={built.diff_operations} "
        f"spec_changes={ {k: len(v) for k, v in changes.items()} }"
    )
    log.ensure(
        name,
        f"calls {built.usage.calls} > {max_calls}",
        condition=built.usage.calls <= max_calls,
    )
    log.ensure(
        name,
        f"output tokens {built.usage.output_tokens} != 0 (only embedding calls allowed)",
        condition=built.usage.output_tokens == 0,
    )
    log.ensure(
        name,
        f"spec_changes not empty: { {k: len(v) for k, v in changes.items()} } ids {changes}",
        condition=built.result.spec_changes == SpecChanges(),
    )
    log.ensure(
        name,
        f"node set changed: +{gained[:5]} -{lost[:5]}",
        condition=not gained and not lost,
    )
    if require_no_diff:
        log.ensure(
            name,
            f"diff operations {built.diff_operations} != 0",
            condition=built.diff_operations == 0,
        )


async def test_real_model_builds_and_updates_the_shop_corpus(neo4j_project: Neo4jProject, tmp_path: Path) -> None:
    llm = real_llm_config()
    log = RunLog()
    try:
        await scenario(neo4j_project, tmp_path, llm, log)
    finally:
        show(f"\n[run log] {log.directory}")


async def check_admin_removed(log: RunLog, name: str, neo4j_project: Neo4jProject, third: Built) -> None:
    """After update 3 no node is grounded in the removed admin.md and none of its fragments is live."""

    admin_grounded = [
        row["spec_id"]
        for row in await neo4j_project.run(
            "MATCH (n:SpecNode)-[:REFERENCED_BY]->(f:Fragment) "
            "WHERE n.group_id = $p AND f.document_id = 'admin.md' RETURN n.spec_id AS spec_id",
            p=neo4j_project.project_id,
        )
    ]
    log.record(name, admin_grounded_nodes=sorted(admin_grounded))
    log.ensure(
        name,
        f"{len(admin_grounded)} nodes still grounded in admin.md (limit 0): {sorted(admin_grounded)[:5]}",
        condition=not admin_grounded,
    )
    admin_live = [
        fid
        for fid, row in third.snapshot.fragments.items()
        if row["document_id"] == "admin.md" and row["status"] != "retired"
    ]
    log.record(name, admin_non_retired_fragments=admin_live)
    log.ensure(
        name,
        f"{len(admin_live)} admin.md fragments not retired (limit 0): {admin_live[:5]}",
        condition=not admin_live,
    )


def check_rename(log: RunLog, name: str, snapshot: Snapshot, orders_spec_id: str, orders_uuid: str) -> None:
    """The rename of the orders module in update 3: it keeps spec_id and uuid and stays one node."""

    renamed = snapshot.nodes.get(orders_spec_id)
    lineage = sorted(
        set(module_spec_ids(snapshot, OLD_NAME))
        | set(module_spec_ids(snapshot, NEW_NAME))
        | ({orders_spec_id} if renamed is not None else set())
    )
    log.record(
        name,
        orders_module_spec_id=orders_spec_id,
        orders_module_name_after=renamed and renamed["name"],
        orders_module_lineage=lineage,
    )
    log.ensure(
        name,
        f"module {orders_spec_id} is gone (expected same spec_id)",
        condition=renamed is not None,
    )
    assert renamed is not None
    log.ensure(
        name,
        f"module {orders_spec_id} uuid {renamed['uuid']!r} != {orders_uuid!r} after build 1",
        condition=renamed["uuid"] == orders_uuid,
    )
    log.ensure(
        name,
        f"node {orders_spec_id} labels {renamed['labels']} no longer include Module",
        condition="Module" in renamed["labels"],
    )
    log.ensure(
        name,
        f"{len(lineage)} Module nodes named {OLD_NAME!r}/{NEW_NAME!r} or with spec_id "
        f"{orders_spec_id} (expected exactly 1): {lineage}",
        condition=lineage == [orders_spec_id],
    )
    after_name = squash(renamed["name"])
    log.ensure(
        name,
        f"module {orders_spec_id} is named {renamed['name']!r}, expected {OLD_NAME!r} or {NEW_NAME!r}",
        condition=after_name.casefold() in {OLD_NAME.casefold(), NEW_NAME.casefold()},
    )
    renamed_to_new = after_name.casefold() == NEW_NAME.casefold()
    log.record(name, orders_module_renamed=renamed_to_new)
    show(
        f"[{name}] module {orders_spec_id} name: {after_name!r} "
        f"({'renamed' if renamed_to_new else 'kept the old name'})"
    )


async def scenario(neo4j_project: Neo4jProject, tmp_path: Path, llm: LLMConfig, log: RunLog) -> None:
    corpus = tmp_path / "corpus"
    stores = open_file_stores(tmp_path / "state", corpus)

    # Build 1: first, shop/1
    load_corpus(corpus, 1)
    first = await build_once(neo4j_project, stores, llm, "first", "1-first", log=log)
    report("1 first", first, first)
    log.ensure("1-first", f"calls {first.usage.calls} not > 0", condition=first.usage.calls > 0)
    found = module_spec_ids(first.snapshot, OLD_NAME)
    log.record("1-first", orders_module_spec_ids=found)
    log.ensure(
        "1-first",
        f"{len(found)} Module nodes named {OLD_NAME!r} (expected 1): "
        f"{sorted(first.snapshot.nodes[s]['name'] for s in found)}; modules: "
        f"{sorted(n['name'] for n in first.snapshot.nodes.values() if 'Module' in n['labels'])}",
        condition=len(found) == 1,
    )
    orders_spec_id = found[0]
    orders_uuid = first.snapshot.nodes[orders_spec_id]["uuid"]

    # Update 2: shop/2 (only checkout.md changes)
    name = "2-update"
    load_corpus(corpus, 2)
    second = await build_once(neo4j_project, stores, llm, "update", name, log=log)
    report("2 update", second, first)
    ratio = second.usage.calls / first.usage.calls
    show(f"[{name}] update/first calls ratio = {ratio:.1%}")
    log.record(name, calls_ratio=round(ratio, 3))
    log.ensure(
        name,
        f"calls {second.usage.calls}/{first.usage.calls} = {ratio:.1%} > {UPDATE_TO_FIRST_CALLS:.0%}",
        condition=second.usage.calls <= UPDATE_TO_FIRST_CALLS * first.usage.calls,
    )
    documents = extracted_documents(second)
    log.record(name, extracted_documents=sorted(documents))
    log.ensure(
        name,
        f"re-extracted documents {sorted(documents)} != ['checkout.md']",
        condition=documents == {"checkout.md"},
    )
    log.ensure(
        name,
        f"units_reused {second.result.metrics.units_reused} < 1",
        condition=second.result.metrics.units_reused >= 1,
    )
    chk_007 = [
        sid
        for sid, node in second.snapshot.nodes.items()
        if re.search(r"FR[_-]CHK[_-]007", sid, re.IGNORECASE)
        or re.search(r"FR[_-]CHK[_-]007", node["name"], re.IGNORECASE)
    ]
    log.record(name, fr_chk_007=chk_007)
    log.ensure(
        name,
        "FR-CHK-007 not found among spec_ids or names (expected >= 1)",
        condition=bool(chk_007),
    )
    check_retention(log, name, first.snapshot, second.snapshot, second)

    # Update 3: shop/3 (admin.md removed, "Orders service" renamed)
    name = "3-update"
    load_corpus(corpus, 3)
    third = await build_once(neo4j_project, stores, llm, "update", name, log=log)
    report("3 update", third, first)
    ratio = third.usage.calls / first.usage.calls
    show(f"[{name}] update/first calls ratio = {ratio:.1%}")
    log.record(name, calls_ratio=round(ratio, 3))
    await check_admin_removed(log, name, neo4j_project, third)
    # The rename: only the identity is stable with a real model. The module keeps spec_id and uuid
    # and stays one node; whether its name follows the text (FR-BLD-23 known terms map the new
    # wording onto the old name) is model behaviour, printed, never asserted.
    check_rename(log, name, third.snapshot, orders_spec_id, orders_uuid)
    check_retention(log, name, second.snapshot, third.snapshot, third, require_stable=False)

    # Updates 4 and 5: no change. Both make 0 model calls and have empty spec_changes; the second
    # is fully idle (0 diff operations).
    name = "4-no-change"
    fourth = await build_once(neo4j_project, stores, llm, "update", name, log=log)
    report("4 no-change", fourth, first)
    check_no_change(log, name, fourth, third, max_calls=0, require_no_diff=False)
    name = "5-no-change"
    fifth = await build_once(neo4j_project, stores, llm, "update", name, log=log)
    report("5 no-change", fifth, first)
    check_no_change(log, name, fifth, fourth, max_calls=0, require_no_diff=True)
    builds = (first, second, third, fourth, fifth)
    show(
        f"[summary] calls first/2/3/4/5 = {'/'.join(str(b.usage.calls) for b in builds)} "
        f"tokens = {'/'.join(str(b.usage.tokens) for b in builds)} "
        f"total time = {sum(b.seconds for b in builds):.0f}s"
    )
