"""Renamed-ontology test (plan 5.T3; FR-ONT-03, ASR2: no ontology literal in code).

A copy of the shipped ``spec-ontology.yaml`` and its judge prompts is written at test time with every
name the format leaves free renamed (``renamed_ontology_data``): node type keys, labels, the node
group, relations, ``id.format`` prefixes, attribute names, vocabulary names, enum values, inference
rule ids, judge prompt names, the ontology name, and the label and relation names inside prompt text.
The shop corpus is then built 1 -> 2 -> 3 twice, with inference and verification on, into two projects:
once with the shipped ontology, once with the renamed copy and the scripted gateway's ``rename``
mapping. Every model answer is the same hand-written one (``ShopAnswers``, ``script_shop_judge``);
the renamed build only sees it under the renamed names.

Kept, because the format or the data model fixes them: format keys, ``owner``, ``origin``, ``tier``,
``role``, ``identity`` values, template names and parameters, the built-in labels and relations,
``{SLUG}``, ``stmt`` and ``quote`` (attribute names the data model gives a meaning:
data-model.md "Spec node", ``graph.build.conform.QUOTE_ATTRIBUTE``). Also kept: ``id_families``
(the format allows renaming them, but the corpus text names them: ``FR-CHK-001`` is an ``FR`` id;
renaming them changes what the text says, not what the code knows) and ``cues`` (regexes over the
text). The test fails when the shipped ontology gains a name the rename tables do not cover.

The two graphs must map onto each other (``Mapper``): the same nodes per mapped label with the same
names, mapped ``spec_id``s, identity keys and attributes; the same edges per mapped relation with the
same basis, evidence and facts; the same ``spec_id`` registry; the same build results (spec changes,
metrics, verification) and inference reports; the same model calls per prompt. And no original label,
relation name, id prefix or rule id reaches a prompt of the renamed builds outside document blocks.

Gate: ``SPECFLOW_TEST_NEO4J=1`` and ``uv run --env-file ../.env.local`` (the ontology checks at the
top run offline). The chains are built once per module (``open_neo4j_project``) into two
``pytest-graph-<hex>`` projects, deleted after the module.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml

from graph.build import BuildResult
from graph.build.models import InferenceReport
from graph.core import Ontology, load_ontology
from graph_support import (
    ScriptedGateway,
    ShopBuilds,
    assert_all_scripted,
    open_neo4j_project,
    outside_untrusted,
    shipped_ontology,
)
from specflow_live import requires_live

live = requires_live("NEO4J")

# Rename tables: original name -> renamed name.

TYPE_KEYS: dict[str, str] = {
    "module": "komponente",
    "feature": "funktion",
    "capability": "querschnitt",
    "functional_requirement": "verhalten",
    "quality_requirement": "guete",
    "constraint": "vorgabe",
    "integration_requirement": "schnittstelle",
    "data_requirement": "datensatz",
    "business_rule": "geschaeftsregel",
}
GROUPS: dict[str, str] = {"requirement": "anforderung"}
LABELS: dict[str, str] = {
    "Module": "Komponente",
    "Feature": "Funktion",
    "Capability": "Querschnitt",
    "FunctionalRequirement": "Verhalten",
    "QualityRequirement": "Guete",
    "Constraint": "Vorgabe",
    "IntegrationRequirement": "Schnittstelle",
    "DataRequirement": "Datensatz",
    "BusinessRule": "Geschaeftsregel",
}
RELATIONS: dict[str, str] = {
    "PART_OF": "TEIL_VON",
    "GROUPED_BY": "GEBUENDELT_UNTER",
    "REALIZED_BY": "UMGESETZT_VON",
    "INTEGRATES_WITH": "RUFT_AUF",
    "RELATES_TO": "VERWEIST_AUF",
}
PREFIXES: dict[str, str] = {
    "MOD::": "KOMP::",
    "FEAT::": "FUNK::",
    "CAP::": "QUER::",
    "REQ::FR::": "ANF::VH::",
    "REQ::NFR::": "ANF::GT::",
    "REQ::CON::": "ANF::VG::",
    "REQ::INT::": "ANF::SS::",
    "REQ::DATA::": "ANF::DS::",
    "REQ::BR::": "ANF::GR::",
}
ATTRIBUTES: dict[str, str] = {
    "acceptance_criteria": "abnahme",
    "source_status": "herkunft",
    "is_assumption": "annahme",
    "module": "gehoert_zu",
    "external": "extern",
    "type": "art",
}
FIXED_ATTRIBUTES = frozenset({"stmt", "quote"})
VOCABULARIES: dict[str, str] = {"source_status": "herkunftsarten", "yes_no": "ja_nein"}
VALUES: dict[str, str] = {
    "stated": "belegt",
    "implied": "abgeleitet",
    "yes": "ja",
    "no": "nein",
    "frontend": "oberflaeche",
    "backend": "dienst",
    "datastore": "speicher",
    "deployment": "betrieb",
    "other": "sonstiges",
}
RULES: dict[str, str] = {"INF-INT-01": "R-SS-1", "INF-CAP-01": "R-QU-1", "INF-CONT-01": "R-TV-1"}
PROMPTS: dict[str, str] = {
    "judge_endpoints.2": "richter_enden.2",
    "judge_realization.1": "richter_umsetzung.1",
    "judge_attach_orphan.2": "richter_waisen.2",
}
NAME = "umbenannte-ontologie"

# The scripted gateway's ``rename``: the names a hand-written answer may hold (labels in node, hub and
# consolidation answers, relations in edge answers, attribute names and enum values in attribute answers).
ANSWER_RENAME: dict[str, str] = {**LABELS, **RELATIONS, **ATTRIBUTES, **VALUES}
SCOPES: dict[str, str] = {**LABELS, **GROUPS}
"""Identity scopes: the label (``scope: type``) or the group key (``scope: group``)."""
_TEXT_NAMES = re.compile(r"\b(" + "|".join(sorted((*LABELS, *RELATIONS), key=len, reverse=True)) + r")\b")


# The renamed copy


def rename_text(text: str) -> str:
    """Prompt text with every whole-word label and relation name renamed (prompt text is free)."""

    return _TEXT_NAMES.sub(lambda match: {**LABELS, **RELATIONS}[match.group(1)], text)


def _type_ref(name: str) -> str:
    return TYPE_KEYS.get(name, GROUPS.get(name, name))


def _attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    renamed: dict[str, Any] = {}
    for name, declared in attributes.items():
        spec = dict(declared)
        values = spec.get("values")
        if isinstance(values, str):
            spec["values"] = VOCABULARIES[values]
        elif isinstance(values, list):
            spec["values"] = [VALUES[value] for value in values]
        elif isinstance(values, dict):
            spec["values"] = {VALUES[value]: text for value, text in values.items()}
        if "aliases" in spec:
            spec["aliases"] = {alias: VALUES[value] for alias, value in spec["aliases"].items()}
        if "target" in spec:
            spec["target"] = _type_ref(spec["target"])
        if "description" in spec:
            spec["description"] = rename_text(spec["description"])
        renamed[ATTRIBUTES.get(name, name)] = spec
    return renamed


def _texts(entry: dict[str, Any]) -> dict[str, Any]:
    for key in ("description", "guidance"):
        if key in entry:
            entry[key] = rename_text(entry[key])
    return entry


def _prefix(id_format: str) -> str:
    prefix = id_format.removesuffix("{SLUG}")
    return PREFIXES[prefix] + "{SLUG}"


def _derive(entry: str) -> str:
    owner, _, attribute = entry.partition(".")
    return f"{_type_ref(owner)}.{ATTRIBUTES.get(attribute, attribute)}"


def _rename_node_types(node_types: Mapping[str, Any]) -> dict[str, Any]:
    types = {}
    for key, declared_type in node_types.items():
        node_type = _texts(dict(declared_type))
        node_type["label"] = LABELS[node_type["label"]]
        if "group" in node_type:
            node_type["group"] = GROUPS[node_type["group"]]
        if "id" in node_type:
            node_type["id"] = {**node_type["id"], "format": _prefix(node_type["id"]["format"])}
        if "attributes" in node_type:
            node_type["attributes"] = _attributes(node_type["attributes"])
        types[TYPE_KEYS[key]] = node_type
    return types


def _rename_edge_types(edge_types: Mapping[str, Any]) -> dict[str, Any]:
    relations = {}
    for name, declared_relation in edge_types.items():
        relation = _texts(dict(declared_relation))
        relation["pairs"] = [
            {**pair, "from": _type_ref(pair["from"]), "to": _type_ref(pair["to"])} for pair in relation["pairs"]
        ]
        if "derive" in relation:
            relation["derive"] = [_derive(entry) for entry in relation["derive"]]
        relations[RELATIONS[name]] = relation
    return relations


def _rename_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    renamed = dict(rule)
    renamed["id"] = RULES[rule["id"]]
    renamed["prompt"] = PROMPTS[rule["prompt"]]
    for key in ("subject", "target", "container"):
        if key in rule:
            renamed[key] = _type_ref(rule[key])
    if "targets" in rule:
        renamed["targets"] = [_type_ref(name) for name in rule["targets"]]
    for key in ("member_relation", "container_relation", "writes"):
        if key in rule:
            renamed[key] = RELATIONS[rule[key]]
    if "via" in rule:
        renamed["via"] = [RELATIONS[name] for name in rule["via"]]
    return renamed


def renamed_ontology_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """The parsed ontology file ``data`` with every free name renamed (module docstring)."""

    data = json.loads(json.dumps(data))  # a deep, plain copy
    data["name"] = NAME
    corpus = data.get("corpus") or {}
    corpus["framing"] = rename_text(corpus.get("framing", ""))
    corpus["exclude"] = [rename_text(item) for item in corpus.get("exclude", [])]
    data["vocabularies"] = {
        VOCABULARIES[name]: [VALUES[value] for value in values]
        if isinstance(values, list)
        else {VALUES[value]: text for value, text in values.items()}
        for name, values in (data.get("vocabularies") or {}).items()
    }
    groups = {}
    for key, declared_group in (data.get("node_groups") or {}).items():
        group = _texts(dict(declared_group))
        group["attributes"] = _attributes(group.get("attributes") or {})
        groups[GROUPS[key]] = group
    data["node_groups"] = groups
    data["node_types"] = _rename_node_types(data["node_types"])
    data["edge_types"] = _rename_edge_types(data["edge_types"])
    spine = data["spine"]
    spine["sources"] = [_type_ref(name) for name in spine["sources"]]
    spine["roots"] = [_type_ref(name) for name in spine["roots"]]
    spine["paths"] = [
        {
            key: [RELATIONS[name] for name in value] if key in ("via", "sequence") else value
            for key, value in path.items()
        }
        for path in spine["paths"]
    ]
    data["inference"]["rules"] = [_rename_rule(rule) for rule in data["inference"]["rules"]]
    return data


def shipped_ontology_dir() -> Any:
    return files("graph") / "core" / "ontology"


def write_renamed_ontology(directory: Path) -> Path:
    """Write the renamed copy and its renamed judge prompts into ``directory``; return the file."""

    source = shipped_ontology_dir()
    data = yaml.safe_load((source / "spec-ontology.yaml").read_text(encoding="utf-8"))
    path = Path(directory) / "renamed-ontology.yaml"
    path.write_text(yaml.safe_dump(renamed_ontology_data(data), sort_keys=False, allow_unicode=True), encoding="utf-8")
    prompts = Path(directory) / "judge_prompts"
    prompts.mkdir(exist_ok=True)
    for original, renamed in PROMPTS.items():
        text = (source / "judge_prompts" / f"{original}.md").read_text(encoding="utf-8")
        (prompts / f"{renamed}.md").write_text(rename_text(text), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def renamed_ontology(tmp_path_factory: pytest.TempPathFactory) -> Ontology:
    return load_ontology(write_renamed_ontology(tmp_path_factory.mktemp("renamed-ontology")), submodule_files=[])


# The rename is total and the copy is valid (offline)


def test_the_rename_tables_cover_every_free_name_of_the_shipped_ontology() -> None:
    shipped = shipped_ontology()
    data = yaml.safe_load((shipped_ontology_dir() / "spec-ontology.yaml").read_text(encoding="utf-8"))
    assert set(data["node_types"]) == set(TYPE_KEYS)
    assert set(data["node_groups"]) == set(GROUPS)
    assert set(shipped.labels) == set(LABELS)
    assert set(shipped.relations) == set(RELATIONS)
    assert {t.id_prefix for t in shipped.node_types.values()} == set(PREFIXES)
    attributes = {name for t in shipped.node_types.values() for name in t.attributes}
    assert attributes - FIXED_ATTRIBUTES == set(ATTRIBUTES)
    assert set(data["vocabularies"]) == set(VOCABULARIES)
    values = {value for t in shipped.node_types.values() for a in t.attributes.values() for value in a.values}
    assert values == set(VALUES)
    assert {rule.id for rule in shipped.inference.rules} == set(RULES)
    assert {rule.prompt for rule in shipped.inference.rules} == set(PROMPTS)
    # Renamed names never reuse an original one (a mapping that kept a name would prove nothing).
    for table in (TYPE_KEYS, GROUPS, LABELS, RELATIONS, PREFIXES, ATTRIBUTES, VOCABULARIES, VALUES, RULES, PROMPTS):
        assert not set(table.values()) & set(table), table
        assert len(set(table.values())) == len(table), table


def test_the_renamed_copy_loads_with_the_same_structure(renamed_ontology: Ontology) -> None:
    shipped = shipped_ontology()
    assert renamed_ontology.name == NAME and renamed_ontology.version == shipped.version
    assert set(renamed_ontology.labels) == set(LABELS.values())
    assert set(renamed_ontology.relations) == set(RELATIONS.values())
    for label, node_type in shipped._by_label.items():
        other = renamed_ontology.node_type(LABELS[label])
        assert other.id_prefix == PREFIXES[node_type.id_prefix]
        assert other.id_families == node_type.id_families
        assert (other.role, other.origin, other.tier, other.identity.key) == (
            node_type.role,
            node_type.origin,
            node_type.tier,
            node_type.identity.key,
        )
        assert other.identity.scope == SCOPES[node_type.identity.scope]
        assert set(other.attributes) == {ATTRIBUTES.get(name, name) for name in node_type.attributes}
    for name, relation in shipped.relations.items():
        other = renamed_ontology.relations[RELATIONS[name]]
        assert sorted(
            (LABELS[p.source], LABELS[p.target], p.default, p.cue_required, p.exclusive) for p in relation.pairs
        ) == sorted((p.source, p.target, p.default, p.cue_required, p.exclusive) for p in other.pairs)
        assert (other.symmetric, other.id_references, len(other.derive)) == (
            relation.symmetric,
            relation.id_references,
            len(relation.derive),
        )
    assert renamed_ontology.spine.sources == {LABELS[label] for label in shipped.spine.sources}
    assert renamed_ontology.spine.roots == {LABELS[label] for label in shipped.spine.roots}
    assert [(rule.id, rule.template, rule.prompt, rule.writes) for rule in renamed_ontology.inference.rules] == [
        (RULES[rule.id], rule.template, PROMPTS[rule.prompt], RELATIONS[rule.writes])
        for rule in shipped.inference.rules
    ]
    assert set(renamed_ontology.judge_prompts) == set(PROMPTS.values())
    # Nothing of the shipped names is left in the renamed file but the kept ones.
    text = yaml.safe_dump(
        renamed_ontology_data(yaml.safe_load((shipped_ontology_dir() / "spec-ontology.yaml").read_text()))
    )
    assert not _TEXT_NAMES.search(text)
    assert not [prefix for prefix in PREFIXES if f"{prefix}{{SLUG}}" in text]


# Mapping the shipped graph onto the renamed one


def map_ref(value: str) -> str:
    """A ``|``-joined key (identity key, edge key, edge ref) or a ``spec_id``, mapped segment by segment:
    scopes and relations by name, ``spec_id``s by prefix (longest first)."""

    def segment(part: str) -> str:
        if part in SCOPES:
            return SCOPES[part]
        if part in RELATIONS:
            return RELATIONS[part]
        for prefix in sorted(PREFIXES, key=len, reverse=True):
            if part.startswith(prefix):
                return PREFIXES[prefix] + part.removeprefix(prefix)
        return part

    return "|".join(segment(part) for part in value.split("|"))


def map_refs(values: Any) -> list[str]:
    return sorted(map_ref(value) for value in values or ())


ENUM_ATTRIBUTES = frozenset(
    name for t in shipped_ontology().node_types.values() for name, a in t.attributes.items() if a.kind == "enum"
)
# Properties that differ between the two projects by construction (ids, times, vectors) or that are
# derived from texts whose length differs (token counts).
VOLATILE_NODE = frozenset(
    {"uuid", "group_id", "created_at", "updated_at", "created_build_id", "last_build_id", "embedding", "name_embedding"}
)
VOLATILE_EDGE = frozenset(
    {"uuid", "group_id", "created_at", "updated_at", "created_build_id", "last_build_id", "inferred_at", "build_id"}
)


def map_node(node: Mapping[str, Any]) -> dict[str, Any]:
    """A shipped-project node projection in renamed names."""

    mapped: dict[str, Any] = {}
    for name, original in node["props"].items():
        value = original
        if name == "spec_id":
            value = map_ref(original)
        elif name in ("spec_id_aliases", "identity_aliases"):
            value = map_refs(original)
        elif name == "identity_key":
            value = map_ref(original)
        elif name in ENUM_ATTRIBUTES and isinstance(original, str):
            value = VALUES.get(original, original)
        mapped[ATTRIBUTES.get(name, name)] = value
    return {**node, "label": LABELS[node["label"]], "props": mapped}


def map_edge(edge: Mapping[str, Any]) -> dict[str, Any]:
    props = dict(edge["props"])
    if "edge_key" in props:
        props["edge_key"] = map_ref(props["edge_key"])
    if "inference_rule" in props:
        props["inference_rule"] = RULES[props["inference_rule"]]
    return {
        **edge,
        "relation": RELATIONS[edge["relation"]],
        "ends": tuple(map_ref(end) for end in edge["ends"]),
        "props": props,
    }


@dataclass
class Graph:
    """One project's graph after a build, canonical: nodes by ``spec_id``, edges sorted."""

    nodes: dict[str, dict[str, Any]]
    edges: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    fragments: list[dict[str, Any]]
    registry: list[tuple[str, str, str]]

    def nodes_by_label(self) -> Counter[str]:
        return Counter(node["label"] for node in self.nodes.values())

    def edges_by_relation(self) -> Counter[str]:
        return Counter(edge["relation"] for edge in self.edges)


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


