"""The gap rules file: the shipped rules load, every problem of a bad file is named, the docs copies equal the shipped
files (docs/graph/gap/ontology.md "Rules file"; FR-GONT-03, FR-GONT-04)."""

from __future__ import annotations

import ast
import copy
from importlib.resources import files
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

import graph.gap
from graph.core import Ontology, load_ontology
from graph.core.errors import ConfigurationError
from graph.gap.errors import GapRulesError
from graph.gap.rules import CATALOGUE, RULE_PARAMETERS, GapRules, load_gap_rules
from graph_support import shipped_ontology

DOCS: Final = Path(__file__).resolve().parents[2] / "docs" / "graph" / "gap"
SHIPPED_RULES: Final = files("graph.gap") / "gap-rules.yaml"


@pytest.fixture(scope="module")
def ontology() -> Ontology:
    return load_ontology()


def shipped_data() -> dict[str, Any]:
    data = yaml.safe_load(SHIPPED_RULES.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "gap-rules.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def problems_of(tmp_path: Path, data: dict[str, Any], ontology: Ontology) -> tuple[str, ...]:
    with pytest.raises(GapRulesError) as caught:
        load_gap_rules(write(tmp_path, data), ontology=ontology)
    assert isinstance(caught.value, ConfigurationError)
    assert caught.value.code == "invalid_gap_rules"
    assert caught.value.fields["problems"] == len(caught.value.problems)
    return caught.value.problems


def test_the_shipped_rules_load_against_the_merged_ontology(ontology: Ontology) -> None:
    rules = load_gap_rules(ontology=ontology)

    assert isinstance(rules, GapRules)
    assert (rules.name, rules.version) == ("gap-rules", "1.0")
    assert len(rules.detectors) == 23
    assert {rule.code for rule in rules.detectors_of_slice(1)} == {
        "MS-OWNER",
        "MS-PROVIDER",
        "MS-STORE",
        "MS-CONTRACT",
        "MS-CRITERIA",
        "MS-COVERAGE",
        "MS-REF",
        "AM-VAGUE",
        "AM-MEASURE",
        "AM-IMPLIED",
        "CT-VALUE",
        "IN-SCOPE",
        "IN-CYCLE",
        "OQ-STATED",
        "OQ-TBD",
        "AS-FLAG",
    }
    assert rules.detectors["MS-PROVIDER"].relation == "PROVIDED_BY"
    assert rules.detectors["MS-PROVIDER"].direction == "out"
    assert rules.detectors["AS-FLAG"].value == "yes"
    assert rules.detectors["MS-ERROR"].topic == "failure"
    assert rules.detectors["MS-REF"].labels == ()
    assert set(rules.detectors["MS-OWNER"].labels) == set(ontology.node_groups["requirement"].labels)
    assert rules.relevance.tracking_patterns["ticket_key"].search("see SHOP-142") is not None
    assert rules.relevance.isolated_share == 0.6
    assert rules.corroboration == (
        "obligation_wording",
        "names_system_part",
        "planned_dependency",
    )
    assert rules.routing.ai_kinds == ("missing", "ambiguous", "inconsistency")
    assert rules.routing.gates.min_confidence == 0.85
    assert set(rules.routing.severity_prior) == set(ontology.node_type("Gap").attributes["kind"].values)
    assert (
        rules.limits.options_min,
        rules.limits.options_max,
        rules.limits.apply_quiet_seconds,
    ) == (2, 4, 120)


def test_the_shipped_rules_resolve_only_spec_types(ontology: Ontology) -> None:
    rules = load_gap_rules(ontology=ontology)

    for rule in rules.detectors.values():
        assert all(ontology.owner_of(label) == "build" for label in rule.labels), rule.code


def test_fr_gont_03_an_unknown_type_relation_and_kind_are_all_named(tmp_path: Path, ontology: Ontology) -> None:
    data = shipped_data()
    data["detectors"]["MS-PROVIDER"]["types"] = ["warehouse"]
    data["detectors"]["MS-STORE"]["relation"] = "KEPT_IN"
    data["detectors"]["AM-VAGUE"]["kind"] = "vagueness"

    problems = problems_of(tmp_path, data, ontology)

    assert any("'warehouse' is not a declared node type or group" in item for item in problems)
    assert any("'KEPT_IN' is not a declared relation" in item for item in problems)
    assert any("'vagueness' is not a gap_kind value" in item for item in problems)
    # The changed kind also breaks the catalogue entry of AM-VAGUE: four problems, one error.
    assert len(problems) == 4


def test_fr_gont_03_every_section_reports_its_problems_in_one_error(tmp_path: Path, ontology: Ontology) -> None:
    data = shipped_data()
    data["format_version"] = 2
    data["extra"] = True
    data["relevance"]["tracking_patterns"]["broken"] = "(unclosed"
    data["relevance"]["isolated_share"] = 1.5
    data["relevance"]["classify_with_model"] = "yes"
    data["seeds"]["corroboration"] = ["obligation_wording", "gut_feeling"]
    data["detectors"]["MS-CRITERIA"]["attribute"] = "acceptance"
    data["detectors"]["AS-FLAG"]["value"] = "maybe"
    data["detectors"]["MS-ERROR"]["topic"] = "weather"
    data["detectors"]["MS-OWNER"]["rule"] = "no_owner_at_all"
    data["detectors"]["IN-CYCLE"].pop("relation")
    data["detectors"]["OQ-TBD"]["colour"] = "red"
    data["placeholder_markers"] = []
    data["vague_terms"] = "fast"
    data["routing"]["ai_kinds"] = ["missing", "guess"]
    data["routing"]["gates"]["min_confidence"] = 1.2
    data["routing"]["gates"]["local_max_nodes"] = 0
    data["routing"]["severity_prior"].pop("assumption")
    data["limits"]["options_min"] = 1
    data["limits"]["options_max"] = 5
    data["limits"]["sites_full_run"] = 0
    data["limits"]["unknown_limit"] = 3

    problems = "\n".join(problems_of(tmp_path, data, ontology))

    for expected in (
        "format_version must be 1",
        "unknown root keys: extra",
        "relevance.tracking_patterns.broken does not compile",
        "relevance.isolated_share must be a number from 0 to 1",
        "relevance.classify_with_model must be a boolean",
        "seeds.corroboration names unknown signals: gut_feeling",
        "detectors.MS-CRITERIA.attribute 'acceptance' is not declared",
        "detectors.AS-FLAG.value 'maybe' is not a value of attribute 'is_assumption'",
        "detectors.MS-ERROR.topic 'weather' is not a checklist topic",
        "detectors.MS-OWNER.rule 'no_owner_at_all' is not a known rule",
        "detectors.IN-CYCLE: rule 'cycle' needs relation",
        "detectors.OQ-TBD has unknown keys: colour",
        "placeholder_markers must not be empty",
        "vague_terms must be a list of non-empty strings",
        "routing.ai_kinds names values that are not gap kinds: guess",
        "routing.gates.min_confidence must be a number above 0 and at most 1",
        "routing.gates.local_max_nodes must be a positive integer",
        "routing.severity_prior misses the kinds: assumption",
        "limits.options_min",
        "limits.options_max",
        "limits.sites_full_run",
        "limits.unknown_limit: unknown key",
    ):
        assert expected in problems, expected


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda data: data["detectors"]["MS-PROVIDER"].update(relation="PROVIDED_BY", direction="sideways"),
            "direction",
        ),
        (
            lambda data: data["detectors"]["MS-PROVIDER"].update(types=["task"]),
            "'task' is not a spec type",
        ),
        (
            lambda data: data["detectors"]["MS-PROVIDER"].update(types=["gap"]),
            "'gap' is not a spec type",
        ),
        (
            lambda data: data["detectors"]["MS-STORE"].update(relation="IMPLEMENTS"),
            "'IMPLEMENTS' is not a spec relation",
        ),
        (
            lambda data: data["detectors"]["IN-CYCLE"].update(types=["module"]),
            "has no pair at any of the detector's",
        ),
        (
            lambda data: data["detectors"]["MS-CONTRACT"].update(relations=["INTEGRATES_WITH", "ABOUT"]),
            "'ABOUT'",
        ),
        (
            lambda data: data["detectors"]["MS-OWNER"].update(slice=0),
            "slice must be a positive integer",
        ),
        (
            lambda data: data["detectors"]["MS-OWNER"].update(family="vibes", strength="medium"),
            "family must be",
        ),
        (
            lambda data: data["detectors"].update({"bad code": data["detectors"]["MS-OWNER"]}),
            "XX-NAME",
        ),
        (
            lambda data: data["detectors"].update({"MS-SELF": "no mapping"}),
            "detectors.MS-SELF must be a mapping",
        ),
        (lambda data: data.update(detectors={}), "detectors must not be empty"),
        (lambda data: data.update(checklist={}), "checklist must not be empty"),
        (
            lambda data: data["checklist"].update(security=[]),
            "checklist.security must not be empty",
        ),
        (lambda data: data.update(name=3), "name and version must be strings"),
        (lambda data: data.update(relevance=None), "relevance must be a mapping"),
        (lambda data: data.update(routing=None), "routing must be a mapping"),
        (lambda data: data.update(limits=None), "limits must be a mapping"),
        (
            lambda data: data["routing"]["severity_prior"].update(missing=-1),
            "must be positive numbers: missing",
        ),
        (
            lambda data: data["routing"]["severity_prior"].update(guess=1),
            "not gap kinds: guess",
        ),
        (
            lambda data: data["relevance"]["tracking_patterns"].update(year=2026),
            "tracking_patterns.year must be a",
        ),
    ],
)
def test_each_invalid_entry_is_named(tmp_path: Path, ontology: Ontology, change: Any, expected: str) -> None:
    data = copy.deepcopy(shipped_data())
    change(data)

    assert expected in "\n".join(problems_of(tmp_path, data, ontology))


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda data: data["detectors"].update({"XX-FOO": dict(data["detectors"]["MS-OWNER"])}),
            "detectors.XX-FOO: not a detector of the catalogue",
        ),
        (
            lambda data: data["detectors"]["MS-PROVIDER"].update(kind="ambiguous"),
            "detectors.MS-PROVIDER: kind, family, strength and rule must be missing, structure, strong",
        ),
        (
            lambda data: data["detectors"]["AM-VAGUE"].update(family="structure"),
            "detectors.AM-VAGUE: kind, family, strength and rule must be",
        ),
        (
            lambda data: data["routing"].update(ai_kinds=["missing", "contradiction"]),
            "routing.ai_kinds may only shrink; AI never answers: contradiction",
        ),
        (
            lambda data: data["detectors"]["IN-SCOPE"].update(types=["requirement", "data_entity"]),
            "detectors.IN-SCOPE.attribute 'in_scope' is not declared on every one",
        ),
        (
            lambda data: data["detectors"]["IN-CYCLE"].update(relation="CARRIES"),
            "detectors.IN-CYCLE.relation 'CARRIES' has no pair",
        ),
    ],
)
def test_the_catalogue_and_the_ai_kinds_are_fixed_by_the_spec(
    tmp_path: Path, ontology: Ontology, change: Any, expected: str
) -> None:
    data = copy.deepcopy(shipped_data())
    change(data)

    assert expected in "\n".join(problems_of(tmp_path, data, ontology))


