"""The plan follows the plan rules, never spec names: a renamed ontology, rules and graph plan the same work.

The build ontology is renamed with the build tests' rename tables (labels, relations, attribute names and
values, type and group keys), the plan rules point at the renamed names, and the
shop fixture is rewritten with the renamed labels, relations, attribute values and spec id prefixes. Both
copies are planned with the same scripted answers; the results must be equal once renamed spec ids are
mapped back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from pydantic import BaseModel

import test_graph_renamed_ontology_live as build_rename
from graph.core import Ontology, load_ontology
from graph.plan.files import open_plan_store
from graph.plan.rules import PlanRules, load_plan_rules
from graph.plan.stitch import stitch
from graph.plan.view import PlanView
from graph_plan_support import (
    PlannedShop,
    fixture_graph,
    fixture_view,
    plan_ontology,
    plan_rules,
    planned_shop,
    script_plan,
    shop_answers,
    shop_fixture_data,
)
from graph_support import ScriptedGateway
from test_graph_renamed_ontology_live import (
    ATTRIBUTES,
    GROUPS,
    LABELS,
    RELATIONS,
    TYPE_KEYS,
    VALUES,
    shipped_ontology_dir,
    write_renamed_data,
)

# The first segment of every spec id the shop fixture uses; the rest of an id is kept, so area, story and
# task ids (derived from the id after its first segment) are the same in both plans.
SPEC_ID_PREFIXES: Final[Mapping[str, str]] = {
    "MOD": "KOMP",
    "IFC": "VERT",
    "ENT": "OBJ",
    "CAP": "QUER",
    "FEAT": "FUNK",
    "REQ": "ANF",
    "CON": "VORG",
    "BR": "GREG",
    "OQ": "FRAGE",
}


def renamed_id(spec_id: str) -> str:
    head, separator, rest = spec_id.partition("::")
    return f"{SPEC_ID_PREFIXES[head]}{separator}{rest}"


TYPE_REFS: Final[Mapping[str, str]] = {**TYPE_KEYS, **GROUPS}
"""Type keys and node group keys, renamed."""


def type_ref(key: str, types: Mapping[str, str]) -> str:
    return types.get(key, key)


def renamed_values(values: Any) -> Any:
    """An attribute value with every vocabulary value renamed (free text is kept)."""

    if isinstance(values, list):
        return [renamed_values(value) for value in values]
    return VALUES.get(values, values) if isinstance(values, str) else values


def renamed_condition(condition: Mapping[str, Any]) -> dict[str, Any]:
    return {ATTRIBUTES.get(name, name): renamed_values(values) for name, values in condition.items()}


# The renamed inputs


def renamed_build_ontology(directory: Path, types: Mapping[str, str]) -> Path:
    """The build tests' renamed copy of the build ontology, with type and group keys renamed by ``types``."""

    data = _read_yaml((shipped_ontology_dir() / "spec-ontology.yaml").read_text(encoding="utf-8"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(build_rename, "TYPE_KEYS", {key: types[key] for key in TYPE_KEYS})
        patch.setattr(build_rename, "GROUPS", {key: types[key] for key in GROUPS})
        return write_renamed_data(directory, data)


def renamed_plan_ontology_data(data: Mapping[str, Any], types: Mapping[str, str]) -> dict[str, Any]:
    """The plan ontology file with its pairs ending at the renamed build type keys."""

    renamed = dict(data)
    renamed["edge_types"] = {
        name: {
            **relation,
            "pairs": [
                {**pair, "from": type_ref(pair["from"], types), "to": type_ref(pair["to"], types)}
                for pair in relation["pairs"]
            ],
        }
        for name, relation in data["edge_types"].items()
    }
    return renamed


def renamed_rules_data(data: Mapping[str, Any], types: Mapping[str, str]) -> dict[str, Any]:
    """The plan rules file pointing at the renamed type keys, relations, attributes and values."""

    renamed = dict(data)
    renamed["types"] = {
        type_ref(key, types): {**rule, **({"exclude": renamed_condition(rule["exclude"])} if "exclude" in rule else {})}
        for key, rule in data["types"].items()
    }
    renamed["exclude_all"] = renamed_condition(data["exclude_all"])
    renamed["relations"] = {role: RELATIONS[name] for role, name in data["relations"].items()}
    for key in ("release", "acceptance_criteria"):
        renamed[key] = {"attribute": ATTRIBUTES[data[key]["attribute"]]}
    assumption = data["assumption"]
    renamed["assumption"] = {"attribute": ATTRIBUTES[assumption["attribute"]], "value": VALUES[assumption["value"]]}
    return renamed


def renamed_fixture_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """The shop fixture with renamed labels, relations, attribute names and values, and spec id prefixes."""

    nodes = [
        {
            **node,
            "spec_id": renamed_id(node["spec_id"]),
            "label": LABELS[node["label"]],
            "attributes": renamed_condition(node.get("attributes") or {}),
        }
        for node in data["nodes"]
    ]
    edges = []
    for edge in data["edges"]:
        source, relation, target = renamed_id(edge["source"]), RELATIONS[edge["relation"]], renamed_id(edge["target"])
        key = f"{source}|{relation}|{target}".replace("::", "_")
        edges.append({**edge, "source": source, "relation": relation, "target": target, "edge_key": key})
    return {**data, "nodes": nodes, "edges": edges}


def _yaml(path: Path, data: Mapping[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def _read_yaml(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


@dataclass(frozen=True)
class Inputs:
    """One copy of the plan inputs: the merged ontology, the plan rules and the fixture data."""

    ontology: Ontology
    rules: PlanRules
    fixture: dict[str, Any]

    async def view(self) -> PlanView:
        return await fixture_view(fixture_graph(self.fixture, self.ontology), self.ontology, self.rules)


def renamed_inputs(directory: Path, types: Mapping[str, str]) -> Inputs:
    """The renamed ontology, rules and fixture, written to and loaded from ``directory``."""

    plan_file = files("graph.plan") / "ontology.yaml"
    plan = renamed_plan_ontology_data(_read_yaml(plan_file.read_text(encoding="utf-8")), types)
    plan_path = _yaml(directory / "ontology.yaml", plan)
    ontology = load_ontology(renamed_build_ontology(directory, types), submodule_files=[plan_path])
    rules = renamed_rules_data(_read_yaml((files("graph.plan") / "plan-rules.yaml").read_text(encoding="utf-8")), types)
    rules_path = _yaml(directory / "plan-rules.yaml", rules)
    return Inputs(ontology, load_plan_rules(rules_path, ontology=ontology), renamed_fixture_data(shop_fixture_data()))


@pytest.fixture(scope="module")
def renamed(tmp_path_factory: pytest.TempPathFactory) -> Inputs:
    """Every spec name renamed, type and group keys included."""

    return renamed_inputs(tmp_path_factory.mktemp("renamed-plan"), TYPE_REFS)


@pytest.fixture(scope="module")
def original() -> Inputs:
    ontology = plan_ontology()
    return Inputs(ontology, plan_rules(ontology), shop_fixture_data())


# Mapping renamed results back


def id_map(data: Mapping[str, Any]) -> dict[str, str]:
    """Renamed spec id or edge key -> the original one, for every node and edge of the fixture ``data``."""

    renamed = renamed_fixture_data(data)
    names = {node["spec_id"]: before["spec_id"] for node, before in zip(renamed["nodes"], data["nodes"], strict=True)}
    names.update(
        (edge["edge_key"], before["edge_key"]) for edge, before in zip(renamed["edges"], data["edges"], strict=True)
    )
    return names


def mapped(value: Any, names: Mapping[str, str]) -> Any:
    """``value`` (a dumped artifact) with every renamed spec id, as a whole string or a key, mapped back."""

    if isinstance(value, str):
        return names.get(value, value)
    if isinstance(value, Mapping):
        return {mapped(key, names): mapped(item, names) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [mapped(item, names) for item in value]
    return value


def dumped(artifact: BaseModel) -> Any:
    return artifact.model_dump(mode="json")


def comparable(value: Any, names: Mapping[str, str] | None = None) -> Any:
    """A dumped artifact mapped back by ``names``, with lists of ids sorted (spec ids sort differently once
    renamed, so member lists ordered by spec id differ only in order)."""

    value = mapped(value, names or {})
    if isinstance(value, dict):
        return {key: comparable(item) for key, item in value.items()}
    if isinstance(value, list):
        items = [comparable(item) for item in value]
        return sorted(items) if all(isinstance(item, str) for item in items) else items
    return value


def mapped_answers(view: PlanView, names: Mapping[str, str]) -> dict[tuple[str, str], BaseModel]:
    """The scripted shop answers of the original ``view``, with spec ids renamed (``names``: original -> renamed)."""

    return {
        key: type(answer).model_validate(mapped(dumped(answer), names)) for key, answer in shop_answers(view).items()
    }


async def _planned(view: PlanView, answers: Mapping[tuple[str, str], BaseModel], tmp_path: Path) -> PlannedShop:
    gateway = script_plan(ScriptedGateway(), answers)
    return await planned_shop(view, open_plan_store(tmp_path), gateway)


# The tests


def test_the_renamed_inputs_share_no_spec_name_with_the_shipped_ones(original: Inputs, renamed: Inputs) -> None:
    assert not set(original.ontology.labels) & set(renamed.ontology.labels) - {"Plan", "WorkArea", "Story", "Task"}
    assert not set(original.rules.relations.values()) & set(renamed.rules.relations.values())
    assert not set(original.rules.exclude_all) & set(renamed.rules.exclude_all)
    renamed_labels = {node["label"] for node in renamed.fixture["nodes"]}
    assert renamed_labels and not renamed_labels & set(LABELS)
    assert not {edge["relation"] for edge in renamed.fixture["edges"]} & set(RELATIONS)


async def test_snapshot_and_partition_place_the_same_nodes_in_the_same_areas_and_stories(
    original: Inputs, renamed: Inputs
) -> None:
    first, second = await original.view(), await renamed.view()
    names = id_map(original.fixture)

    assert sorted(mapped([node.spec_id for node in second.snapshot.plannable], names)) == sorted(
        node.spec_id for node in first.snapshot.plannable
    )
    assert sorted(mapped([node.spec_id for node in second.snapshot.context], names)) == sorted(
        node.spec_id for node in first.snapshot.context
    )
    assert {area.area_id for area in first.partition.areas} == {area.area_id for area in second.partition.areas}
    assert len(first.partition.areas) > 2 and len(first.partition.stories) > 5
    assert comparable(dumped(second.partition), names) == comparable(dumped(first.partition))


async def test_the_planners_and_stitch_produce_the_same_tasks_and_waits(
    original: Inputs, renamed: Inputs, tmp_path: Path
) -> None:
    first, second = await original.view(), await renamed.view()
    back = id_map(original.fixture)
    forward = {old: new for new, old in back.items()}

    planned = await _planned(first, shop_answers(first), tmp_path / "original")
    replanned = await _planned(second, mapped_answers(first, forward), tmp_path / "renamed")

    assert planned.tasks.tasks and all(task.planned_by == "model" for task in planned.tasks.tasks)
    for artifact, other in (
        (planned.describe, replanned.describe),
        (planned.project, replanned.project),
        (planned.tasks, replanned.tasks),
    ):
        assert comparable(dumped(other), back) == comparable(dumped(artifact))
    assert comparable(dumped(stitch(second, replanned.project, replanned.tasks))) == comparable(
        dumped(stitch(first, planned.project, planned.tasks))
    )