async def read_graph(builds: ShopBuilds) -> Graph:
    project, pid = builds.project, builds.project_id
    ontology = builds.ontology
    assert ontology is not None
    nodes = {}
    for row in await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p "
        "RETURN [l IN labels(n) WHERE l <> 'SpecNode'] AS labels, properties(n) AS props, "
        "COLLECT { MATCH (n)-[:REFERENCED_BY]->(f:Fragment) RETURN f.fragment_id ORDER BY f.fragment_id } AS "
        "referenced, "
        "COLLECT { MATCH (n)-[:MENTIONED_IN]->(f:Fragment) RETURN f.fragment_id ORDER BY f.fragment_id } AS mentioned",
        p=pid,
    ):
        assert len(row["labels"]) == 1, row["labels"]
        props = {key: value for key, value in row["props"].items() if key not in VOLATILE_NODE}
        if "statements" in props:  # JSON statements: their times differ
            props["statements"] = [
                {key: value for key, value in item.items() if key != "at"} for item in json.loads(props["statements"])
            ]
        nodes[props["spec_id"]] = _plain(
            {"label": row["labels"][0], "props": props, "referenced": row["referenced"], "mentioned": row["mentioned"]}
        )
    edges = []
    for row in await project.run(
        "MATCH (s:SpecNode)-[r]->(t:SpecNode) WHERE r.group_id = $p "
        "RETURN type(r) AS relation, s.spec_id AS source, t.spec_id AS target, properties(r) AS props",
        p=pid,
    ):
        symmetric = ontology.relations[row["relation"]].symmetric
        ends = (row["source"], row["target"])
        props = {key: value for key, value in row["props"].items() if key not in VOLATILE_EDGE}
        # A symmetric edge is stored once in canonical identity-key order, which a rename of the
        # scopes may change: its ends compare as a set.
        edges.append(
            _plain(
                {
                    "relation": row["relation"],
                    "ends": sorted(ends) if symmetric else ends,
                    "props": props,
                    "symmetric": symmetric,
                }
            )
        )
    documents = [
        _plain({key: value for key, value in row["props"].items() if key not in VOLATILE_NODE})
        for row in await project.run(
            "MATCH (d:Document) WHERE d.group_id = $p RETURN properties(d) AS props ORDER BY d.document_id", p=pid
        )
    ]
    fragments = [
        _plain({key: value for key, value in row["props"].items() if key not in VOLATILE_NODE})
        for row in await project.run(
            "MATCH (f:Fragment) WHERE f.group_id = $p RETURN properties(f) AS props ORDER BY f.fragment_id", p=pid
        )
    ]
    registry = sorted(
        (row.spec_id, row.identity_key, row.state) for row in (await builds.stores.registry.taken(pid)).values()
    )
    return Graph(nodes, sorted(edges, key=_edge_order), documents, fragments, registry)


