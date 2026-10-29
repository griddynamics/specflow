"""The ontology: file format, loading and validation, and the compiled ``Ontology`` (docs/graph/ontology.md).

The shipped build file is ``spec-ontology.yaml`` next to this module; judge prompts live in
``judge_prompts/`` next to the ontology file that references them.
"""

from __future__ import annotations

from graph.core.ontology.compiled import (
    BUILT_IN_LABELS,
    BUILT_IN_NAMES,
    BUILT_IN_RELATIONS,
    AttributeSpec,
    DeriveRule,
    Dropped,
    DropReason,
    FingerprintPart,
    IdentityRule,
    InferenceRule,
    InferenceSpec,
    Legalised,
    NodeGroup,
    NodeType,
    Ontology,
    Pair,
    Relation,
    Spine,
    SpinePath,
)
from graph.core.ontology.loader import NAME_PATTERN, load_ontology

__all__ = [
    "BUILT_IN_LABELS",
    "BUILT_IN_NAMES",
    "BUILT_IN_RELATIONS",
    "NAME_PATTERN",
    "AttributeSpec",
    "DeriveRule",
    "DropReason",
    "Dropped",
    "FingerprintPart",
    "IdentityRule",
    "InferenceRule",
    "InferenceSpec",
    "Legalised",
    "NodeGroup",
    "NodeType",
    "Ontology",
    "Pair",
    "Relation",
    "Spine",
    "SpinePath",
    "load_ontology",
]
