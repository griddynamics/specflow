"""Live Neo4j half of the cost estimate tests (plan 5.T2; build.md "Cost estimate"; FR-BLD-16).

Before every build of the shop chain (``first`` of snapshot 1, ``update`` to 2 and 3, an ``update``
without change, a ``rebuild`` of 3) ``estimate_build`` runs on a gateway without answers; then the
build runs with the hand-written answers (``ShopAnswers``) and the estimate is compared with the
calls that build really made. Exact where the method is exact (hub catalog, node windows, a rebuild's
calls), within bounds elsewhere. The estimate leaves the graph (snapshot) and the stores (file tree)
unchanged and records no call.

Gate: ``SPECFLOW_TEST_NEO4J=1`` and ``uv run --env-file ../.env.local``. Projects are
``pytest-graph-<hex>`` ids that ``neo4j_project`` deletes afterwards.
"""

from __future__ import annotations

import dataclasses
import shutil
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from graph.build import (
    BuildContext,
    BuildEstimate,
    BuildResult,
    BuildSettings,
    FileCorpusSource,
    estimate_build,
    open_file_stores,
    run_build,
)
from graph.build.conform import DEDUP_PROMPT
from graph.build.consolidate import HUBS_PROMPT
from graph.build.graphiti import EDGES_PROMPT, NODES_PROMPT
from graph.build.prompts import ATTRIBUTES_PROMPT, HUB_CATALOG_PROMPT
from graph.core.llm.gateway import EMBED_PROMPT, embed_batches, embed_inputs, estimate_tokens, request_tokens
from graph_support import (
    SHOP_DIR,
    Neo4jProject,
    ScriptedGateway,
    live_llm_config,
    shop_answers,
    shop_corpus,
    tree_hash,
)
from specflow_live import requires_live

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")

OPTIONS = shop_answers(1).options
STEP_OF = {
    HUB_CATALOG_PROMPT: "hub_catalog",
    NODES_PROMPT: "node_extraction",
    HUBS_PROMPT: "hub_consolidation",
    EDGES_PROMPT: "edge_extraction",
    ATTRIBUTES_PROMPT: "attributes",
    DEDUP_PROMPT: "requirement_dedup",
    EMBED_PROMPT: "embeddings",
}
EXACT_TOKENS = ("hub_catalog",)


def actual(gateway: ScriptedGateway) -> dict[str, tuple[int, int]]:
    """Calls and input tokens per estimate step of what a build really asked: structured calls by
    ``request_tokens``, embeddings as the gateway batches them."""

    found: dict[str, list[int]] = {step: [0, 0] for step in STEP_OF.values()}
    for call in gateway.calls:
        step = STEP_OF.get(call.prompt)
        if step is None:
            continue
        if step == "embeddings":
            items, _ = embed_inputs(list(call.texts))
            found[step][0] += len(embed_batches(items))
            found[step][1] += sum(estimate_tokens(text) for text in items)
        else:
            found[step][0] += 1
            found[step][1] += request_tokens(call.messages)
    return {step: (calls, tokens) for step, (calls, tokens) in found.items()}


class Shop:
    """One project built from the shop snapshots, sharing file stores and the corpus index."""

    def __init__(
        self, project: Neo4jProject, gateways: Callable[..., ScriptedGateway], root: Path, project_id: str
    ) -> None:
        self.project, self.gateways, self.root, self.project_id = project, gateways, root, project_id
        self.stores = open_file_stores(root)

    def context(self, snapshot: int, gateway: ScriptedGateway, **overrides: object) -> BuildContext:
        ctx = BuildContext(
            project_id=self.project_id,
            ontology=self.project.ontology,
            driver=self.project.driver,
            gateway=gateway,
            registry=self.stores.registry,
            corpus=shop_corpus(snapshot, self.root),
            extraction=self.stores.extraction,
            builds=self.stores.builds,
            options=OPTIONS,
            database=self.project.database,
            settings=BuildSettings(inference=False),
        )
        return dataclasses.replace(ctx, **overrides) if overrides else ctx

    async def estimate(self, snapshot: int, **overrides: object) -> BuildEstimate:
        """The estimate, checked to call no model and to leave the graph and the stores unchanged."""

        silent = self.gateways(live_llm_config())
        graph, files = await self.project.snapshot(self.project_id), tree_hash(self.root)
        estimate = await estimate_build(self.context(snapshot, silent, **overrides))
        assert silent.calls == [] and silent.usage().total().calls == 0
        assert await self.project.snapshot(self.project_id) == graph
        assert tree_hash(self.root) == files
        return estimate

    async def build(
        self, snapshot: int, answers: int | None = None, **overrides: object
    ) -> tuple[BuildResult, ScriptedGateway]:
        gateway = shop_answers(answers or snapshot).install(self.gateways(live_llm_config()))
        result = await run_build(self.context(snapshot, gateway, **overrides))
        assert result.failed_step is None, (result.failed_step, result.error_code)
        return result, gateway


def calls_of(estimate: BuildEstimate) -> dict[str, int]:
    return {step: cost.calls for step, cost in estimate.steps.items()}


def assert_bounded(estimate: BuildEstimate, real: dict[str, tuple[int, int]], name: str) -> None:
    """Calls within one of the real calls; tokens within half to twice the real ones (none when none)."""

    for step, (calls, tokens) in real.items():
        cost = estimate.steps[step]
        assert abs(cost.calls - calls) <= 1, (name, step, cost.calls, calls)
        if tokens:
            assert tokens / 2 <= cost.input_tokens <= tokens * 2, (name, step, cost.input_tokens, tokens)
        elif not cost.calls:
            assert cost.input_tokens == 0, (name, step)


