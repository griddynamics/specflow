"""The gap rules file: detectors, relevance, checklist, routing and limits (docs/graph/gap/ontology.md "Rules file").

``load_gap_rules`` reads the shipped ``gap-rules.yaml`` (or a project's copy) and validates it against the merged
ontology. Every problem found is listed in one ``GapRulesError`` (a ``ConfigurationError``, FR-GONT-03). Every
spec-side name graph.gap uses (types, groups, relations, attributes and values) comes from this file (FR-GONT-04);
the detector codes and rule names are graph.gap's own.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, TypeGuard, cast

import yaml
from pydantic import ValidationError

from graph.core import Ontology
from graph.core.ontology.loader import _load_unique
from graph.gap.errors import GapRulesError
from graph.gap.names import GAP, DetectorFamily, SeedStrength
from graph.gap.settings import GapLimits

FORMAT_VERSION: Final = 1
_SHIPPED_RULES: Final = files("graph.gap") / "gap-rules.yaml"

ROOT_KEYS: Final = frozenset(
    {
        "format_version",
        "name",
        "version",
        "relevance",
        "seeds",
        "detectors",
        "placeholder_markers",
        "vague_terms",
        "checklist",
        "routing",
        "limits",
    }
)
RELEVANCE_KEYS: Final = frozenset(
    {
        "tracking_titles",
        "tracking_patterns",
        "isolated_share",
        "obligation_cues",
        "classify_with_model",
    }
)
CORROBORATION_SIGNALS: Final = frozenset({"obligation_wording", "names_system_part", "planned_dependency"})
FAMILIES: Final[tuple[DetectorFamily, ...]] = ("structure", "text")
STRENGTHS: Final[tuple[SeedStrength, ...]] = ("strong", "weak")
DIRECTIONS: Final = ("in", "out")
DETECTOR_CODE: Final = re.compile(r"^[A-Z]{2}-[A-Z]{2,16}$")
DETECTOR_KEYS: Final = frozenset({"kind", "family", "strength", "rule", "slice"})

# The parameters each detector rule needs (detection.md "Detector catalogue"); a rule takes no other parameter.
RULE_PARAMETERS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "no_spine_owner": frozenset({"types"}),
        "missing_relation": frozenset({"types", "relation", "direction"}),
        "integration_without_interface": frozenset({"types", "relations"}),
        "empty_attribute": frozenset({"types", "attribute"}),
        "checklist": frozenset({"types"}),
        "dangling_reference": frozenset(),
        "checklist_topic": frozenset({"types", "topic"}),
        "vague_terms": frozenset({"types"}),
        "no_number": frozenset({"types"}),
        "attribute_value": frozenset({"types", "attribute", "value"}),
        "refused_merge": frozenset({"types"}),
        "similar_across_documents": frozenset({"types"}),
        "overlapping_conditions": frozenset({"types"}),
        "in_scope_uses_out_of_scope": frozenset({"types", "attribute"}),
        "cycle": frozenset({"types", "relation"}),
        "near_names": frozenset({"types"}),
        "carries_differ": frozenset({"types", "relation"}),
        "present": frozenset({"types"}),
        "placeholder_markers": frozenset(),
    }
)
PARAMETER_KEYS: Final = frozenset().union(*RULE_PARAMETERS.values())

# The detector catalogue (detection.md "Detector catalogue"): code -> kind, family, strength and rule. These are fixed
# by the spec; a rules file may drop a detector or tune its parameters and slice, never add one or change these.
CATALOGUE: Final[Mapping[str, tuple[str, DetectorFamily, SeedStrength, str]]] = MappingProxyType(
    {
        "MS-OWNER": ("missing", "structure", "weak", "no_spine_owner"),
        "MS-PROVIDER": ("missing", "structure", "strong", "missing_relation"),
        "MS-STORE": ("missing", "structure", "strong", "missing_relation"),
        "MS-CONTRACT": ("missing", "structure", "strong", "integration_without_interface"),
        "MS-CRITERIA": ("missing", "structure", "weak", "empty_attribute"),
        "MS-COVERAGE": ("missing", "structure", "strong", "checklist"),
        "MS-REF": ("missing", "text", "strong", "dangling_reference"),
        "MS-CONSUMER": ("missing", "structure", "weak", "missing_relation"),
        "MS-ERROR": ("missing", "structure", "weak", "checklist_topic"),
        "MS-LIFECYCLE": ("missing", "structure", "weak", "checklist_topic"),
        "AM-VAGUE": ("ambiguous", "text", "strong", "vague_terms"),
        "AM-MEASURE": ("ambiguous", "text", "strong", "no_number"),
        "AM-IMPLIED": ("ambiguous", "structure", "weak", "attribute_value"),
        "CT-VALUE": ("contradiction", "text", "strong", "refused_merge"),
        "CT-CROSS": ("contradiction", "text", "weak", "similar_across_documents"),
        "CT-RULE": ("contradiction", "text", "weak", "overlapping_conditions"),
        "IN-SCOPE": ("inconsistency", "structure", "strong", "in_scope_uses_out_of_scope"),
        "IN-CYCLE": ("inconsistency", "structure", "strong", "cycle"),
        "IN-TERM": ("inconsistency", "text", "weak", "near_names"),
        "IN-PAYLOAD": ("inconsistency", "text", "weak", "carries_differ"),
        "OQ-STATED": ("open_question", "text", "strong", "present"),
        "OQ-TBD": ("open_question", "text", "strong", "placeholder_markers"),
        "AS-FLAG": ("assumption", "structure", "strong", "attribute_value"),
    }
)
# The kinds AI may answer (answers.md "Routing"); a rules file may only shrink the list.
AI_KINDS: Final = ("missing", "ambiguous", "inconsistency")
GATE_KEYS: Final = frozenset({"local_max_nodes", "local_max_areas", "min_confidence"})
ROUTING_KEYS: Final = frozenset({"ai_kinds", "gates", "severity_prior"})


@dataclass(frozen=True)
class RelevanceRules:
    """How sections are judged spec, tracking or mixed (detection.md "Relevance filter")."""

    tracking_titles: tuple[str, ...]
    tracking_patterns: Mapping[str, re.Pattern[str]]
    isolated_share: float
    obligation_cues: tuple[str, ...]
    classify_with_model: bool


@dataclass(frozen=True)
class DetectorRule:
    """One detector entry. ``types`` are the rules' type or group keys, ``labels`` the labels they resolve to in the
    merged ontology (empty for a rule without types)."""

    code: str
    kind: str
    family: DetectorFamily
    strength: SeedStrength
    rule: str
    types: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    relation: str | None = None
    relations: tuple[str, ...] = ()
    direction: Literal["in", "out"] | None = None
    attribute: str | None = None
    value: str | None = None
    topic: str | None = None
    slice: int = 1


@dataclass(frozen=True)
class Gates:
    """The gates an AI answer must pass (answers.md "Routing")."""

    local_max_nodes: int
    local_max_areas: int
    min_confidence: float


@dataclass(frozen=True)
class RoutingRules:
    """Which kinds AI may answer, the gates and the severity priors used to rank sites."""

    ai_kinds: tuple[str, ...]
    gates: Gates
    severity_prior: Mapping[str, float]


@dataclass(frozen=True)
class GapRules:
    """The validated rules over the merged ontology (``GapContext.rules``)."""

    name: str
    version: str
    relevance: RelevanceRules
    corroboration: tuple[str, ...]
    detectors: Mapping[str, DetectorRule]
    placeholder_markers: tuple[str, ...]
    vague_terms: tuple[str, ...]
    checklist: Mapping[str, tuple[str, ...]]
    routing: RoutingRules
    limits: GapLimits

    def detectors_of_slice(self, slice_: int) -> tuple[DetectorRule, ...]:
        """The detectors that ship with ``slice_`` or an earlier slice, in file order."""

        return tuple(rule for rule in self.detectors.values() if rule.slice <= slice_)


def load_gap_rules(path: Path | None = None, *, ontology: Ontology) -> GapRules:
    """Load the shipped rules (``path`` None) or the given file and validate it against ``ontology``.

    Raises ``GapRulesError`` listing every problem: unreadable or invalid YAML, unknown keys, an unknown gap kind,
    type, group, relation, attribute or value, a pattern that does not compile, a share or threshold out of range,
    an unknown rule or a rule missing a parameter, a topic no checklist holds, or a limit out of range.
    """

    data = _read(path)
    problems: list[str] = []
    if not isinstance(data, dict):
        raise GapRulesError(["the rules file must be a mapping"])
    _unknown(data, ROOT_KEYS, "", problems)
    if data.get("format_version") != FORMAT_VERSION:
        problems.append(f"format_version must be {FORMAT_VERSION}")
    name, version = data.get("name"), data.get("version")
    if not isinstance(name, str) or not isinstance(version, str | int) or isinstance(version, bool):
        problems.append("name and version must be strings")
    kinds = _gap_kinds(ontology, problems)
    relevance = _relevance(data.get("relevance"), problems)
    corroboration = _corroboration(data.get("seeds"), problems)
    checklist = _checklist(data.get("checklist"), problems)
    detectors = _detectors(data.get("detectors"), ontology, kinds, checklist, problems)
    markers = _terms(data, "placeholder_markers", problems)
    vague = _terms(data, "vague_terms", problems)
    routing = _routing(data.get("routing"), kinds, problems)
    limits = _limits(data.get("limits"), problems)
    if problems or relevance is None or routing is None or limits is None:
        raise GapRulesError(problems or ["the rules file is invalid"])
    return GapRules(
        name=str(name),
        version=str(version),
        relevance=relevance,
        corroboration=corroboration,
        detectors=MappingProxyType(detectors),
        placeholder_markers=markers,
        vague_terms=vague,
        checklist=MappingProxyType(checklist),
        routing=routing,
        limits=limits,
    )


def _read(path: Path | None) -> Any:
    try:
        text = _SHIPPED_RULES.read_text(encoding="utf-8") if path is None else Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GapRulesError([f"cannot read the rules file ({type(error).__name__})"]) from error
    try:
        return _load_unique(text)
    except yaml.YAMLError as error:
        problem = getattr(error, "problem", None) or str(error)
        raise GapRulesError([f"invalid YAML: {problem}"]) from error


def _unknown(value: Mapping[str, Any], allowed: frozenset[str], where: str, problems: list[str]) -> None:
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        prefix = f"{where} has unknown keys" if where else "unknown root keys"
        problems.append(f"{prefix}: {', '.join(unknown)}")


def _mapping(value: Any, where: str, problems: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        problems.append(f"{where} must be a mapping")
        return None
    return value


def _strings(value: Any, where: str, problems: list[str], *, non_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        problems.append(f"{where} must be a list of non-empty strings")
        return ()
    if non_empty and not value:
        problems.append(f"{where} must not be empty")
    return tuple(value)


def _number(value: Any) -> TypeGuard[float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _gap_kinds(ontology: Ontology, problems: list[str]) -> tuple[str, ...]:
    if not ontology.is_declared(GAP):
        problems.append("the ontology does not hold the gap ontology file (no Gap type)")
        return ()
    spec = ontology.node_type(GAP).attributes.get("kind")
    return tuple(spec.values) if spec is not None else ()


def _relevance(value: Any, problems: list[str]) -> RelevanceRules | None:
    raw = _mapping(value, "relevance", problems)
    if raw is None:
        return None
    _unknown(raw, RELEVANCE_KEYS, "relevance", problems)
    patterns: dict[str, re.Pattern[str]] = {}
    raw_patterns = _mapping(raw.get("tracking_patterns"), "relevance.tracking_patterns", problems) or {}
    for key, pattern in raw_patterns.items():
        if not isinstance(pattern, str):
            problems.append(f"relevance.tracking_patterns.{key} must be a string")
            continue
        try:
            patterns[str(key)] = re.compile(pattern)
        except re.error as error:
            problems.append(f"relevance.tracking_patterns.{key} does not compile: {error}")
    share = raw.get("isolated_share")
    if not _number(share) or not 0 <= share <= 1:
        problems.append("relevance.isolated_share must be a number from 0 to 1")
        share = 0.0
    classify = raw.get("classify_with_model")
    if not isinstance(classify, bool):
        problems.append("relevance.classify_with_model must be a boolean")
    return RelevanceRules(
        tracking_titles=_strings(raw.get("tracking_titles"), "relevance.tracking_titles", problems),
        tracking_patterns=MappingProxyType(patterns),
        isolated_share=float(share),
        obligation_cues=_strings(raw.get("obligation_cues"), "relevance.obligation_cues", problems),
        classify_with_model=bool(classify),
    )


def _corroboration(value: Any, problems: list[str]) -> tuple[str, ...]:
    raw = _mapping(value, "seeds", problems)
    if raw is None:
        return ()
    _unknown(raw, frozenset({"corroboration"}), "seeds", problems)
    signals = _strings(raw.get("corroboration"), "seeds.corroboration", problems)
    unknown = sorted(set(signals) - CORROBORATION_SIGNALS)
    if unknown:
        problems.append(f"seeds.corroboration names unknown signals: {', '.join(unknown)}")
    return signals


def _checklist(value: Any, problems: list[str]) -> dict[str, tuple[str, ...]]:
    raw = _mapping(value, "checklist", problems)
    if raw is None:
        return {}
    if not raw:
        problems.append("checklist must not be empty")
    return {str(topic): _strings(cues, f"checklist.{topic}", problems) for topic, cues in raw.items()}


def _terms(data: Mapping[str, Any], key: str, problems: list[str]) -> tuple[str, ...]:
    return _strings(data.get(key), key, problems)


def _detectors(
    value: Any,
    ontology: Ontology,
    kinds: Sequence[str],
    checklist: Mapping[str, tuple[str, ...]],
    problems: list[str],
) -> dict[str, DetectorRule]:
    raw = _mapping(value, "detectors", problems)
    if raw is None:
        return {}
    if not raw:
        problems.append("detectors must not be empty")
    detectors: dict[str, DetectorRule] = {}
    for code, entry in raw.items():
        found = _detector(str(code), entry, ontology=ontology, kinds=kinds, checklist=checklist, problems=problems)
        if found is not None:
            detectors[found.code] = found
    return detectors


def _detector_header(code: str, raw: Mapping[str, Any], where: str, kinds: Sequence[str], problems: list[str]) -> None:
    """The checks of a detector's kind, family, strength, rule and slice."""

    kind, family, strength = raw.get("kind"), raw.get("family"), raw.get("strength")
    fixed = CATALOGUE.get(code)
    if fixed is not None and (kind, family, strength, raw.get("rule")) != fixed:
        problems.append(f"{where}: kind, family, strength and rule must be {', '.join(fixed)} (detector catalogue)")
    if kind not in kinds:
        problems.append(f"{where}.kind '{kind}' is not a gap_kind value")
    if family not in FAMILIES:
        problems.append(f"{where}.family must be one of {', '.join(FAMILIES)}")
    if strength not in STRENGTHS:
        problems.append(f"{where}.strength must be one of {', '.join(STRENGTHS)}")
    slice_ = raw.get("slice", 1)
    if not isinstance(slice_, int) or isinstance(slice_, bool) or slice_ < 1:
        problems.append(f"{where}.slice must be a positive integer")