def _edge_order(edge: Mapping[str, Any]) -> str:
    return json.dumps([edge["relation"], edge["ends"], edge["props"].get("edge_key")], sort_keys=True)


def mapped_graph(graph: Graph) -> Graph:
    """The shipped project's graph in renamed names."""

    nodes = {map_ref(spec_id): map_node(node) for spec_id, node in graph.nodes.items()}
    edges = []
    for edge in graph.edges:
        mapped = map_edge(edge)
        if edge["symmetric"]:
            mapped["ends"] = tuple(sorted(mapped["ends"]))
            mapped["props"].pop("edge_key", None)
        edges.append(_plain(mapped))
    registry = sorted((map_ref(spec_id), map_ref(key), state) for spec_id, key, state in graph.registry)
    return Graph(nodes, sorted(edges, key=_edge_order), graph.documents, graph.fragments, registry)


def without_symmetric_keys(graph: Graph) -> Graph:
    edges = []
    for original in graph.edges:
        edge = json.loads(json.dumps(original))
        if edge["symmetric"]:
            edge["ends"] = sorted(edge["ends"])
            edge["props"].pop("edge_key", None)
        edges.append(_plain(edge))
    return Graph(graph.nodes, sorted(edges, key=_edge_order), graph.documents, graph.fragments, graph.registry)


