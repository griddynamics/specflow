"""Builds and updates the graph from a project's documents (docs/graph/build.md).

Public names are re-exported here as their modules land; step artifacts stay in ``graph.build.models``.
The api.md step functions are on ``steps`` (``graph.build.steps.resolve``): exported directly, the
names ``resolve``, ``conform``, ``diff`` and ``persist`` would shadow the modules of the same name.
"""

from __future__ import annotations

from graph.build.files import (
    FileBuildStore,
    FileCorpusSource,
    FileExtractionStore,
    FileStores,
    open_file_stores,
)
from graph.build.models import (
    STEPS,
    BuildEstimate,
    BuildMetrics,
    BuildMode,
    BuildResult,
    BuildStatus,
    InferenceReport,
    SpecChanges,
    StepEstimate,
    StepName,
    VerificationReport,
    VerificationSummary,
)
from graph.build.options import BuildContext, BuildOptions, BuildSettings, FragmentTokens, Locality
from graph.build.ports import (
    EXTRACTION_KINDS,
    BuildRecord,
    BuildStore,
    CorpusRevision,
    CorpusSource,
    ExtractionKind,
    ExtractionStore,
    StepState,
)
from graph.build.run import STEP_FUNCTIONS, StepFunctions, estimate_build, plan, run_build, steps
from graph.build.verify import verify_project

__all__ = [
    "EXTRACTION_KINDS",
    "STEPS",
    "STEP_FUNCTIONS",
    "BuildContext",
    "BuildEstimate",
    "BuildMetrics",
    "BuildMode",
    "BuildOptions",
    "BuildRecord",
    "BuildResult",
    "BuildSettings",
    "BuildStatus",
    "BuildStore",
    "CorpusRevision",
    "CorpusSource",
    "ExtractionKind",
    "ExtractionStore",
    "FileBuildStore",
    "FileCorpusSource",
    "FileExtractionStore",
    "FileStores",
    "FragmentTokens",
    "InferenceReport",
    "Locality",
    "SpecChanges",
    "StepEstimate",
    "StepFunctions",
    "StepName",
    "StepState",
    "VerificationReport",
    "VerificationSummary",
    "estimate_build",
    "open_file_stores",
    "plan",
    "run_build",
    "steps",
    "verify_project",
]