def _detector(
    code: str,
    entry: Any,
    *,
    ontology: Ontology,
    kinds: Sequence[str],
    checklist: Mapping[str, tuple[str, ...]],
    problems: list[str],
) -> DetectorRule | None:
    where = f"detectors.{code}"
    if not DETECTOR_CODE.match(code):
        problems.append(f"{where}: the code must look like XX-NAME")
    elif code not in CATALOGUE:
        problems.append(f"{where}: not a detector of the catalogue (adding one is a spec change)")
    raw = _mapping(entry, where, problems)
    if raw is None:
        return None
    count = len(problems)
    _detector_header(code, raw, where, kinds, problems)
    rule = raw.get("rule")
    if not isinstance(rule, str) or rule not in RULE_PARAMETERS:
        problems.append(f"{where}.rule '{rule}' is not a known rule")
        _unknown(raw, DETECTOR_KEYS | PARAMETER_KEYS, where, problems)
        return None
    needed = RULE_PARAMETERS[rule]
    _unknown(raw, DETECTOR_KEYS | needed, where, problems)
    missing = sorted(needed - set(raw))
    if missing:
        problems.append(f"{where}: rule '{rule}' needs {', '.join(missing)}")
    types, labels = _types(raw.get("types"), ontology, where, problems, needed="types" in needed)
    relation = _relation(raw, ontology, labels, where, problems, both_ends=rule == "cycle")
    relations = _relation_list(raw.get("relations"), ontology, where, problems)
    attribute, value = _attribute(raw, ontology, labels, where, problems)
    topic = raw.get("topic")
    if "topic" in needed and topic not in checklist:
        problems.append(f"{where}.topic '{topic}' is not a checklist topic")
    if len(problems) > count:
        return None
    return DetectorRule(
        code=code,
        kind=str(raw.get("kind")),
        family=cast(DetectorFamily, raw.get("family")),
        strength=cast(SeedStrength, raw.get("strength")),
        rule=rule,
        types=types,
        labels=labels,
        relation=relation,
        relations=relations,
        direction=cast(Literal["in", "out"] | None, raw.get("direction")),
        attribute=attribute,
        value=value,
        topic=topic if isinstance(topic, str) else None,
        slice=int(raw.get("slice", 1)),
    )