async def test_the_shop_chain_is_estimated_before_every_build(
    neo4j_project: Neo4jProject, scripted_gateway: Callable[..., ScriptedGateway], tmp_path: Path
) -> None:
    shop = Shop(neo4j_project, scripted_gateway, tmp_path / "stores", neo4j_project.project_id)

    # first: hub catalog and node windows exact; every step the build uses is predicted
    estimate = await shop.estimate(1)
    result, gateway = await shop.build(1)
    real = actual(gateway)
    assert estimate.mode == result.mode == "first"
    assert estimate.units_to_extract == result.metrics.units_extracted
    assert estimate.steps["hub_catalog"].calls == real["hub_catalog"][0] == 1
    assert estimate.steps["hub_catalog"].input_tokens == real["hub_catalog"][1]
    assert estimate.steps["node_extraction"].calls == real["node_extraction"][0]
    nodes_tokens = real["node_extraction"][1]
    assert abs(estimate.steps["node_extraction"].input_tokens - nodes_tokens) <= nodes_tokens * 0.02
    for step, (calls, _) in real.items():
        assert not calls or estimate.steps[step].calls >= 1, step
    assert estimate.steps["embeddings"].calls == real["embeddings"][0]

    # updates: node windows exact (calls and tokens), the rest within bounds
    for snapshot in (2, 3):
        estimate = await shop.estimate(snapshot)
        result, gateway = await shop.build(snapshot)
        real = actual(gateway)
        assert estimate.mode == result.mode == "update"
        assert estimate.units_to_extract == result.metrics.units_extracted
        assert (estimate.steps["node_extraction"].calls, estimate.steps["node_extraction"].input_tokens) == real[
            "node_extraction"
        ]
        assert estimate.steps["hub_catalog"].calls == real["hub_catalog"][0] == 0
        # snapshot 3's "Export details" unit keeps its content: its stored edges are reused, not called
        assert estimate.steps["edge_extraction"].calls == real["edge_extraction"][0]
        assert_bounded(estimate, real, f"update {snapshot}")

    # an update without change: nothing at all
    estimate = await shop.estimate(3)
    result, gateway = await shop.build(3)
    assert estimate.mode == result.mode == "update"
    assert estimate.dirty_fragments == estimate.units_to_extract == 0
    assert all(cost.calls == cost.input_tokens == 0 for cost in estimate.steps.values()), estimate.steps
    assert all(calls == 0 for calls, _ in actual(gateway).values())

    # rebuild: every step's calls exact, hub catalog tokens exact
    estimate = await shop.estimate(3, rebuild=True)
    result, gateway = await shop.build(3, rebuild=True)
    real = actual(gateway)
    assert estimate.mode == result.mode == "rebuild"
    assert calls_of(estimate) == {step: calls for step, (calls, _) in real.items()}
    assert estimate.steps["hub_catalog"].input_tokens == real["hub_catalog"][1]
    assert_bounded(estimate, real, "rebuild")


async def test_three_new_documents_in_a_built_project(
    neo4j_project: Neo4jProject, scripted_gateway: Callable[..., ScriptedGateway], tmp_path: Path
) -> None:
    shop = Shop(neo4j_project, scripted_gateway, tmp_path / "stores", neo4j_project.project_id)
    await shop.build(1)
    folder = tmp_path / "corpus"
    shutil.copytree(SHOP_DIR / "1", folder)
    for index, name in enumerate(("checkout.md", "orders.md", "overview.md")):
        text = (SHOP_DIR / "1" / name).read_text(encoding="utf-8")
        (folder / f"added-{index}.md").write_text(text.replace("# ", "# Added ", 1), encoding="utf-8")

    estimate = await shop.estimate(1, corpus=FileCorpusSource(folder, shop.root))

    assert estimate.mode == "update"
    assert estimate.change_set.new == ("added-0.md", "added-1.md", "added-2.md")
    assert set(estimate.change_set.unchanged) == {path.name for path in (SHOP_DIR / "1").glob("*.md")}
    # only the new documents' fragments are extracted (orders.md is two units), the others reused
    assert estimate.dirty_fragments == estimate.fragments_by_change["new"] > 0
    assert estimate.units_to_extract >= 3 and estimate.units_reused > 0
    for step in ("node_extraction", "edge_extraction", "attributes", "embeddings"):
        assert estimate.steps[step].calls > 0 and estimate.steps[step].input_tokens > 0, step
    assert estimate.steps["hub_catalog"].calls == 0  # an update reads the persisted hubs instead


async def test_an_estimate_leaves_nothing_behind_for_the_next_build(
    neo4j_project: Neo4jProject, scripted_gateway: Callable[..., ScriptedGateway], tmp_path: Path
) -> None:
    """The Graphiti rendering client never reaches a real build: a build after an estimate makes the
    same requests (prompt and semantic key, in count) as one without."""

    plain = Shop(neo4j_project, scripted_gateway, tmp_path / "plain", neo4j_project.project_id)
    estimated = Shop(neo4j_project, scripted_gateway, tmp_path / "estimated", neo4j_project.new_project())
    _, without = await plain.build(1)
    await estimated.estimate(1)
    _, after = await estimated.build(1)

    def requests(gateway: ScriptedGateway) -> Counter[tuple[str, object]]:
        return Counter(
            (call.prompt, call.key if call.call_class != "embedding" else len(call.texts)) for call in gateway.calls
        )

    assert requests(after) == requests(without)
    assert [list(call.messages) for call in after.calls if call.prompt == NODES_PROMPT] == [
        list(call.messages) for call in without.calls if call.prompt == NODES_PROMPT
    ]