def test_a_rules_file_may_drop_a_detector_and_shrink_the_ai_kinds(tmp_path: Path, ontology: Ontology) -> None:
    data = shipped_data()
    del data["detectors"]["MS-OWNER"]
    data["routing"]["ai_kinds"] = ["missing"]

    rules = load_gap_rules(write(tmp_path, data), ontology=ontology)

    assert "MS-OWNER" not in rules.detectors
    assert rules.routing.ai_kinds == ("missing",)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda data: data["seeds"].update(system_parts=["task"]), "seeds.system_parts: 'task' is not a spec type"),
        (lambda data: data["seeds"].pop("system_parts"), "seeds.system_parts must list the types"),
        (
            lambda data: data["detectors"]["MS-COVERAGE"].update(topics=["security", "weather"]),
            "detectors.MS-COVERAGE.topics are not checklist topics: weather",
        ),
        (
            lambda data: data["detectors"]["MS-CONTRACT"].update(provided_by="IMPLEMENTS"),
            "detectors.MS-CONTRACT.provided_by 'IMPLEMENTS' is not a declared spec relation",
        ),
        (
            lambda data: data["detectors"]["MS-CONTRACT"].update(interfaces=["gizmo"]),
            "detectors.MS-CONTRACT.interfaces: 'gizmo' is not a declared node type or group",
        ),
        (
            lambda data: data["detectors"]["MS-OWNER"].update(exclude={"colour": ["red"]}),
            "detectors.MS-OWNER.exclude: 'colour' is not declared on every end of the rule",
        ),
        (
            lambda data: data["detectors"]["MS-OWNER"].update(exclude={"external": ["maybe"]}),
            "detectors.MS-OWNER.exclude.external: maybe not values of the attribute",
        ),
        (
            lambda data: data["detectors"]["MS-CONTRACT"].update(exclude="external"),
            "detectors.MS-CONTRACT.exclude must be a mapping",
        ),
        (
            lambda data: data["detectors"]["IN-SCOPE"].update(value="maybe"),
            "detectors.IN-SCOPE.value 'maybe' is not a value of attribute 'in_scope'",
        ),
    ],
)
def test_the_detection_parameters_resolve_in_the_ontology(
    tmp_path: Path, ontology: Ontology, change: Any, expected: str
) -> None:
    data = copy.deepcopy(shipped_data())
    change(data)

    assert expected in "\n".join(problems_of(tmp_path, data, ontology))