def _types(
    value: Any, ontology: Ontology, where: str, problems: list[str], *, needed: bool
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if value is None:
        return (), ()
    keys = _strings(value, f"{where}.types", problems, non_empty=needed)
    labels: list[str] = []
    for key in keys:
        found = _labels_of(key, ontology)
        if not found:
            problems.append(f"{where}.types: '{key}' is not a declared node type or group")
            continue
        foreign = [label for label in found if ontology.owner_of(label) != "build"]
        if foreign:
            problems.append(f"{where}.types: '{key}' is not a spec type")
            continue
        labels.extend(found)
    return keys, tuple(dict.fromkeys(labels))


def _labels_of(key: str, ontology: Ontology) -> tuple[str, ...]:
    if key in ontology.node_groups:
        return tuple(sorted(ontology.node_groups[key].labels))
    if key in ontology.node_types:
        return (ontology.node_types[key].label,)
    return ()


def _relation(
    raw: Mapping[str, Any],
    ontology: Ontology,
    labels: Sequence[str],
    where: str,
    problems: list[str],
    *,
    both_ends: bool = False,
) -> str | None:
    value, direction = raw.get("relation"), raw.get("direction")
    if direction is not None and direction not in DIRECTIONS:
        problems.append(f"{where}.direction must be in or out")
    if value is None:
        return None
    if not isinstance(value, str) or value not in ontology.relations:
        problems.append(f"{where}.relation '{value}' is not a declared relation")
        return None
    if ontology.owner_of(value) != "build":
        problems.append(f"{where}.relation '{value}' is not a spec relation")
        return None
    pairs = ontology.relation(value).pairs
    ends = {pair.target for pair in pairs} if direction == "in" else {pair.source for pair in pairs}
    if both_ends:
        ends = {pair.source for pair in pairs if pair.source in labels and pair.target in labels}
    if labels and not ends & set(labels):
        problems.append(f"{where}.relation '{value}' has no pair at any of the detector's types")
    return value


def _relation_list(value: Any, ontology: Ontology, where: str, problems: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    names = _strings(value, f"{where}.relations", problems)
    problems.extend(
        f"{where}.relations: '{name}' is not a declared spec relation"
        for name in names
        if name not in ontology.relations or ontology.owner_of(name) != "build"
    )
    return names


def _attribute(
    raw: Mapping[str, Any],
    ontology: Ontology,
    labels: Sequence[str],
    where: str,
    problems: list[str],
) -> tuple[str | None, str | None]:
    attribute, value = raw.get("attribute"), raw.get("value")
    if attribute is None:
        return None, None
    specs = [ontology.node_type(label).attributes.get(str(attribute)) for label in labels]
    declared = [spec for spec in specs if spec is not None]
    if not isinstance(attribute, str) or not declared or len(declared) < len(specs):
        # A type without the attribute would be skipped silently (detection.md "Rules file"): every type needs it.
        problems.append(f"{where}.attribute '{attribute}' is not declared on every one of the detector's types")
        return None, None
    if "value" in raw:
        allowed = {item for spec in declared for item in spec.values}
        if not isinstance(value, str) or (allowed and value not in allowed):
            problems.append(f"{where}.value '{value}' is not a value of attribute '{attribute}'")
            return attribute, None
    return attribute, value if isinstance(value, str) else None


def _routing(value: Any, kinds: Sequence[str], problems: list[str]) -> RoutingRules | None:
    raw = _mapping(value, "routing", problems)
    if raw is None:
        return None
    _unknown(raw, ROUTING_KEYS, "routing", problems)
    ai_kinds = _strings(raw.get("ai_kinds"), "routing.ai_kinds", problems, non_empty=False)
    unknown = [kind for kind in ai_kinds if kind not in kinds]
    if unknown:
        problems.append(f"routing.ai_kinds names values that are not gap kinds: {', '.join(unknown)}")
    widened = [kind for kind in ai_kinds if kind in kinds and kind not in AI_KINDS]
    if widened:
        problems.append(f"routing.ai_kinds may only shrink; AI never answers: {', '.join(widened)}")
    gates = _mapping(raw.get("gates"), "routing.gates", problems) or {}
    _unknown(gates, GATE_KEYS, "routing.gates", problems)
    for key in ("local_max_nodes", "local_max_areas"):
        found = gates.get(key)
        if not isinstance(found, int) or isinstance(found, bool) or found < 1:
            problems.append(f"routing.gates.{key} must be a positive integer")
    confidence = gates.get("min_confidence")
    if not _number(confidence) or not 0 < confidence <= 1:
        problems.append("routing.gates.min_confidence must be a number above 0 and at most 1")
    priors = _mapping(raw.get("severity_prior"), "routing.severity_prior", problems) or {}
    unknown_priors = sorted(str(kind) for kind in priors if kind not in kinds)
    if unknown_priors:
        problems.append(f"routing.severity_prior names values that are not gap kinds: {', '.join(unknown_priors)}")
    missing = [kind for kind in kinds if kind not in priors]
    if missing:
        problems.append(f"routing.severity_prior misses the kinds: {', '.join(missing)}")
    bad = sorted(str(kind) for kind, prior in priors.items() if not _number(prior) or prior <= 0)
    if bad:
        problems.append(f"routing.severity_prior must be positive numbers: {', '.join(bad)}")
    return RoutingRules(
        ai_kinds=ai_kinds,
        gates=Gates(
            local_max_nodes=int(gates.get("local_max_nodes") or 0),
            local_max_areas=int(gates.get("local_max_areas") or 0),
            min_confidence=float(confidence) if _number(confidence) else 0.0,
        ),
        severity_prior=MappingProxyType({str(kind): float(prior) for kind, prior in priors.items() if _number(prior)}),
    )


def _limits(value: Any, problems: list[str]) -> GapLimits | None:
    raw = _mapping(value, "limits", problems)
    if raw is None:
        return None
    try:
        return GapLimits.model_validate(raw)
    except ValidationError as error:
        for item in error.errors():
            location = ".".join(str(part) for part in item["loc"])
            message = "unknown key" if item["type"] == "extra_forbidden" else item["msg"]
            problems.append(f"limits{'.' + location if location else ''}: {message}")
        return None
