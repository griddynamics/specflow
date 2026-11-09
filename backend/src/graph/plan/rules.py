"""The plan rules file: which spec types are planned and how they are placed."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

import yaml
import yaml.constructor

from graph.core import Ontology
from graph.core.ontology.loader import _load_unique
from graph.plan.errors import PlanRulesError
from graph.plan.names import plan_vocabularies

Role = Literal["area_anchor", "shared", "member", "context"]
TYPE_KEYS = frozenset({"role", "exclude", "phase", "required", "story", "project_input"})
ROOT_KEYS = frozenset(
    {
        "format_version",
        "name",
        "version",
        "types",
        "exclude_all",
        "relations",
        "release",
        "assumption",
        "acceptance_criteria",
        "phases",
        "phase_guidance",
    }
)
_SHIPPED_RULES = files("graph.plan") / "plan-rules.yaml"


def _shipped_relation_roles() -> frozenset[str]:
    data = yaml.safe_load(_SHIPPED_RULES.read_text(encoding="utf-8"))
    return frozenset(data["relations"])


# The relation roles are those the shipped rules declare; a custom rules file must use the same set.
RELATION_KEYS = _shipped_relation_roles()
# The story kinds a type can anchor; the plan finds interfaces, data entities and features by these, never
# by the ontology's type keys.
ANCHORED_STORY_KINDS: Final = ("feature", "interface", "entity")
_AREA_RELATION_ROLES = tuple(sorted(RELATION_KEYS - frozenset(("containment", "grouping", "dependency", "reference"))))


@dataclass(frozen=True)
class TypeRule:
    """A plannable type, group key or context type in the rules file."""

    key: str
    role: Role
    labels: tuple[str, ...]
    exclude: Mapping[str, tuple[str, ...]] = MappingProxyType({})
    phase: str | None = None
    required: bool = False
    story: str | None = None
    project_input: bool = False


@dataclass(frozen=True)
class PhaseRule:
    """Fallback waits and complexity of a plan phase."""

    name: str = ""
    after: tuple[str, ...] = ()
    after_mode: Literal["first", "all"] = "first"
    complexity: str = "medium"


@dataclass(frozen=True)
class PlanRules:
    """Validated rules over the merged ontology."""

    name: str
    version: str
    types: Mapping[str, TypeRule]
    relations: Mapping[str, str]
    phases: tuple[PhaseRule, ...]
    phase_guidance: str
    hop_limit: int
    release: str = "release"
    exclude_all: Mapping[str, tuple[str, ...]] = MappingProxyType({})
    acceptance_criteria: str | None = None
    assumption: tuple[str, str] | None = None

    def labels_of(self, key: str) -> tuple[str, ...]:
        return self.types[key].labels

    def is_assumption(self, attributes: Mapping[str, object]) -> bool:
        """Whether a node's attributes flag it as an assumption."""

        return self.assumption is not None and attributes.get(self.assumption[0]) == self.assumption[1]

    def acceptance_of(self, attributes: Mapping[str, object]) -> tuple[str, ...]:
        """A node's acceptance criteria, as a tuple of texts."""

        value = attributes.get(self.acceptance_criteria) if self.acceptance_criteria else None
        if isinstance(value, str):
            return (value,) if value.strip() else ()
        if isinstance(value, list | tuple):
            return tuple(str(item) for item in value if str(item).strip())
        return ()

    def story_kind(self, label: str) -> str | None:
        """The story kind a label's type anchors (``interface``, ``entity``, ``feature``), else None."""

        return self.rule_of(label).story

    def rule_of(self, label: str) -> TypeRule:
        return next(rule for rule in self.types.values() if label in rule.labels)

    def relation(self, role: str) -> str:
        """The ontology relation that plays a rules role."""

        return self.relations[role]

    def relation_role(self, name: str) -> str:
        """The rules role a relation plays."""

        return next(role for role, value in self.relations.items() if value == name)


