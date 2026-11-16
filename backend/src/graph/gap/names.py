"""Names and limits of ``graph.gap``: the gap vocabularies (docs/graph/gap/gap-ontology.yaml) and the first-slice
findings names (docs/graph/gap.md), kept until the migration of first-slice findings removes them.

The literals below repeat the vocabulary values of the shipped ontology file; ``gap_vocabularies`` reads the file so a
test holds the two equal. Spec-side names (build types, relations and attributes) never appear here: they come from
the rules file (FR-GONT-04).
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Final, Literal, get_args

import yaml

# Gap types and relations (owner gap, docs/graph/gap/ontology.md).

GAP: Final = "Gap"
CLARIFICATION: Final = "Clarification"
GAP_PREFIX: Final = "GAP::"
IMPACTS: Final = "IMPACTS"
CITES: Final = "CITES"
FOLLOWS_UP: Final = "FOLLOWS_UP"
ANSWERS: Final = "ANSWERS"
GROUNDED_IN: Final = "GROUNDED_IN"
PLACED_IN: Final = "PLACED_IN"
REPLACES: Final = "REPLACES"
AI_ACTOR: Final = "ai"
"""The ``answered_by`` of an answer a gap run makes (AI or pointer)."""
RUN_ACTOR: Final = "gap-run"
"""The actor of the events a gap run writes, and the ``created_by`` of the gaps it records."""
OPEN_STATUSES: Final[tuple[str, ...]] = ("open", "answered", "applying")
"""Gaps the inbox and the plan's findings port show as open (lifecycle.md "Plan link")."""
GRAPH_STATUSES: Final[tuple[str, ...]] = ("open", "answered", "applying", "resolved")
CLR_PREFIX: Final = "CLR::"

GapLabel = Literal["Gap", "Clarification"]
GapRelation = Literal[
    "ABOUT",
    "IMPACTS",
    "CITES",
    "FOLLOWS_UP",
    "ANSWERS",
    "PLACED_IN",
    "GROUNDED_IN",
    "REPLACES",
]

GapKind = Literal[
    "missing",
    "ambiguous",
    "contradiction",
    "inconsistency",
    "open_question",
    "assumption",
]
GapSeverity = Literal["blocker", "major", "minor"]
GapStatus = Literal["open", "answered", "applying", "resolved"]
GapOrigin = Literal["analysis", "development", "person"]
GapRoute = Literal["ask", "ai"]
GapResolution = Literal["answered", "ai_answered", "pointer", "follow_up"]
ClarificationMode = Literal["option", "own", "ai", "pointer"]
ClarificationPlacement = Literal["amend", "document", "none"]
ClarificationStatus = Literal["pending", "applied", "absorbed", "conflict", "orphaned", "reverted", "superseded"]

# Values that are not graph vocabularies: a report's severity may be ``info`` (a run note, never a gap), and the
# relational store's own enumerations (docs/graph/gap/data-model.md "Relational store").
ReportSeverity = Literal["blocker", "major", "minor", "info"]
RunMode = Literal["update", "full", "site"]
RunStatus = Literal["running", "completed", "degraded", "failed"]
FinishedRunStatus = Literal["completed", "degraded", "failed"]
SiteOutcome = Literal["gap", "not_a_gap", "not_spec", "duplicate", "skipped", "deferred"]
Relevance = Literal["spec", "tracking", "mixed"]
DecidedBy = Literal["rules", "model", "person"]
NoiseScope = Literal["document", "section", "pattern"]
AmendmentOperation = Literal["insert_after", "replace", "append_section"]
AmendmentStatus = Literal["pending", "active", "retired", "suspended", "orphaned", "reverted", "undecided"]
"""``pending``: recorded with an answer, not yet applied; the apply step makes it ``active`` (answers.md "Applying
answers")."""
ReconcileOutcome = Literal["applied", "reanchored", "absorbed", "conflict", "orphaned", "undecided"]
"""``undecided``: the same-meaning judge failed; the amendment stays applied until the next reconcile (FR-GREC-08)."""
FollowUpReason = Literal["not_fixed", "orphaned", "conflict", "reverted"]
Verdict = Literal["gap", "not_a_gap", "not_spec", "duplicate_of"]
EditPlacement = Literal["amend", "document"]
DetectorFamily = Literal["structure", "text"]
SeedStrength = Literal["strong", "weak"]
InvestigatorName = Literal["loop", "agents"]
GatewayRole = Literal["gap_relevance", "gap_investigate", "gap_judge", "gap_edits"]

GAP_LABELS: Final[tuple[GapLabel, ...]] = get_args(GapLabel)
GAP_RELATIONS: Final[tuple[GapRelation, ...]] = get_args(GapRelation)
GAP_KINDS: Final[tuple[GapKind, ...]] = get_args(GapKind)
GATEWAY_ROLES: Final[tuple[GatewayRole, ...]] = get_args(GatewayRole)

# The vocabulary of each gap literal, by its name in the ontology file.
VOCABULARY_LITERALS: Final[Mapping[str, object]] = MappingProxyType(
    {
        "gap_kind": GapKind,
        "gap_severity": GapSeverity,
        "gap_status": GapStatus,
        "gap_origin": GapOrigin,
        "gap_route": GapRoute,
        "gap_resolution": GapResolution,
        "clarification_mode": ClarificationMode,
        "clarification_placement": ClarificationPlacement,
        "clarification_status": ClarificationStatus,
    }
)


def _ontology_data() -> dict[str, object]:
    data = yaml.safe_load((files("graph.gap") / "ontology.yaml").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("the gap ontology must be a mapping")
    return data


@lru_cache(maxsize=1)
def gap_vocabularies() -> Mapping[str, tuple[str, ...]]:
    """The shipped gap ontology's vocabulary values, in file order."""

    values = _ontology_data().get("vocabularies")
    if not isinstance(values, dict):
        raise TypeError("the gap ontology must declare vocabularies")
    return MappingProxyType({name: tuple(value) for name, value in values.items()})


# First slice: findings (docs/graph/gap.md), kept until the migration of first-slice findings.

FINDING: Final = "Finding"
EXTERNAL_REF: Final = "ExternalRef"
ABOUT: Final = "ABOUT"
REFERS_TO: Final = "REFERS_TO"
OWNER: Final = "gap"
AGENT: Final = "agent"
FINDING_PREFIX: Final = "FND::"

FindingKind = Literal["gap", "conflict", "question", "note"]
FindingStatus = Literal["open", "resolved", "dismissed"]
RefType = Literal["pr", "commit", "file", "url"]

KINDS: Final[tuple[FindingKind, ...]] = ("gap", "conflict", "question", "note")
REF_TYPES: Final[tuple[RefType, ...]] = ("pr", "commit", "file", "url")
STATUSES: Final[tuple[FindingStatus, ...]] = ("open", "resolved", "dismissed")

# The kind of a blocking finding to the block reason the plan stores ("Blocking link"); ``note`` never blocks.
BLOCK_REASONS: Final[dict[str, str]] = {
    "question": "spec_ambiguous",
    "conflict": "spec_contradiction",
    "gap": "spec_missing",
}

TEXT_LIMIT: Final = 2000
ABOUT_LIMIT: Final = 20
REFS_LIMIT: Final = 10
OPEN_LIMIT: Final = 50
NOTE_LIMIT: Final = 500
AUTHOR_LIMIT: Final = 200
STORY_LIMIT: Final = 200
FILE_LIMIT: Final = 500
URL_LIMIT: Final = 2000
PAGE_LIMITS: Final = (1, 200)
