"""Ontology tests."""

from __future__ import annotations

import copy
import re
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

import graph.core.ontology as ontology_package
import graph.core.ontology.loader as loader_module
from graph.core.errors import ConformanceError, OntologyError
from graph.core.ontology import (
    BUILT_IN_NAMES,
    AnsweredElsewhereSpec,
    ConformSpec,
    Dropped,
    IdentityRule,
    Legalised,
    Ontology,
    SpinePath,
    load_ontology,
)
from graph_support import FROZEN_ONTOLOGY_FILE, GAP_ONTOLOGY, copy_frozen_prompts, load_frozen_ontology

SHIPPED_DIR = Path(ontology_package.__file__).parent
SHIPPED_FILE = SHIPPED_DIR / "spec-ontology.yaml"
SHIPPED_PROMPTS = SHIPPED_DIR / "judge_prompts"
SHIPPED_DATA: dict[str, Any] = yaml.safe_load(SHIPPED_FILE.read_text(encoding="utf-8"))
BASE: dict[str, Any] = yaml.safe_load(FROZEN_ONTOLOGY_FILE.read_text(encoding="utf-8"))
"""The flag-off ontology file, parsed: the base every bad file mutates."""

REQUIREMENT_LABELS = [
    "BusinessRule",
    "Constraint",
    "DataRequirement",
    "FunctionalRequirement",
    "IntegrationRequirement",
    "QualityRequirement",
]
EXTRACTED_LABELS = sorted(
    ["Module", "Feature", "Capability", "Interface", "DataEntity", "OpenQuestion", *REQUIREMENT_LABELS]
)
RELATIONS = [
    "CARRIES",
    "CONSUMED_BY",
    "DEPENDS_ON",
    "DEPLOYED_TO",
    "GROUPED_BY",
    "INTEGRATES_WITH",
    "PART_OF",
    "PROVIDED_BY",
    "REALIZED_BY",
    "RELATES_TO",
    "STORED_IN",
]
"""Labels and relations of the shipped ontology."""

# Helpers: build a mutated copy of the shipped file


def _walk(data: Any, path: str) -> tuple[Any, str | int]:
    parts: list[str | int] = [int(part) if part.lstrip("-").isdigit() else part for part in path.split(".")]
    for part in parts[:-1]:
        data = data[part]
    return data, parts[-1]


def put(data: dict[str, Any], path: str, value: Any) -> None:
    container, last = _walk(data, path)
    container[last] = value


def drop(data: dict[str, Any], path: str) -> None:
    container, last = _walk(data, path)
    del container[last]


def add(data: dict[str, Any], path: str, value: Any) -> None:
    container, last = _walk(data, path)
    container[last].append(value)


def rename(data: dict[str, Any], path: str, new_key: str) -> None:
    container, last = _walk(data, path)
    container[new_key] = container.pop(last)