def load_plan_rules(path: Path | None = None, *, ontology: Ontology) -> PlanRules:
    """Load and validate the shipped or given rules file against ``ontology``."""

    text = _SHIPPED_RULES.read_text(encoding="utf-8") if path is None else path.read_text(encoding="utf-8")
    try:
        data = _load_unique(text)
    except yaml.constructor.ConstructorError as error:
        raise PlanRulesError([str(error.problem)]) from error
    if not isinstance(data, dict):
        raise PlanRulesError(["the rules file must be a mapping"])
    problems: list[str] = []
    unknown_root = set(data) - ROOT_KEYS
    if unknown_root:
        problems.append(f"unknown root keys: {', '.join(map(str, sorted(unknown_root, key=str)))}")
    if data.get("format_version") != 1:
        problems.append("format_version must be 1")
    raw_types = _mapping(data, "types", problems)
    raw_relations = _mapping(data, "relations", problems)
    rules = _type_rules(raw_types, ontology, problems)
    relations = _relations(raw_relations, ontology, problems)
    phases = _phases(_mapping(data, "phases", problems), problems)
    _relation_checks(rules, relations, ontology, problems)
    _attribute_checks(data, rules, ontology, problems)
    _phase_checks(phases, problems)
    if not isinstance(data.get("name"), str) or not isinstance(data.get("version"), (str, int)):
        problems.append("name and version must be strings")
    if problems:
        raise PlanRulesError(problems)
    return PlanRules(
        name=data["name"],
        version=str(data["version"]),
        types=MappingProxyType(rules),
        relations=MappingProxyType(relations),
        phases=tuple(phases),
        exclude_all=_exclude_all(data.get("exclude_all")),
        phase_guidance=data.get("phase_guidance", ""),
        hop_limit=_hop_limit(relations, ontology),
        release=_attribute_name(data.get("release")) or "release",
        acceptance_criteria=_attribute_name(data.get("acceptance_criteria")),
        assumption=_assumption(data.get("assumption")),
    )


def _mapping(value: object, key: str, problems: list[str]) -> dict[str, object]:
    result = value.get(key) if isinstance(value, dict) else None
    if not isinstance(result, dict):
        problems.append(f"{key} must be a mapping")
        return {}
    return result


def _exclude_all(value: object) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        return MappingProxyType({})
    return MappingProxyType(
        {
            str(attribute): tuple(raw_values) if isinstance(raw_values, list) else (raw_values,)
            for attribute, raw_values in value.items()
        }
    )


def _type_rules(raw: dict[str, object], ontology: Ontology, problems: list[str]) -> dict[str, TypeRule]:
    rules: dict[str, TypeRule] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            problems.append(f"types.{key} must be a mapping")
            continue
        _type_value_problems(key, value, problems)
        unknown = set(value) - TYPE_KEYS
        if unknown:
            problems.append(f"types.{key} has unknown keys: {', '.join(sorted(unknown))}")
        role = value.get("role")
        if role not in ("area_anchor", "shared", "member", "context"):
            problems.append(f"types.{key}.role must be area_anchor, shared, member or context")
            continue
        labels = (
            tuple(sorted(node_type.label for node_type in ontology.node_types.values() if node_type.group == key))
            if key in ontology.node_groups
            else ((ontology.node_types[key].label,) if key in ontology.node_types else ())
        )
        if not labels:
            problems.append(f"type '{key}' is not a declared node type or group")
            continue
        if any(ontology.owner_of(label) == "plan" for label in labels):
            problems.append(f"type '{key}' is owned by plan")
        if role in ("area_anchor", "shared", "member") and not any(
            ontology.is_legal("IN_AREA", label, "WorkArea") for label in labels
        ):
            problems.append(f"type '{key}' is not a legal source of IN_AREA")
        if role in ("area_anchor", "shared", "member") and not any(
            ontology.is_legal("IMPLEMENTS", "Task", label) for label in labels
        ):
            problems.append(f"type '{key}' is not a legal target of IMPLEMENTS from Task")
        exclude = _exclude(value.get("exclude"), ontology, labels, key, problems)
        rules[key] = TypeRule(
            key=key,
            role=role,
            labels=labels,
            exclude=exclude,
            phase=value.get("phase"),
            required=value.get("required", False),
            story=_story_kind(value.get("story")),
            project_input=value.get("project_input", False),
        )
    _role_checks(rules, ontology, problems)
    return rules


