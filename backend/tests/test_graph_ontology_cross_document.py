"""Ontology format of: the ``cross_document`` template and its parameters, ``also_writes``, the rule ``cap``, relation
cues as a whole-word gate (the ``stated_only`` exception, check 12), ``conform.answered_elsewhere`` (check 15) and a
``store: false`` provider reference that derives an exclusive relation.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

import graph.core.ontology as ontology_package
from graph.core.errors import ConformanceError, OntologyError
from graph.core.ontology import AnsweredElsewhereSpec, ConformSpec, DeriveRule, Ontology, load_ontology
from graph_support import frozen_ontology

SHIPPED_DIR = Path(ontology_package.__file__).parent
SHIPPED_FILE = SHIPPED_DIR / "spec-ontology.yaml"
SHIPPED_PROMPTS = SHIPPED_DIR / "judge_prompts"
XDOC_PROMPT = "judge_xdoc_test"
ANSWERED_PROMPT = "judge_answered_test"
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
DEPENDENCY_CUES = ["requires", "depends on", "only after"]
XDOC_RULE: dict[str, Any] = {
    "id": "INF-XDOC-T",
    "template": "cross_document",
    "prompt": XDOC_PROMPT,
    "subject": ["module", "feature", "interface", "data_entity"],
    "members": "requirement",
    "via": ["PART_OF", "RELATES_TO"],
    "mentions": True,
    "min_documents": 2,
    "max_pairs": 8,
    "pair_floor": 0.6,
    "max_candidates": 3,
    "exclude_where": {"in_scope": ["no"]},
    "writes": "RELATES_TO",
    "also_writes": ["DEPENDS_ON", "PART_OF"],
    "cap": 150,
}
ANSWERED: dict[str, Any] = {
    "label": "open_question",
    "prompt": ANSWERED_PROMPT,
    "candidates": 3,
    "min_cosine": 0.5,
    "floor": 0.8,
    "cap": 60,
}


def base() -> dict[str, Any]:
    """The shipped file without ``cross_document`` rules or ``answered_elsewhere``, cues on DEPENDS_ON."""

    data: dict[str, Any] = yaml.safe_load(SHIPPED_FILE.read_text(encoding="utf-8"))
    data["inference"]["rules"] = [rule for rule in data["inference"]["rules"] if rule["template"] != "cross_document"]
    data["conform"].pop("answered_elsewhere", None)
    data["edge_types"]["DEPENDS_ON"]["cues"] = list(DEPENDENCY_CUES)
    return data


def with_rule(**changes: Any) -> dict[str, Any]:
    """``base()`` with the test cross-document rule last; a change of None removes that parameter."""

    data = base()
    rule = {**copy.deepcopy(XDOC_RULE), **changes}
    data["inference"]["rules"].append({key: value for key, value in rule.items() if value is not None})
    return data


def load(directory: Path, data: dict[str, Any]) -> Ontology:
    directory.mkdir(parents=True, exist_ok=True)
    prompts = directory / "judge_prompts"
    shutil.copytree(SHIPPED_PROMPTS, prompts)
    for ref in (XDOC_PROMPT, ANSWERED_PROMPT):
        (prompts / f"{ref}.md").write_text(f"Stub judge prompt {ref} (test only).\n", encoding="utf-8")
    path = directory / "ontology.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_ontology(path, submodule_files=[])


@pytest.fixture(scope="module")
def xdoc(tmp_path_factory: pytest.TempPathFactory) -> Ontology:
    return load(tmp_path_factory.mktemp("xdoc"), with_rule())


def rule_of(ontology: Ontology, rule_id: str = "INF-XDOC-T") -> Any:
    return next(rule for rule in ontology.inference.rules if rule.id == rule_id)


# The cross_document template


def test_a_cross_document_rule_compiles_every_parameter(xdoc: Ontology) -> None:
    rule = rule_of(xdoc)
    assert rule.template == "cross_document"
    assert rule.subject == frozenset({"Module", "Feature", "Interface", "DataEntity"})
    assert rule.targets == REQUIREMENT_LABELS
    assert rule.via == ("PART_OF", "RELATES_TO")
    assert rule.mentions
    assert (rule.min_documents, rule.max_pairs, rule.pair_floor, rule.max_candidates) == (2, 8, 0.6, 3)
    assert rule.exclude_where == {"in_scope": frozenset({"no"})}
    assert rule.writes == "RELATES_TO"
    assert rule.also_writes == ("DEPENDS_ON", "PART_OF")
    assert rule.written == ("RELATES_TO", "DEPENDS_ON", "PART_OF")
    assert xdoc.judge_prompt(XDOC_PROMPT).startswith("Stub judge prompt")
    assert xdoc.inference.rules[-1] == rule


def test_the_optional_parameters_may_be_left_out(tmp_path: Path) -> None:
    loaded = load(tmp_path, with_rule(mentions=None, exclude_where=None, also_writes=None, cap=None))
    rule = rule_of(loaded)
    assert not rule.mentions
    assert rule.exclude_where == {}
    assert rule.also_writes == ()
    assert rule.written == ("RELATES_TO",)
    assert rule.cap is None


def test_a_single_subject_key_is_one_subject(tmp_path: Path) -> None:
    rule = rule_of(load(tmp_path, with_rule(subject="module")))
    assert rule.subject == frozenset({"Module"})


def test_an_owner_relation_is_legal_to_the_hub_and_to_owner_targets(tmp_path: Path) -> None:
    """``PART_OF`` alone in ``also_writes`` with only module subjects: requirement -> module is legal."""

    rule = rule_of(load(tmp_path, with_rule(subject=["module"], also_writes=["PART_OF"])))
    assert rule.written == ("RELATES_TO", "PART_OF")


# Rule cap


def test_the_rule_cap_overrides_the_inference_cap(xdoc: Ontology) -> None:
    assert xdoc.inference.cap_of(rule_of(xdoc)) == 150
    for rule in xdoc.inference.rules[:-1]:
        assert rule.cap is None
        assert xdoc.inference.cap_of(rule) == xdoc.inference.cap


def test_any_template_may_set_a_cap(tmp_path: Path) -> None:
    data = base()
    data["inference"]["rules"][0]["cap"] = 7
    loaded = load(tmp_path, data)
    first, *rest = loaded.inference.rules
    assert (first.cap, loaded.inference.cap_of(first)) == (7, 7)
    assert all(loaded.inference.cap_of(rule) == loaded.inference.cap for rule in rest)


# Cues: the stated_only exception


def test_only_the_rule_naming_a_stated_only_relation_in_also_writes_may_infer_it(xdoc: Ontology) -> None:
    assert "DEPENDS_ON" in xdoc.stated_only_relations()
    assert xdoc.cue_gated_rules("DEPENDS_ON") == frozenset({"INF-XDOC-T"})
    assert xdoc.cue_gated_rules("PART_OF") == frozenset()
    assert xdoc.cue_gated_rules("RELATES_TO") == frozenset()


def test_without_a_cross_document_rule_no_rule_may_infer_a_stated_only_relation(tmp_path: Path) -> None:
    assert load(tmp_path, base()).cue_gated_rules("DEPENDS_ON") == frozenset()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Checkout requires an open session.", True, id="word"),
        pytest.param("Checkout REQUIRES an open session.", True, id="case"),
        pytest.param("Export runs only after the nightly import.", True, id="phrase"),
        pytest.param("Billing depends on the ledger.", True, id="two-words"),
        pytest.param("The prerequisites are listed below.", False, id="inside-a-word"),
        pytest.param("All requirements are listed below.", False, id="prefix-of-a-word"),
        pytest.param("Payments relate to orders.", False, id="no-cue"),
        pytest.param("", False, id="empty"),
    ],
)
def test_cue_matches_whole_words_ignoring_case(xdoc: Ontology, text: str, *, expected: bool) -> None:
    assert xdoc.cue_matches("DEPENDS_ON", text) is expected


def test_cue_matches_is_false_for_a_relation_without_cues_and_fails_for_an_unknown_one(xdoc: Ontology) -> None:
    assert xdoc.relation("RELATES_TO").cues == ()
    assert not xdoc.cue_matches("RELATES_TO", "Checkout requires an open session.")
    with pytest.raises(ConformanceError):
        xdoc.cue_matches("NOPE", "requires")


def test_a_cue_with_a_leading_inline_flag_is_an_ontology_error(tmp_path: Path) -> None:
    """``(?i)requires`` compiles alone but not inside the whole-word wrapper: a listed problem, not a crash."""

    data = base()
    data["edge_types"]["DEPENDS_ON"]["cues"] = ["requires", "(?i)depends on"]
    with pytest.raises(OntologyError, match=r"edge_types\.DEPENDS_ON\.cues\[1\].*cannot be matched as whole words"):
        load(tmp_path, data)


def test_cue_required_legalisation_still_searches_anywhere_in_the_line() -> None:
    """The whole-word gate is ``cue_matches`` only; ``cue_required`` pairs keep their substring search."""

    shipped = load_ontology(submodule_files=[])
    assert shipped.legalise("PART_OF", "Module", "Module", ["It has two subsystems."]).relation == "PART_OF"
    assert not shipped.cue_matches("PART_OF", "It has two subsystems.")
    assert shipped.cue_matches("PART_OF", "The cart is a sub-service of checkout.")


def test_cues_and_the_cross_document_rule_leave_every_fingerprint_unchanged(xdoc: Ontology) -> None:
    shipped = load_ontology(submodule_files=[])
    for part in ("nodes", "edges", "attributes", "resolution"):
        assert xdoc.fingerprint(part) == shipped.fingerprint(part)


# conform.answered_elsewhere


@pytest.mark.parametrize("label", ["open_question", "OpenQuestion"], ids=["type-key", "label"])
def test_the_answered_elsewhere_block_compiles(tmp_path: Path, label: str) -> None:
    data = base()
    data["conform"]["answered_elsewhere"] = {**ANSWERED, "label": label}
    loaded = load(tmp_path, data)
    spec = loaded.conform_spec()
    assert spec is not None
    assert spec.answered_elsewhere == AnsweredElsewhereSpec("OpenQuestion", ANSWERED_PROMPT, 3, 0.5, 0.8, 60)
    assert loaded.judge_prompt(ANSWERED_PROMPT).startswith("Stub judge prompt")


def test_without_the_block_the_check_is_off(tmp_path: Path) -> None:
    spec = load(tmp_path, base()).conform_spec()
    assert spec == ConformSpec("judge_owner_conflict", 200, 0.7, "RELATES_TO")
    assert spec is not None and spec.answered_elsewhere is None


# A provider reference (store: false) deriving an exclusive relation needs nothing new


def test_a_provider_reference_derives_the_exclusive_provided_by(tmp_path: Path) -> None:
    data = base()
    data["node_types"]["interface"]["attributes"]["provider"] = {
        "type": "ref",
        "target": "module",
        "store": False,
        "description": "the module the text names as providing the contract",
    }
    data["edge_types"]["PROVIDED_BY"]["derive"] = ["interface.provider"]
    loaded = load(tmp_path, data)
    derived = DeriveRule("PROVIDED_BY", frozenset({"Interface"}), "provider", frozenset({"Module"}))
    assert derived in loaded.derive_rules
    assert not loaded.attribute_specs("Interface")["provider"].store


# The earlier files are unchanged


def test_the_frozen_and_shipped_rules_have_no_cap_and_write_one_relation() -> None:
    for loaded in (frozen_ontology(), load_ontology(submodule_files=[])):
        for rule in loaded.inference.rules:
            if rule.template == "cross_document":
                continue
            assert (rule.cap, rule.also_writes, rule.written) == (None, (), (rule.writes,))
            assert loaded.inference.cap_of(rule) == loaded.inference.cap
            assert (rule.min_documents, rule.max_pairs, rule.pair_floor) == (None, None, None)
