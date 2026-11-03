"""The plan ontology package data is merged and covered by the default schema."""

from __future__ import annotations

from importlib.resources import files
from typing import get_args

from graph.core import Ontology, load_ontology
from graph.core.schema import schema_objects
from graph.plan.names import (
    PLAN_LABELS,
    PLAN_RELATIONS,
    AreaKind,
    AreaStatus,
    BlockReason,
    Complexity,
    DoneWhenSource,
    InterfaceKind,
    NotBuildableReason,
    Phase,
    PlanMode,
    PlannedBy,
    StoryKind,
    StoryProgress,
    StoryStatus,
    TaskStatus,
    plan_ontology_version,
    plan_vocabularies,
)


def test_the_shipped_plan_ontology_is_discovered_by_default() -> None:
    ontology = load_ontology()

    assert isinstance(ontology, Ontology)
    assert ontology.owner_of("Task") == "plan"
    assert ontology.owner_of("IN_AREA") == "plan"
    assert ontology.is_declared("Task")
    assert ontology.is_declared("WAITS_FOR")


def test_the_shipped_plan_ontology_is_complete_package_data() -> None:
    package_file = files("graph.plan") / "ontology.yaml"
    assert package_file.is_file()
    assert package_file.read_text(encoding="utf-8").startswith("# plan-ontology.yaml:")


def test_plan_names_come_from_the_shipped_ontology_vocabularies() -> None:
    vocabularies = plan_vocabularies()

    assert {
        "phase": get_args(Phase),
        "task_status": get_args(TaskStatus),
        "complexity": get_args(Complexity),
        "area_kind": get_args(AreaKind),
        "area_status": get_args(AreaStatus),
        "interface_kind": get_args(InterfaceKind),
        "story_kind": get_args(StoryKind),
        "story_status": get_args(StoryStatus),
        "story_progress": get_args(StoryProgress),
        "done_when_source": get_args(DoneWhenSource),
        "not_buildable_reason": get_args(NotBuildableReason),
    } == {name: values for name, values in vocabularies.items() if name != "planned_by"}
    assert get_args(PlannedBy) == vocabularies["planned_by"]
    assert get_args(PlanMode) == ("first", "update", "replan")
    assert get_args(BlockReason) == (
        "spec_ambiguous",
        "spec_contradiction",
        "spec_missing",
        "environment",
        "other",
    )
    assert plan_ontology_version() == "2.0"


def test_default_schema_objects_cover_the_shipped_plan_ontology() -> None:
    ontology = load_ontology()
    objects = schema_objects(ontology)

    assert {
        "graph_plan_id",
        "graph_plan_area_id",
        "graph_plan_story_id",
        "graph_plan_task_id",
        "graph_foreign_uuid_plan",
        "graph_foreign_uuid_workarea",
        "graph_foreign_uuid_story",
        "graph_foreign_uuid_task",
        "graph_plan_task_status",
        "graph_plan_task_story",
        "graph_plan_story_area",
        "graph_edge_key_in_area",
        "graph_edge_key_in_story",
        "graph_edge_key_implements",
        "graph_edge_key_waits_for",
        "graph_edge_key_supersedes",
    } <= {item.name for item in objects}


def test_the_plan_owns_every_plan_label_and_relation_and_build_owns_spec_dependencies() -> None:
    ontology = load_ontology()

    assert {name: ontology.owner_of(name) for name in (*PLAN_LABELS, *PLAN_RELATIONS)} == dict.fromkeys(
        (*PLAN_LABELS, *PLAN_RELATIONS), "plan"
    )
    assert ontology.owner_of("DEPENDS_ON") == "build"