def map_dropped(key: str) -> str:
    """A dropped-edges key (``<RELATION>`` or ``<RELATION>:<reason>``) in renamed names."""

    relation, separator, reason = key.partition(":")
    return f"{RELATIONS.get(relation, relation)}{separator}{reason}"


def map_result(result: BuildResult) -> dict[str, Any]:
    """The comparable part of a shipped build result, in renamed names."""

    changes = result.spec_changes
    verification = result.verification
    assert verification is not None
    metrics = result.metrics
    return {
        "mode": result.mode,
        "status": result.status,
        "settled": result.settled,
        "failed_step": result.failed_step,
        "error_code": result.error_code,
        "skipped_fragments": result.skipped_fragments,
        "spec_changes": {name: map_refs(getattr(changes, name)) for name in type(changes).model_fields},
        "verification": {
            **verification.model_dump(mode="json", exclude={"metrics"}),
            "metrics": {
                **verification.metrics.model_dump(mode="json"),
                "dropped_edges": {map_dropped(k): v for k, v in verification.metrics.dropped_edges.items()},
            },
        },
        "metrics": {
            "fragments_by_status": metrics.fragments_by_status,
            "units": (metrics.units_extracted, metrics.units_reused),
            "nodes_by_label": {LABELS[k]: v for k, v in metrics.nodes_by_label.items()},
            "edges_by_relation": {RELATIONS[k]: v for k, v in metrics.edges_by_relation.items()},
            "dropped_edges": {map_dropped(k): v for k, v in metrics.dropped_edges.items()},
            "exclusive_conflicts": metrics.exclusive_conflicts,
            "spine_unreachable": metrics.spine_unreachable,
            "attribution_fallbacks": metrics.attribution_fallbacks,
            "attribute_batches_failed": metrics.attribute_batches_failed,
            "llm_requests": {step: counts.requests for step, counts in metrics.llm.items()},
            "steps": sorted(metrics.duration_s),
        },
    }


