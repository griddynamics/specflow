"""Current-plan snapshot reads."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from graph.core.errors import ConfigurationError
from graph.core.reader import GraphNode, Provenance
from graph.plan.models import BuildChangeReport, ChangedSpecNode, CurrentPlan, PlanOptions
from graph.plan.queries import READ_AREAS, READ_PLAN, READ_STORIES, READ_TASKS, read_links
from graph.plan.snapshot import _chapter, fingerprint, read_current_plan
from graph_plan_support import plan_ontology, plan_rules, shop_snapshot
from graph_support import OfflineExecutor

PROJECT = "p-plan"
CREATED = datetime(2026, 1, 1, 12, tzinfo=UTC)
UPDATED = datetime(2026, 1, 2, 12, tzinfo=UTC)


async def test_read_current_plan_returns_the_plan_area_story_task_and_links() -> None:
    executor = OfflineExecutor(
        {
            READ_PLAN.name: [
                {
                    "plan_id": "PLAN::MAIN",
                    "profile": "profile",
                    "last_build_id": "build-2",
                    "graph_fingerprint": "fingerprint",
                    "ontology_version": "1",
                    "plan_ontology_version": "1",
                    "rules_version": "1",
                    "applying": False,
                    "last_run_id": "run-1",
                    "created_at": CREATED,
                    "updated_at": UPDATED,
                }
            ],
            READ_AREAS.name: [
                {
                    "area_id": "AREA::SHOP",
                    "kind": "module",
                    "anchor": "MOD::SHOP",
                    "title": "Shop",
                    "description": "The shop",
                    "interfaces": [{"name": "API", "kind": "api", "spec_id": "INT::API"}],
                    "not_buildable": [],
                    "planned_by": "model",
                    "status": "active",
                    "created_at": CREATED,
                    "updated_at": UPDATED,
                    "last_run_id": "run-1",
                }
            ],
            READ_STORIES.name: [
                {
                    "story_id": "STORY::SHOP::ORDERS",
                    "area_id": "AREA::SHOP",
                    "kind": "feature",
                    "anchor": "FR::ORDERS",
                    "chapter": None,
                    "title": "Orders",
                    "summary": "Order handling",
                    "release": "1",
                    "status": "active",
                    "created_at": CREATED,
                    "updated_at": UPDATED,
                    "last_run_id": "run-1",
                }
            ],
            READ_TASKS.name: [
                {
                    "task_id": "TASK::SHOP::DATA_ORDERS",
                    "area_id": "AREA::SHOP",
                    "action": "DATA_ORDERS",
                    "revision": 1,
                    "story_id": "STORY::SHOP::ORDERS",
                    "phase": "data",
                    "title": "Orders data",
                    "description": "Add orders",
                    "done_when": [{"text": "Orders are stored", "source": "spec"}],
                    "complexity": "medium",
                    "implements": ["FR::ORDERS"],
                    "release": ["1"],
                    "has_open_questions": False,
                    "planned_by": "model",
                    "prompt_ref": "tasks.1",
                    "spec_build_id": "build-2",
                    "status": "pending",
                    "claimed_by": None,
                    "claimed_at": None,
                    "attempts": 0,
                    "commit": None,
                    "block_reason": None,
                    "note": None,
                    "created_at": CREATED,
                    "updated_at": UPDATED,
                    "created_run_id": "run-1",
                    "last_run_id": "run-1",
                }
            ],
            read_links("IMPLEMENTS").name: [
                {
                    "type": "IMPLEMENTS",
                    "source": "TASK::SHOP::DATA_ORDERS",
                    "target": "FR::ORDERS",
                    "edge_key": "TASK::SHOP::DATA_ORDERS|IMPLEMENTS|FR::ORDERS",
                    "properties": [],
                },
                {
                    "type": "IMPLEMENTS",
                    "source": "STORY::SHOP::ORDERS",
                    "target": "FR::ORDERS",
                    "edge_key": "STORY::SHOP::ORDERS|IMPLEMENTS|FR::ORDERS",
                    "properties": [],
                },
            ],
            read_links("IN_AREA").name: [
                {
                    "type": "IN_AREA",
                    "source": "FR::ORDERS",
                    "target": "AREA::SHOP",
                    "edge_key": "FR::ORDERS|IN_AREA|AREA::SHOP",
                    "properties": [["rule", "owner"]],
                }
            ],
            read_links("IN_STORY").name: [
                {
                    "type": "IN_STORY",
                    "source": "TASK::SHOP::DATA_ORDERS",
                    "target": "STORY::SHOP::ORDERS",
                    "edge_key": "TASK::SHOP::DATA_ORDERS|IN_STORY|STORY::SHOP::ORDERS",
                    "properties": [],
                }
            ],
            read_links("WAITS_FOR").name: [
                {
                    "type": "WAITS_FOR",
                    "source": "TASK::SHOP::LOGIC_ORDERS",
                    "target": "TASK::SHOP::DATA_ORDERS",
                    "edge_key": "TASK::SHOP::LOGIC_ORDERS|WAITS_FOR|TASK::SHOP::DATA_ORDERS",
                    "properties": [["source", "planner"]],
                }
            ],
            read_links("SUPERSEDES").name: [
                {
                    "type": "SUPERSEDES",
                    "source": "TASK::SHOP::DATA_ORDERS_R2",
                    "target": "TASK::SHOP::DATA_ORDERS",
                    "edge_key": "TASK::SHOP::DATA_ORDERS_R2|SUPERSEDES|TASK::SHOP::DATA_ORDERS",
                    "properties": [],
                }
            ],
        }
    )

    current = await read_current_plan(executor, PROJECT)

    assert current == CurrentPlan.model_validate_json(current.model_dump_json())
    assert current.plan is not None and current.plan.last_build_id == "build-2"
    assert [area.area_id for area in current.areas] == ["AREA::SHOP"]
    assert [story.story_id for story in current.stories] == ["STORY::SHOP::ORDERS"]
    assert [task.task_id for task in current.tasks] == ["TASK::SHOP::DATA_ORDERS"]
    assert [link.edge_key for link in current.links] == [
        "FR::ORDERS|IN_AREA|AREA::SHOP",
        "STORY::SHOP::ORDERS|IMPLEMENTS|FR::ORDERS",
        "TASK::SHOP::DATA_ORDERS_R2|SUPERSEDES|TASK::SHOP::DATA_ORDERS",
        "TASK::SHOP::DATA_ORDERS|IMPLEMENTS|FR::ORDERS",
        "TASK::SHOP::DATA_ORDERS|IN_STORY|STORY::SHOP::ORDERS",
        "TASK::SHOP::LOGIC_ORDERS|WAITS_FOR|TASK::SHOP::DATA_ORDERS",
    ]
    in_area = current.links[0]
    assert in_area.source == "FR::ORDERS"
    assert in_area.target == "AREA::SHOP"
    assert in_area.properties == (("rule", "owner"),)
    assert executor.reads[0] == (READ_PLAN.name, PROJECT)


async def test_read_current_plan_returns_none_for_an_empty_project() -> None:
    current = await read_current_plan(OfflineExecutor(), PROJECT)

    assert current == CurrentPlan(plan=None)


async def test_snapshot_selects_plannable_and_context_nodes_and_excludes_gift_cards() -> None:
    artifact = await shop_snapshot()

    ids = {node.spec_id for node in artifact.plannable}
    context_ids = {node.spec_id for node in artifact.context}
    excluded = {"FEAT::GIFT_CARDS", "REQ::FR::GIFT_CARDS"}
    assert excluded.isdisjoint(ids)
    assert excluded.isdisjoint(context_ids)
    assert "MOD::STRIPE" in context_ids
    assert "OQ::PAYMENT_PROVIDERS" in context_ids
    assert "MOD::WEB_SHOP" in ids
    assert "MOD::STRIPE" in artifact.revisions.nodes
    assert "OQ::PAYMENT_PROVIDERS" in artifact.revisions.nodes
    assert artifact.edges
    assert all(edge.source in ids or edge.target in ids for edge in artifact.edges)


async def test_snapshot_uses_the_first_chapter_by_document_title_then_ordinal() -> None:
    artifact = await shop_snapshot()

    node = next(item for item in artifact.plannable if item.spec_id == "REQ::LOGIC_EXPORT")
    assert node.chapter is not None
    assert node.chapter.document == "Shop requirements"
    assert node.chapter.heading == "Requirements"


def test_chapter_accepts_tuple_string_and_none_heading_paths() -> None:
    documents = {"shop": type("Document", (), {"title": "Shop"})()}
    cases = (
        ("shop.modules", ("Shop requirements", "Modules"), "Modules"),
        ("shop.modules", "Shop requirements > Modules", "Modules"),
        ("shop.modules", None, "Shop"),
    )
    for fragment_id, heading_path, expected in cases:
        fragments = {
            fragment_id: type("Fragment", (), {"heading_path": heading_path, "ordinal": 1, "document_id": "shop"})()
        }
        node = GraphNode(
            spec_id="MOD::WEB_SHOP",
            uuid="node",
            label="Module",
            name="Web shop",
            provenance=(
                Provenance(
                    document_id="shop",
                    path=None,
                    fragment_id=fragment_id,
                    line_start=None,
                    line_end=None,
                    quote=None,
                ),
            ),
        )
        chapter = _chapter(node, fragments, documents)
        assert chapter is not None
        assert chapter.heading == expected


async def test_snapshot_fingerprint_is_stable_and_depends_only_on_planning_options() -> None:
    artifact = await shop_snapshot()

    same = await shop_snapshot()
    assert same.fingerprint == artifact.fingerprint

    rules = plan_rules(plan_ontology())
    ontology = plan_ontology()
    changed_build = fingerprint(
        ontology,
        rules,
        _with_last_build(artifact.plannable),
        artifact.edges,
        task_size=artifact.current.plan.options.task_size if artifact.current.plan else 5,
        planner_members=40,
    )
    assert changed_build != artifact.fingerprint

    base = fingerprint(ontology, rules, artifact.plannable, artifact.edges, task_size=5, planner_members=40)
    assert (
        fingerprint(
            ontology,
            rules,
            artifact.plannable,
            artifact.edges,
            task_size=PlanOptions(review=False, review_model="other", max_attempts=5).task_size,
            planner_members=40,
        )
        == base
    )
    assert fingerprint(ontology, rules, artifact.plannable, artifact.edges, task_size=6, planner_members=40) != base


async def test_snapshot_marks_an_all_changed_evidence_update_unchanged() -> None:
    artifact = await shop_snapshot(
        plan=_plan(last_build_id="build-2"),
        previous_result={"status": "succeeded"},
        changes=(
            BuildChangeReport(
                build_id="build-2",
                previous_build_id="build-2",
                nodes=(ChangedSpecNode(spec_id="MOD::WEB_SHOP", classes=("changed_evidence",)),),
            ),
        ),
    )

    assert artifact.mode == "update"
    assert artifact.change_set.meaning == {}
    assert artifact.change_set.links == frozenset()
    assert artifact.unchanged is True
    assert artifact.revisions.consumed == ("build-2",)


async def test_snapshot_without_plannable_nodes_raises_no_spec_graph() -> None:
    with pytest.raises(ConfigurationError) as raised:
        await shop_snapshot("empty")

    assert raised.value.code == "no_spec_graph"


def _with_last_build(nodes: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(
        node.model_copy(update={"last_build_id": "changed"}) if node.spec_id == "MOD::WEB_SHOP" else node
        for node in nodes
    )


def _plan(last_build_id: str) -> dict[str, object]:
    return {
        "plan_id": "PLAN::SHOP",
        "last_build_id": last_build_id,
        "ontology_version": "1",
        "plan_ontology_version": "1",
        "rules_version": "1",
        "created_at": CREATED,
        "updated_at": UPDATED,
    }


async def test_a_first_run_consumes_every_build_the_graph_already_reflects() -> None:
    artifact = await shop_snapshot()

    builds = {node.last_build_id for node in artifact.plannable if node.last_build_id}
    assert set(artifact.revisions.consumed) >= builds
    assert artifact.revisions.consumed[-1] == artifact.build_id
