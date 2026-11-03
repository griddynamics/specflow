"""Live Neo4j check that an update refuses a graph whose ontology labels changed."""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import pytest_asyncio

from graph.build import BuildResult, run_build
from graph.core import ConfigurationError
from graph_support import (
    Neo4jProject,
    ScriptedGateway,
    ShopBuilds,
    assert_all_scripted,
    frozen_ontology,
    live_llm_config,
    open_neo4j_project,
    run_shop_build,
    shipped_ontology,
)
from graph_v2_support import ShopV2Builds
from specflow_live import requires_live

pytestmark = [requires_live("NEO4J"), pytest.mark.asyncio(loop_scope="module")]

ONTOLOGY: Final = shipped_ontology()
# Properties that differ between two builds of the same graph by construction.
VOLATILE: Final = frozenset(
    {
        "uuid",
        "group_id",
        "created_at",
        "updated_at",
        "inferred_at",
        "created_build_id",
        "last_build_id",
        "embedding",
        "name_embedding",
        "version",
    }
)
# When a statement was first made: a statement a continuing node already made keeps them.
STATEMENT_STAMPS: Final = frozenset({"revision_id", "version", "at"})
# The requirement relabeled in the probe: v2.0 types it a BusinessRule; the probe leaves it as a
# FunctionalRequirement, as a v1.3 type would have.
RELABELED: Final = "Order: at most one coupon"
DROP_BATCH: Final = 2000


@dataclass(frozen=True)
class GraphView:
    """A project's graph without what differs between two builds of it by construction."""

    spec_nodes: dict[str, dict[str, Any]]
    spec_links: tuple[str, ...]
    active: tuple[str, ...]
    history: dict[str, int]


@dataclass
class Migration:
    project: Neo4jProject
    clean_id: str
    drop_id: str
    clean: BuildResult
    clean_view: GraphView
    v13: BuildResult
    v13_labels: frozenset[str]
    v13_relations: frozenset[str]
    refused: list[ConfigurationError]
    refused_calls: int
    unchanged: bool
    rebuild: BuildResult
    rebuild_view: GraphView
    dropped: int
    after_drop: int
    first: BuildResult
    first_view: GraphView
    probe_first: BuildResult
    probe_rebuild: BuildResult
    probe_node: dict[str, Any]


def _stable(props: dict[str, Any]) -> dict[str, Any]:
    kept = {name: value for name, value in props.items() if name not in VOLATILE}
    if "statements" in kept:
        statements = json.loads(kept["statements"])
        kept["statements"] = [{k: v for k, v in item.items() if k not in STATEMENT_STAMPS} for item in statements]
    return kept


def _row(*parts: Any) -> str:
    return json.dumps(parts, sort_keys=True, default=str)


async def graph_view(project: Neo4jProject, project_id: str) -> GraphView:
    nodes = await project.run(
        "MATCH (n) WHERE n.group_id = $p RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props",
        p=project_id,
    )
    name: dict[str, str] = {}
    spec_nodes: dict[str, dict[str, Any]] = {}
    active, history = [], Counter[str]()
    for node in nodes:
        props, labels = node["props"], sorted(node["labels"])
        if "SpecNode" in labels:
            name[node["id"]] = props["spec_id"]
            spec_nodes[props["spec_id"]] = {"labels": labels, **_stable(props)}
            continue
        name[node["id"]] = f"{labels[0]}:{props.get('fragment_id') or props.get('document_id')}"
        if props.get("status") == "active":
            active.append(_row(labels, _stable(props)))
        else:
            history[f"{labels[0]}:{props.get('status')}"] += 1
    links = await project.run(
        "MATCH (s)-[r]->(t) WHERE s.group_id = $p "
        "RETURN elementId(s) AS s, labels(s) AS source, type(r) AS type, elementId(t) AS t, "
        "labels(t) AS target, t.status AS status, properties(r) AS props",
        p=project_id,
    )
    spec_links = []
    for link in links:
        row = _row(name[link["s"]], link["type"], name[link["t"]], _stable(link["props"]))
        if "SpecNode" in link["source"]:
            spec_links.append(row)
        elif link["status"] == "active":  # CONTAINS of an active fragment
            active.append(row)
    return GraphView(spec_nodes, tuple(sorted(spec_links)), tuple(sorted(active)), dict(history))


async def drop_project_graph(project: Neo4jProject, project_id: str) -> int:
    """Delete every node of the project's ``group_id`` with its relationships, in batches; return how many (the
    procedure of "Migration", as ``examples/graph_eval.py --cleanup`` runs it).
    """

    deleted = 0
    while True:
        rows = await project.run(
            "MATCH (n {group_id: $p}) WITH n LIMIT $batch DETACH DELETE n RETURN count(*) AS c",
            p=project_id,
            batch=DROP_BATCH,
        )
        if not rows[0]["c"]:
            return deleted
        deleted += int(rows[0]["c"])