def renamed_result(result: BuildResult) -> dict[str, Any]:
    """The comparable part of a renamed build result (``map_result`` without the mapping)."""

    changes = result.spec_changes
    verification = result.verification
    assert verification is not None
    metrics = result.metrics
    return {
        "mode": result.mode,
        "status": result.status,
        "settled": result.settled,
        "failed_step": result.failed_step,
        "error_code": result.error_code,
        "skipped_fragments": result.skipped_fragments,
        "spec_changes": {name: sorted(getattr(changes, name)) for name in type(changes).model_fields},
        "verification": {
            **verification.model_dump(mode="json", exclude={"metrics"}),
            "metrics": verification.metrics.model_dump(mode="json"),
        },
        "metrics": {
            "fragments_by_status": metrics.fragments_by_status,
            "units": (metrics.units_extracted, metrics.units_reused),
            "nodes_by_label": metrics.nodes_by_label,
            "edges_by_relation": metrics.edges_by_relation,
            "dropped_edges": metrics.dropped_edges,
            "exclusive_conflicts": metrics.exclusive_conflicts,
            "spine_unreachable": metrics.spine_unreachable,
            "attribution_fallbacks": metrics.attribution_fallbacks,
            "attribute_batches_failed": metrics.attribute_batches_failed,
            "llm_requests": {step: counts.requests for step, counts in metrics.llm.items()},
            "steps": sorted(metrics.duration_s),
        },
    }


