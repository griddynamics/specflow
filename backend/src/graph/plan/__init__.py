"""Turns the spec graph into implementation plans for coding agents."""

from __future__ import annotations

from graph.plan.checks import check_plan
from graph.plan.dag import topological_order, transitive_prerequisites
from graph.plan.errors import PlanRulesError, TaskStateError
from graph.plan.estimate import estimate_plan
from graph.plan.files import FilePlanStore, open_plan_store
from graph.plan.lifecycle import TaskLifecycle
from graph.plan.models import (
    BuildChangeReport,
    ChangedSpecEdge,
    ChangedSpecNode,
    Claim,
    MeaningRevisions,
    PlanArea,
    PlanCheckMetrics,
    PlanCheckReport,
    PlanDag,
    PlanEstimate,
    PlanResult,
    PlanRunRecord,
    PlanStepEstimate,
    PlanStory,
    PlanTask,
    StoryDetail,
    StoryWork,
    TaskBrief,
)
from graph.plan.names import (
    AreaKind,
    AreaStatus,
    BlockReason,
    Complexity,
    DoneWhenSource,
    InterfaceKind,
    NotBuildableReason,
    Phase,
    PlanLabel,
    PlanMode,
    PlannedBy,
    PlanRelation,
    StoryKind,
    StoryProgress,
    StoryStatus,
    TaskStatus,
    plan_ontology_version,
    plan_vocabularies,
)
from graph.plan.options import PlanContext, PlanOptions
from graph.plan.ports import PlanStore
from graph.plan.reader import PlanReader
from graph.plan.rules import PlanRules, load_plan_rules
from graph.plan.run import STEPS, StepFunctions, run_plan, steps
from graph.plan.schema import ensure_plan_schema

__all__ = [
    "STEPS",
    "AreaKind",
    "AreaStatus",
    "BlockReason",
    "BuildChangeReport",
    "ChangedSpecEdge",
    "ChangedSpecNode",
    "Claim",
    "Complexity",
    "DoneWhenSource",
    "FilePlanStore",
    "InterfaceKind",
    "MeaningRevisions",
    "NotBuildableReason",
    "Phase",
    "PlanArea",
    "PlanCheckMetrics",
    "PlanCheckReport",
    "PlanContext",
    "PlanDag",
    "PlanEstimate",
    "PlanLabel",
    "PlanMode",
    "PlanOptions",
    "PlanReader",
    "PlanRelation",
    "PlanResult",
    "PlanRules",
    "PlanRulesError",
    "PlanRunRecord",
    "PlanStepEstimate",
    "PlanStore",
    "PlanStory",
    "PlanTask",
    "PlannedBy",
    "StepFunctions",
    "StoryDetail",
    "StoryKind",
    "StoryProgress",
    "StoryStatus",
    "StoryWork",
    "TaskBrief",
    "TaskLifecycle",
    "TaskStateError",
    "TaskStatus",
    "check_plan",
    "ensure_plan_schema",
    "estimate_plan",
    "load_plan_rules",
    "open_plan_store",
    "plan_ontology_version",
    "plan_vocabularies",
    "run_plan",
    "steps",
    "topological_order",
    "transitive_prerequisites",
]