async def _labels_and_relations(project: Neo4jProject, project_id: str) -> tuple[frozenset[str], frozenset[str]]:
    labels = await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p UNWIND labels(n) AS l RETURN DISTINCT l", p=project_id
    )
    relations = await project.run(
        "MATCH (s:SpecNode)-[r]->(:SpecNode) WHERE s.group_id = $p RETURN DISTINCT type(r) AS r", p=project_id
    )
    return frozenset(row["l"] for row in labels) - {"SpecNode"}, frozenset(row["r"] for row in relations)


async def _v13_build(
    project: Neo4jProject, factory: Any, root: Path, project_id: str
) -> tuple[ShopBuilds, BuildResult]:
    builds = ShopBuilds(project, factory, root, project_id=project_id, ontology=frozen_ontology())
    result, _ = await builds.build(1)
    return builds, result


async def _rebuild(builds: ShopV2Builds) -> BuildResult:
    gateway = builds.gateway(1)
    return await run_shop_build(dataclasses.replace(builds.context(1, gateway), rebuild=True))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def migration(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Migration]:
    gateways: list[ScriptedGateway] = []

    def factory(config: Any = None, **options: Any) -> ScriptedGateway:
        gateway = ScriptedGateway(config, **options)
        gateways.append(gateway)
        return gateway

    root = Path(tmp_path_factory.mktemp("migration"))
    async with open_neo4j_project() as project:
        # the reference: a clean v2.0 first build of shop-v2
        clean_id = project.new_project()
        clean, _ = await ShopV2Builds(project, factory, root / "clean", project_id=clean_id).build(1)
        clean_view = await graph_view(project, clean_id)

        # a v1.3 project; an update under v2.0 is refused (same options, then the v2.0 corpus and options)
        old, v13 = await _v13_build(project, factory, root / "rebuild", project.project_id)
        v13_labels, v13_relations = await _labels_and_relations(project, project.project_id)
        before = await project.snapshot()
        new = ShopV2Builds(project, factory, root / "rebuild", project_id=project.project_id)
        refusing = factory(live_llm_config())
        attempts = (dataclasses.replace(old.context(1, refusing), ontology=ONTOLOGY), new.context(1, refusing))
        refused = []
        for ctx in attempts:
            with pytest.raises(ConfigurationError) as raised:
                await run_build(ctx)
            refused.append(raised.value)
        latest = await old.stores.builds.latest_build(project.project_id)
        unchanged = await project.snapshot() == before and latest is not None and latest.build_id == v13.build_id

        # rebuild over the persisted v1.3 graph
        rebuild = await _rebuild(new)
        rebuild_view = await graph_view(project, project.project_id)

        # the documented path: drop the project's graph, keep the stores, build again
        drop_id = project.new_project()
        await _v13_build(project, factory, root / "drop", drop_id)
        dropped = await drop_project_graph(project, drop_id)
        after_drop = await project.count(drop_id)
        first, _ = await ShopV2Builds(project, factory, root / "drop", project_id=drop_id).build(1)
        first_view = await graph_view(project, drop_id)

        # probe: a requirement whose label the new ontology changes keeps its old label in a rebuild
        probe_id = project.new_project()
        probe = ShopV2Builds(project, factory, root / "probe", project_id=probe_id)
        probe_first, _ = await probe.build(1)
        await project.run(
            "MATCH (n:BusinessRule) WHERE n.group_id = $p AND n.name = $name "
            "REMOVE n:BusinessRule SET n:FunctionalRequirement, n.spec_id = $fr + substring(n.spec_id, size($br))",
            p=probe_id,
            name=RELABELED,
            fr=ONTOLOGY.id_prefix("FunctionalRequirement"),
            br=ONTOLOGY.id_prefix("BusinessRule"),
        )
        probe_rebuild = await _rebuild(probe)
        rows = await project.run(
            "MATCH (n:SpecNode) WHERE n.group_id = $p AND n.name = $name "
            "RETURN labels(n) AS labels, n.label_disagreement AS disagreement, n.spec_id AS spec_id",
            p=probe_id,
            name=RELABELED,
        )
        assert len(rows) == 1, rows
        assert_all_scripted(*gateways)
        yield Migration(
            project=project,
            clean_id=clean_id,
            drop_id=drop_id,
            clean=clean,
            clean_view=clean_view,
            v13=v13,
            v13_labels=v13_labels,
            v13_relations=v13_relations,
            refused=refused,
            refused_calls=len(refusing.calls),
            unchanged=unchanged,
            rebuild=rebuild,
            rebuild_view=rebuild_view,
            dropped=dropped,
            after_drop=after_drop,
            first=first,
            first_view=first_view,
            probe_first=probe_first,
            probe_rebuild=probe_rebuild,
            probe_node=dict(rows[0]),
        )


def _assert_verified(result: BuildResult, mode: str) -> None:
    assert (result.mode, result.status, result.failed_step, result.error_code) == (mode, "succeeded", None, None)
    assert result.settled
    assert result.verification is not None
    assert result.verification.ok, result.verification
    assert result.verification.found == {} and result.verification.problems == {}, result.verification.model_dump_json(
        indent=1
    )


