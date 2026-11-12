"""The ontology field ``impact``: shipped roles, loading checks and backward compatibility (trace.md
"Impact roles in the ontology", "Shipped roles"; FR-ONT-07)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from graph.core import OntologyError, load_ontology
from graph.core.ontology import Ontology
from graph_support import frozen_ontology, shipped_ontology
from graph_trace_support import load_edited, shipped_data, without_impact

RANK_2_REQUIREMENTS = (
    "BusinessRule",
    "Constraint",
    "FunctionalRequirement",
    "IntegrationRequirement",
    "QualityRequirement",
)
SHIPPED_RELATION_ROLES = {
    "DEPENDS_ON": "uses",
    "INTEGRATES_WITH": "uses",
    "DEPLOYED_TO": "uses",
    "CARRIES": "uses",
    "CONSUMED_BY": "used_by",
    "PROVIDED_BY": "coupled",
    "STORED_IN": "coupled",
    "PART_OF": "contains",
    "GROUPED_BY": "contains",
    "REALIZED_BY": "contains",
    "RELATES_TO": "related",
}


def problems(error: pytest.ExceptionInfo[OntologyError]) -> str:
    return str(error.value)


def edited(tmp_path: Path, edit: Any) -> Ontology:
    data = copy.deepcopy(shipped_data())
    edit(data)
    return load_edited(tmp_path, data)


def pair(data: dict[str, Any], relation: str, source: str, target: str) -> dict[str, Any]:
    return next(
        item for item in data["edge_types"][relation]["pairs"] if (item["from"], item["to"]) == (source, target)
    )


# Shipped roles


def test_the_shipped_relations_carry_the_roles_of_trace_md() -> None:
    ontology = shipped_ontology()
    assert ontology.version == "2.1"
    assert {name: relation.impact for name, relation in ontology.relations.items()} == SHIPPED_RELATION_ROLES


def test_a_requirement_owned_directly_by_a_module_binds_the_module() -> None:
    ontology = shipped_ontology()
    for label in RANK_2_REQUIREMENTS:
        assert ontology.impact_role("PART_OF", label, "Module") == "used_by", label
    assert ontology.impact_role("PART_OF", "FunctionalRequirement", "Feature") == "contains"
    assert ontology.impact_role("PART_OF", "Feature", "Module") == "contains"
    assert ontology.impact_role("PART_OF", "Module", "Module") == "contains"


def test_relates_to_uses_interfaces_and_entities_and_never_walks_open_questions() -> None:
    ontology = shipped_ontology()
    assert ontology.impact_role("RELATES_TO", "FunctionalRequirement", "Interface") == "uses"
    assert ontology.impact_role("RELATES_TO", "Interface", "FunctionalRequirement") == "used_by"
    assert ontology.impact_role("RELATES_TO", "Constraint", "DataEntity") == "uses"
    assert ontology.impact_role("RELATES_TO", "DataEntity", "Constraint") == "used_by"
    assert ontology.impact_role("RELATES_TO", "Constraint", "Module") == "related"
    assert ontology.impact_role("RELATES_TO", "FunctionalRequirement", "BusinessRule") == "related"
    questions = [p for p in ontology.pairs("RELATES_TO") if "OpenQuestion" in (p.source, p.target)]
    assert questions and {p.impact for p in questions} == {"none"}


def test_an_undeclared_pair_takes_the_relation_role_and_an_unknown_relation_none() -> None:
    ontology = shipped_ontology()
    assert ontology.impact_role("DEPENDS_ON", "Module", "Module") == "uses"
    assert ontology.impact_role("NOT_A_RELATION", "Module", "Module") == "none"


def test_the_merged_ontology_loads_and_submodule_relations_are_not_walked() -> None:
    merged = load_ontology()
    foreign = [relation for relation in merged.relations.values() if relation.owner != "build"]
    assert foreign and {relation.impact for relation in foreign} == {"none"}


# Backward compatibility


def test_an_ontology_without_impact_loads_with_every_role_none(tmp_path: Path) -> None:
    plain = load_edited(tmp_path, without_impact(shipped_data()))
    roles = {pair.impact for relation in plain.relations.values() for pair in relation.pairs}
    assert roles == {"none"} and {relation.impact for relation in plain.relations.values()} == {"none"}
    assert {pair.impact for relation in frozen_ontology().relations.values() for pair in relation.pairs} == {"none"}


def test_impact_leaves_every_fingerprint_unchanged(tmp_path: Path) -> None:
    """The field reaches no prompt: the extraction fingerprints are those of the file without it."""

    plain = load_edited(tmp_path, without_impact(shipped_data()))
    for part in ("nodes", "edges", "attributes", "resolution"):
        assert shipped_ontology().fingerprint(part) == plain.fingerprint(part), part


# Loading checks


@pytest.mark.parametrize("where", ["relation", "pair"])
def test_an_unknown_role_is_rejected(tmp_path: Path, where: str) -> None:
    def edit(data: dict[str, Any]) -> None:
        target = data["edge_types"]["CARRIES"] if where == "relation" else data["edge_types"]["CARRIES"]["pairs"][0]
        target["impact"] = "breaks"

    with pytest.raises(OntologyError) as raised:
        edited(tmp_path, edit)
    assert "edge_types.CARRIES" in problems(raised) and "impact" in problems(raised)


def test_reversed_symmetric_pairs_need_inverse_roles(tmp_path: Path) -> None:
    """FR-ONT-07: a symmetric pair ``uses`` whose reverse pair is also ``uses``."""

    def edit(data: dict[str, Any]) -> None:
        pair(data, "RELATES_TO", "interface", "requirement")["impact"] = "uses"

    with pytest.raises(OntologyError) as raised:
        edited(tmp_path, edit)
    assert "edge_types.RELATES_TO.impact: reversed pairs need inverse roles" in problems(raised)


def test_a_self_inverse_role_must_match_its_reverse(tmp_path: Path) -> None:
    def edit(data: dict[str, Any]) -> None:
        pair(data, "RELATES_TO", "requirement", "module")["impact"] = "coupled"

    with pytest.raises(OntologyError) as raised:
        edited(tmp_path, edit)
    assert "reversed pairs need inverse roles" in problems(raised)


def test_matching_inverse_roles_on_a_symmetric_relation_load(tmp_path: Path) -> None:
    def edit(data: dict[str, Any]) -> None:
        pair(data, "RELATES_TO", "requirement", "module")["impact"] = "used_by"
        pair(data, "RELATES_TO", "module", "requirement")["impact"] = "uses"

    ontology = edited(tmp_path, edit)
    assert ontology.impact_role("RELATES_TO", "Module", "Constraint") == "uses"


@pytest.mark.parametrize("where", ["relation", "pair"])
def test_contains_is_refused_on_a_symmetric_relation(tmp_path: Path, where: str) -> None:
    def edit(data: dict[str, Any]) -> None:
        if where == "relation":
            data["edge_types"]["RELATES_TO"]["impact"] = "contains"
        else:
            pair(data, "RELATES_TO", "requirement", "module")["impact"] = "contains"
            pair(data, "RELATES_TO", "module", "requirement")["impact"] = "contains"

    with pytest.raises(OntologyError) as raised:
        edited(tmp_path, edit)
    assert "a symmetric relation may not have the role 'contains'" in problems(raised)


@pytest.mark.parametrize("where", ["relation", "pair"])
def test_contains_needs_an_owner_pair_or_a_spine_path(tmp_path: Path, where: str) -> None:
    def edit(data: dict[str, Any]) -> None:
        target = data["edge_types"]["CARRIES"] if where == "relation" else data["edge_types"]["CARRIES"]["pairs"][0]
        target["impact"] = "contains"

    with pytest.raises(OntologyError) as raised:
        edited(tmp_path, edit)
    assert "edge_types.CARRIES.impact: 'contains' needs an owner pair" in problems(raised)


def test_contains_is_allowed_on_a_relation_named_only_by_a_spine_path(tmp_path: Path) -> None:
    """``GROUPED_BY`` has no owner pair; the spine names it, so ``contains`` loads."""

    ontology = load_edited(tmp_path, shipped_data())
    assert not any(p.owner for p in ontology.pairs("GROUPED_BY"))
    assert ontology.relation("GROUPED_BY").impact == "contains"