def map_report(report: InferenceReport, *, mapped: bool) -> dict[str, Any]:
    ref = map_ref if mapped else (lambda value: value)
    rule = (lambda value: RULES[value]) if mapped else (lambda value: value)
    return {
        "enabled": report.enabled,
        "rules": [{**item.model_dump(mode="json"), "rule": rule(item.rule)} for item in report.rules],
        "integration_outliers": sorted(ref(value) for value in report.integration_outliers),
        "orphans_without_candidate": sorted(ref(value) for value in report.orphans_without_candidate),
        "spine_unreachable_before": report.spine_unreachable_before,
        "inferred_added": sorted(ref(value) for value in report.inferred_added),
        "inferred_removed": sorted(ref(value) for value in report.inferred_removed),
        "error_code": report.error_code,
    }


def calls(gateway: ScriptedGateway, *, mapped: bool) -> Counter[tuple[str, str]]:
    """Model calls by (prompt, call class), embeddings included (one entry per embedded text)."""

    found: Counter[tuple[str, str]] = Counter()
    for call in gateway.calls:
        prompt = PROMPTS.get(call.prompt, call.prompt) if mapped else call.prompt
        found[(prompt, call.call_class)] += len(call.texts) if call.call_class == "embedding" else 1
    return found


def judged(gateway: ScriptedGateway, prompts: Mapping[str, str] | None = None) -> list[tuple[str, Any]]:
    judge_prompts = set(PROMPTS) | set(PROMPTS.values())
    return sorted(
        ((prompts or {}).get(call.prompt, call.prompt), call.key)
        for call in gateway.calls
        if call.prompt in judge_prompts
    )