def _differences(got: dict[str, dict[str, Any]], want: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The spec nodes that differ, with the properties that do (got, want)."""

    differing = {}
    for key in sorted(got.keys() | want.keys()):
        mine, theirs = got.get(key, {}), want.get(key, {})
        if mine != theirs:
            differing[key] = {
                p: (mine.get(p), theirs.get(p)) for p in mine.keys() | theirs.keys() if mine.get(p) != theirs.get(p)
            }
    return differing


def _assert_same_graph(view: GraphView, clean: GraphView) -> None:
    assert view.spec_nodes == clean.spec_nodes, json.dumps(_differences(view.spec_nodes, clean.spec_nodes), indent=1)
    assert view.spec_links == clean.spec_links
    assert view.active == clean.active
    assert clean.history == {}


async def _assert_d6_d7(project: Neo4jProject, project_id: str) -> None:
    owners = sorted(label for label in ONTOLOGY.labels if ONTOLOGY.owner_pairs(label))
    rows = await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p "
        "WITH n, [l IN labels(n) WHERE l IN $owned][0] AS owned "
        "OPTIONAL MATCH (n)-[r]->(t:SpecNode) "
        "WHERE (owned IS NOT NULL AND type(r) = 'PART_OF') OR (n:Interface AND type(r) = 'PROVIDED_BY') "
        "OR (n:Module AND t:Module AND type(r) = 'PART_OF') "
        "WITH n, count(DISTINCT t) AS targets WHERE targets > 1 RETURN n.name AS name",
        p=project_id,
        owned=owners,
    )
    assert rows == []
    cycles = await project.run(
        "MATCH path = (n:SpecNode)-[:DEPENDS_ON*1..10]->(n) WHERE n.group_id = $p RETURN count(path) AS n", p=project_id
    )
    assert cycles[0]["n"] == 0


async def test_the_v13_project_has_v13_labels_and_relations(migration: Migration) -> None:
    assert migration.v13.mode == "first"
    assert migration.v13.status in {"succeeded", "partially_succeeded"}  # shop/1 skips a fragment on purpose
    # v1.3 types and relations the v2.0 clean build of shop-v2 does not have
    assert {"Capability", "QualityRequirement"} <= migration.v13_labels
    assert {"REALIZED_BY", "GROUPED_BY"} <= migration.v13_relations
    assert migration.v13_labels <= set(frozen_ontology().labels)


async def test_an_update_under_v2_is_refused_with_ontology_changed_and_changes_nothing(migration: Migration) -> None:
    # the ontology is checked before the options: the v2.0 corpus's options differ too
    assert [(error.code, error.fields) for error in migration.refused] == [("ontology_changed", {"part": "labels"})] * 2
    assert migration.refused_calls == 0
    assert migration.unchanged


async def test_the_clean_v2_build_verifies(migration: Migration) -> None:
    _assert_verified(migration.clean, "first")
    await _assert_d6_d7(migration.project, migration.clean_id)


async def test_rebuild_over_the_v13_graph_gives_the_clean_v2_graph_here(migration: Migration) -> None:
    result = migration.rebuild
    _assert_verified(result, "rebuild")
    _assert_same_graph(migration.rebuild_view, migration.clean_view)
    # no v1.3 node is left: every one is deleted, or continues with the label v2.0 gives it
    labels, relations = await _labels_and_relations(migration.project, migration.project.project_id)
    assert labels == set(migration.clean.metrics.nodes_by_label)
    assert relations == set(migration.clean.metrics.edges_by_relation)
    await _assert_d6_d7(migration.project, migration.project.project_id)
    # the old corpus stays as history only: its fragments retired, its removed documents
    assert migration.rebuild_view.history == {"Document:removed": 2, "Fragment:retired": 43}
    assert result.spec_changes.removed and not result.spec_changes.carried_over


async def test_rebuild_keeps_the_old_label_of_a_requirement_the_new_ontology_retypes(migration: Migration) -> None:
    _assert_verified(migration.probe_first, "first")
    # the build succeeds and verifies: nothing reports the stale label but ``label_disagreement``
    _assert_verified(migration.probe_rebuild, "rebuild")
    node = migration.probe_node
    # the extraction says BusinessRule; Diff continues the node by its identity key and keeps its label
    assert sorted(node["labels"]) == ["FunctionalRequirement", "SpecNode"]
    assert node["disagreement"] == "BusinessRule"
    assert node["spec_id"] == "REQ::FR::ORDER_MOST_ONE_COUPON"
    verification = migration.probe_rebuild.verification
    assert verification is not None and verification.metrics.label_disagreement == 1


async def test_drop_and_first_gives_the_clean_v2_graph(migration: Migration) -> None:
    assert migration.dropped > 0 and migration.after_drop == 0
    _assert_verified(migration.first, "first")
    _assert_same_graph(migration.first_view, migration.clean_view)
    assert migration.first_view.history == {}
    labels, relations = await _labels_and_relations(migration.project, migration.drop_id)
    assert labels == set(migration.clean.metrics.nodes_by_label)
    assert relations == set(migration.clean.metrics.edges_by_relation)
    await _assert_d6_d7(migration.project, migration.drop_id)