def test_the_shipped_detection_parameters(ontology: Ontology) -> None:
    rules = load_gap_rules(ontology=ontology)

    assert set(rules.system_parts) == {"Module", "Interface", "DataEntity"}
    assert dict(rules.detectors["MS-OWNER"].exclude) == {"external": ("yes",)}
    contract = rules.detectors["MS-CONTRACT"]
    assert (contract.provided_by, contract.consumed_by, contract.interface_labels) == (
        "PROVIDED_BY",
        "CONSUMED_BY",
        ("Interface",),
    )
    assert set(contract.member_labels) == set(ontology.node_groups["requirement"].labels)
    assert len(rules.detectors["MS-COVERAGE"].topics) == 6
    assert rules.detectors["IN-SCOPE"].value == "no"
    assert rules.detectors["OQ-TBD"].labels == ("OpenQuestion",)


def test_the_catalogue_names_every_shipped_detector() -> None:
    assert set(CATALOGUE) == set(shipped_data()["detectors"])


def test_a_file_that_is_no_yaml_mapping_or_has_a_duplicate_key_is_refused(tmp_path: Path, ontology: Ontology) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text("- a list\n", encoding="utf-8")
    with pytest.raises(GapRulesError, match="invalid_gap_rules") as caught:
        load_gap_rules(path, ontology=ontology)
    assert caught.value.problems == ("the rules file must be a mapping",)

    path.write_text("name: a\nname: b\n", encoding="utf-8")
    with pytest.raises(GapRulesError) as caught:
        load_gap_rules(path, ontology=ontology)
    assert "duplicate key" in caught.value.problems[0]

    with pytest.raises(GapRulesError) as caught:
        load_gap_rules(tmp_path / "missing.yaml", ontology=ontology)
    assert caught.value.problems == ("cannot read the rules file (FileNotFoundError)",)
    assert "- cannot read the rules file" in str(caught.value)