def _story_kind(value: object) -> str | None:
    return value if isinstance(value, str) and value in ANCHORED_STORY_KINDS else None


def _type_value_problems(key: str, value: dict[str, object], problems: list[str]) -> None:
    problems.extend(
        f"types.{key}.{boolean} must be a boolean"
        for boolean in ("required", "project_input")
        if boolean in value and not isinstance(value[boolean], bool)
    )
    if "story" in value and _story_kind(value["story"]) is None:
        problems.append(f"types.{key}.story must be one of {', '.join(ANCHORED_STORY_KINDS)}")
    if "phase" in value and not isinstance(value["phase"], str):
        problems.append(f"types.{key}.phase must be a phase")
    if "exclude" in value and not isinstance(value["exclude"], dict):
        problems.append(f"types.{key}.exclude must be a mapping")


def _exclude(
    value: object,
    ontology: Ontology,
    labels: tuple[str, ...],
    key: str,
    problems: list[str],
) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for attribute, raw_values in value.items():
        specs = [ontology.node_type(label).attributes.get(attribute) for label in labels]
        specs = [spec for spec in specs if spec is not None]
        if not specs:
            problems.append(f"types.{key}.exclude.{attribute} is not a declared attribute")
            continue
        spec = specs[0]
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        allowed_values = tuple(spec.values) if spec is not None else ()
        if not set(values) <= set(allowed_values):
            problems.append(f"types.{key}.exclude.{attribute} names an undeclared value")
        result[attribute] = tuple(values)
    return result


def _role_checks(rules: dict[str, TypeRule], ontology: Ontology, problems: list[str]) -> None:
    anchors = [rule for rule in rules.values() if rule.role == "area_anchor"]
    if len(anchors) != 1:
        problems.append("there must be exactly one area_anchor type")
    for rule in rules.values():
        if rule.role == "member" and not rule.phase:
            problems.append(f"member type '{rule.key}' must declare a phase")
        if (rule.required or rule.story) and rule.role != "member":
            problems.append(f"required and story are only allowed on member types: {rule.key}")
        if rule.role == "context" and any((rule.phase, rule.required, rule.story, rule.project_input, rule.exclude)):
            problems.append(f"context type '{rule.key}' may not set a member or exclusion field")
        if rule.phase and rule.role != "member":
            problems.append(f"phase is only allowed on member types: {rule.key}")
        if rule.story and not any(ontology.is_legal("IMPLEMENTS", "Story", label) for label in rule.labels):
            problems.append(f"story type '{rule.key}' is not a legal target of IMPLEMENTS from Story")


