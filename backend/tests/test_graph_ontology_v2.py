"""Ontology format 2."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

import graph.core.ontology as ontology_package
from graph.core.errors import OntologyError
from graph.core.ontology import (
    QUARANTINE_REASONS,
    AnsweredElsewhereSpec,
    ConformSpec,
    DeriveViaRule,
    Dropped,
    Legalised,
    NodeView,
    Ontology,
    SpinePath,
    Support,
    load_ontology,
)
from graph_support import FROZEN_ONTOLOGY_FILE, frozen_ontology, with_v2_flags

SHIPPED_DIR = Path(ontology_package.__file__).parent
SHIPPED_FILE = SHIPPED_DIR / "spec-ontology.yaml"
SHIPPED_PROMPTS = SHIPPED_DIR / "judge_prompts"
FROZEN_PROMPTS = FROZEN_ONTOLOGY_FILE.parent / "judge_prompts"
DOCS_TEMPLATE = Path(__file__).resolve().parents[2] / "docs" / "graph" / "spec-ontology.yaml"
BASE: dict[str, Any] = yaml.safe_load(FROZEN_ONTOLOGY_FILE.read_text(encoding="utf-8"))
"""The flag-off ontology file, parsed: the base the flag mechanics edit."""
REQUIREMENT_LABELS = frozenset(
    {
        "BusinessRule",
        "Constraint",
        "DataRequirement",
        "FunctionalRequirement",
        "IntegrationRequirement",
        "QualityRequirement",
    }
)


def write(
    directory: Path,
    data: dict[str, Any] | str,
    *,
    extra_prompts: tuple[str, ...] = (),
    prompts_from: Path = FROZEN_PROMPTS,
) -> Path:
    """``data`` as ``ontology.yaml`` in ``directory`` with copies of the judge prompts in ``prompts_from``
    (default: the frozen ones), plus a stub prompt file for each of ``extra_prompts``."""

    directory.mkdir(parents=True, exist_ok=True)
    prompts = directory / "judge_prompts"
    if not prompts.exists():
        shutil.copytree(prompts_from, prompts)
    for ref in extra_prompts:
        (prompts / f"{ref}.md").write_text(f"Stub judge prompt {ref} (test only).\n", encoding="utf-8")
    path = directory / "ontology.yaml"
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def load(directory: Path, data: dict[str, Any], **kwargs: Any) -> Ontology:
    return load_ontology(write(directory, data, **kwargs), submodule_files=[])


@pytest.fixture(scope="module")
def flagged(tmp_path_factory: pytest.TempPathFactory) -> Ontology:
    """The flag-off ontology with every format-2 flag set (``graph_support.with_v2_flags``)."""

    return load(tmp_path_factory.mktemp("flagged"), with_v2_flags(BASE))


# The frozen test ontology


def test_the_frozen_file_is_format_2_with_every_new_flag_off() -> None:
    shipped = frozen_ontology()
    assert BASE["format_version"] == 2
    assert shipped.version == "1.3"
    assert shipped.conform_spec() is None
    assert shipped.spine_exclude() == {}
    assert shipped.acyclic_relations() == frozenset()
    assert shipped.stated_only_relations() == frozenset()
    assert shipped.derive_via_rules() == ()
    for label in shipped.labels:
        assert shipped.owner_pairs(label) == ()
        assert shipped.supported_by(label) == ()
        assert shipped.merge_across_labels(label)
        assert shipped.inherit_rules(label) == {}
        assert all(spec.default is None for spec in shipped.attribute_specs(label).values())
    for relation in shipped.relations.values():
        for pair in relation.pairs:
            assert pair.relation == relation.name
            assert (pair.owner, pair.owner_rank, pair.where_reason, pair.otherwise) == (
                False,
                1,
                "condition_failed",
                None,
            )
            assert not (pair.source_where or pair.target_where or pair.not_across)
            assert (pair.max_depth, pair.distinct_owner, pair.redundant_via_owner) == (None, False, False)
    for rule in shipped.inference.rules:
        assert rule.exclude_owned_by == frozenset() and rule.exclude_where == {}


def test_the_frozen_test_ontology_loads_with_its_frozen_prompts() -> None:
    frozen = frozen_ontology()
    assert frozen.version == "1.3"
    assert {rule.prompt for rule in frozen.inference.rules} == set(frozen.judge_prompts)
    for ref, text in frozen.judge_prompts.items():
        assert text == (FROZEN_ONTOLOGY_FILE.parent / "judge_prompts" / f"{ref}.md").read_text(encoding="utf-8")


def test_the_v2_docs_template_loads_with_no_problem(tmp_path: Path) -> None:
    """The docs ontology template loads. Prompts it names that do not ship yet get a test-only stub."""

    data = yaml.safe_load(DOCS_TEMPLATE.read_text(encoding="utf-8"))
    refs = {rule["prompt"] for rule in data["inference"]["rules"]} | {data["conform"]["judge_prompt"]}
    missing = tuple(sorted(ref for ref in refs if not (SHIPPED_PROMPTS / f"{ref}.md").is_file()))
    template = load_ontology(
        write(tmp_path, DOCS_TEMPLATE.read_text(encoding="utf-8"), extra_prompts=missing, prompts_from=SHIPPED_PROMPTS),
        submodule_files=[],
    )
    assert template.version == "2.0"
    answered = AnsweredElsewhereSpec("OpenQuestion", "judge_answered_elsewhere.3", 4, 0.35, 0.8, 60)
    assert template.conform_spec() == ConformSpec("judge_owner_conflict.2", 200, 0.7, "RELATES_TO", answered)
    assert [rule.id for rule in template.inference.rules][-1] == "INF-XDOC-01"
    assert template.stated_only_relations() == template.acyclic_relations() == frozenset({"DEPENDS_ON"})
    assert template.default_relations("Module", "Module") == ("INTEGRATES_WITH", "DEPLOYED_TO")
    for label in REQUIREMENT_LABELS:
        assert template.default_relations(label, "DataEntity") == ("RELATES_TO",)
        owned = label not in {"DataRequirement", "IntegrationRequirement"}
        assert template.default_relations(label, "Feature") == (("PART_OF",) if owned else ())
    assert [(pair.target, pair.owner_rank) for pair in template.owner_pairs("IntegrationRequirement")] == [
        ("Interface", 1),
        ("Module", 2),
    ]
    assert template.inherit_rules("FunctionalRequirement") == {"release": "PART_OF"}
    assert template.attribute_default("Feature", "in_scope") == "yes"
    assert not template.merge_across_labels("Interface")
    assert {support.relation for support in template.supported_by("DataEntity")} == {
        "PART_OF",
        "STORED_IN",
        "CARRIES",
    }
    assert template.spine_exclude() == {"in_scope": frozenset({"no"})}
    assert template.derive_via_rules() == (DeriveViaRule("INTEGRATES_WITH", "CONSUMED_BY", "PROVIDED_BY"),)
    backend = NodeView("Module", {"type": "backend"})
    deployment = NodeView("Module", {"type": "deployment"})
    assert template.legalise("CALLS", backend, deployment, []) == Legalised("DEPLOYED_TO", flipped=False)
    assert template.legalise("CALLS", backend, backend, []) == Legalised("INTEGRATES_WITH", flipped=False)


# The shipped file and the docs template agree in structure

PROMPT_TEXT_KEYS = frozenset({"description", "guidance", "aliases", "cues"})
"""Prompt text a type, relation or attribute may word differently in the shipped file."""


def _structure(value: Any) -> Any:
    """``value`` without its prompt text: no descriptions, guidance, aliases or cues, and enum values (or
    a vocabulary) given as a description mapping reduced to the list of values, in order."""

    if isinstance(value, dict):
        return {
            key: list(item) if key == "values" and isinstance(item, dict) else _structure(item)
            for key, item in value.items()
            if key not in PROMPT_TEXT_KEYS
        }
    if isinstance(value, list):
        return [_structure(item) for item in value]
    return value


def _file_structure(data: dict[str, Any]) -> dict[str, Any]:
    """The structure of a parsed ontology file (``_structure``), without ``corpus`` (prompt text) and
    without the judge prompt references of inference rules and conform (judge prompts are prompt text)."""

    structure = _structure({key: value for key, value in data.items() if key != "corpus"})
    structure["vocabularies"] = {name: list(values) for name, values in (data.get("vocabularies") or {}).items()}
    for rule in structure["inference"]["rules"]:
        rule.pop("prompt")
    structure["conform"].pop("judge_prompt")
    return structure


def test_the_shipped_file_has_the_structure_of_the_docs_template() -> None:
    """Everything but prompt text is equal: labels, groups, roles, ids, identity, flags, attributes (kinds,
    values, defaults, inherit), relations and their pairs with all flags, spine, conform and inference."""

    template = yaml.safe_load(DOCS_TEMPLATE.read_text(encoding="utf-8"))
    shipped = yaml.safe_load(SHIPPED_FILE.read_text(encoding="utf-8"))
    assert _file_structure(shipped) == _file_structure(template)
    # Declaration order matters too (the first default of a label pair, rule order, prompt rendering).
    for section in ("node_groups", "node_types", "edge_types"):
        assert list(shipped[section]) == list(template[section]), section
        for key, entry in shipped[section].items():
            assert list(entry.get("attributes") or {}) == list(template[section][key].get("attributes") or {}), key
    assert [rule["id"] for rule in shipped["inference"]["rules"]] == [
        rule["id"] for rule in template["inference"]["rules"]
    ]


@pytest.mark.parametrize("path", [SHIPPED_FILE, DOCS_TEMPLATE], ids=["shipped", "docs-template"])
def test_the_requirement_group_reads_a_table_as_the_sentences_it_stands_for(path: Path) -> None:
    """at ontology level, in both copies; the version stays 2.0."""

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["version"] == "2.0"
    text = " ".join(data["node_groups"]["requirement"]["description"].split())
    for rule in (
        "A table stands for sentences: read it as them.",
        "requirements, rules, settings, services, environments",
        "is one requirement with all its cells, never one per cell",
        "are one DataRequirement",
        "gives one requirement per meaningful cell, named as the sentence the cell stands for",
        "same-value cells of one row are one requirement naming all their columns",
        "a value every row of a column shares is one requirement for that column",
        '"n/a" or "not applicable"',
        "unless the",
        "who may or may not do what is a BusinessRule",
        "also when the row names a hub",
        "only lists terms or people",
        "a list of feature, module or interface names give no requirement.",
    ):
        assert rule.casefold() in text.casefold(), rule
    for rule in (
        "if it takes two test cases to prove two distinct rules, it is two requirements",
        "applies to a list of channels, objects, fields or places is one requirement covering the whole list",
        "every row is its own requirement",
        '"System never writes secret values to payloads, logs or traces"',
        "unless its cells state distinct rules",
    ):
        assert rule.casefold() in text.casefold(), rule
    question = " ".join(data["node_types"]["open_question"]["description"].split())
    assert "either-or question" in question and "never one per option" in question


def test_the_structure_comparison_sees_a_flag_and_ignores_prompt_text() -> None:
    template = yaml.safe_load(DOCS_TEMPLATE.read_text(encoding="utf-8"))
    reworded = copy.deepcopy(template)
    reworded["corpus"]["exclude"].append("emoji")
    reworded["node_types"]["module"]["description"] = "Reworded."
    reworded["node_types"]["module"]["attributes"]["type"]["values"]["other"] = "anything else"
    reworded["edge_types"]["PART_OF"]["cues"] = ["consists of"]
    reworded["conform"]["judge_prompt"] = "judge_owner_conflict.3"
    assert _file_structure(reworded) == _file_structure(template)
    flagged = copy.deepcopy(template)
    flagged["edge_types"]["PART_OF"]["pairs"][0]["owner_rank"] = 2
    assert _file_structure(flagged) != _file_structure(template)
    defaulted = copy.deepcopy(template)
    defaulted["node_types"]["module"]["attributes"]["in_scope"]["default"] = "no"
    assert _file_structure(defaulted) != _file_structure(template)


# Format version


def test_a_format_1_file_is_rejected_with_a_clear_message(tmp_path: Path) -> None:
    data = copy.deepcopy(BASE)
    data["format_version"] = 1
    with pytest.raises(OntologyError) as raised:
        load(tmp_path, data)
    assert raised.value.problems == (
        (
            f"{tmp_path / 'ontology.yaml'}: format_version: format_version 1 is not supported; this library reads "
            "format_version 2 (docs/graph/ontology.md)"
        ),
    )


# The valid case of each flag (``with_v2_flags``) and the compiled API that reads it


def test_attribute_default_and_inherit(flagged: Ontology) -> None:
    assert flagged.attribute_default("Module", "external") == "no"
    assert flagged.attribute_default("Module", "type") is None
    assert flagged.attribute_default("Module", "nothing") is None
    assert flagged.attribute_default("Nowhere", "external") is None
    for label in REQUIREMENT_LABELS:
        assert flagged.inherit_rules(label) == {"acceptance_criteria": "PART_OF"}
    assert flagged.inherit_rules("Module") == {}


def test_owner_pairs_are_ordered_by_rank(flagged: Ontology) -> None:
    pairs = flagged.owner_pairs("FunctionalRequirement")
    assert [(pair.relation, pair.target, pair.owner_rank) for pair in pairs] == [
        ("PART_OF", "Feature", 1),
        ("PART_OF", "Module", 2),
    ]
    assert flagged.owner_pairs("Feature") == ()
    assert flagged.exclusive_pairs == frozen_ontology().exclusive_pairs


def test_pair_conditions_and_normalising_flags(flagged: Ontology) -> None:
    owned = flagged.pair_rule("PART_OF", "FunctionalRequirement", "Module")
    assert owned is not None
    assert owned.target_where == {"external": frozenset({"no"})} and owned.source_where == {}
    assert (owned.otherwise, owned.where_reason) == ("RELATES_TO", "external_owner")
    assert owned.where_reason in QUARANTINE_REASONS
    nested = flagged.pair_rule("PART_OF", "Module", "Module")
    assert nested is not None
    assert (nested.max_depth, nested.not_across, nested.default) == (1, ("external",), True)
    relates = flagged.pair_rule("RELATES_TO", "Constraint", "BusinessRule")
    assert relates is not None and relates.distinct_owner
    grouped = flagged.pair_rule("GROUPED_BY", "Constraint", "Capability")
    assert grouped is not None and grouped.redundant_via_owner
    assert flagged.pair_rule("PART_OF", "Module", "Feature") is None


def test_where_holds_uses_the_attribute_default_and_lets_null_pass(flagged: Ontology) -> None:
    pair = flagged.pair_rule("PART_OF", "FunctionalRequirement", "Module")
    assert pair is not None
    requirement = NodeView("FunctionalRequirement")
    assert flagged.where_holds(pair, requirement, NodeView("Module", {"external": "no"}))
    assert not flagged.where_holds(pair, requirement, NodeView("Module", {"external": "yes"}))
    assert flagged.where_holds(pair, requirement, "Module")  # null: the default "no" holds
    to_deployment = flagged.pair_rule("PART_OF", "Module", "Module")
    assert to_deployment is not None
    assert flagged.where_holds(to_deployment, "Module", "Module")  # null type, no default: holds


def test_condition_matches_reads_the_default_and_never_matches_null_without_one(flagged: Ontology) -> None:
    """Spine ``exclude`` and ``exclude_where`` semantics: the opposite of ``where`` for a bare null."""

    external = flagged.spine_exclude()
    assert flagged.condition_matches(external, NodeView("Module", {"external": "yes"}))
    assert not flagged.condition_matches(external, NodeView("Module", {"external": "no"}))
    assert not flagged.condition_matches(external, "Module")  # null: the default "no" does not match
    assumption = {"is_assumption": frozenset({"yes"})}
    assert flagged.condition_matches(assumption, NodeView("Constraint", {"is_assumption": "yes"}))
    assert not flagged.condition_matches(assumption, "Constraint")  # null, no default: no match
    assert not flagged.condition_matches({}, NodeView("Module", {"external": "yes"}))
    excluded_pair = flagged.pair_rule("PART_OF", "FunctionalRequirement", "Module")
    assert excluded_pair is not None
    assert flagged.where_holds(excluded_pair, "FunctionalRequirement", "Module")  # where: null holds


def test_relation_flags(flagged: Ontology) -> None:
    assert flagged.acyclic_relations() == frozenset({"GROUPED_BY"})
    assert flagged.stated_only_relations() == frozenset({"GROUPED_BY"})
    assert flagged.derive_via_rules() == (DeriveViaRule("INTEGRATES_WITH", "REALIZED_BY", "REALIZED_BY"),)
    assert flagged.relation("INTEGRATES_WITH").derive_via == flagged.derive_via_rules()[0]


def test_node_type_flags(flagged: Ontology) -> None:
    assert flagged.supported_by("Capability") == (Support("REALIZED_BY", "out"), Support("GROUPED_BY", "in"))
    assert flagged.supported_by("Module") == ()
    assert not flagged.merge_across_labels("Capability")
    assert flagged.merge_across_labels("Module")


def test_spine_exclude_may_name_an_attribute_of_the_owner(flagged: Ontology) -> None:
    assert flagged.spine_exclude() == {"external": frozenset({"yes"})}


def test_conform_section_and_its_judge_prompt(flagged: Ontology) -> None:
    assert flagged.conform_spec() == ConformSpec("judge_endpoints.2", 10, 0.5, "RELATES_TO")
    assert flagged.judge_prompt("judge_endpoints.2") == (FROZEN_PROMPTS / "judge_endpoints.2.md").read_text(
        encoding="utf-8"
    )


def test_a_conform_prompt_loads_from_judge_prompts_once_its_file_exists(tmp_path: Path) -> None:
    data = with_v2_flags(BASE)
    data["conform"]["judge_prompt"] = "judge_owner_conflict.1"
    with pytest.raises(OntologyError) as raised:
        load(tmp_path / "missing", data)
    assert any("judge_prompts/judge_owner_conflict.1.md not found" in problem for problem in raised.value.problems)
    loaded = load(tmp_path / "present", data, extra_prompts=("judge_owner_conflict.1",))
    assert loaded.judge_prompt("judge_owner_conflict.1").startswith("Stub judge prompt")


def test_inference_parameters(flagged: Ontology) -> None:
    by_id = {rule.id: rule for rule in flagged.inference.rules}
    assert by_id["INF-INT-01"].exclude_owned_by == frozenset({"Feature"})
    realization = by_id["INF-CAP-01"]
    assert realization.container_paths == (
        SpinePath(("PART_OF",), sequence=False, min_hops=1, max_hops=3),
        SpinePath(("RELATES_TO",), sequence=False, min_hops=1, max_hops=1),
    )
    assert by_id["INF-CONT-01"].exclude_where == {"is_assumption": frozenset({"yes"})}


def test_the_frozen_realization_rule_has_one_container_path() -> None:
    rule = next(rule for rule in frozen_ontology().inference.rules if rule.template == "realization")
    assert rule.container_paths == (SpinePath(("PART_OF",), sequence=False, min_hops=1, max_hops=3),)


def test_the_earlier_realization_form_is_rejected(tmp_path: Path) -> None:
    """``container_relation`` with ``max_hops`` is not a parameter."""

    data = copy.deepcopy(BASE)
    rule = data["inference"]["rules"][1]
    del rule["container_paths"]
    rule.update(container_relation="PART_OF", max_hops=3)
    with pytest.raises(OntologyError) as raised:
        load(tmp_path, data)
    problems = " ".join(raised.value.problems)
    assert "inference.rules.1.container_relation: unknown key" in problems
    assert "inference.rules.1.max_hops: unknown key" in problems
    assert "inference.rules[1].container_paths: required by template 'realization'" in problems


# Legalisation: attributes choose between several defaults of one label pair


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param(NodeView("Module", {"type": "deployment"}), "PART_OF", id="deployment-meets-part-of"),
        pytest.param(NodeView("Module", {"type": "backend"}), "INTEGRATES_WITH", id="backend-meets-integrates"),
        pytest.param(NodeView("Module", {"type": "frontend"}), "PART_OF", id="none-meets-first-declared"),
        pytest.param(NodeView("Module"), "PART_OF", id="null-meets-both-first-declared"),
        pytest.param("Module", "PART_OF", id="bare-label"),
    ],
)
def test_legalise_chooses_the_default_whose_where_the_ends_meet(
    flagged: Ontology, target: NodeView | str, expected: str
) -> None:
    """PART_OF is declared before INTEGRATES_WITH in the file (alphabetically it is after it)."""

    assert flagged.default_relations("Module", "Module") == ("PART_OF", "INTEGRATES_WITH")
    assert flagged.default_relation("Module", "Module") == "PART_OF"
    assert flagged.legalise("CALLS", NodeView("Module"), target, []) == Legalised(expected, flipped=False)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param(NodeView("Module", {"type": "deployment"}), "PART_OF", id="deployment"),
        pytest.param(NodeView("Module", {"type": "backend"}), "INTEGRATES_WITH", id="backend"),
        pytest.param(NodeView("Module", {"type": "frontend"}), "PART_OF", id="none-meets"),
        pytest.param("Module", "PART_OF", id="bare-label"),
    ],
)
def test_default_for_chooses_by_attributes_as_legalise_does(
    flagged: Ontology, target: NodeView | str, expected: str
) -> None:
    assert flagged.default_for("Module", target) == expected
    assert flagged.default_relation("Module", "Module") == "PART_OF"  # label only
    assert flagged.default_for("Feature", "Module") is None


def test_legalise_reads_a_null_value_as_the_attribute_default(tmp_path: Path) -> None:
    data = with_v2_flags(BASE)
    data["node_types"]["module"]["attributes"]["type"]["default"] = "backend"
    loaded = load(tmp_path, data)
    assert loaded.legalise("CALLS", "Module", NodeView("Module", {"type": None}), []) == Legalised(
        "INTEGRATES_WITH", flipped=False
    )
    assert loaded.legalise("CALLS", "Module", NodeView("Module", {"type": "deployment"}), []) == Legalised(
        "PART_OF", flipped=False
    )


def test_legalise_keeps_a_legal_relation_whatever_the_where(flagged: Ontology) -> None:
    """``where`` is applied by normalisation, not by legalisation."""

    external = NodeView("Module", {"external": "yes"})
    assert flagged.legalise("PART_OF", "FunctionalRequirement", external, []) == Legalised("PART_OF", flipped=False)
    assert flagged.legalise("PART_OF", external, "FunctionalRequirement", []) == Legalised("PART_OF", flipped=True)
    assert flagged.legalise("REALIZED_BY", "Feature", "Module", []) == Dropped("illegal_pair")


def test_legalise_with_node_views_matches_bare_labels_without_a_where() -> None:
    shipped = frozen_ontology()
    for source, target in (("Module", "Module"), ("FunctionalRequirement", "Feature"), ("Feature", "Capability")):
        for relation in ("PART_OF", "GROUPED_BY", "CALLS", "RELATES_TO"):
            views = (NodeView(source, {"type": "deployment"}), NodeView(target, {"type": "deployment"}))
            assert shipped.legalise(relation, *views, []) == shipped.legalise(relation, source, target, [])


# Fingerprint


def test_stated_only_changes_only_the_edge_fingerprint(tmp_path: Path) -> None:
    data = copy.deepcopy(BASE)
    data["edge_types"]["GROUPED_BY"]["stated_only"] = True
    changed = load(tmp_path, data)
    shipped = frozen_ontology()
    assert changed.fingerprint("edges") != shipped.fingerprint("edges")
    for part in ("nodes", "attributes", "resolution"):
        assert changed.fingerprint(part) == shipped.fingerprint(part)


def test_other_format_2_flags_leave_the_fingerprints_unchanged(tmp_path: Path) -> None:
    data = with_v2_flags(BASE)
    data["edge_types"]["GROUPED_BY"]["stated_only"] = False
    changed = load(tmp_path, data)
    shipped = frozen_ontology()
    for part in ("nodes", "edges", "attributes", "resolution"):
        assert changed.fingerprint(part) == shipped.fingerprint(part)