def test_rules_need_the_gap_ontology_file() -> None:
    with pytest.raises(GapRulesError) as caught:
        load_gap_rules(ontology=shipped_ontology())

    assert "the ontology does not hold the gap ontology file (no Gap type)" in caught.value.problems


def test_a_project_override_is_validated_the_same_way(tmp_path: Path, ontology: Ontology) -> None:
    data = shipped_data()
    data["version"] = "1.1"
    data["vague_terms"] = ["snappy"]
    del data["detectors"]["CT-RULE"]

    rules = load_gap_rules(write(tmp_path, data), ontology=ontology)

    assert rules.version == "1.1"
    assert rules.vague_terms == ("snappy",)
    assert "CT-RULE" not in rules.detectors


def test_every_rule_of_the_catalogue_is_used_by_a_shipped_detector() -> None:
    used = {entry["rule"] for entry in shipped_data()["detectors"].values()}

    assert used == set(RULE_PARAMETERS)


def test_the_docs_copies_equal_the_shipped_files_but_for_the_header_line() -> None:
    for shipped, copy_name in (
        ("ontology.yaml", "gap-ontology.yaml"),
        ("gap-rules.yaml", "gap-rules.yaml"),
    ):
        package = (files("graph.gap") / shipped).read_text(encoding="utf-8").splitlines()
        docs = (DOCS / copy_name).read_text(encoding="utf-8").splitlines()
        assert package[1:] == docs[1:], copy_name
        assert package[0].startswith(f"# {shipped}:") and docs[0].startswith(f"# {copy_name}:")


def test_the_rules_file_is_package_data() -> None:
    assert SHIPPED_RULES.is_file()
    assert SHIPPED_RULES.read_text(encoding="utf-8").startswith("# gap-rules.yaml: the gap rules graph.gap ships")


def _string_literals(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    }


def test_fr_gont_04_no_spec_label_relation_or_attribute_is_a_literal_of_the_gap_modules() -> None:
    ontology = shipped_ontology()
    spec_names = (
        set(ontology.labels)
        | {name for name in ontology.relations if ontology.owner_of(name) == "build"}
        | {name for label in ontology.labels for name in ontology.node_type(label).attributes}
        | set(ontology.node_types)
        | set(ontology.node_groups)
    )
    package = Path(graph.gap.__file__).resolve().parent
    # gap's own field names that a spec type also uses, and the gap kind open_question (a gap_kind value)
    shared = {"kind", "status", "name", "text", "type", "quote", "open_question"}
    modules = sorted(package.rglob("*.py"))
    assert {package / "steps" / "detect.py", package / "resolve.py", package / "migrate.py"} <= set(modules)
    for module in modules:
        found = (_string_literals(module) & spec_names) - shared
        assert not found, (module.relative_to(package).as_posix(), sorted(found))
