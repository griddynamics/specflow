"""The plan rules file loads and is validated against the merged ontology."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from graph.core import load_ontology
from graph.plan.errors import PlanRulesError
from graph.plan.rules import load_plan_rules


def write_rules(path: Path, changes: dict[str, object]) -> Path:
    data = yaml.safe_load(
        (Path(__file__).parent.parent / "src" / "graph" / "plan" / "plan-rules.yaml").read_text(encoding="utf-8")
    )
    data = deepcopy(data)
    data.update(changes)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_the_shipped_plan_rules_load() -> None:
    rules = load_plan_rules(ontology=load_ontology())

    assert rules.name == "plan-rules" and rules.version == "2.0"
    assert rules.labels_of("functional_requirement") == ("FunctionalRequirement",)
    assert rules.rule_of("FunctionalRequirement").required
    assert rules.rule_of("Interface").story
    assert rules.relation("containment") == "PART_OF"
    assert rules.relation_role("PART_OF") == "containment"
    assert rules.relation("deploy") == "DEPLOYED_TO"
    assert rules.relation("provision") == "PROVIDED_BY"
    assert rules.relation("consumption") == "CONSUMED_BY"
    assert rules.hop_limit == 3
    assert len(rules.phases) == 7 and rules.phase_guidance.strip()


def test_an_unknown_type_and_illegal_in_area_source_raise_both_problems(tmp_path: Path) -> None:
    rules = yaml.safe_load(
        (Path(__file__).parent.parent / "src" / "graph" / "plan" / "plan-rules.yaml").read_text(encoding="utf-8")
    )
    rules["types"]["unknown"] = {"role": "member", "phase": "logic"}
    rules["types"]["open_question"] = {"role": "member", "phase": "logic"}
    path = tmp_path / "plan-rules.yaml"
    path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")

    with pytest.raises(PlanRulesError) as raised:
        load_plan_rules(path, ontology=load_ontology())

    assert any("unknown" in problem for problem in raised.value.problems)
    assert any("IN_AREA" in problem for problem in raised.value.problems)


def test_story_on_a_non_story_member_is_refused(tmp_path: Path) -> None:
    rules = yaml.safe_load(
        (Path(__file__).parent.parent / "src" / "graph" / "plan" / "plan-rules.yaml").read_text(encoding="utf-8")
    )
    rules["types"]["functional_requirement"]["story"] = "feature"
    path = tmp_path / "plan-rules.yaml"
    path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")

    with pytest.raises(PlanRulesError) as raised:
        load_plan_rules(path, ontology=load_ontology())

    assert any("functional_requirement" in problem for problem in raised.value.problems)


def test_a_story_value_must_be_a_story_kind(tmp_path: Path) -> None:
    rules = yaml.safe_load(
        (Path(__file__).parent.parent / "src" / "graph" / "plan" / "plan-rules.yaml").read_text(encoding="utf-8")
    )
    rules["types"]["interface"]["story"] = True
    path = tmp_path / "plan-rules.yaml"
    path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")

    with pytest.raises(PlanRulesError) as raised:
        load_plan_rules(path, ontology=load_ontology())

    assert any("types.interface.story" in problem for problem in raised.value.problems)


def test_check_5_a_missing_relation_role_is_refused(tmp_path: Path) -> None:
    data = yaml.safe_load(
        (Path(__file__).parent.parent / "src" / "graph" / "plan" / "plan-rules.yaml").read_text(encoding="utf-8")
    )
    del data["relations"]["containment"]
    path = write_rules(tmp_path / "plan-rules.yaml", data)

    with pytest.raises(PlanRulesError) as raised:
        load_plan_rules(path, ontology=load_ontology())

    assert any("relation roles are missing" in problem for problem in raised.value.problems)


def test_check_6_an_undeclared_release_attribute_is_refused(tmp_path: Path) -> None:
    data = yaml.safe_load(
        (Path(__file__).parent.parent / "src" / "graph" / "plan" / "plan-rules.yaml").read_text(encoding="utf-8")
    )
    data["release"] = {"attribute": "missing_release"}
    path = write_rules(tmp_path / "plan-rules.yaml", data)

    with pytest.raises(PlanRulesError) as raised:
        load_plan_rules(path, ontology=load_ontology())

    assert any("missing_release" in problem for problem in raised.value.problems)


def test_check_7_a_later_phase_reference_is_refused(tmp_path: Path) -> None:
    data = yaml.safe_load(
        (Path(__file__).parent.parent / "src" / "graph" / "plan" / "plan-rules.yaml").read_text(encoding="utf-8")
    )
    data["phases"]["setup"]["after"] = ["acceptance"]
    path = write_rules(tmp_path / "plan-rules.yaml", data)

    with pytest.raises(PlanRulesError) as raised:
        load_plan_rules(path, ontology=load_ontology())

    assert any("setup" in problem and "earlier" in problem for problem in raised.value.problems)


def test_the_shipped_rules_give_each_spec_type_its_role_and_story_kind() -> None:
    rules = load_plan_rules(ontology=load_ontology())

    assert rules.rule_of("Module").role == "area_anchor"
    assert rules.rule_of("Capability").role == "shared"
    assert rules.rule_of("OpenQuestion").role == "context"
    entity = rules.rule_of("DataEntity")
    assert entity.role == "member" and entity.required and entity.story == "entity"
    assert rules.story_kind("DataEntity") == "entity"
    assert rules.story_kind("Interface") == "interface"
    assert rules.story_kind("Feature") == "feature"
    assert rules.story_kind("FunctionalRequirement") is None
