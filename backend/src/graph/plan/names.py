"""The plan's own names, read once from its shipped ontology."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Final, Literal, get_args

import yaml

PlanLabel = Literal["Plan", "WorkArea", "Story", "Task"]
PlanRelation = Literal["IN_AREA", "IN_STORY", "IMPLEMENTS", "WAITS_FOR", "SUPERSEDES"]
Phase = Literal["setup", "contract", "data", "logic", "integration", "quality", "acceptance"]
TaskStatus = Literal["pending", "in_progress", "completed", "failed", "blocked", "superseded", "retired"]
Complexity = Literal["low", "medium", "high"]
AreaKind = Literal["module", "project"]
AreaStatus = Literal["active", "retired"]
InterfaceKind = Literal["api", "event", "data", "library", "cli", "config"]
PlannedBy = Literal["model", "rules"]
StoryKind = Literal["feature", "interface", "entity", "core", "chapter", "setup", "closing"]
StoryStatus = Literal["active", "retired"]
StoryProgress = Literal["pending", "in_progress", "blocked", "completed"]
TaskProgress = Literal["done", "skipped", "todo"]
StoryState = Literal["completed", "in_progress", "blocked", "ready", "waiting", "empty"]
StoryView = Literal["open", "mine", "done"]
DoneWhenSource = Literal["spec", "planner"]
NotBuildableReason = Literal["process", "governance", "release_gate", "policy_only"]
BlockReason = Literal["spec_ambiguous", "spec_contradiction", "spec_missing", "environment", "other"]
PlanMode = Literal["first", "update", "replan"]

PLAN_LABELS: Final[tuple[PlanLabel, ...]] = get_args(PlanLabel)
PLAN_RELATIONS: Final[tuple[PlanRelation, ...]] = get_args(PlanRelation)


def _ontology_data() -> dict[str, object]:
    package_file = files("graph.plan") / "ontology.yaml"
    data = yaml.safe_load(package_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("the plan ontology must be a mapping")
    return data


@lru_cache(maxsize=1)
def plan_vocabularies() -> Mapping[str, tuple[str, ...]]:
    """The shipped ontology's vocabulary values, in file order."""

    values = _ontology_data().get("vocabularies")
    if not isinstance(values, dict):
        raise TypeError("the plan ontology must declare vocabularies")
    return MappingProxyType({name: tuple(value) for name, value in values.items()})


def plan_ontology_version() -> str:
    """The shipped plan ontology's file version."""

    version = _ontology_data().get("version")
    if not isinstance(version, str | int):
        raise TypeError("the plan ontology must declare a version")
    return str(version)