@dataclass
class Step:
    snapshot: int
    result: BuildResult
    gateway: ScriptedGateway
    graph: Graph
    report: InferenceReport


@dataclass
class Chains:
    shipped: list[Step]
    renamed: list[Step]


async def run_chain(builds: ShopBuilds) -> list[Step]:
    steps = []
    for snapshot in (1, 2, 3):
        result, gateway = await builds.build(snapshot)
        assert (result.failed_step, result.error_code) == (None, None), (
            snapshot,
            result.failed_step,
            result.error_code,
        )
        payload = await builds.stores.builds.get_artifact(builds.project_id, result.build_id, "infer")
        assert payload is not None
        steps.append(Step(snapshot, result, gateway, await read_graph(builds), InferenceReport.model_validate(payload)))
    return steps


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def chains(renamed_ontology: Ontology, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Chains]:
    """Both chains, built once for the module (12 builds) into two projects deleted afterwards. The
    tests reading them are synchronous: the driver lives on this fixture's module loop."""

    root = tmp_path_factory.mktemp("renamed-chains")
    gateways: list[ScriptedGateway] = []

    def factory(config: Any = None, **options: Any) -> ScriptedGateway:
        gateway = ScriptedGateway(config, **options)
        gateways.append(gateway)
        return gateway

    async with open_neo4j_project() as project:
        shipped = ShopBuilds(project, factory, root / "shipped")
        renamed = ShopBuilds(
            project,
            factory,
            root / "renamed",
            project_id=project.new_project(),
            ontology=renamed_ontology,
            rename=ANSWER_RENAME,
        )
        built = Chains(await run_chain(shipped), await run_chain(renamed))
        assert_all_scripted(*gateways)
        yield built


def pairs(chains: Chains) -> list[tuple[Step, Step]]:
    return list(zip(chains.shipped, chains.renamed, strict=True))


def differences(expected: Any, found: Any, path: str = "") -> dict[str, tuple[Any, Any]]:
    """The leaf paths where two JSON-like values differ, with both values."""

    expected, found = _plain(expected), _plain(found)
    if isinstance(expected, dict) and isinstance(found, dict):
        result: dict[str, tuple[Any, Any]] = {}
        for key in sorted(set(expected) | set(found)):
            result.update(differences(expected.get(key), found.get(key), f"{path}.{key}"))
        return result
    return {} if expected == found else {path or ".": (expected, found)}


def node_differences(
    expected: Mapping[str, Mapping[str, Any]], found: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, tuple[Any, Any]]]:
    """Per ``spec_id``, the fields (``props.<name>`` for properties) whose mapped and found values differ."""

    differences: dict[str, dict[str, tuple[Any, Any]]] = {}
    for spec_id in sorted(set(expected) | set(found)):
        want, got = expected.get(spec_id, {}), found.get(spec_id, {})
        fields = {
            f"props.{name}": (want.get("props", {}).get(name), got.get("props", {}).get(name))
            for name in set(want.get("props", {})) | set(got.get("props", {}))
        }
        fields.update({name: (want.get(name), got.get(name)) for name in ("label", "referenced", "mentioned")})
        changed = {name: pair for name, pair in fields.items() if pair[0] != pair[1]}
        if changed:
            differences[spec_id] = changed
    return differences


@live
def test_the_graphs_map_onto_each_other_after_every_build(chains: Chains) -> None:
    for shipped, renamed in pairs(chains):
        expected, found = mapped_graph(shipped.graph), without_symmetric_keys(renamed.graph)
        assert renamed.graph.nodes_by_label() == Counter(
            {LABELS[label]: n for label, n in shipped.graph.nodes_by_label().items()}
        ), shipped.snapshot
        assert renamed.graph.edges_by_relation() == Counter(
            {RELATIONS[relation]: n for relation, n in shipped.graph.edges_by_relation().items()}
        ), shipped.snapshot
        assert sorted(found.nodes) == sorted(expected.nodes), shipped.snapshot
        assert node_differences(expected.nodes, found.nodes) == {}, shipped.snapshot
        assert found.edges == expected.edges, shipped.snapshot
        assert found.registry == expected.registry, shipped.snapshot
        assert (found.documents, found.fragments) == (expected.documents, expected.fragments), shipped.snapshot
        # The renamed graph holds renamed names only.
        assert not {node["label"] for node in found.nodes.values()} & set(LABELS)
        assert not [spec_id for spec_id in found.nodes if any(spec_id.startswith(p) for p in PREFIXES)]