def _relations(raw: dict[str, object], ontology: Ontology, problems: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for role, name in raw.items():
        if role not in RELATION_KEYS:
            problems.append(f"relations has unknown key '{role}'")
        if not isinstance(name, str) or name not in ontology.relations:
            problems.append(f"relation '{name}' is not declared")
            continue
        if ontology.owner_of(name) == "plan":
            problems.append(f"relation role '{role}' names the plan-owned relation '{name}'")
            continue
        result[role] = name
    return result


def _phases(raw: dict[str, object], problems: list[str]) -> list[PhaseRule]:
    result: list[PhaseRule] = []
    for name, value in raw.items():
        if not isinstance(value, dict):
            problems.append(f"phases.{name} must be a mapping")
            continue
        complexity = value.get("complexity")
        after = value.get("after", [])
        if not isinstance(complexity, str):
            problems.append(f"phases.{name}.complexity must be a complexity value")
            continue
        if not isinstance(after, list) or not all(isinstance(item, str) for item in after):
            problems.append(f"phases.{name}.after must be a list of phases")
            continue
        result.append(
            PhaseRule(
                name=name,
                after=tuple(after),
                after_mode=value.get("after_mode", "first"),
                complexity=complexity,
            )
        )
    return result


def _relation_checks(
    rules: dict[str, TypeRule], relations: dict[str, str], ontology: Ontology, problems: list[str]
) -> None:
    missing = RELATION_KEYS - set(relations)
    if missing:
        problems.append(f"relation roles are missing: {', '.join(sorted(missing))}")
    by_name: dict[str, list[str]] = {}
    for role, name in relations.items():
        by_name.setdefault(name, []).append(role)
    for name, roles in sorted(by_name.items()):
        if len(roles) > 1:
            problems.append(f"relation '{name}' fills more than one role: {', '.join(sorted(roles))}")
    anchors = next((rule.labels for rule in rules.values() if rule.role == "area_anchor"), ())
    if not anchors:
        return
    for role in _AREA_RELATION_ROLES:
        role_name = relations.get(role)
        if role_name is None:
            continue
        if not any(pair.source in anchors or pair.target in anchors for pair in ontology.pairs(role_name)):
            problems.append(f"relation role '{role}' has no pair ending at the area_anchor type")
    containment = relations.get("containment")
    problems.extend(
        f"containment has no pair from member type '{rule.key}'"
        for rule in rules.values()
        if containment
        and rule.role == "member"
        and not rule.story
        and not any(
            ontology.is_legal(containment, source, pair.target)
            for source in rule.labels
            for pair in ontology.pairs(containment)
            if pair.target in anchors or pair.target not in ontology.spine.roots
        )
    )


def _attribute_checks(
    data: dict[str, object], rules: dict[str, TypeRule], ontology: Ontology, problems: list[str]
) -> None:
    exclude_all = data.get("exclude_all")
    if not isinstance(exclude_all, dict):
        problems.append("exclude_all must be a mapping")
    else:
        for attribute, raw_values in exclude_all.items():
            if not _has_attribute(rules, ontology, str(attribute)):
                continue
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            specs = [
                ontology.node_type(label).attributes.get(str(attribute))
                for rule in rules.values()
                if rule.role != "context"
                for label in rule.labels
            ]
            if any(spec is not None and not set(values) <= set(spec.values) for spec in specs):
                problems.append(f"exclude_all.{attribute} names an undeclared value")
        problems.extend(
            f"exclude_all attribute '{attribute}' is not declared by a plannable type"
            for attribute in exclude_all
            if not _has_attribute(rules, ontology, str(attribute))
        )
    for key in ("release", "acceptance_criteria"):
        attribute = _attribute_name(data.get(key))
        if attribute and not _has_attribute(rules, ontology, attribute):
            problems.append(f"{key} attribute '{attribute}' is not declared by a plannable type")
    assumption = data.get("assumption")
    if not isinstance(assumption, dict):
        problems.append("assumption must be a mapping")
        return
    attribute = _attribute_name(assumption)
    value = assumption.get("value")
    if attribute and not _has_attribute(rules, ontology, attribute):
        problems.append(f"assumption attribute '{attribute}' is not declared by a plannable type")
    if not isinstance(value, str):
        problems.append("assumption.value must be a string")


def _has_attribute(rules: dict[str, TypeRule], ontology: Ontology, attribute: str) -> bool:
    return any(
        attribute in ontology.node_type(label).attributes
        for rule in rules.values()
        if rule.role != "context"
        for label in rule.labels
    )


def _assumption(value: object) -> tuple[str, str] | None:
    attribute = _attribute_name(value)
    flag = value.get("value") if isinstance(value, dict) else None
    return (attribute, flag) if attribute and isinstance(flag, str) else None


def _attribute_name(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    attribute = value.get("attribute")
    return attribute if isinstance(attribute, str) else None


def _phase_checks(phases: list[PhaseRule], problems: list[str]) -> None:
    names = [phase.name for phase in phases]
    if tuple(names) != plan_vocabularies()["phase"]:
        problems.append("phases must list every phase value once")
    if len(names) != len(set(names)):
        problems.append("phases lists a phase more than once")
    if not {phase.complexity for phase in phases} <= set(plan_vocabularies()["complexity"]):
        problems.append("phase complexity must be a complexity value")
    for index, phase in enumerate(phases):
        if any(after not in names[:index] for after in phase.after):
            problems.append(f"phases.{phase.name}.after names a phase that is not earlier")


def _hop_limit(relations: dict[str, str], ontology: Ontology) -> int:
    containment = relations.get("containment")
    return max((path.max_hops for path in ontology.spine.paths if containment in path.relations), default=1)
