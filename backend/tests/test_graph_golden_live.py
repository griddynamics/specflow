"""Live parity of the graph store with the golden data recorded from Neo4j (design open decision 5).

The scripted scenarios of ``graph_probes.scenarios`` (build, update, infer, verify, renamed ontology) run on
the store ``SPECFLOW_TEST_GRAPH_STORE`` selects, twice, into separate projects:

- the two runs must be identical (the scenarios are deterministic: hand-written model answers, scripted
  embeddings, snapshots without ids and clocks);
- each canonical snapshot must equal ``tests/fixtures/graph/golden_live/snapshots/<scenario>.json``;
- the search rankings of the shop queries must have the same top-5 sets as
  ``golden_live/search_rankings.json`` (rank differences are printed, not failures).

``SPECFLOW_GOLDEN_WRITE=1`` rewrites the golden files from the first run (Neo4j only; a store whose
probe is not the reference one refuses). Record on an empty Neo4j database (a new container): Lucene's
fulltext statistics span the whole database, so other projects in it shift the scores and near-ties at the
top-five cut. Gate: ``SPECFLOW_TEST_GRAPH_STORE=<store>`` (or
``SPECFLOW_TEST_NEO4J=1``); one run takes a few minutes.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from graph.core import load_ontology
from graph_probes import requires_live_graph, selected_backend
from graph_probes import scenarios as scenario_module
from graph_probes.golden import (
    EXPLAINED_SET_DIFFERENCES,
    compare_rankings,
    load_rankings,
    load_snapshot,
    snapshot_differences,
    write_rankings,
    write_snapshot,
)
from graph_support import GraphProject, ScriptedGateway, assert_all_scripted, open_graph_project, shipped_ontology
from test_graph_renamed_ontology_live import ANSWER_RENAME, write_renamed_ontology

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

WRITE = os.environ.get("SPECFLOW_GOLDEN_WRITE") == "1"


@dataclass
class Run:
    outcomes: dict[str, scenario_module.Outcome]
    rankings: dict[str, Any]


async def run_everything(project: GraphProject, root: Path) -> Run:
    gateways: list[ScriptedGateway] = []

    def factory(config: Any = None, **options: Any) -> ScriptedGateway:
        gateway = ScriptedGateway(config, **options)
        gateways.append(gateway)
        return gateway

    (root / "renamed-ontology").mkdir(parents=True)
    renamed = load_ontology(write_renamed_ontology(root / "renamed-ontology"), submodule_files=[])
    # Search scores come from indexes that span the whole database (Lucene statistics count every
    # project), so each scenario project but the one the rankings read is deleted as soon as its
    # snapshot is taken: the search runs on one copy of the corpus, the same on every run.
    outcomes: dict[str, scenario_module.Outcome] = {}

    async def run(name: str, scenario: Any, *args: Any, **options: Any) -> None:
        outcomes[name] = await scenario(project, factory, root, *args, **options)
        if name != "infer":
            assert await project.probe.delete_projects([outcomes[name].project_id]) == 0

    await run("build", scenario_module.build)
    await run("update", scenario_module.update)
    await run("infer", scenario_module.infer)
    await run("verify", scenario_module.verify)
    await run("renamed_ontology", scenario_module.renamed_ontology, ontology=renamed, rename=ANSWER_RENAME)
    # The search rankings read the project of the ``infer`` scenario (a shop build, inference on).
    rankings = await scenario_module.search_rankings(project, outcomes["infer"].project_id)
    assert await project.probe.delete_projects([outcomes["infer"].project_id]) == 0
    assert_all_scripted(*gateways)
    return Run(outcomes, rankings)


@dataclass
class Runs:
    first: Run
    second: Run
    backend: str


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def runs(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Runs]:
    """Both runs, made once for the module; the tests reading them are synchronous (the connections
    live on this fixture's module loop)."""

    root = tmp_path_factory.mktemp("golden")
    assert shipped_ontology()
    async with open_graph_project() as project:
        first = await run_everything(project, root / "first")
        second = await run_everything(project, root / "second")
        built = Runs(first, second, project.backend)
        if WRITE:
            for name, outcome in first.outcomes.items():
                write_snapshot(built.backend, name, outcome.snapshot, outcome.meta)
            write_rankings(built.backend, first.rankings)
        yield built


@pytest.mark.parametrize("scenario", scenario_module.SCENARIOS)
def test_a_scenario_leaves_the_same_graph_every_time(runs: Runs, scenario: str) -> None:
    first, second = runs.first.outcomes[scenario], runs.second.outcomes[scenario]
    assert snapshot_differences(first.snapshot, second.snapshot) == []
    assert first.snapshot == second.snapshot
    assert first.meta == second.meta
    assert first.snapshot["nodes"] and first.snapshot["edges"]


def test_search_rankings_are_the_same_every_time(runs: Runs) -> None:
    differing = [
        (text, mode)
        for text, entry in runs.first.rankings["queries"].items()
        for mode in entry["modes"]
        if entry[mode] != runs.second.rankings["queries"][text][mode]
    ]
    detail = [
        (text, mode, runs.first.rankings["queries"][text][mode], runs.second.rankings["queries"][text][mode])
        for text, mode in differing[:2]
    ]
    assert differing == [], detail
    assert runs.first.rankings == runs.second.rankings


@pytest.mark.parametrize("scenario", scenario_module.SCENARIOS)
def test_a_scenario_graph_equals_the_golden_snapshot(runs: Runs, scenario: str) -> None:
    golden = load_snapshot(scenario)
    outcome = runs.first.outcomes[scenario]
    actual = {"nodes": outcome.snapshot["nodes"], "edges": outcome.snapshot["edges"]}
    assert snapshot_differences(golden, actual) == []
    assert {"nodes": golden["nodes"], "edges": golden["edges"]} == actual
    assert golden["meta"] == outcome.meta


# Ids of a golden top five another run may have swapped for others, by store. Neo4j scores text with Lucene BM25
# per field; ArcadeDB's BM25 differs in how it counts document length and average length, so a close call
# between two nodes can flip (adapter docs, "Known differences": one id of a top five at most, never more).
RANKING_TOLERANCE = {"arcadedb": 1}


def test_search_rankings_have_the_golden_top_five_sets(runs: Runs, capsys: pytest.CaptureFixture[str]) -> None:
    explained = EXPLAINED_SET_DIFFERENCES.get(selected_backend() or "", {})
    tolerance = RANKING_TOLERANCE.get(selected_backend() or "", 0)
    parity = compare_rankings(load_rankings(), runs.first.rankings, explained=explained, tolerance=tolerance)
    if parity.rank_differences or parity.explained:
        with capsys.disabled():
            sys.stderr.write("\n" + parity.report() + "\n")
    assert parity.ok, parity.report()