@live
def test_build_results_inference_and_verification_map_onto_each_other(chains: Chains) -> None:
    for shipped, renamed in pairs(chains):
        assert differences(map_result(shipped.result), renamed_result(renamed.result)) == {}, shipped.snapshot
        assert differences(map_report(shipped.report, mapped=True), map_report(renamed.report, mapped=False)) == {}, (
            shipped.snapshot
        )
    first = chains.shipped[0]
    # The chain really exercises inference and verification (else the comparison proves little).
    assert first.result.verification is not None and first.result.verification.ok
    assert sum(rule.accepted for rule in first.report.rules) > 0 and first.report.inferred_added
    assert any(edge["props"].get("basis") == "inferred" for edge in first.graph.edges)


@live
def test_the_builds_make_the_same_model_calls(chains: Chains) -> None:
    for shipped, renamed in pairs(chains):
        assert calls(renamed.gateway, mapped=False) == calls(shipped.gateway, mapped=True), shipped.snapshot
        assert judged(renamed.gateway) == judged(shipped.gateway, PROMPTS), shipped.snapshot
        assert [
            call.key
            for call in renamed.gateway.calls
            if call.call_class != "embedding" and call.prompt not in PROMPTS.values()
        ] == [
            call.key for call in shipped.gateway.calls if call.call_class != "embedding" and call.prompt not in PROMPTS
        ], shipped.snapshot


# Distinctive original names a renamed build's prompt must not show. Skipped as too generic to search
# for in English prompt text: the one-word type keys (module, feature, capability, constraint), the
# group key (requirement), the attribute names module, external and type, and every enum value
# (stated, implied, yes, no, frontend, backend, datastore, deployment, other).
GENERIC_NAMES = frozenset({"module", "feature", "capability", "constraint", "requirement", "external", "type", *VALUES})
DISTINCTIVE_NAMES = tuple(
    name
    for name in (
        *LABELS,
        *RELATIONS,
        *RULES,
        *TYPE_KEYS,
        *ATTRIBUTES,
        *VOCABULARIES,
        *PROMPTS,
        *(prompt.partition(".")[0] for prompt in PROMPTS),
        shipped_ontology().name,
    )
    if name not in GENERIC_NAMES
)
ORIGINAL_MARKERS = re.compile(
    r"(?<![\w-])("
    + "|".join(re.escape(name) for name in sorted(set(DISTINCTIVE_NAMES), key=len, reverse=True))
    + r")(?![\w-])"
    + "|"
    + "|".join(re.escape(prefix) for prefix in sorted(PREFIXES, key=len, reverse=True))
)


@live
def test_no_shipped_ontology_name_reaches_a_prompt_of_the_renamed_builds(chains: Chains) -> None:
    """ASR2 in the prompts: what the library writes around the documents comes from the ontology. Document
    text sits inside untrusted blocks (the shop corpus names "Feature" and "Module" itself)."""

    def names_outside_documents(steps: list[Step], pattern: re.Pattern[str]) -> dict[tuple[str, str], list[str]]:
        found: dict[tuple[str, str], set[str]] = {}
        for step in steps:
            for call in step.gateway.calls:
                for message in call.messages:
                    content = message.get("content")
                    if isinstance(content, str):
                        names = {match.group(0) for match in pattern.finditer(outside_untrusted(content))}
                        if names:
                            found.setdefault((call.prompt, str(message.get("role"))), set()).update(names)
        return {key: sorted(names) for key, names in found.items()}

    assert names_outside_documents(chains.renamed, ORIGINAL_MARKERS) == {}
    # The check can see names: the shipped builds' prompts show the shipped names, the renamed ones theirs.
    shown = {name for names in names_outside_documents(chains.shipped, ORIGINAL_MARKERS).values() for name in names}
    assert {"FunctionalRequirement", "PART_OF", "acceptance_criteria", "source_status"} <= shown, sorted(shown)
    renamed_markers = re.compile(
        r"\b(" + "|".join(sorted((*LABELS.values(), *RELATIONS.values()), key=len, reverse=True)) + r")\b"
    )
    assert names_outside_documents(chains.renamed, renamed_markers)