def mutated(*edits: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    data = copy.deepcopy(BASE)
    for edit in edits:
        edit(data)
    return data


def write_ontology(
    directory: Path,
    data: dict[str, Any] | str,
    *,
    prompts: bool = True,
    filename: str = "ontology.yaml",
    prompts_from: Path | None = None,
) -> Path:
    """Write ``data`` as an ontology file in ``directory`` with the judge prompts of ``prompts_from``
    (default: the frozen ontology's, ``copy_frozen_prompts``) next to it."""

    directory.mkdir(parents=True, exist_ok=True)
    if prompts and not (directory / "judge_prompts").exists():
        if prompts_from is None:
            copy_frozen_prompts(directory)
        else:
            shutil.copytree(prompts_from, directory / "judge_prompts")
    path = directory / filename
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def load_problems(path: Path | None, submodule_files: Sequence[Path] | None = ()) -> tuple[str, ...]:
    """The problems ``load_ontology`` reports; empty when the files load."""

    try:
        load_ontology(path, submodule_files=submodule_files)
    except OntologyError as error:
        return error.problems
    return ()


def has_problem(problems: Sequence[str], *fragments: str) -> bool:
    return any(all(fragment in problem for fragment in fragments) for problem in problems)


@pytest.fixture(scope="module")
def shipped() -> Ontology:
    return load_ontology(submodule_files=[])


# The one gap submodule file of the tests (graph_support); deep-copied before any mutation.
GAP_FILE: dict[str, Any] = GAP_ONTOLOGY


# the shipped file loads


def test_shipped_ontology_loads_with_twelve_types_and_eleven_relations(shipped: Ontology) -> None:
    assert len(shipped.node_types) == 12
    assert len(shipped.relations) == 11
    assert shipped.labels == tuple(EXTRACTED_LABELS)
    assert shipped.extracted_labels == tuple(EXTRACTED_LABELS)
    assert shipped.extracted_relations == tuple(RELATIONS)
    assert shipped.name == "spec-ontology"
    assert shipped.version == "2.0"
    assert shipped.framing == "a software specification"
    assert "people" in shipped.exclude


def test_load_ontology_without_arguments_has_exactly_the_shipped_extracted_surface() -> None:
    """: no installed submodule ships an ontology file yet, so discovery adds nothing."""

    discovered = load_ontology()
    assert discovered.extracted_labels == tuple(EXTRACTED_LABELS)
    assert len(discovered.extracted_labels) == 12
    assert discovered.extracted_relations == tuple(RELATIONS)
    assert len(discovered.extracted_relations) == 11


def test_default_discovery_merges_the_ontology_file_of_each_submodule_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """: ``graph/<sub>/ontology.yaml`` package data is found through ``importlib.resources``."""

    root = tmp_path / "graph"
    shutil.copytree(SHIPPED_DIR, root / "core" / "ontology")
    write_ontology(root / "gap", GAP_FILE, prompts=False)
    (root / "plan").mkdir()  # a submodule without an ontology file
    write_ontology(root / "_private", GAP_FILE, prompts=False)  # skipped: not a submodule
    (root / "ontology.yaml").write_text("not: a submodule\n", encoding="utf-8")  # a file, not a package
    asked: list[str] = []

    def package_files(package: str) -> Path:
        asked.append(package)
        return root

    monkeypatch.setattr(loader_module, "files", package_files)
    merged = load_ontology()
    assert set(asked) == {"graph"}
    assert merged.owner_of("Finding") == "gap"
    assert merged.is_legal("CITES", "Finding", "Fragment")
    assert len(merged.node_types) == 13 and len(merged.relations) == 13
    assert merged.extracted_labels == tuple(EXTRACTED_LABELS)

    (root / "gap" / "ontology.yaml").unlink()
    assert len(load_ontology().node_types) == 12


def test_shipped_ontology_has_five_inference_rules_with_resolved_judge_prompts(
    shipped: Ontology,
) -> None:
    rules = shipped.inference.rules
    assert [rule.id for rule in rules] == ["INF-CONT-01", "INF-CONT-02", "INF-INT-01", "INF-CAP-01", "INF-XDOC-01"]
    assert {rule.template for rule in rules} == {"endpoints", "realization", "attach_orphan", "cross_document"}
    assert shipped.inference.cap == 100  #  (was 50)
    assert (shipped.inference.first_cap, shipped.inference.first_budget) == (2000, 11_000_000)
    assert shipped.inference.floor == 0.7
    conform = shipped.conform_spec()
    assert conform is not None and conform.answered_elsewhere is not None
    refs = {rule.prompt for rule in rules} | {conform.judge_prompt, conform.answered_elsewhere.prompt}
    # INF-CONT-01/02 name their batch prompt without ``batch`` (off), so a replay can turn batching on.
    refs |= {rule.batch_prompt for rule in rules if rule.batch_prompt}
    assert [(rule.batch, rule.batch_prompt) for rule in rules if rule.template == "attach_orphan"] == [
        (None, "judge_attach_orphan_batch")
    ] * 2
    for ref in refs:
        text = shipped.judge_prompt(ref)
        assert text.strip()
        assert text == (SHIPPED_PROMPTS / f"{ref}.md").read_text(encoding="utf-8")
    assert set(shipped.judge_prompts) == refs
    assert sorted(path.stem for path in SHIPPED_PROMPTS.glob("*.md")) == sorted(refs)


def test_judge_prompt_unknown_reference_is_a_conformance_error(shipped: Ontology) -> None:
    with pytest.raises(ConformanceError) as raised:
        shipped.judge_prompt("judge_nothing")
    assert raised.value.code == "ontology_unknown_judge_prompt"


def test_shipped_file_loads_from_a_copy_with_its_prompts(tmp_path: Path) -> None:
    assert load_problems(write_ontology(tmp_path, SHIPPED_DATA, prompts_from=SHIPPED_PROMPTS)) == ()


def test_frozen_file_loads_from_a_copy_with_its_prompts(tmp_path: Path) -> None:
    assert load_problems(write_ontology(tmp_path, BASE)) == ()


def test_inference_rules_resolve_groups_to_labels(shipped: Ontology) -> None:
    by_id = {rule.id: rule for rule in shipped.inference.rules}
    assert by_id["INF-CONT-01"].subject == frozenset(REQUIREMENT_LABELS)
    assert by_id["INF-CONT-01"].targets == frozenset({"Feature", "Module"})
    assert by_id["INF-CAP-01"].subject == frozenset({"Capability"})
    assert by_id["INF-INT-01"].writes == "INTEGRATES_WITH"
    assert by_id["INF-INT-01"].via == ("PART_OF", "RELATES_TO") and by_id["INF-INT-01"].mentions
    assert by_id["INF-INT-01"].exclude_owned_by == frozenset({"Interface"})
    assert by_id["INF-CONT-02"].subject == frozenset({"Feature"})
    assert by_id["INF-CONT-02"].targets == frozenset({"Module"})
    for rule_id in ("INF-CONT-01", "INF-CONT-02"):
        assert by_id[rule_id].exclude_where == {"in_scope": frozenset({"no"})}
        assert (by_id[rule_id].prompt, by_id[rule_id].writes) == ("judge_attach_orphan", "PART_OF")
    assert by_id["INF-CAP-01"].container_paths == (
        SpinePath(("PART_OF",), sequence=False, min_hops=1, max_hops=3),
        SpinePath(("RELATES_TO",), sequence=False, min_hops=1, max_hops=1),
        SpinePath(("PART_OF", "PROVIDED_BY"), sequence=True, min_hops=2, max_hops=2),
        SpinePath(("PART_OF", "CONSUMED_BY"), sequence=True, min_hops=2, max_hops=2),
    )
    xdoc = by_id["INF-XDOC-01"]
    assert (xdoc.template, xdoc.prompt) == ("cross_document", "judge_cross_document")
    assert xdoc.subject == frozenset({"Module", "Feature", "Interface", "DataEntity"})
    assert xdoc.targets == frozenset(REQUIREMENT_LABELS)
    assert xdoc.via == ("PART_OF", "RELATES_TO") and xdoc.mentions
    assert (xdoc.min_documents, xdoc.max_candidates, xdoc.max_pairs, xdoc.pair_floor) == (2, 3, 8, 0.55)
    assert xdoc.exclude_where == {"in_scope": frozenset({"no"})}
    assert xdoc.written == ("RELATES_TO", "DEPENDS_ON", "PART_OF")
    assert xdoc.cap == 36 and shipped.inference.cap_of(xdoc) == 36  # in every mode
    assert xdoc.first_cap is None and shipped.inference.first_cap_of(xdoc) == 2000
    assert by_id["INF-CAP-01"].reads == ("INF-CONT-01", "INF-CONT-02")
    assert shipped.inference.reads_of(by_id["INF-CAP-01"]) == (by_id["INF-CONT-01"], by_id["INF-CONT-02"])
    assert all(rule.reads == () for rule in shipped.inference.rules if rule.id != "INF-CAP-01")
    assert all(rule.batch is None and rule.first_cap is None for rule in shipped.inference.rules)  # R0b decides
    assert all(shipped.inference.cap_of(rule) == 100 for rule in shipped.inference.rules if rule is not xdoc)
    assert xdoc.floor == 0.85 and shipped.inference.floor_of(xdoc) == 0.85
    assert by_id["INF-CONT-01"].floor == 0.8 and shipped.inference.floor_of(by_id["INF-CONT-01"]) == 0.8
    for rule_id in ("INF-CONT-02", "INF-INT-01", "INF-CAP-01"):
        assert by_id[rule_id].floor is None and shipped.inference.floor_of(by_id[rule_id]) == 0.7
    assert by_id["INF-CAP-01"].prompt == "judge_realization"
    assert shipped.cue_gated_rules("DEPENDS_ON") == frozenset({"INF-XDOC-01"})


def test_dependency_cues_of_the_shipped_ontology_match_whole_words(shipped: Ontology) -> None:
    for text in (
        "A refund requires a cancelled order.",
        "Refunds require order cancellation.",
        "Export depends on the reporting service.",
        "Invoicing is dependent on order totals.",
        "A refund is accepted only after the order is cancelled.",
        "An approved order is a prerequisite for shipping.",
        "The order must be cancelled before a refund is issued.",
    ):
        assert shipped.cue_matches("DEPENDS_ON", text), text
    for text in (
        "Each requirement is reviewed.",
        "The field is required.",
        "Refunds and cancellations share a page.",
        "The order must be cancelled. Before that, the customer is asked.",
    ):
        assert not shipped.cue_matches("DEPENDS_ON", text), text


def test_spine_and_hubs_of_the_shipped_ontology(shipped: Ontology) -> None:
    assert shipped.spine.roots == frozenset({"Module"})
    assert shipped.spine.sources == frozenset(REQUIREMENT_LABELS)
    assert shipped.spine_exclude() == {"in_scope": frozenset({"no"})}
    assert shipped.spine_relations == frozenset(
        {"PART_OF", "PROVIDED_BY", "CONSUMED_BY", "STORED_IN", "GROUPED_BY", "REALIZED_BY", "RELATES_TO"}
    )
    assert shipped.hub_labels == frozenset({"Module", "Feature", "Capability", "Interface", "DataEntity"})
    assert len(shipped.spine.paths) == 7


def test_conform_section_of_the_shipped_ontology(shipped: Ontology) -> None:
    answered = AnsweredElsewhereSpec("OpenQuestion", "judge_answered_elsewhere", 4, 0.35, 0.8, 60)
    assert shipped.conform_spec() == ConformSpec("judge_owner_conflict", 200, 0.7, "RELATES_TO", answered)


# Format 2 flags: one bad file per flag (the valid case of each is in test_graph_ontology_v2.py)

PART_OF_PAIRS = "edge_types.PART_OF.pairs"
FR_TO_MODULE = f"{PART_OF_PAIRS}.1"  # functional_requirement -> module
FEATURE_TO_MODULE = f"{PART_OF_PAIRS}.7"
MODULE_TO_MODULE = f"{PART_OF_PAIRS}.9"
CONFORM = {"judge_prompt": "judge_endpoints", "cap": 10, "floor": 0.5, "fallback_relation": "RELATES_TO"}


def owner_pairs(d: dict[str, Any]) -> None:
    """Make requirement -> feature and requirement -> module owner pairs (as format 2 needs for inherit)."""

    for index in range(7):
        put(d, f"{PART_OF_PAIRS}.{index}.owner", value=True)


V2_BAD_FILES = [
    # Attributes: default, inherit
    pytest.param(
        [lambda d: put(d, "node_groups.requirement.attributes.is_assumption.default", "maybe")],
        "is_assumption.default",
        "default 'maybe' is not one of the values",
        id="check3-attribute-default-value",
    ),
    pytest.param(
        [lambda d: put(d, "node_groups.requirement.attributes.acceptance_criteria.default", "none")],
        "acceptance_criteria.default",
        "not allowed for a 'text' attribute",
        id="check1-attribute-default-on-text",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.feature.attributes.module.inherit", {"via": "PART_OF"})],
        "attributes.module.inherit",
        "not allowed for a 'ref' attribute",
        id="check1-attribute-inherit-on-ref",
    ),
    pytest.param(
        [
            owner_pairs,
            lambda d: put(d, "node_groups.requirement.attributes.acceptance_criteria.inherit", {"via": "NOPE"}),
        ],
        "node_groups.requirement.attributes.acceptance_criteria.inherit.via",
        "'NOPE' is not a declared relation",
        id="check3-inherit-relation",
    ),
    pytest.param(
        [lambda d: put(d, "node_groups.requirement.attributes.acceptance_criteria.inherit", {"via": "PART_OF"})],
        "node_groups.requirement.attributes.acceptance_criteria.inherit.via",
        "'PART_OF' is not the relation of an owner pair of BusinessRule",
        id="check13-inherit-without-owner-pair",
    ),
    # Pairs: owner, owner_rank
    pytest.param(
        [lambda d: put(d, f"{FR_TO_MODULE}.owner_rank", 0)],
        f"{FR_TO_MODULE}.owner_rank",
        "Input should be greater than or equal to 1",
        id="check1-owner-rank-type",
    ),
    pytest.param(
        [lambda d: put(d, f"{FR_TO_MODULE}.owner_rank", 2)],
        "edge_types.PART_OF.pairs[1].owner_rank",
        "applies only to an owner pair",
        id="check1-owner-rank-without-owner",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.RELATES_TO.pairs.0.owner", value=True)],
        "edge_types.RELATES_TO.pairs[0]",
        "a symmetric relation may not have an owner pair",
        id="check10-symmetric-owner",
    ),
    # Pairs: the default_from flag
    pytest.param(
        [lambda d: put(d, f"{FR_TO_MODULE}.default_from", ["GROUPED_BY"])],
        "edge_types.PART_OF.pairs[1].default_from",
        "applies only to a default pair",
        id="check4-default-from-without-default",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.INTEGRATES_WITH.pairs.0.default_from", ["NOPE"])],
        "edge_types.INTEGRATES_WITH.pairs[0].default_from",
        "'NOPE' is not a declared relation",
        id="check3-default-from-relation",
    ),
    # Pairs: source_where, target_where, where_reason, otherwise
    pytest.param(
        [lambda d: put(d, f"{FR_TO_MODULE}.target_where", {"colour": ["red"]})],
        "edge_types.PART_OF.pairs[1].target_where",
        "attribute 'colour' is not declared on Module",
        id="check3-target-where-attribute",
    ),
    pytest.param(
        [lambda d: put(d, f"{FR_TO_MODULE}.source_where", {"stmt": ["x"]})],
        "edge_types.PART_OF.pairs[1].source_where",
        "attribute 'stmt' of FunctionalRequirement is not an enum attribute",
        id="check3-source-where-text-attribute",
    ),
    pytest.param(
        [lambda d: put(d, f"{FR_TO_MODULE}.target_where", {"external": ["maybe"]})],
        "edge_types.PART_OF.pairs[1].target_where",
        "'maybe' is not a value of Module.external",
        id="check3-target-where-value",
    ),
    pytest.param(
        [lambda d: put(d, f"{FR_TO_MODULE}.target_where", {"external": []})],
        f"{FR_TO_MODULE}.target_where.external",
        "List should have at least 1 item",
        id="check1-target-where-empty",
    ),
    pytest.param(
        [lambda d: put(d, f"{FR_TO_MODULE}.target_where", {"external": [False]})],
        f"{FR_TO_MODULE}.target_where.external.0",
        "Input should be a valid string",
        id="check1-target-where-unquoted-no",
    ),
    pytest.param(
        [lambda d: put(d, f"{FR_TO_MODULE}.where_reason", "because")],
        "edge_types.PART_OF.pairs[1].where_reason",
        "where_reason 'because' is not a quarantine reason",
        id="check11-where-reason",
    ),
    pytest.param(
        [lambda d: put(d, f"{FR_TO_MODULE}.otherwise", "NOPE")],
        "edge_types.PART_OF.pairs[1].otherwise",
        "'NOPE' is not a declared relation",
        id="check3-otherwise-relation",
    ),
    pytest.param(
        [lambda d: put(d, f"{FEATURE_TO_MODULE}.otherwise", "RELATES_TO")],
        "edge_types.PART_OF.pairs[7].otherwise",
        "otherwise 'RELATES_TO' is not legal for the ends of this pair",
        id="check11-otherwise-illegal",
    ),
    pytest.param(
        [
            lambda d: put(d, "edge_types.PART_OF.pairs.0.otherwise", "GROUPED_BY"),
            lambda d: put(d, "edge_types.GROUPED_BY.stated_only", value=True),
        ],
        "edge_types.PART_OF.pairs[0].otherwise",
        "otherwise 'GROUPED_BY' is a stated_only relation",
        id="check11-otherwise-stated-only",
    ),
    # Pairs: max_depth, not_across, distinct_owner, redundant_via_owner (booleans: type only)
    pytest.param(
        [lambda d: put(d, f"{MODULE_TO_MODULE}.max_depth", 0)],
        f"{MODULE_TO_MODULE}.max_depth",
        "Input should be greater than or equal to 1",
        id="check1-max-depth",
    ),
    pytest.param(
        [lambda d: put(d, f"{MODULE_TO_MODULE}.not_across", ["colour"])],
        "edge_types.PART_OF.pairs[9].not_across",
        "attribute 'colour' is not declared on Module",
        id="check3-not-across",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.RELATES_TO.pairs.0.distinct_owner", "yes")],
        "edge_types.RELATES_TO.pairs.0.distinct_owner",
        "Input should be a valid boolean",
        id="check1-distinct-owner",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.GROUPED_BY.pairs.1.redundant_via_owner", 1)],
        "edge_types.GROUPED_BY.pairs.1.redundant_via_owner",
        "Input should be a valid boolean",
        id="check1-redundant-via-owner",
    ),
    # Pairs: several defaults
    pytest.param(
        [
            lambda d: put(d, f"{MODULE_TO_MODULE}.default", value=True),
            lambda d: put(d, f"{MODULE_TO_MODULE}.target_where", {"type": ["backend", "deployment"]}),
            lambda d: put(d, "edge_types.INTEGRATES_WITH.pairs.0.target_where", {"type": ["backend"]}),
        ],
        "edge_types.PART_OF.pairs[9]: pair Module -> Module already has default relation INTEGRATES_WITH",
        "with a where condition that is not disjoint",
        id="check4-defaults-overlap",
    ),
    # Relations: acyclic, stated_only, derive_via
    pytest.param(
        [lambda d: put(d, "edge_types.GROUPED_BY.acyclic", "yes")],
        "edge_types.GROUPED_BY.acyclic",
        "Input should be a valid boolean",
        id="check1-acyclic",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.PART_OF.stated_only", value=True)],
        "edge_types.PART_OF",
        "a stated_only relation may not have derive",
        id="check12-stated-only-derive",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.INTEGRATES_WITH.stated_only", value=True)],
        "edge_types.INTEGRATES_WITH",
        "a stated_only relation may not have a default pair",
        id="check12-stated-only-default",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.INTEGRATES_WITH.stated_only", value=True)],
        "inference.rules[0].writes",
        "relation 'INTEGRATES_WITH' is stated_only; no inference rule may write it",
        id="check12-stated-only-inference",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.RELATES_TO.stated_only", value=True)],
        "edge_types.RELATES_TO",
        "a stated_only relation may not have id_references",
        id="check12-stated-only-id-references",
    ),
    pytest.param(
        [
            lambda d: put(d, "edge_types.GROUPED_BY.stated_only", value=True),
            lambda d: put(d, "edge_types.GROUPED_BY.derive_via", {"consumer": "PART_OF", "provider": "PART_OF"}),
        ],
        "edge_types.GROUPED_BY",
        "a stated_only relation may not have derive_via",
        id="check12-stated-only-derive-via",
    ),
    pytest.param(
        [
            lambda d: put(
                d, "edge_types.INTEGRATES_WITH.derive_via", {"consumer": "REALIZED_BY", "provider": "REALIZED_BY"}
            ),
            lambda d: put(d, "edge_types.INTEGRATES_WITH.acyclic", value=True),
        ],
        "edge_types.INTEGRATES_WITH",
        "a derive_via relation may not be acyclic",
        id="check14-derive-via-acyclic",
    ),
    pytest.param(
        [
            lambda d: put(
                d, "edge_types.INTEGRATES_WITH.derive_via", {"consumer": "REALIZED_BY", "provider": "REALIZED_BY"}
            ),
            lambda d: put(d, "edge_types.INTEGRATES_WITH.pairs.0.exclusive", value=True),
        ],
        "edge_types.INTEGRATES_WITH",
        "a derive_via relation may not have an exclusive pair",
        id="check14-derive-via-exclusive",
    ),
    pytest.param(
        [
            owner_pairs,
            lambda d: put(d, "conform", {**CONFORM, "fallback_relation": "PART_OF"}),
            lambda d: put(d, f"{FR_TO_MODULE}.target_where", {"external": ["no"]}),
        ],
        "conform.fallback_relation",
        "fallback_relation 'PART_OF' has source_where or target_where on FunctionalRequirement -> Module",
        id="check14-fallback-where",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.INTEGRATES_WITH.derive_via", {"consumer": "NOPE", "provider": "REALIZED_BY"})],
        "edge_types.INTEGRATES_WITH.derive_via.consumer",
        "'NOPE' is not a declared relation",
        id="check3-derive-via-relation",
    ),
    pytest.param(
        [
            lambda d: put(
                d, "edge_types.INTEGRATES_WITH.derive_via", {"consumer": "GROUPED_BY", "provider": "REALIZED_BY"}
            )
        ],
        "edge_types.INTEGRATES_WITH.derive_via",
        "consumer GROUPED_BY and provider REALIZED_BY share no source label",
        id="check13-derive-via-no-hub",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.INTEGRATES_WITH.derive_via", {"consumer": "PART_OF", "provider": "PART_OF"})],
        "edge_types.INTEGRATES_WITH.derive_via",
        "derive_via yields Feature -> Feature",
        id="check13-derive-via-illegal",
    ),
    # Node types: supported_by, merge_across_labels
    pytest.param(
        [lambda d: put(d, "node_types.capability.supported_by", [{"relation": "NOPE", "direction": "in"}])],
        "node_types.capability.supported_by[0]",
        "'NOPE' is not a declared relation",
        id="check3-supported-by-relation",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.capability.supported_by", [{"relation": "PART_OF", "direction": "in"}])],
        "node_types.capability.supported_by[0]",
        "PART_OF has no pair to Capability",
        id="check3-supported-by-end",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.capability.supported_by", [{"relation": "PART_OF", "direction": "up"}])],
        "node_types.capability.supported_by.0.direction",
        "Input should be 'in' or 'out'",
        id="check1-supported-by-direction",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.capability.merge_across_labels", "no")],
        "node_types.capability.merge_across_labels",
        "Input should be a valid boolean",
        id="check1-merge-across-labels",
    ),
    # Spine exclude
    pytest.param(
        [lambda d: put(d, "spine.exclude", {"colour": ["red"]})],
        "spine.exclude",
        "attribute 'colour' is not declared on a subject or its owner",
        id="check3-spine-exclude-attribute",
    ),
    pytest.param(
        [lambda d: put(d, "spine.exclude", {"external": ["yes"]})],
        "spine.exclude",
        "attribute 'external' is not declared on a subject or its owner",
        id="check3-spine-exclude-owner-attribute-without-owner-pairs",
    ),
    pytest.param(
        [lambda d: put(d, "spine.exclude", {"is_assumption": ["maybe"]})],
        "spine.exclude",
        "'maybe' is not a value of BusinessRule.is_assumption",
        id="check3-spine-exclude-value",
    ),
    # Conform
    pytest.param(
        [lambda d: put(d, "conform", {**CONFORM, "judge_prompt": "judge_nothing"})],
        "conform.judge_prompt",
        "judge prompt file judge_prompts/judge_nothing.md not found",
        id="check7-conform-judge-prompt",
    ),
    pytest.param(
        [lambda d: put(d, "conform", {key: value for key, value in CONFORM.items() if key != "cap"})],
        "conform.cap",
        "required field missing",
        id="check1-conform-cap",
    ),
    pytest.param(
        [owner_pairs, lambda d: put(d, "conform", {**CONFORM, "fallback_relation": "NOPE"})],
        "conform.fallback_relation",
        "'NOPE' is not a declared relation",
        id="check3-conform-fallback-relation",
    ),
    pytest.param(
        [lambda d: put(d, "conform", CONFORM)],
        "conform.fallback_relation",
        "fallback_relation 'RELATES_TO' is not legal for the ends of any owner pair",
        id="check11-fallback-without-owner-pairs",
    ),
    pytest.param(
        [
            owner_pairs,
            lambda d: put(d, "conform", {**CONFORM, "fallback_relation": "GROUPED_BY"}),
            lambda d: put(d, "edge_types.GROUPED_BY.stated_only", value=True),
        ],
        "conform.fallback_relation",
        "fallback_relation 'GROUPED_BY' is a stated_only relation",
        id="check11-fallback-stated-only",
    ),
    pytest.param(
        [
            lambda d: put(d, "node_groups.requirement.attributes.is_assumption.inherit", {"via": "PART_OF"}),
            lambda d: put(d, "node_groups.requirement.attributes.is_assumption.default", "no"),
            owner_pairs,
        ],
        "node_groups.requirement.attributes.is_assumption",
        "an attribute may not have both a default and inherit",
        id="check13-default-and-inherit",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.RELATES_TO.pairs.1.target_where", {"external": ["no"]})],
        "edge_types.RELATES_TO.pairs[1]",
        "a symmetric relation may not have a source_where or target_where",
        id="check10-symmetric-where",
    ),
    # attach_orphan writes
    pytest.param(
        [lambda d: put(d, "inference.rules.2.targets", ["capability"])],
        "inference.rules[2].writes",
        "PART_OF is not legal for any subject -> target pair",
        id="check7-orphan-writes-illegal",
    ),
    pytest.param(
        [owner_pairs, lambda d: put(d, "inference.rules.2.writes", "RELATES_TO")],
        "inference.rules[2].writes",
        "RELATES_TO is not an owner pair for any subject -> target pair",
        id="check7-orphan-writes-not-owner",
    ),
    # Inference parameters: exclude_owned_by, container_paths, exclude_where
    pytest.param(
        [lambda d: put(d, "inference.rules.0.exclude_owned_by", ["nothing"])],
        "inference.rules[0].exclude_owned_by",
        "'nothing' is not a declared node type or group",
        id="check3-exclude-owned-by",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.2.exclude_owned_by", ["module"])],
        "inference.rules[2].exclude_owned_by",
        "not a parameter of template 'attach_orphan'",
        id="check7-exclude-owned-by-template",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.container_relation", "PART_OF")],
        "inference.rules.1.container_relation",
        "unknown key",
        id="check1-container-relation-removed",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.container_paths", [{"via": ["NOPE"]}])],
        "inference.rules[1].container_paths[0]",
        "'NOPE' is not a declared relation",
        id="check3-container-paths-relation",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.container_paths", [{"via": ["GROUPED_BY"]}])],
        "inference.rules[1].container_paths",
        "no path reaches a container from a member",
        id="check7-container-paths-unreachable",
    ),
    pytest.param(
        [lambda d: drop(d, "inference.rules.1.container_paths")],
        "inference.rules[1].container_paths",
        "required by template 'realization'",
        id="check7-container-paths-missing",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.2.exclude_where", {"colour": ["red"]})],
        "inference.rules[2].exclude_where",
        "attribute 'colour' is not declared on a subject or its owner",
        id="check3-exclude-where-attribute",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.2.exclude_where", {"is_assumption": ["maybe"]})],
        "inference.rules[2].exclude_where",
        "'maybe' is not a value of BusinessRule.is_assumption",
        id="check3-exclude-where-value",
    ),
]


# cross_document rules, also_writes, rule cap, the cue exception, answered_elsewhere (the valid
# case of each is in test_graph_ontology_cross_document.py)

XDOC = "inference.rules.3"
"""The cross-document rule ``xdoc`` appends after the three frozen rules."""


def xdoc(**changes: Any) -> Callable[[dict[str, Any]], None]:
    """Append a valid ``cross_document`` rule (with ``changes``; None removes a parameter)."""

    rule = {
        "id": "INF-XDOC-T",
        "template": "cross_document",
        "prompt": "judge_endpoints",
        "subject": ["module", "feature"],
        "members": "requirement",
        "via": ["PART_OF", "RELATES_TO"],
        "mentions": True,
        "min_documents": 2,
        "max_pairs": 8,
        "pair_floor": 0.5,
        "max_candidates": 3,
        "writes": "RELATES_TO",
        "also_writes": ["PART_OF"],
        **changes,
    }
    return lambda d: add(d, "inference.rules", {key: value for key, value in rule.items() if value is not None})


def depends_on(*cues: str) -> Callable[[dict[str, Any]], None]:
    """Declare a stated-only DEPENDS_ON between requirements, with ``cues``."""

    relation: dict[str, Any] = {
        "origin": "extracted",
        "tier": "spec",
        "stated_only": True,
        "pairs": [{"from": "requirement", "to": "requirement"}],
    }
    if cues:
        relation["cues"] = list(cues)
    return lambda d: put(d, "edge_types.DEPENDS_ON", relation)


def answered(**changes: Any) -> list[Callable[[dict[str, Any]], None]]:
    """Owner pairs and a conform section with an ``answered_elsewhere`` block (with ``changes``)."""

    block = {
        "label": "functional_requirement",
        "prompt": "judge_endpoints",
        "candidates": 3,
        "min_cosine": 0.5,
        "floor": 0.8,
        "cap": 60,
        **changes,
    }
    return [owner_pairs, lambda d: put(d, "conform", {**CONFORM, "answered_elsewhere": block})]


PKG4_BAD_FILES = [
    # cross_document parameters
    pytest.param(
        [xdoc(max_pairs=None)],
        "inference.rules[3].max_pairs",
        "required by template 'cross_document'",
        id="check7-cross-document-missing-parameter",
    ),
    pytest.param(
        [xdoc(target="module")],
        "inference.rules[3].target",
        "not a parameter of template 'cross_document'",
        id="check7-cross-document-unknown-parameter",
    ),
    pytest.param(
        [xdoc(subject=["module", "functional_requirement"])],
        "inference.rules[3].subject",
        "not a hub type: FunctionalRequirement",
        id="check7-cross-document-subject-not-a-hub",
    ),
    pytest.param(
        [xdoc(subject=[])],
        "inference.rules[3].subject",
        "needs at least one type or group key",
        id="check7-cross-document-subject-empty",
    ),
    pytest.param(
        [xdoc(subject=["module", "nothing"])],
        "inference.rules[3].subject[1]",
        "'nothing' is not a declared node type or group",
        id="check3-cross-document-subject",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.2.subject", ["requirement"])],
        "inference.rules[2].subject",
        "template 'attach_orphan' takes one type or group key",
        id="check7-subject-list-on-another-template",
    ),
    pytest.param(
        [xdoc(members="nothing")],
        "inference.rules[3].members",
        "'nothing' is not a declared node type or group",
        id="check3-cross-document-members",
    ),
    pytest.param(
        [xdoc(via=["PART_OF", "GROUPED_BY"])],
        "inference.rules[3].via[1]",
        "GROUPED_BY is not legal between a member and a subject",
        id="check7-cross-document-via-illegal",
    ),
    pytest.param(
        [xdoc(via=["PART_OF", "PART_OF"])],
        "inference.rules[3].via",
        "names a relation twice",
        id="check7-cross-document-via-twice",
    ),
    pytest.param(
        [xdoc(via=[])],
        "inference.rules[3].via",
        "needs at least one relation",
        id="check7-cross-document-via-empty",
    ),
    pytest.param(
        [xdoc(min_documents=1)],
        f"{XDOC}.min_documents",
        "Input should be greater than or equal to 2",
        id="check1-cross-document-min-documents",
    ),
    pytest.param(
        [xdoc(pair_floor=1.5)],
        f"{XDOC}.pair_floor",
        "Input should be less than or equal to 1",
        id="check1-cross-document-pair-floor",
    ),
    pytest.param(
        [xdoc(floor=1.5)],
        f"{XDOC}.floor",
        "Input should be less than or equal to 1",
        id="check1-rule-floor-above-one",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.0.floor", -0.1)],
        "inference.rules.0.floor",
        "Input should be greater than or equal to 0",
        id="check1-rule-floor-below-zero",
    ),
    pytest.param(
        [xdoc(max_pairs=0)],
        f"{XDOC}.max_pairs",
        "Input should be greater than or equal to 1",
        id="check1-cross-document-max-pairs",
    ),
    pytest.param(
        [xdoc(exclude_where={"colour": ["red"]})],
        "inference.rules[3].exclude_where",
        "attribute 'colour' is not declared on a subject or its owner",
        id="check3-cross-document-exclude-where",
    ),
    pytest.param(
        [xdoc(writes="GROUPED_BY", also_writes=None)],
        "inference.rules[3].writes",
        "GROUPED_BY is not legal between two members or from a member to an owner candidate",
        id="check7-cross-document-writes-illegal",
    ),
    # also_writes
    pytest.param(
        [lambda d: put(d, "inference.rules.2.also_writes", ["RELATES_TO"])],
        "inference.rules[2].also_writes",
        "not a parameter of template 'attach_orphan'",
        id="check7-also-writes-on-another-template",
    ),
    pytest.param(
        [xdoc(also_writes=["GROUPED_BY"])],
        "inference.rules[3].also_writes[0]",
        "GROUPED_BY is not legal between two members or from a member to an owner candidate",
        id="check7-also-writes-illegal",
    ),
    pytest.param(
        [xdoc(also_writes=["RELATES_TO"])],
        "inference.rules[3].also_writes[0]",
        "'RELATES_TO' is the relation the rule writes",
        id="check7-also-writes-repeats-writes",
    ),
    pytest.param(
        [xdoc(also_writes=["PART_OF", "PART_OF"])],
        "inference.rules[3].also_writes",
        "names a relation twice",
        id="check7-also-writes-twice",
    ),
    pytest.param(
        [xdoc(also_writes=["NOPE"])],
        "inference.rules[3].also_writes[0]",
        "'NOPE' is not a declared relation",
        id="check3-also-writes-relation",
    ),
    pytest.param(
        [xdoc(also_writes=[])],
        f"{XDOC}.also_writes",
        "List should have at least 1 item",
        id="check1-also-writes-empty",
    ),
    # The cue exception to stated_only (check 12)
    pytest.param(
        [depends_on(), xdoc(also_writes=["PART_OF", "DEPENDS_ON"])],
        "inference.rules[3].also_writes[1]",
        "relation 'DEPENDS_ON' is stated_only without cues",
        id="check12-also-writes-stated-only-without-cues",
    ),
    pytest.param(
        [depends_on("requires"), xdoc(writes="DEPENDS_ON", also_writes=["RELATES_TO"])],
        "inference.rules[3].writes",
        "relation 'DEPENDS_ON' is stated_only; no inference rule may write it",
        id="check12-cross-document-writes-stated-only",
    ),
    # Rule cap
    pytest.param(
        [lambda d: put(d, "inference.rules.0.cap", 0)],
        "inference.rules.0.cap",
        "Input should be greater than 0",
        id="check1-rule-cap",
    ),
    pytest.param(
        [xdoc(cap="many")],
        f"{XDOC}.cap",
        "Input should be a valid integer",
        id="check1-cross-document-cap-type",
    ),
    # conform.answered_elsewhere (check 15)
    pytest.param(
        answered(label="nothing"),
        "conform.answered_elsewhere.label",
        "'nothing' is not a declared node type",
        id="check15-answered-elsewhere-label-unknown",
    ),
    pytest.param(
        answered(label="module"),
        "conform.answered_elsewhere.label",
        "Module is not an extracted atomic type",
        id="check15-answered-elsewhere-label-not-atomic",
    ),
    pytest.param(
        answered(label="requirement"),
        "conform.answered_elsewhere.label",
        "'requirement' is not a declared node type",
        id="check15-answered-elsewhere-label-group",
    ),
    pytest.param(
        answered(prompt="judge_answered_elsewhere"),
        "conform.answered_elsewhere.prompt",
        "judge prompt file judge_prompts/judge_answered_elsewhere.md not found",
        id="check15-answered-elsewhere-prompt-missing",
    ),
    pytest.param(
        answered(candidates=0),
        "conform.answered_elsewhere.candidates",
        "Input should be greater than 0",
        id="check1-answered-elsewhere-candidates",
    ),
    pytest.param(
        answered(cap=0),
        "conform.answered_elsewhere.cap",
        "Input should be greater than 0",
        id="check1-answered-elsewhere-cap",
    ),
    pytest.param(
        answered(min_cosine=1.2),
        "conform.answered_elsewhere.min_cosine",
        "Input should be less than or equal to 1",
        id="check1-answered-elsewhere-min-cosine",
    ),
    pytest.param(
        answered(floor=-0.1),
        "conform.answered_elsewhere.floor",
        "Input should be greater than or equal to 0",
        id="check1-answered-elsewhere-floor",
    ),
    pytest.param(
        answered(colour="red"),
        "conform.answered_elsewhere.colour",
        "unknown key",
        id="check1-answered-elsewhere-unknown-key",
    ),
]


# inference limits by mode, rule reads, attach_orphan batch (frozen rules: 0 INF-INT-01 endpoints
# 1 INF-CAP-01 realization, 2 INF-CONT-01 attach_orphan; the valid cases are in the tests after BAD_FILES)

PKG5_BAD_FILES = [
    pytest.param(
        [lambda d: put(d, "inference.first_cap", 0)],
        "inference.first_cap",
        "Input should be greater than 0",
        id="check1-inference-first-cap",
    ),
    pytest.param(
        [lambda d: put(d, "inference.first_budget", "twelve million")],
        "inference.first_budget",
        "Input should be a valid integer",
        id="check1-inference-first-budget-type",
    ),
    pytest.param(
        [lambda d: put(d, "inference.first_budget", -1)],
        "inference.first_budget",
        "Input should be greater than 0",
        id="check1-inference-first-budget",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.0.first_cap", 0)],
        "inference.rules.0.first_cap",
        "Input should be greater than 0",
        id="check1-rule-first-cap",
    ),
    pytest.param(
        [xdoc(first_cap=2.5)],
        f"{XDOC}.first_cap",
        "Input should be a valid integer",
        id="check1-cross-document-first-cap-type",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.reads", [])],
        "inference.rules.1.reads",
        "List should have at least 1 item",
        id="check1-reads-empty",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.reads", "INF-INT-01")],
        "inference.rules.1.reads",
        "Input should be a valid list",
        id="check1-reads-type",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.reads", ["INF-CONT-01"])],
        "inference.rules[1].reads[0]",
        "'INF-CONT-01' is not a rule declared before 'INF-CAP-01'",
        id="check16-reads-a-later-rule",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.reads", ["INF-NOPE-01"])],
        "inference.rules[1].reads[0]",
        "'INF-NOPE-01' is not a rule declared before 'INF-CAP-01'",
        id="check16-reads-an-unknown-rule",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.reads", ["INF-INT-01", "INF-CAP-01"])],
        "inference.rules[1].reads[1]",
        "'INF-CAP-01' is the rule itself",
        id="check16-reads-itself",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.reads", ["INF-INT-01", "INF-INT-01"])],
        "inference.rules[1].reads",
        "names a rule twice",
        id="check16-reads-a-rule-twice",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.2.reads", ["INF-INT-01"])],
        "inference.rules[2].reads",
        "only a realization rule may name reads, not a 'attach_orphan' rule",
        id="check16-reads-on-attach-orphan",
    ),
    pytest.param(
        [xdoc(reads=["INF-CONT-01"])],
        "inference.rules[3].reads",
        "only a realization rule may name reads, not a 'cross_document' rule",
        id="check16-reads-on-cross-document",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.batch", 4)],
        "inference.rules[1].batch",
        "not a parameter of template 'realization'",
        id="check16-batch-on-realization",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.2.batch", 1)],
        "inference.rules.2.batch",
        "Input should be greater than or equal to 2",
        id="check16-batch-below-two",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.2.batch", value=True)],
        "inference.rules.2.batch",
        "Input should be a valid integer",
        id="check1-batch-type",
    ),
]


# and checks 1-16: one crafted bad file per check

# (id, edits, where fragment, message fragment)
BAD_FILES = [
    # 1: known keys, required fields, value types
    pytest.param(
        [lambda d: put(d, "node_types.module.colour", "red")],
        "node_types.module.colour",
        "unknown key",
        id="check1-unknown-key",
    ),
    pytest.param(
        [lambda d: drop(d, "node_types.module.label")],
        "node_types.module.label",
        "required field missing",
        id="check1-missing-required",
    ),
    pytest.param(
        [lambda d: put(d, "inference.cap", "fifty")],
        "inference.cap",
        "Input should be a valid integer",
        id="check1-wrong-type",
    ),
    pytest.param(
        [lambda d: put(d, "format_version", 1)],
        "format_version",
        "format_version 1 is not supported; this library reads format_version 2",
        id="check1-format-version",
    ),
    pytest.param(
        [lambda d: put(d, "format_version", "2")],
        "format_version",
        "Input should be 2",
        id="check1-format-version-type",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.module.attributes.type.max_chars", 5)],
        "node_types.module.attributes.type.max_chars",
        "not allowed for a 'enum' attribute",
        id="check1-attribute-field-not-for-kind",
    ),
    pytest.param(
        [lambda d: drop(d, "node_types.module.attributes.stmt.max_chars")],
        "node_types.module.attributes.stmt.max_chars",
        "required for a 'text' attribute",
        id="check1-attribute-field-missing-for-kind",
    ),
    # 2: safe names
    pytest.param(
        [lambda d: put(d, "node_types.module.label", "Mod-ule")],
        "node_types.module.label",
        "must match",
        id="check2-label-pattern",
    ),
    pytest.param(
        [lambda d: rename(d, "edge_types.GROUPED_BY", "GROUPED BY")],
        "edge_types.GROUPED BY",
        "must match",
        id="check2-relation-pattern",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.module.attributes.Bad-Name", {"type": "text", "max_chars": 5})],
        "attributes.Bad-Name",
        "must match",
        id="check2-attribute-pattern",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.module.attributes.spec_id", {"type": "text", "max_chars": 5})],
        "attributes.spec_id",
        "reserved property name",
        id="check2-reserved-attribute",
    ),
    # 3: references resolve
    pytest.param(
        [lambda d: add(d, "edge_types.PART_OF.pairs", {"from": "feature", "to": "nowhere"})],
        "edge_types.PART_OF.pairs",
        "'nowhere' is not a declared node type or group",
        id="check3-pair-endpoint",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.feature.group", "nogroup")],
        "node_types.feature.group",
        "'nogroup' is not a declared node group",
        id="check3-group",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.feature.attributes.module.target", "nothing")],
        "attributes.module.target",
        "'nothing' is not a declared node type or group",
        id="check3-ref-target",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.REALIZED_BY.derive", ["capability.nope"])],
        "edge_types.REALIZED_BY.derive",
        "'nope' is not a ref attribute of 'capability'",
        id="check3-derive",
    ),
    pytest.param(
        [lambda d: put(d, "spine.roots", ["nothing"])],
        "spine.roots",
        "'nothing' is not a declared node type or group",
        id="check3-spine-root",
    ),
    pytest.param(
        [lambda d: put(d, "spine.paths", [{"via": ["NO_SUCH"], "min": 1, "max": 2}])],
        "spine.paths[0]",
        "'NO_SUCH' is not a declared relation",
        id="check3-spine-relation",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.0.subject", "nothing")],
        "inference.rules[0].subject",
        "'nothing' is not a declared node type or group",
        id="check3-inference-subject",
    ),
    pytest.param(
        [lambda d: put(d, "node_groups.requirement.attributes.source_status.values", "novocab")],
        "source_status.values",
        "'novocab' is not a declared vocabulary",
        id="check3-vocabulary",
    ),
    pytest.param(
        [lambda d: put(d, "node_groups.requirement.attributes.source_status.aliases.foo", "bar")],
        "source_status.aliases.foo",
        "'bar' is not one of the values",
        id="check3-alias-value",
    ),
    # 4: pairs once, one default, cues
    pytest.param(
        [lambda d: add(d, "edge_types.PART_OF.pairs", {"from": "feature", "to": "module"})],
        "edge_types.PART_OF.pairs",
        "declared more than once",
        id="check4-pair-twice",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.PART_OF.pairs.-1.default", value=True)],
        "edge_types.PART_OF.pairs",
        "already has default relation",
        id="check4-two-defaults",
    ),
    pytest.param(
        [lambda d: drop(d, "edge_types.PART_OF.cues")],
        "edge_types.PART_OF.cues",
        "needs cues",
        id="check4-cue-required-without-cues",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.PART_OF.cues", ["part of|(unclosed"])],
        "edge_types.PART_OF.cues[0]",
        "invalid regular expression",
        id="check4-cue-regex",
    ),
    # 5: id prefixes
    pytest.param(
        [lambda d: put(d, "node_types.constraint.id.format", "REQ::FR::{SLUG}")],
        "node_types.",
        "equals the prefix of type",
        id="check5-prefix-equal",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.constraint.id.format", "REQ::{SLUG}")],
        "node_types.",
        "starts with the prefix of type 'constraint'",
        id="check5-prefix-of-another",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.module.id.format", "DOC::{SLUG}")],
        "node_types.module.id.format",
        "collides with built-in prefix",
        id="check5-builtin-prefix",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.module.id.format", "no placeholder")],
        "node_types.module.id.format",
        "must match",
        id="check5-format-shape",
    ),
    # 6: spine
    pytest.param(
        [lambda d: put(d, "spine.paths", [{"via": ["GROUPED_BY"], "min": 1, "max": 3}])],
        "spine",
        "reach no root through a declared path",
        id="check6-spine-unreachable",
    ),
    pytest.param(
        [lambda d: put(d, "spine.paths", [{"via": ["PART_OF"], "sequence": ["PART_OF"]}])],
        "spine.paths[0]",
        "exactly one of 'via' and 'sequence'",
        id="check6-spine-path-form",
    ),
    # 7: inference rules
    pytest.param(
        [lambda d: put(d, "inference.rules.0.template", "telepathy")],
        "inference.rules[0].template",
        "unknown template 'telepathy'",
        id="check7-template",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.prompt", "judge_missing")],
        "inference.rules[1].prompt",
        "judge_missing.md not found",
        id="check7-judge-prompt",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.prompt", "judge realization")],
        "inference.rules[1].prompt",
        "must match",
        id="check7-prompt-reference-shape",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.2.writes", "GROUPED_BY")],
        "inference.rules[2].writes",
        "is not legal for",
        id="check7-writes-illegal",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.0.container", "module")],
        "inference.rules[0].container",
        "not a parameter of template 'endpoints'",
        id="check7-foreign-parameter",
    ),
    pytest.param(
        [lambda d: drop(d, "inference.rules.2.max_candidates")],
        "inference.rules[2].max_candidates",
        "required by template 'attach_orphan'",
        id="check7-missing-parameter",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.0.via", [])],
        "inference.rules[0].via",
        "needs at least one relation",
        id="check7-via-empty",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.0.via", ["PART_OF", "PART_OF"])],
        "inference.rules[0].via",
        "names a relation twice",
        id="check7-via-twice",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.0.via", ["PART_OF", "NO_SUCH"])],
        "inference.rules[0].via[1]",
        "is not a declared relation",
        id="check3-via-relation",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.0.via", ["PART_OF", "GROUPED_BY"])],
        "inference.rules[0].via[1]",
        "GROUPED_BY is not legal from IntegrationRequirement",
        id="check7-via-illegal",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.0.target", "functional_requirement")],
        "inference.rules[0].mentions",
        "only hubs are mentioned",
        id="check7-mentions-without-hub",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.0.via", "PART_OF")],
        "inference.rules.0.via",
        "Input should be a valid list",
        id="check1-via-not-a-list",
    ),
    pytest.param(
        [lambda d: put(d, "inference.rules.1.id", "INF-INT-01")],
        "inference.rules[1].id",
        "used twice",
        id="check7-rule-id-twice",
    ),
    # 8: built-in names
    pytest.param(
        [lambda d: put(d, "node_types.module.label", "Fragment")],
        "node_types.module.label",
        "is a built-in name",
        id="check8-builtin-label",
    ),
    pytest.param(
        [lambda d: put(d, "node_types.module.label", "SpecNode")],
        "node_types.module.label",
        "is a built-in name",
        id="check8-builtin-label-specnode",
    ),
    pytest.param(
        [lambda d: rename(d, "edge_types.GROUPED_BY", "MENTIONED_IN")],
        "edge_types.MENTIONED_IN",
        "is a built-in name",
        id="check8-builtin-relation",
    ),
    # 9: origins match the owner
    pytest.param(
        [lambda d: put(d, "node_types.module.origin", "analysis")],
        "node_types.module.origin",
        "is not owned by owner 'build'",
        id="check9-type-origin",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.PART_OF.origin", "planner")],
        "edge_types.PART_OF.origin",
        "is not owned by owner 'build'",
        id="check9-relation-origin",
    ),
    pytest.param(
        [lambda d: put(d, "owner", "gap")],
        "owner",
        "the build file must have owner 'build'",
        id="check9-main-owner",
    ),
    # 10: symmetric relations
    pytest.param(
        [
            lambda d: put(
                d,
                "edge_types.RELATES_TO.pairs",
                [{"from": "functional_requirement", "to": "constraint"}],
            )
        ],
        "edge_types.RELATES_TO",
        "lacks reversed pairs",
        id="check10-symmetric-without-reversed-pair",
    ),
    pytest.param(
        [lambda d: put(d, "edge_types.RELATES_TO.pairs.0.exclusive", value=True)],
        "edge_types.RELATES_TO.pairs[0]",
        "symmetric relation may not have an exclusive pair",
        id="check10-symmetric-exclusive",
    ),
    *V2_BAD_FILES,
    *PKG4_BAD_FILES,
    *PKG5_BAD_FILES,
]


@pytest.mark.parametrize(("edits", "where", "message"), BAD_FILES)
def test_each_validation_check_rejects_its_bad_file(
    tmp_path: Path, edits: list[Callable[[dict[str, Any]], None]], where: str, message: str
) -> None:
    path = write_ontology(tmp_path, mutated(*edits))
    problems = load_problems(path)
    assert problems, "the bad file loaded"
    assert has_problem(problems, where, message), problems


# What each check of reports, recognised in the problem text itself.
CHECK_PROBLEMS: dict[int, re.Pattern[str]] = {
    1: re.compile(
        r"unknown key|required field missing|Input should be|(not allowed|required) for a '\w+' attribute"
        r"|duplicate key|invalid YAML|must be a mapping|is not supported|List should have|applies only to an owner"
    ),
    2: re.compile(r"(\.label|edge_types\.[^:.]+|attributes\.[^:.]+): name '.*' must match|reserved property name"),
    3: re.compile(
        r"is not a declared (node type|node group|relation|vocabulary)|is not a ref attribute|is not one of the values"
        r"|is not declared on|is not an enum attribute|is not a value of|has no pair (to|from)"
    ),
    4: re.compile(
        r"declared more than once|already has default relation|needs cues|invalid regular expression"
        r"|applies only to a default pair"
    ),
    5: re.compile(
        r"equals the prefix of type|starts with the prefix of type|collides with built-in prefix|id\.format: .*must "
        r"match"
    ),
    6: re.compile(r"reach no root through a declared path|exactly one of 'via' and 'sequence'"),
    7: re.compile(
        r"unknown template|\.md not found|\.prompt: .*must match|is not legal for|not a parameter of template"
        r"|required by template|used twice|needs at least one relation|names a relation twice|is not legal from"
        r"|only hubs are mentioned|no path reaches a container"
        r"|is not an owner pair for any|is not legal between|not a hub type|is the relation the rule writes"
        r"|one type or group key"
    ),
    8: re.compile(r"is a built-in name"),
    9: re.compile(r"is not owned by owner|must have owner"),
    10: re.compile(
        r"lacks reversed pairs|symmetric relation may not have an (exclusive|owner) pair"
        r"|symmetric relation may not have a source_where"
    ),
    11: re.compile(r"is not a quarantine reason|(otherwise|fallback_relation) '\w+' is (a stated_only|not legal)"),
    12: re.compile(r"stated_only relation may not have|is stated_only; no inference rule|is stated_only without cues"),
    13: re.compile(
        r"is not the relation of an owner pair|derive_via yields|share no source label|both a default and inherit"
    ),
    14: re.compile(r"a derive_via relation may not|fallback_relation '\w+' has source_where or target_where"),
    15: re.compile(r"conform\.answered_elsewhere\.(label|prompt): |is not an extracted atomic type"),
    16: re.compile(r"\.reads(\[\d+\])?: |\.batch: "),
}


def checks_reported(problems: Sequence[str]) -> set[int]:
    return {number for problem in problems for number, pattern in CHECK_PROBLEMS.items() if pattern.search(problem)}


def test_the_bad_files_cover_every_check_by_the_problems_they_raise(tmp_path: Path) -> None:
    """Coverage by actual problem text: each bad file reports a problem of the check it is filed under,
    and the problems of all bad files together cover checks 1-16."""

    covered: set[int] = set()
    for index, case in enumerate(BAD_FILES):
        edits, _, message = case.values
        assert message, f"{case.id}: give the expected message fragment"
        problems = load_problems(write_ontology(tmp_path / str(index), mutated(*edits)))
        reported = checks_reported(problems)
        filed = int(case.id.split("-")[0].removeprefix("check"))
        assert filed in reported, (case.id, problems)
        covered |= reported
    assert covered == set(range(1, 17))


def test_problems_name_the_file(tmp_path: Path) -> None:
    path = write_ontology(tmp_path, mutated(lambda d: put(d, "node_types.module.colour", 1)))
    assert all(str(path) in problem for problem in load_problems(path))


def test_invalid_yaml_and_duplicate_keys_and_non_mappings_are_reported(tmp_path: Path) -> None:
    text = SHIPPED_FILE.read_text(encoding="utf-8")
    duplicate = text.replace("name: spec-ontology", "name: spec-ontology\nname: again", 1)
    assert has_problem(load_problems(write_ontology(tmp_path / "dup", duplicate)), "duplicate key")
    assert has_problem(load_problems(write_ontology(tmp_path / "bad", "a: [unclosed")), "invalid YAML")
    assert has_problem(load_problems(write_ontology(tmp_path / "list", "- a\n- b\n")), "must be a mapping")
    missing = tmp_path / "missing" / "ontology.yaml"
    assert has_problem(load_problems(missing), "cannot read the file")


def test_a_missing_judge_prompt_file_fails_loading(tmp_path: Path) -> None:
    path = write_ontology(tmp_path, BASE, prompts=False)
    problems = load_problems(path)
    assert len(problems) == 3
    for ref in ("judge_endpoints", "judge_realization", "judge_attach_orphan"):
        assert has_problem(problems, f"{ref}.md not found")


def test_one_missing_judge_prompt_names_only_that_rule(tmp_path: Path) -> None:
    path = write_ontology(tmp_path, BASE)
    (path.parent / "judge_prompts" / "judge_realization.md").unlink()
    problems = load_problems(path)
    assert len(problems) == 1
    assert has_problem(problems, "inference.rules[1].prompt", "judge_realization.md not found")


def test_a_rule_floor_of_any_template_overrides_the_inference_floor_for_that_rule(tmp_path: Path) -> None:
    data = mutated(lambda d: put(d, "inference.rules.0.floor", 0.9), xdoc(floor=0.0))
    loaded = load_ontology(write_ontology(tmp_path, data), submodule_files=[])
    first, *_, last = loaded.inference.rules
    assert (first.floor, loaded.inference.floor_of(first)) == (0.9, 0.9)
    assert (last.floor, loaded.inference.floor_of(last)) == (0.0, 0.0)
    middle = loaded.inference.rules[1]
    assert middle.floor is None and loaded.inference.floor_of(middle) == loaded.inference.floor


def test_inference_limits_by_mode_reads_and_batch_load(tmp_path: Path) -> None:
    data = mutated(
        lambda d: put(d, "inference.first_cap", 400),
        lambda d: put(d, "inference.first_budget", 1_000_000),
        lambda d: put(d, "inference.rules.0.first_cap", 7),
        lambda d: put(d, "inference.rules.1.reads", ["INF-INT-01"]),
        lambda d: put(d, "inference.rules.2.batch", 6),
        lambda d: put(d, "inference.rules.2.batch_prompt", "judge_attach_orphan_batch"),
        xdoc(cap=2, first_cap=900),
    )
    assert any(
        "inference.rules[2].batch_prompt: judge prompt file judge_prompts/judge_attach_orphan_batch.md not found"
        in problem
        for problem in load_problems(write_ontology(tmp_path / "without", data))
    )  # a named batch prompt must ship
    no_prompt = mutated(lambda d: put(d, "inference.rules.2.batch", 6))
    assert any(
        "inference.rules[2].batch: needs a batch_prompt" in problem
        for problem in load_problems(write_ontology(tmp_path / "unnamed", no_prompt))
    )
    endpoints_batch = mutated(lambda d: put(d, "inference.rules.0.batch_prompt", "judge_attach_orphan_batch"))
    assert any(
        "batch_prompt: not a parameter of template 'endpoints'" in problem
        for problem in load_problems(write_ontology(tmp_path / "endpoints", endpoints_batch))
    )
    path = write_ontology(tmp_path, data)
    shutil.copy(SHIPPED_PROMPTS / "judge_attach_orphan_batch.md", path.parent / "judge_prompts")
    loaded = load_ontology(path, submodule_files=[])
    spec = loaded.inference
    endpoints, realization, orphan, cross = spec.rules
    assert (spec.cap, spec.first_cap, spec.first_budget) == (50, 400, 1_000_000)
    # A rule's first_cap overrides the ontology's, and never takes a rule below its cap.
    assert (endpoints.first_cap, spec.first_cap_of(endpoints)) == (7, 50)
    assert (realization.first_cap, spec.first_cap_of(realization)) == (None, 400)
    assert (cross.cap, cross.first_cap, spec.cap_of(cross), spec.first_cap_of(cross)) == (2, 900, 2, 900)
    assert realization.reads == ("INF-INT-01",) and spec.reads_of(realization) == (endpoints,)
    assert [rule.reads for rule in (endpoints, orphan, cross)] == [(), (), ()]
    assert spec.reads_of(orphan) == ()
    assert orphan.batch == 6 and [rule.batch for rule in (endpoints, realization, cross)] == [None, None, None]
    assert (orphan.batch_prompt, endpoints.batch_prompt) == ("judge_attach_orphan_batch", None)
    assert orphan.batch_prompt in loaded.judge_prompts


def test_reads_may_name_several_earlier_rules_in_any_order(tmp_path: Path) -> None:
    data = mutated(
        lambda d: add(d, "inference.rules", {**BASE["inference"]["rules"][1], "id": "INF-CAP-02"}),
        lambda d: put(d, "inference.rules.3.reads", ["INF-CONT-01", "INF-INT-01"]),
    )
    loaded = load_ontology(write_ontology(tmp_path, data), submodule_files=[])
    last = loaded.inference.rules[-1]
    assert last.reads == ("INF-CONT-01", "INF-INT-01")
    assert [rule.id for rule in loaded.inference.reads_of(last)] == ["INF-CONT-01", "INF-INT-01"]


def test_without_first_cap_and_first_budget_a_first_build_keeps_the_cap() -> None:
    """The frozen v1.3-f2 file sets none of the  fields and loads unchanged."""

    spec = load_frozen_ontology().inference
    assert (spec.first_cap, spec.first_budget) == (None, None)
    for rule in spec.rules:
        assert (rule.first_cap, rule.reads, rule.batch) == (None, (), None)
        assert spec.first_cap_of(rule) == spec.cap_of(rule) == 50
        assert spec.reads_of(rule) == ()


def test_a_rule_naming_a_new_prompt_loads_once_its_file_exists(tmp_path: Path) -> None:
    data = mutated(lambda d: put(d, "inference.rules.1.prompt", "judge_realization_next"))
    path = write_ontology(tmp_path, data)
    assert has_problem(load_problems(path), "judge_realization_next.md not found")
    (path.parent / "judge_prompts" / "judge_realization_next.md").write_text("next prompt\n")
    loaded = load_ontology(path, submodule_files=[])
    assert loaded.judge_prompt("judge_realization_next") == "next prompt\n"


def test_all_problems_come_back_in_one_ontology_error(tmp_path: Path) -> None:
    """: an unknown key and an unresolved pair, plus more, are all listed."""

    data = mutated(
        lambda d: put(d, "edge_types.PART_OF.weight", 3),
        lambda d: add(d, "edge_types.PART_OF.pairs", {"from": "feature", "to": "nowhere"}),
        lambda d: put(d, "node_types.module.label", "Fragment"),
        lambda d: put(d, "spine.roots", ["nothing"]),
        lambda d: put(d, "inference.rules.0.template", "telepathy"),
        lambda d: put(d, "node_types.constraint.id.format", "REQ::FR::{SLUG}"),
        lambda d: put(d, "edge_types.RELATES_TO.pairs.0.exclusive", value=True),
    )
    path = write_ontology(tmp_path, data)
    with pytest.raises(OntologyError) as raised:
        load_ontology(path, submodule_files=[])
    error = raised.value
    assert error.code == "ontology_invalid"
    assert error.fields["problems"] == len(error.problems)
    expected = [
        ("edge_types.PART_OF.weight", "unknown key"),
        ("edge_types.PART_OF.pairs", "'nowhere' is not a declared node type or group"),
        ("node_types.module.label", "is a built-in name"),
        ("spine.roots", "'nothing' is not a declared node type or group"),
        ("inference.rules[0].template", "unknown template"),
        ("node_types.", "equals the prefix of type"),
        ("edge_types.RELATES_TO.pairs[0]", "exclusive"),
    ]
    for where, message in expected:
        assert has_problem(error.problems, where, message), (where, message, error.problems)
    text = str(error)
    assert all(problem in text for problem in error.problems)


def test_an_unknown_key_alone_does_not_hide_later_problems(tmp_path: Path) -> None:
    data = mutated(
        lambda d: put(d, "node_types.feature.colour", "red"),
        lambda d: put(d, "node_types.feature.group", "nogroup"),
    )
    problems = load_problems(write_ontology(tmp_path, data))
    assert has_problem(problems, "colour", "unknown key")
    assert has_problem(problems, "'nogroup' is not a declared node group")


# submodule files merge into one validated ontology


def test_gap_relation_to_fragment_loads(tmp_path: Path) -> None:
    gap = write_ontology(tmp_path / "gap", GAP_FILE)
    merged = load_ontology(submodule_files=[gap])
    assert merged.is_legal("CITES", "Finding", "Fragment")
    assert merged.owner_of("CITES") == "gap"
    assert merged.owner_of("Finding") == "gap"
    assert merged.owner_of("Module") == "build"
    assert merged.is_declared("Finding")
    assert len(merged.node_types) == 13
    assert len(merged.relations) == 13


def test_gap_relation_crosses_owners_and_expands_groups(tmp_path: Path) -> None:
    merged = load_ontology(submodule_files=[write_ontology(tmp_path / "gap", GAP_FILE)])
    for label in [*REQUIREMENT_LABELS, "Module"]:
        assert merged.is_legal("FLAGS", "Finding", label)
    assert not merged.is_legal("FLAGS", "Finding", "Feature")


def test_gap_types_are_not_extracted_and_leave_the_build_surface_unchanged(tmp_path: Path, shipped: Ontology) -> None:
    merged = load_ontology(submodule_files=[write_ontology(tmp_path / "gap", GAP_FILE)])
    assert merged.extracted_labels == shipped.extracted_labels
    assert merged.extracted_relations == shipped.extracted_relations
    assert set(merged.entity_models()) == set(shipped.entity_models())
    assert set(merged.edge_models()) == set(shipped.edge_models())
    assert merged.edge_type_map() == shipped.edge_type_map()
    assert merged.fingerprint("nodes") == shipped.fingerprint("nodes")
    assert merged.fingerprint("edges") == shipped.fingerprint("edges")
    assert not merged.node_type("Finding").extracted
    assert merged.node_type("Finding").role is None
    assert "Finding" not in merged.hub_labels


def test_gap_file_declaring_an_extracted_type_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(GAP_FILE)
    data["node_types"]["finding"].update({"origin": "extracted", "role": "atomic", "id": {"format": "GAP::{SLUG}"}})
    problems = load_problems(None, [write_ontology(tmp_path / "gap", data)])
    assert has_problem(problems, "node_types.finding.origin", "is not owned by owner 'gap'")


def test_gap_file_declaring_an_extracted_relation_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(GAP_FILE)
    data["edge_types"]["CITES"]["origin"] = "extracted"
    problems = load_problems(None, [write_ontology(tmp_path / "gap", data)])
    assert has_problem(problems, "edge_types.CITES.origin", "is not owned by owner 'gap'")


def test_build_relation_to_fragment_fails(tmp_path: Path) -> None:
    data = mutated(lambda d: add(d, "edge_types.PART_OF.pairs", {"from": "feature", "to": "Fragment"}))
    problems = load_problems(write_ontology(tmp_path, data))
    assert has_problem(problems, "built-in label 'Fragment'", "another owner than build")


def test_gap_relation_to_a_built_in_label_other_than_evidence_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(GAP_FILE)
    data["edge_types"]["CITES"]["pairs"] = [{"from": "finding", "to": "SpecNode"}]
    problems = load_problems(None, [write_ontology(tmp_path / "gap", data)])
    assert has_problem(problems, "edge_types.CITES.pairs[0].to", "'SpecNode' is not a declared")


def test_gap_relation_may_end_at_document(tmp_path: Path) -> None:
    data = copy.deepcopy(GAP_FILE)
    data["edge_types"]["CITES"]["pairs"].append({"from": "finding", "to": "Document"})
    merged = load_ontology(submodule_files=[write_ontology(tmp_path / "gap", data)])
    assert merged.is_legal("CITES", "Finding", "Document")


def test_submodule_file_may_not_have_owner_build(tmp_path: Path) -> None:
    data = copy.deepcopy(GAP_FILE)
    data["owner"] = "build"
    problems = load_problems(None, [write_ontology(tmp_path / "gap", data)])
    assert has_problem(problems, "owner", "a submodule file may not have owner 'build'")


@pytest.mark.parametrize("section", ["corpus", "spine", "inference"])
def test_submodule_file_may_not_declare_build_only_sections(tmp_path: Path, section: str) -> None:
    data = copy.deepcopy(GAP_FILE)
    data[section] = {"framing": "x"} if section == "corpus" else {}
    problems = load_problems(None, [write_ontology(tmp_path / "gap", data)])
    assert has_problem(problems, section, "only the build file may declare this section")


def test_names_stay_unique_across_files(tmp_path: Path) -> None:
    data = copy.deepcopy(GAP_FILE)
    data["node_types"]["module"] = {"label": "Module", "origin": "analysis", "tier": "context"}
    data["node_types"]["finding"]["label"] = "Feature"
    data["edge_types"]["PART_OF"] = {
        "origin": "analysis",
        "tier": "governance",
        "pairs": [{"from": "finding", "to": "module"}],
    }
    problems = load_problems(None, [write_ontology(tmp_path / "gap", data)])
    assert has_problem(problems, "node_types.module", "already declared in")
    assert has_problem(problems, "label 'Feature' is already used by type 'feature'")
    assert has_problem(problems, "edge_types.PART_OF", "already declared in")


def test_a_relation_may_not_share_its_name_with_a_label(tmp_path: Path) -> None:
    data = copy.deepcopy(GAP_FILE)
    data["edge_types"]["Finding"] = data["edge_types"].pop("CITES")
    problems = load_problems(None, [write_ontology(tmp_path / "gap", data)])
    assert has_problem(problems, "edge_types.Finding", "may not share its name with a label")


def test_vocabularies_and_groups_are_shared_by_name_across_files(tmp_path: Path) -> None:
    same = copy.deepcopy(GAP_FILE)
    same["vocabularies"] = copy.deepcopy(SHIPPED_DATA["vocabularies"])
    same["node_groups"] = {"requirement": copy.deepcopy(SHIPPED_DATA["node_groups"]["requirement"])}
    assert load_problems(None, [write_ontology(tmp_path / "same", same)]) == ()

    different = copy.deepcopy(same)
    different["vocabularies"]["yes_no"] = ["yes", "no", "maybe"]
    different["node_groups"]["requirement"]["tier"] = "plan"
    problems = load_problems(None, [write_ontology(tmp_path / "diff", different)])
    assert has_problem(problems, "vocabularies.yes_no", "declared differently in")
    assert has_problem(problems, "node_groups.requirement", "declared differently in")


def test_cross_file_problems_are_reported_together(tmp_path: Path) -> None:
    data = copy.deepcopy(GAP_FILE)
    data["node_types"]["finding"]["origin"] = "extracted"
    data["edge_types"]["CITES"]["pairs"] = [{"from": "finding", "to": "nowhere"}]
    build = mutated(lambda d: put(d, "node_types.module.colour", "red"))
    problems = load_problems(write_ontology(tmp_path / "build", build), [write_ontology(tmp_path / "gap", data)])
    assert has_problem(problems, "colour", "unknown key")
    assert has_problem(problems, "origin", "is not owned by owner 'gap'")
    assert has_problem(problems, "'nowhere' is not a declared node type or group")
    assert len({problem.split(":")[0] for problem in problems}) == 2


def test_empty_submodule_list_replaces_discovery(tmp_path: Path) -> None:
    gap = write_ontology(tmp_path / "gap", GAP_FILE)
    assert len(load_ontology(submodule_files=[]).node_types) == 12
    assert len(load_ontology(submodule_files=[gap]).node_types) == 13


# Legalise

CUE_LINE = ["The orders database is a component of the orders service."]
PLAIN_LINE = ["The checkout service calls the payment service."]

LEGALISE_TABLE = [
    # a legal pair is kept
    pytest.param(
        "PART_OF", "FunctionalRequirement", "Feature", PLAIN_LINE, Legalised("PART_OF", flipped=False), id="keep"
    ),
    pytest.param(
        "GROUPED_BY", "Feature", "Capability", PLAIN_LINE, Legalised("GROUPED_BY", flipped=False), id="keep-group"
    ),
    pytest.param(
        "RELATES_TO",
        "FunctionalRequirement",
        "Constraint",
        PLAIN_LINE,
        Legalised("RELATES_TO", flipped=False),
        id="keep-symmetric",
    ),
    # a reversed pair is flipped
    pytest.param(
        "PART_OF",
        "Module",
        "FunctionalRequirement",
        PLAIN_LINE,
        Legalised("PART_OF", flipped=True),
        id="flip-module-part-of-fr",
    ),
    pytest.param(
        "PART_OF",
        "Feature",
        "BusinessRule",
        PLAIN_LINE,
        Legalised("PART_OF", flipped=True),
        id="flip-feature-part-of-rule",
    ),
    pytest.param(
        "GROUPED_BY", "Capability", "Feature", PLAIN_LINE, Legalised("GROUPED_BY", flipped=True), id="flip-grouped-by"
    ),
    pytest.param(
        "REALIZED_BY", "Module", "Capability", PLAIN_LINE, Legalised("REALIZED_BY", flipped=True), id="flip-realized-by"
    ),
    # a symmetric relation is never flipped, either order
    pytest.param(
        "RELATES_TO",
        "Constraint",
        "FunctionalRequirement",
        PLAIN_LINE,
        Legalised("RELATES_TO", flipped=False),
        id="symmetric-order-1",
    ),
    pytest.param(
        "RELATES_TO",
        "FunctionalRequirement",
        "Constraint",
        PLAIN_LINE,
        Legalised("RELATES_TO", flipped=False),
        id="symmetric-order-2",
    ),
    # a wrong relation on a pair with a default becomes the default
    pytest.param(
        "REALIZED_BY", "Module", "Module", PLAIN_LINE, Legalised("INTEGRATES_WITH", flipped=False), id="pair-default"
    ),
    pytest.param(
        "GROUPED_BY", "Module", "Module", [], Legalised("INTEGRATES_WITH", flipped=False), id="pair-default-no-evidence"
    ),
    # an unknown relation on a pair with a default becomes the default
    pytest.param(
        "CALLS", "Module", "Module", PLAIN_LINE, Legalised("INTEGRATES_WITH", flipped=False), id="unknown-then-default"
    ),
    pytest.param(
        "INTEGRATES_WITH",
        "Module",
        "Module",
        PLAIN_LINE,
        Legalised("INTEGRATES_WITH", flipped=False),
        id="default-kept",
    ),
    # cue_required: PART_OF between modules needs a cue in the evidence
    pytest.param("PART_OF", "Module", "Module", CUE_LINE, Legalised("PART_OF", flipped=False), id="cue-matches"),
    pytest.param(
        "PART_OF",
        "Module",
        "Module",
        ["It CONSISTS OF several services."],
        Legalised("PART_OF", flipped=False),
        id="cue-matches-ignoring-case",
    ),
    pytest.param(
        "PART_OF",
        "Module",
        "Module",
        [PLAIN_LINE[0], CUE_LINE[0]],
        Legalised("PART_OF", flipped=False),
        id="cue-in-any-evidence-line",
    ),
    pytest.param(
        "PART_OF",
        "Module",
        "Module",
        PLAIN_LINE,
        Legalised("INTEGRATES_WITH", flipped=False),
        id="cue-missing-falls-to-default",
    ),
    pytest.param(
        "PART_OF",
        "Module",
        "Module",
        [],
        Legalised("INTEGRATES_WITH", flipped=False),
        id="cue-no-evidence-falls-to-default",
    ),
    # v2.0 default_from pairs: GROUPED_BY requirement -> feature is ownership, PART_OF
    # requirement -> data entity a relation; any other relation on those ends stays illegal
    pytest.param(
        "GROUPED_BY",
        "FunctionalRequirement",
        "Feature",
        PLAIN_LINE,
        Legalised("PART_OF", flipped=False),
        id="requirement-feature-default",
    ),
    pytest.param(
        "PART_OF",
        "QualityRequirement",
        "DataEntity",
        PLAIN_LINE,
        Legalised("RELATES_TO", flipped=False),
        id="requirement-entity-default",
    ),
    pytest.param(
        "PART_OF", "DataRequirement", "DataEntity", PLAIN_LINE, Legalised("PART_OF", flipped=False), id="data-kept"
    ),
    pytest.param(
        "DEPENDS_ON", "FunctionalRequirement", "Feature", PLAIN_LINE, Dropped("illegal_pair"), id="default-from-only"
    ),
    pytest.param(
        "REALIZED_BY", "BusinessRule", "Feature", PLAIN_LINE, Dropped("illegal_pair"), id="default-from-realized-by"
    ),
    pytest.param(
        "GROUPED_BY", "FunctionalRequirement", "DataEntity", PLAIN_LINE, Dropped("illegal_pair"), id="entity-from-only"
    ),
    pytest.param(
        "GROUPED_BY", "IntegrationRequirement", "Feature", PLAIN_LINE, Dropped("illegal_pair"), id="no-int-default"
    ),
    pytest.param(
        "RELATES_TO", "Feature", "FunctionalRequirement", PLAIN_LINE, Dropped("illegal_pair"), id="no-reverse-default"
    ),
    # illegal and unknown are dropped
    pytest.param(
        "REALIZED_BY", "IntegrationRequirement", "Feature", PLAIN_LINE, Dropped("illegal_pair"), id="illegal-pair"
    ),
    pytest.param("RELATES_TO", "Feature", "Feature", PLAIN_LINE, Dropped("illegal_pair"), id="illegal-symmetric"),
    pytest.param(
        "RELATES_TO",
        "FunctionalRequirement",
        "Module",
        PLAIN_LINE,
        Legalised("RELATES_TO", flipped=False),
        id="symmetric-requirement-module",
    ),
    pytest.param("PART_OF", "Capability", "Capability", PLAIN_LINE, Dropped("illegal_pair"), id="illegal-same-label"),
    pytest.param("CALLS", "Feature", "Capability", PLAIN_LINE, Dropped("unknown_relation"), id="unknown-relation"),
    pytest.param(
        "SpecNode", "Feature", "Module", PLAIN_LINE, Dropped("unknown_relation"), id="built-in-name-is-unknown"
    ),
    pytest.param("PART_OF", "Nowhere", "Module", PLAIN_LINE, Dropped("illegal_pair"), id="unknown-label"),
]


@pytest.mark.parametrize(("relation", "source", "target", "evidence", "expected"), LEGALISE_TABLE)
def test_legalise(
    shipped: Ontology,
    relation: str,
    source: str,
    target: str,
    evidence: list[str],
    *,
    expected: Legalised | Dropped,
) -> None:
    assert shipped.legalise(relation, source, target, evidence) == expected


def test_legalise_cue_missing_when_the_pair_has_no_default(tmp_path: Path) -> None:
    data = mutated(lambda d: put(d, "edge_types.INTEGRATES_WITH.pairs.0.default", value=False))
    loaded = load_ontology(write_ontology(tmp_path, data), submodule_files=[])
    assert loaded.legalise("PART_OF", "Module", "Module", PLAIN_LINE) == Dropped("cue_missing")
    assert loaded.legalise("PART_OF", "Module", "Module", []) == Dropped("cue_missing")
    assert loaded.legalise("PART_OF", "Module", "Module", CUE_LINE) == Legalised("PART_OF", flipped=False)
    assert loaded.legalise("REALIZED_BY", "Module", "Module", PLAIN_LINE) == Dropped("illegal_pair")


def test_legalise_flips_a_reversed_cue_required_pair_only_with_a_cue(tmp_path: Path) -> None:
    """The reversed pair is the one that needs the cue; a missing cue drops the edge."""

    data = mutated(lambda d: put(d, "edge_types.PART_OF.pairs.1.cue_required", value=True))
    loaded = load_ontology(write_ontology(tmp_path, data), submodule_files=[])
    assert loaded.legalise("PART_OF", "FunctionalRequirement", "Module", CUE_LINE) == Legalised(
        "PART_OF", flipped=False
    )
    assert loaded.legalise("PART_OF", "Module", "FunctionalRequirement", CUE_LINE) == Legalised("PART_OF", flipped=True)
    assert loaded.legalise("PART_OF", "Module", "FunctionalRequirement", PLAIN_LINE) == Dropped("cue_missing")
    assert loaded.legalise("PART_OF", "FunctionalRequirement", "Module", PLAIN_LINE) == Dropped("cue_missing")


def test_legalise_works_on_a_renamed_ontology(tmp_path: Path) -> None:
    """Library code knows the format, not the content: a renamed copy legalises the same way."""

    text = SHIPPED_FILE.read_text(encoding="utf-8")
    text = text.replace("PART_OF", "BELONGS_TO").replace("MOD::", "COMP::")
    text = re.sub(r"\bModule\b", "Component", text)
    text = re.sub(r"\bmodule\b", "component", text)
    loaded = load_ontology(write_ontology(tmp_path, text, prompts_from=SHIPPED_PROMPTS), submodule_files=[])
    assert "Module" not in loaded.labels
    assert loaded.legalise("BELONGS_TO", "Component", "FunctionalRequirement", []) == Legalised(
        "BELONGS_TO", flipped=True
    )
    assert loaded.legalise("BELONGS_TO", "Component", "Component", CUE_LINE) == Legalised("BELONGS_TO", flipped=False)
    assert loaded.legalise("BELONGS_TO", "Component", "Component", PLAIN_LINE) == Legalised(
        "INTEGRATES_WITH", flipped=False
    )
    assert loaded.id_prefix("Component") == "COMP::"


def test_pair_helpers(shipped: Ontology) -> None:
    assert shipped.is_legal("PART_OF", "Feature", "Module")
    assert not shipped.is_legal("PART_OF", "Module", "Feature")
    assert shipped.is_legal("PART_OF", "Module", "Module")  # cues are not considered
    assert shipped.default_relation("Module", "Module") == "INTEGRATES_WITH"
    assert shipped.default_relations("Module", "Module") == ("INTEGRATES_WITH", "DEPLOYED_TO")
    assert shipped.default_relation("Feature", "Module") is None
    assert shipped.is_symmetric("RELATES_TO")
    assert not shipped.is_symmetric("PART_OF")
    assert shipped.exclusive_pairs == frozenset(
        {("PART_OF", "Module", "Module"), ("PROVIDED_BY", "Interface", "Module")}
    )
    for label in ("FunctionalRequirement", "QualityRequirement", "Constraint", "BusinessRule"):
        assert [(pair.relation, pair.target, pair.owner_rank) for pair in shipped.owner_pairs(label)] == [
            ("PART_OF", "Feature", 1),
            ("PART_OF", "Module", 2),
        ]
    assert [(pair.target, pair.owner_rank) for pair in shipped.owner_pairs("IntegrationRequirement")] == [
        ("Interface", 1),
        ("Module", 2),
    ]
    assert [pair.target for pair in shipped.owner_pairs("DataRequirement")] == ["DataEntity"]
    assert [pair.target for pair in shipped.owner_pairs("Feature")] == ["Module"]
    assert shipped.owner_pairs("Module") == shipped.owner_pairs("OpenQuestion") == ()
    assert shipped.acyclic_relations() == shipped.stated_only_relations() == frozenset({"DEPENDS_ON"})
    assert shipped.id_reference_relations == ("RELATES_TO",)
    with pytest.raises(ConformanceError) as raised:
        shipped.is_symmetric("NOPE")
    assert raised.value.code == "ontology_unknown_relation"


def test_derive_rules_resolve_to_labels(shipped: Ontology) -> None:
    by_relation: dict[str, list[Any]] = {}
    for rule in shipped.derive_rules:
        by_relation.setdefault(rule.relation, []).append(rule)
    assert set(by_relation) == {"PART_OF", "REALIZED_BY", "PROVIDED_BY"}
    (provided,) = by_relation["PROVIDED_BY"]
    assert (provided.source_labels, provided.attribute, provided.target_labels) == (
        frozenset({"Interface"}),
        "provider",
        frozenset({"Module"}),
    )
    assert not shipped.attribute_specs("Interface")["provider"].store
    part_of_sources = set().union(*(rule.source_labels for rule in by_relation["PART_OF"]))
    assert part_of_sources == {"Feature", *REQUIREMENT_LABELS}
    assert {rule.attribute for rule in by_relation["PART_OF"]} == {"module", "interface", "entity"}
    assert {rule.target_labels for rule in by_relation["PART_OF"]} == {
        frozenset({"Module"}),
        frozenset({"Interface"}),
        frozenset({"DataEntity"}),
    }
    (realized,) = by_relation["REALIZED_BY"]
    assert (realized.source_labels, realized.target_labels) == (
        frozenset({"Capability"}),
        frozenset({"Module"}),
    )


# Attribute normalisation


@pytest.mark.parametrize(
    ("label", "name", "raw", "expected"),
    [
        # enums: declared values, any case
        ("Module", "type", "backend", "backend"),
        ("Module", "type", "Backend", "backend"),
        ("Module", "type", "  DATASTORE  ", "datastore"),
        # enums: aliases, any case
        ("Module", "type", "DB", "datastore"),
        ("Module", "type", "Web", "frontend"),
        ("Module", "type", "queue", "datastore"),
        ("Module", "external", "third-party", "yes"),
        ("Module", "external", "Internal", "no"),
        ("Module", "external", True, "yes"),
        ("Module", "external", False, "no"),
        ("FunctionalRequirement", "is_assumption", "assumed", "yes"),
        ("FunctionalRequirement", "is_assumption", "No", "no"),
        ("FunctionalRequirement", "source_status", "Explicit", "stated"),
        ("FunctionalRequirement", "source_status", "implicit", "implied"),
        # enum values that are rejected
        ("Module", "type", "spaceship", None),
        ("Module", "external", "maybe", None),
        ("FunctionalRequirement", "source_status", "stated or implied", None),
        # text values are stripped
        ("FunctionalRequirement", "stmt", "  Users can log in.  ", "Users can log in."),
        # refs: the stripped name
        ("Feature", "module", "  Orders service ", "Orders service"),
        # empty and unusable values
        ("Module", "type", None, None),
        ("Module", "type", "", None),
        ("Module", "stmt", "   ", None),
        ("Module", "type", ["backend"], None),
        ("Module", "type", {"a": 1}, None),
        # unknown attribute
        ("Module", "nope", "x", None),
        ("Feature", "external", "yes", None),
        ("Capability", "type", "backend", None),
    ],
)
def test_normalise_attribute(shipped: Ontology, label: str, name: str, raw: Any, expected: str | None) -> None:
    assert shipped.normalise_attribute(label, name, raw) == expected


@pytest.mark.parametrize("label", ["FunctionalRequirement", "Module", "Feature"])
def test_normalise_attribute_cuts_text_to_max_chars(shipped: Ontology, label: str) -> None:
    spec = shipped.attribute_specs(label)["quote"]
    assert spec.max_chars == 300
    cut = shipped.normalise_attribute(label, "quote", "x" * 1000)
    assert cut == "x" * 300
    assert shipped.normalise_attribute(label, "quote", "y" * 300) == "y" * 300


def test_normalise_attribute_cuts_at_each_attributes_own_limit(shipped: Ontology) -> None:
    assert len(shipped.normalise_attribute("FunctionalRequirement", "stmt", "s" * 900) or "") == 600
    assert len(shipped.normalise_attribute("FunctionalRequirement", "acceptance_criteria", "a" * 2000) or "") == 1200


def test_normalise_attribute_for_an_unknown_label_is_a_conformance_error(shipped: Ontology) -> None:
    with pytest.raises(ConformanceError) as raised:
        shipped.normalise_attribute("Nowhere", "stmt", "x")
    assert raised.value.code == "ontology_unknown_label"


def test_attribute_specs_resolve_vocabularies_and_group_inheritance(shipped: Ontology) -> None:
    status = shipped.attribute_specs("Constraint")["source_status"]
    assert status.kind == "enum"
    assert status.values == ("stated", "implied")
    assert set(status.value_descriptions) == {"stated", "implied"}
    assert status.aliases["explicit"] == "stated"
    yes_no = shipped.attribute_specs("Constraint")["is_assumption"]
    assert yes_no.values == ("yes", "no")
    assert dict(yes_no.value_descriptions) == {}
    module_type = shipped.attribute_specs("Module")["type"]
    assert module_type.values == ("frontend", "backend", "datastore", "deployment", "other")
    assert dict(module_type.value_descriptions)["datastore"].startswith("a database")
    ref = shipped.attribute_specs("Feature")["module"]
    assert (ref.kind, ref.target_labels, ref.store) == ("ref", frozenset({"Module"}), False)
    group = {"stmt", "quote", "acceptance_criteria", "source_status", "is_assumption", "release", "in_scope"}
    refs = {"IntegrationRequirement": {"module", "interface"}, "DataRequirement": {"entity"}}
    for label in REQUIREMENT_LABELS:
        assert set(shipped.attribute_specs(label)) == group | refs.get(label, {"module"})
    in_scope = shipped.attribute_specs("FunctionalRequirement")["in_scope"]
    assert (in_scope.values, in_scope.default) == (("yes", "no"), "yes")
    assert shipped.inherit_rules("FunctionalRequirement") == {"release": "PART_OF"}
    assert shipped.inherit_rules("Feature") == {}
    assert shipped.attribute_specs("Interface")["kind"].values == ("api", "event", "queue", "file", "other")
    assert (
        set(shipped.attribute_specs("DataEntity"))
        == set(shipped.attribute_specs("OpenQuestion"))
        == {
            "stmt",
            "quote",
        }
    )


def test_attribute_output_model_has_optional_string_fields(shipped: Ontology) -> None:
    """The quote is declared but never asked of the attributes pass: the node call returns it."""

    assert "quote" in shipped.attribute_specs("Module")
    model = shipped.attribute_output_model("Module")
    assert set(model.model_fields) == {"stmt", "external", "type", "in_scope"}
    assert model().model_dump() == {"stmt": None, "external": None, "type": None, "in_scope": None}
    assert model.model_validate({"type": "db"}).type == "db"  # normalised afterwards
    assert "One of:" in (model.model_fields["type"].description or "")
    assert "datastore: a database" in (model.model_fields["type"].description or "")
    assert set(shipped.attribute_output_model("Feature").model_fields) == {
        "stmt",
        "module",
        "release",
        "in_scope",
    }


def test_attribute_output_model_for_a_non_extracted_label_is_a_conformance_error(
    tmp_path: Path,
) -> None:
    merged = load_ontology(submodule_files=[write_ontology(tmp_path / "gap", GAP_FILE)])
    with pytest.raises(ConformanceError) as raised:
        merged.attribute_output_model("Finding")
    assert raised.value.code == "ontology_no_attribute_model"


# Identity helpers: id families, prefixes, rules, scopes


@pytest.mark.parametrize(
    ("explicit_id", "label"),
    [
        ("FR-ADM-007", "FunctionalRequirement"),
        ("fr_adm_007", "FunctionalRequirement"),
        ("UC-3", "FunctionalRequirement"),
        ("US.12", "FunctionalRequirement"),
        ("NFR-12", "QualityRequirement"),
        ("PERF 3", "QualityRequirement"),
        ("SEC-4", "QualityRequirement"),
        ("CON-1", "Constraint"),
        ("BR-9", "BusinessRule"),
        ("INT-2", "IntegrationRequirement"),
        ("DR-1", "DataRequirement"),
        ("DATA-5", "DataRequirement"),
        ("XYZ-1", None),
        ("12", None),
        ("", None),
    ],
)
def test_label_for_id_family(shipped: Ontology, explicit_id: str, label: str | None) -> None:
    assert shipped.label_for_id_family(explicit_id) == label


@pytest.mark.parametrize(
    ("label", "prefix"),
    [
        ("Module", "MOD::"),
        ("Feature", "FEAT::"),
        ("Capability", "CAP::"),
        ("FunctionalRequirement", "REQ::FR::"),
        ("QualityRequirement", "REQ::NFR::"),
        ("Constraint", "REQ::CON::"),
        ("BusinessRule", "REQ::BR::"),
        ("IntegrationRequirement", "REQ::INT::"),
        ("DataRequirement", "REQ::DATA::"),
    ],
)
def test_id_prefix(shipped: Ontology, label: str, prefix: str) -> None:
    assert shipped.id_prefix(label) == prefix
    assert shipped.node_type(label).id_format == f"{prefix}{{SLUG}}"


def test_id_prefixes_are_unique_and_none_is_a_prefix_of_another(shipped: Ontology) -> None:
    prefixes = [shipped.id_prefix(label) for label in shipped.labels]
    assert len(set(prefixes)) == len(prefixes)
    assert not [a for a in prefixes for b in prefixes if a != b and b.startswith(a)]
    assert not [a for a in prefixes if a.startswith(("DOC::", "FRG::"))]


def test_id_prefix_without_a_format_is_a_conformance_error(tmp_path: Path) -> None:
    merged = load_ontology(submodule_files=[write_ontology(tmp_path / "gap", GAP_FILE)])
    with pytest.raises(ConformanceError) as raised:
        merged.id_prefix("Finding")
    assert raised.value.code == "ontology_no_id_format"
    with pytest.raises(ConformanceError):
        merged.id_prefix("Nowhere")


def test_identity_rule(shipped: Ontology) -> None:
    assert shipped.identity_rule("Module") == IdentityRule("surface", "Module", None)
    assert shipped.identity_rule("Feature") == IdentityRule("surface", "Feature", None)
    for label in REQUIREMENT_LABELS:
        assert shipped.identity_rule(label) == IdentityRule("id", "requirement", "span")
    for label in ("Interface", "DataEntity"):
        assert shipped.identity_rule(label) == IdentityRule("surface", label, None)
    assert shipped.identity_rule("OpenQuestion") == IdentityRule("id", "OpenQuestion", "span")


def test_new_types_of_the_shipped_ontology(shipped: Ontology) -> None:
    """The v2.0 types: prefixes, roles, the label veto on merges and the support a DataEntity needs."""

    assert [shipped.id_prefix(label) for label in ("Interface", "DataEntity", "OpenQuestion")] == [
        "IFC::",
        "ENT::",
        "QST::",
    ]
    assert [shipped.node_type(label).role for label in ("Interface", "DataEntity", "OpenQuestion")] == [
        "anchor",
        "group",
        "atomic",
    ]
    assert {label for label in shipped.labels if not shipped.merge_across_labels(label)} == {"Interface", "DataEntity"}
    assert [(support.relation, support.direction) for support in shipped.supported_by("DataEntity")] == [
        ("PART_OF", "in"),
        ("STORED_IN", "out"),
        ("CARRIES", "in"),
    ]
    assert shipped.derive_via_rules()[0].relation == "INTEGRATES_WITH"


def test_requirement_scope_is_shared_by_the_requirement_types(shipped: Ontology) -> None:
    scopes = {shipped.requirement_scope(label) for label in REQUIREMENT_LABELS}
    assert scopes == {"requirement"}
    assert shipped.requirement_scope("Module") == "Module"
    assert shipped.requirement_scope("Capability") == "Capability"
    assert shipped.scope_labels("requirement") == frozenset(REQUIREMENT_LABELS)
    assert shipped.scope_labels("Module") == frozenset({"Module"})
    assert shipped.scope_labels("nothing") == frozenset()
    assert shipped.node_groups["requirement"].labels == tuple(REQUIREMENT_LABELS)


def test_identity_defaults_follow_the_role(tmp_path: Path) -> None:
    """Without an identity: an atomic type keys on id (span fallback), others on surface."""

    def unset(d: dict[str, Any]) -> None:
        drop(d, "node_groups.requirement.identity")
        put(d, "node_types.feature.identity", {"key": "id"})
        put(d, "node_types.module.identity", {"key": "id", "scope": "group"})

    loaded_problems = load_problems(write_ontology(tmp_path, mutated(unset)))
    assert has_problem(loaded_problems, "node_types.module.identity.scope", "scope 'group' needs a group")

    def fine(d: dict[str, Any]) -> None:
        drop(d, "node_groups.requirement.identity")
        put(d, "node_types.feature.identity", {"key": "id"})

    loaded = load_ontology(write_ontology(tmp_path / "ok", mutated(fine)), submodule_files=[])
    assert loaded.identity_rule("Feature") == IdentityRule("id", "Feature", "surface")
    assert loaded.identity_rule("Constraint") == IdentityRule("id", "Constraint", "span")
    assert loaded.requirement_scope("Constraint") == "Constraint"


# Ownership lookups


def test_owner_of_and_is_declared(shipped: Ontology) -> None:
    assert shipped.owner_of("PART_OF") == "build"
    assert shipped.owner_of("Module") == "build"
    for name in BUILT_IN_NAMES:
        assert shipped.owner_of(name) == "build"
        assert not shipped.is_declared(name)
    assert shipped.owner_of("Nope") is None
    assert shipped.is_declared("Module")
    assert shipped.is_declared("RELATES_TO")


def test_node_type_and_relation_lookups_raise_for_unknown_names(shipped: Ontology) -> None:
    with pytest.raises(ConformanceError) as label_error:
        shipped.node_type("Nowhere")
    assert label_error.value.code == "ontology_unknown_label"
    with pytest.raises(ConformanceError) as relation_error:
        shipped.relation("NOWHERE")
    assert relation_error.value.code == "ontology_unknown_relation"
    assert shipped.pairs("RELATES_TO")


# Graphiti-facing models


def test_entity_models_carry_the_prompt_in_their_docstring(shipped: Ontology) -> None:
    models = shipped.entity_models()
    assert sorted(models) == EXTRACTED_LABELS
    for label, model in models.items():
        node_type = shipped.node_type(label)
        assert model.__name__ == label
        assert model.model_fields == {}
        assert (model.__doc__ or "").startswith(node_type.description.splitlines()[0])
        assert (model.__doc__ or "").strip() == model.__doc__
        if node_type.guidance:
            assert model.__doc__ == f"{node_type.description}\n\n{node_type.guidance}"
        else:
            assert model.__doc__ == node_type.description
    assert "A named piece of functionality" in (models["Feature"].__doc__ or "")
    assert "also extract each" in (models["Feature"].__doc__ or "")
    assert "GOOD:" in (models["Module"].__doc__ or "")
    assert shipped.entity_models() is not shipped.entity_models()


def test_entity_model_docstring_inherits_the_group_description(tmp_path: Path) -> None:
    data = mutated(lambda d: drop(d, "node_types.constraint.description"))
    loaded = load_ontology(write_ontology(tmp_path, data), submodule_files=[])
    doc = loaded.entity_models()["Constraint"].__doc__ or ""
    assert doc == BASE["node_groups"]["requirement"]["description"].strip()
    assert loaded.node_type("Constraint").description == doc


def test_edge_models_carry_the_relation_description(shipped: Ontology) -> None:
    models = shipped.edge_models()
    assert sorted(models) == RELATIONS
    for name, model in models.items():
        assert model.model_fields == {}
        assert model.__doc__ == shipped.relation(name).description
    assert "Symmetric" in (models["RELATES_TO"].__doc__ or "")


def test_edge_type_map_lists_extracted_relations_per_label_pair(shipped: Ontology) -> None:
    mapping = shipped.edge_type_map()
    assert mapping[("Module", "Module")] == ["DEPLOYED_TO", "INTEGRATES_WITH", "PART_OF"]
    assert mapping[("Feature", "Module")] == ["PART_OF"]
    assert mapping[("Feature", "Feature")] == ["DEPENDS_ON"]
    assert mapping[("Interface", "Module")] == ["CONSUMED_BY", "PROVIDED_BY"]
    assert mapping[("Interface", "DataEntity")] == ["CARRIES"]
    assert mapping[("DataEntity", "Module")] == ["STORED_IN"]
    assert mapping[("IntegrationRequirement", "Interface")] == ["PART_OF", "RELATES_TO"]
    assert mapping[("DataRequirement", "DataEntity")] == ["PART_OF", "RELATES_TO"]
    assert mapping[("OpenQuestion", "Feature")] == ["RELATES_TO"]
    assert mapping[("Feature", "Capability")] == ["GROUPED_BY"]
    assert mapping[("Capability", "Module")] == ["REALIZED_BY"]
    assert mapping[("FunctionalRequirement", "Feature")] == ["PART_OF"]
    assert mapping[("BusinessRule", "Capability")] == ["GROUPED_BY"]
    assert list(mapping) == sorted(mapping)
    labels = set(EXTRACTED_LABELS)
    assert all(source in labels and target in labels for source, target in mapping)


def test_edge_type_map_includes_both_directions_of_a_symmetric_relation(shipped: Ontology) -> None:
    mapping = shipped.edge_type_map()
    for source in REQUIREMENT_LABELS:
        for target in REQUIREMENT_LABELS:
            assert "RELATES_TO" in mapping[(source, target)], (source, target)
    assert mapping[("Constraint", "FunctionalRequirement")] == ["DEPENDS_ON", "RELATES_TO"]
    assert mapping[("FunctionalRequirement", "Constraint")] == ["DEPENDS_ON", "RELATES_TO"]
    assert mapping[("FunctionalRequirement", "FunctionalRequirement")] == ["DEPENDS_ON", "RELATES_TO"]
    assert mapping[("Feature", "OpenQuestion")] == ["RELATES_TO"]
    assert mapping[("Module", "FunctionalRequirement")] == ["RELATES_TO"]
    assert mapping[("FunctionalRequirement", "Module")] == ["PART_OF", "RELATES_TO"]
    assert "RELATES_TO" not in mapping[("Module", "Module")]


def test_edge_type_map_has_no_duplicate_relation_names(shipped: Ontology) -> None:
    assert all(len(names) == len(set(names)) for names in shipped.edge_type_map().values())


# Fingerprints

PARTS = ("nodes", "edges", "attributes", "resolution")


def fingerprints(path: Path) -> dict[str, str]:
    loaded = load_ontology(path, submodule_files=[])
    return {part: loaded.fingerprint(part) for part in PARTS}


def test_fingerprints_are_distinct_hex_digests_and_stable_across_loads(shipped: Ontology) -> None:
    values = {part: shipped.fingerprint(part) for part in PARTS}
    assert len(set(values.values())) == 4
    assert all(len(value) == 64 and int(value, 16) >= 0 for value in values.values())
    again = load_ontology(submodule_files=[])
    assert {part: again.fingerprint(part) for part in PARTS} == values


def test_unknown_fingerprint_part_is_a_conformance_error(shipped: Ontology) -> None:
    with pytest.raises(ConformanceError) as raised:
        shipped.fingerprint("everything")
    assert raised.value.code == "ontology_unknown_fingerprint_part"


def test_fingerprints_survive_a_version_bump_and_a_name_change(tmp_path: Path) -> None:
    baseline = fingerprints(write_ontology(tmp_path / "base", BASE))
    bumped = fingerprints(
        write_ontology(
            tmp_path / "bumped",
            mutated(lambda d: put(d, "version", "2.7"), lambda d: put(d, "name", "renamed")),
        )
    )
    assert bumped == baseline


def test_fingerprints_survive_reformatting_the_file(tmp_path: Path) -> None:
    baseline = fingerprints(write_ontology(tmp_path / "base", BASE))
    reordered = yaml.safe_dump(BASE, sort_keys=False, width=40, default_flow_style=True)
    assert fingerprints(write_ontology(tmp_path / "again", reordered)) == baseline


def test_fingerprints_ignore_judge_prompt_text_and_inference_settings(tmp_path: Path) -> None:
    baseline = fingerprints(write_ontology(tmp_path / "base", BASE))
    path = write_ontology(tmp_path / "other", mutated(lambda d: put(d, "inference.cap", 7)))
    (path.parent / "judge_prompts" / "judge_endpoints.md").write_text("changed\n")
    assert fingerprints(path) == baseline


def changed_parts(tmp_path: Path, *edits: Callable[[dict[str, Any]], None]) -> set[str]:
    baseline = fingerprints(write_ontology(tmp_path / "base", BASE))
    edited = fingerprints(write_ontology(tmp_path / "edited", mutated(*edits)))
    return {part for part in PARTS if edited[part] != baseline[part]}


def test_a_node_description_edit_changes_the_node_and_resolution_fingerprints(
    tmp_path: Path,
) -> None:
    assert changed_parts(tmp_path, lambda d: put(d, "node_types.module.description", "Reworded definition.")) == {
        "nodes",
        "resolution",
    }


def test_a_node_guidance_edit_changes_only_the_node_fingerprint(tmp_path: Path) -> None:
    assert changed_parts(tmp_path, lambda d: put(d, "node_types.feature.guidance", "Different guidance.")) == {"nodes"}


def test_a_group_description_edit_changes_the_node_fingerprint(tmp_path: Path) -> None:
    assert "nodes" in changed_parts(
        tmp_path, lambda d: put(d, "node_groups.requirement.description", "Reworded group.")
    )


def test_a_relation_description_edit_changes_only_the_edge_fingerprint(tmp_path: Path) -> None:
    assert changed_parts(tmp_path, lambda d: put(d, "edge_types.PART_OF.description", "Reworded relation.")) == {
        "edges"
    }


def test_an_attribute_description_edit_changes_only_the_attribute_fingerprint(
    tmp_path: Path,
) -> None:
    assert changed_parts(
        tmp_path,
        lambda d: put(d, "node_types.module.attributes.stmt.description", "Reworded attribute."),
    ) == {"attributes"}


def test_an_alias_edit_changes_no_fingerprint(tmp_path: Path) -> None:
    """Aliases only normalise answers; they never reach a prompt."""

    assert changed_parts(tmp_path, lambda d: put(d, "node_types.module.attributes.type.aliases.vm", "backend")) == set()


def test_a_corpus_edit_changes_every_fingerprint(tmp_path: Path) -> None:
    assert changed_parts(tmp_path, lambda d: put(d, "corpus.framing", "a legal contract")) == set(PARTS)
    assert changed_parts(tmp_path / "exclude", lambda d: add(d, "corpus.exclude", "footnotes")) == set(PARTS)


def test_a_new_id_family_changes_the_node_fingerprint(tmp_path: Path) -> None:
    assert "nodes" in changed_parts(tmp_path, lambda d: add(d, "node_types.constraint.id_families", "LEGAL"))


def test_a_new_pair_changes_the_edge_fingerprint(tmp_path: Path) -> None:
    assert "edges" in changed_parts(
        tmp_path, lambda d: add(d, "edge_types.GROUPED_BY.pairs", {"from": "module", "to": "capability"})
    )


def test_target_exclude_where_is_an_attach_orphan_filter_over_its_targets(tmp_path: Path) -> None:
    """INF-CONT-01/02 leave out-of-scope targets out of their candidates ( owner precision);
    the condition is checked like ``exclude_where``, over the rule's targets."""

    shipped = load_ontology(SHIPPED_FILE, submodule_files=[])
    for rule in shipped.inference.rules:
        expected = {"in_scope": frozenset({"no"})} if rule.template == "attach_orphan" else {}
        assert rule.target_exclude_where == expected, rule.id

    def problems(name: str, index: int, condition: dict[str, list[str]]) -> tuple[str, ...]:
        data = copy.deepcopy(SHIPPED_DATA)
        put(data, f"inference.rules.{index}.target_exclude_where", condition)
        return load_problems(write_ontology(tmp_path / name, data, prompts_from=SHIPPED_PROMPTS), submodule_files=[])

    assert any(
        "inference.rules[2].target_exclude_where: not a parameter of template 'endpoints'" in problem
        for problem in problems("endpoints", 2, {"in_scope": ["no"]})
    )
    assert any(
        "target_exclude_where" in problem and "'colour' is not declared" in problem
        for problem in problems("undeclared", 0, {"colour": ["red"]})
    )
    assert any(
        "target_exclude_where" in problem and "'maybe' is not a value" in problem
        for problem in problems("value", 1, {"in_scope": ["maybe"]})
    )
