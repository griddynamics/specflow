"""Load, merge and validate ontology files (docs/graph/ontology.md "Loading and validation").

``load_ontology`` reads the build file (the shipped ``spec-ontology.yaml`` or a given path) and the
ontology files of the installed submodules, merges them and runs checks 1-10 over the union. Every
problem found is collected; any problem raises one ``OntologyError`` that lists them all. Submodule
files are found as package data (``graph/<sub>/ontology.yaml``) without importing any submodule.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ValidationError

from graph.core.errors import OntologyError
from graph.core.ontology.compiled import (
    BUILD_OWNER,
    BUILT_IN_NAMES,
    EVIDENCE_LABELS,
    EXTRACTED_ORIGIN,
    HUB_ROLES,
    AttributeSpec,
    DeriveRule,
    IdentityRule,
    InferenceRule,
    InferenceSpec,
    NodeGroup,
    NodeType,
    Ontology,
    Pair,
    Relation,
    Spine,
    SpinePath,
)
from graph.core.ontology.model import (
    OPTIONAL_TEMPLATE_PARAMETERS,
    OWNER_ORIGINS,
    TEMPLATE_PARAMETERS,
    AttributeDef,
    CorpusDef,
    EdgeTypeDef,
    FileHeaderDef,
    IdentityDef,
    InferenceDef,
    InferenceRuleDef,
    NodeGroupDef,
    NodeTypeDef,
    SpineDef,
)
from logger.logger import INFO, get_logger, log_event

PACKAGE: Final = "graph"
BUILD_FILE_PARTS: Final = ("core", "ontology", "spec-ontology.yaml")
SUBMODULE_FILE: Final = "ontology.yaml"
JUDGE_PROMPT_DIR: Final = "judge_prompts"

NAME_PATTERN: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
"""Labels and relation names: safe to render into Cypher."""
DERIVE_PARTS: Final = 2
"""Parts of a derive entry ``<type or group>.<ref attribute>``."""
KEY_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
"""Type, group, vocabulary and attribute names (snake_case)."""
ID_FORMAT_PATTERN: Final = re.compile(r"^((?:[A-Z][A-Z0-9_]*::)+)\{SLUG\}$")
ID_FAMILY_PATTERN: Final = re.compile(r"^[A-Z]{1,16}$")
PROMPT_REF_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}\.[0-9]{1,4}$")
BUILT_IN_ID_PREFIXES: Final = ("DOC::", "FRG::")
RESERVED_ATTRIBUTES: Final = frozenset(
    {
        "uuid",
        "spec_id",
        "spec_id_aliases",
        "name",
        "identity_key",
        "identity_aliases",
        "statements",
        "statement_text",
        "label_disagreement",
        "embedding",
        "name_embedding",
        "embedding_model",
        "created_at",
        "updated_at",
        "created_build_id",
        "last_build_id",
        "group_id",
    }
) | frozenset(name for name in dir(BaseModel) if not name.startswith("__"))
"""Node properties the data model owns, and names a pydantic model cannot take as fields."""

_LOGGER = get_logger("graph.core.ontology")


class _Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, display: str, where: str, message: str) -> None:
        self.items.append(f"{display}: {where}: {message}" if where else f"{display}: {message}")


@dataclass(frozen=True)
class _Source:
    directory: Traversable
    filename: str
    display: str

    def read(self) -> str:
        return (self.directory / self.filename).read_text(encoding="utf-8")

    def judge_prompt(self, ref: str) -> str | None:
        prompt = self.directory / JUDGE_PROMPT_DIR / f"{ref}.md"
        return prompt.read_text(encoding="utf-8") if prompt.is_file() else None


@dataclass
class _File:
    source: _Source
    header: FileHeaderDef
    main: bool
    corpus: CorpusDef | None = None
    vocabularies: dict[str, tuple[tuple[str, ...], dict[str, str]]] = field(default_factory=dict)
    groups: dict[str, NodeGroupDef] = field(default_factory=dict)
    types: dict[str, NodeTypeDef] = field(default_factory=dict)
    relations: dict[str, EdgeTypeDef] = field(default_factory=dict)
    spine: SpineDef | None = None
    inference: InferenceDef | None = None
    broken: set[str] = field(default_factory=set)

    @property
    def owner(self) -> str:
        return self.header.owner

    @property
    def display(self) -> str:
        return self.source.display


def load_ontology(path: Path | None = None, *, submodule_files: Sequence[Path] | None = None) -> Ontology:
    """Load the build file plus submodule files, merge and validate them; raise ``OntologyError``.

    ``path`` None loads the shipped ``spec-ontology.yaml``. ``submodule_files`` None discovers
    ``graph/<sub>/ontology.yaml`` package data; a sequence (tests) replaces discovery.
    """

    main = _package_build_source() if path is None else _path_source(Path(path))
    subs = (
        _discover_submodule_sources()
        if submodule_files is None
        else [_path_source(Path(item)) for item in submodule_files]
    )
    problems = _Problems()
    parsed = [_parse_file(main, problems, main=True)]
    parsed += [_parse_file(source, problems, main=False) for source in subs]
    ontology = _Compiler([item for item in parsed if item is not None], problems).compile()
    if problems.items or ontology is None:
        raise OntologyError(problems.items or [f"{main.display}: no valid build file"])
    log_event(
        _LOGGER,
        INFO,
        "graph.ontology.loaded",
        ontology_name=ontology.name,
        ontology_version=ontology.version,
        node_types=len(ontology.node_types),
        relations=len(ontology.relations),
        files=len(parsed),
    )
    return ontology


# Sources


def _package_build_source() -> _Source:
    directory = files(PACKAGE)
    for part in BUILD_FILE_PARTS[:-1]:
        directory = directory / part
    return _Source(directory, BUILD_FILE_PARTS[-1], "/".join((PACKAGE, *BUILD_FILE_PARTS)))


def _path_source(path: Path) -> _Source:
    return _Source(path.parent, path.name, str(path))


def _discover_submodule_sources() -> list[_Source]:
    """``graph/<sub>/ontology.yaml`` of every installed submodule, found without importing it."""

    sources: list[_Source] = []
    for child in sorted(files(PACKAGE).iterdir(), key=lambda item: item.name):
        if child.name.startswith(("_", ".")) or not child.is_dir():
            continue
        if (child / SUBMODULE_FILE).is_file():
            sources.append(_Source(child, SUBMODULE_FILE, f"{PACKAGE}/{child.name}/{SUBMODULE_FILE}"))
    return sources


# Parsing (check 1)


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects a mapping key written twice."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if isinstance(key, list | dict):
            continue
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    mapping: dict[Any, Any] = loader.construct_mapping(node, deep=True)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _load_unique(text: str) -> Any:
    """The single YAML document in ``text``, read with the duplicate-key-rejecting safe loader."""

    loader = _UniqueKeyLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _read_mapping(source: _Source, problems: _Problems) -> dict[str, Any] | None:
    """The file's top-level mapping; None (with a problem) when it cannot be read, is not YAML or is no mapping."""

    try:
        raw = _load_unique(source.read())
    except (OSError, UnicodeDecodeError) as error:
        problems.add(source.display, "", f"cannot read the file ({type(error).__name__})")
        return None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = f" at line {mark.line + 1}" if mark is not None else ""
        problems.add(source.display, "", f"invalid YAML{line}: {getattr(error, 'problem', None) or error}")
        return None
    if not isinstance(raw, dict):
        problems.add(source.display, "", "the file must be a mapping")
        return None
    return raw


def _parse_entries(parsed: _File, header: FileHeaderDef, problems: _Problems) -> None:
    """Validate the vocabularies, node groups, node types and edge types of a file into ``parsed``."""

    display = parsed.source.display
    for name, value in header.vocabularies.items():
        vocabulary = _parse_vocabulary(value)
        if vocabulary is None:
            problems.add(display, f"vocabularies.{name}", "must be a non-empty list or mapping of strings")
            parsed.broken.add(name)
        else:
            parsed.vocabularies[name] = vocabulary
    sections: list[tuple[str, type[BaseModel], dict[str, Any], dict[str, Any]]] = [
        ("node_groups", NodeGroupDef, header.node_groups, parsed.groups),
        ("node_types", NodeTypeDef, header.node_types, parsed.types),
        ("edge_types", EdgeTypeDef, header.edge_types, parsed.relations),
    ]
    for section, model, entries, target in sections:
        for name, value in entries.items():
            item = _validate(model, value, display, f"{section}.{name}", problems)
            if item is None:
                parsed.broken.add(name)
            else:
                target[name] = item


def _check_owner(display: str, header: FileHeaderDef, problems: _Problems, *, main: bool) -> None:
    """The build file must have the build owner and a submodule file must not."""

    if main and header.owner != BUILD_OWNER:
        problems.add(display, "owner", f"the build file must have owner '{BUILD_OWNER}'")
    if not main and header.owner == BUILD_OWNER:
        problems.add(display, "owner", f"a submodule file may not have owner '{BUILD_OWNER}'")


def _parse_file(source: _Source, problems: _Problems, *, main: bool) -> _File | None:
    raw = _read_mapping(source, problems)
    if raw is None:
        return None
    header = _validate(FileHeaderDef, raw, source.display, "", problems)
    if header is None:
        return None
    parsed = _File(source=source, header=header, main=main)
    display = source.display
    _check_owner(display, header, problems, main=main)
    if header.corpus is not None:
        parsed.corpus = _validate(CorpusDef, header.corpus, display, "corpus", problems)
    _parse_entries(parsed, header, problems)
    if header.spine is not None:
        parsed.spine = _validate(SpineDef, header.spine, display, "spine", problems)
    if header.inference is not None:
        parsed.inference = _validate(InferenceDef, header.inference, display, "inference", problems)
    if not main:
        for section in ("corpus", "spine", "inference"):
            if getattr(header, section) is not None:
                problems.add(display, section, "only the build file may declare this section")
    return parsed


def _parse_vocabulary(value: Any) -> tuple[tuple[str, ...], dict[str, str]] | None:
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        if len(set(value)) != len(value):
            return None
        return tuple(value), {}
    if (
        isinstance(value, dict)
        and value
        and all(isinstance(key, str) and isinstance(text, str) for key, text in value.items())
    ):
        return tuple(value), dict(value)
    return None


def _validate[M: BaseModel](model: type[M], data: Any, display: str, where: str, problems: _Problems) -> M | None:
    """Validate one entry; report every error; unknown keys alone do not discard the entry."""

    try:
        return model.model_validate(data)
    except ValidationError as error:
        errors = error.errors()
    for item in errors:
        location = ".".join(str(part) for part in item["loc"])
        path = ".".join(part for part in (where, location) if part)
        if item["type"] == "extra_forbidden":
            message = "unknown key"
        elif item["type"] == "missing":
            message = "required field missing"
        else:
            message = item["msg"]
        problems.add(display, path, message)
    if any(item["type"] != "extra_forbidden" for item in errors):
        return None
    stripped = copy.deepcopy(data)
    for item in errors:
        if not _remove(stripped, item["loc"]):
            return None
    try:
        return model.model_validate(stripped)
    except ValidationError:
        return None


def _remove(data: Any, location: tuple[Any, ...]) -> bool:
    container = data
    for part in location[:-1]:
        try:
            container = container[part]
        except (KeyError, IndexError, TypeError):
            return False
    if isinstance(container, dict) and location and location[-1] in container:
        del container[location[-1]]
        return True
    return False


# Merging and checks 2-10

_ATTRIBUTE_FIELDS: Final = {
    "text": {"type", "description", "max_chars"},
    "enum": {"type", "description", "values", "aliases"},
    "ref": {"type", "description", "target", "store"},
}
"""The fields each attribute type may set."""
_REQUIRED_ATTRIBUTE_FIELD: Final = {"text": "max_chars", "enum": "values", "ref": "target"}
"""The field each attribute type must set."""


class _Compiler:
    """Merges parsed files and turns them into an ``Ontology``, reporting every problem."""

    def __init__(self, parsed: list[_File], problems: _Problems) -> None:
        self.parsed = parsed
        self.problems = problems
        self.vocabularies: dict[str, tuple[tuple[str, ...], dict[str, str], _File]] = {}
        self.groups: dict[str, tuple[NodeGroupDef, _File]] = {}
        self.types: dict[str, tuple[NodeTypeDef, _File]] = {}
        self.relation_defs: dict[str, tuple[EdgeTypeDef, _File]] = {}
        self.broken: set[str] = set()
        self.key_labels: dict[str, tuple[str, ...]] = {}
        self.group_attributes: dict[str, dict[str, AttributeSpec]] = {}
        self.node_types: dict[str, NodeType] = {}
        self.relations: dict[str, Relation] = {}
        self.legal: set[tuple[str, str, str]] = set()

    def compile(self) -> Ontology | None:
        main = next((item for item in self.parsed if item.main), None)
        self._merge()
        self._check_names()
        self._resolve_keys()
        self._compile_groups()
        self._compile_types()
        self._check_id_prefixes()
        self._compile_relations()
        self._compile_derive()
        spine = self._compile_spine(main)
        inference, prompts = self._compile_inference(main)
        if main is None or self.problems.items:
            return None
        corpus = main.corpus or CorpusDef()
        return Ontology(
            name=main.header.name,
            version=main.header.version,
            framing=corpus.framing.strip(),
            exclude=tuple(corpus.exclude),
            node_types=MappingProxyType(dict(sorted(self.node_types.items()))),
            node_groups=MappingProxyType(self._node_groups()),
            relations=MappingProxyType(dict(sorted(self.relations.items()))),
            spine=spine,
            inference=inference,
            judge_prompts=MappingProxyType(prompts),
            declared_labels=tuple(self.node_types[key].label for key in self.types if key in self.node_types),
        )

    def _add(self, file: _File, where: str, message: str) -> None:
        self.problems.add(file.display, where, message)

    # Merge

    def _merge(self) -> None:
        labels: dict[str, str] = {}
        for file in self.parsed:
            self.broken |= file.broken
            self._merge_vocabularies(file)
            self._merge_groups(file)
            self._merge_types(file, labels)
            self._merge_relations(file, labels)
        for key, (_, file) in self.types.items():
            if key in self.groups:
                self._add(file, f"node_types.{key}", "a type key may not equal a node group key")

    def _merge_vocabularies(self, file: _File) -> None:
        for name, (values, texts) in file.vocabularies.items():
            known = self.vocabularies.get(name)
            if known is None:
                self.vocabularies[name] = (values, texts, file)
            elif (known[0], known[1]) != (values, texts):
                self._add(file, f"vocabularies.{name}", f"declared differently in {known[2].display}")

    def _merge_groups(self, file: _File) -> None:
        for key, group in file.groups.items():
            known_group = self.groups.get(key)
            if known_group is None:
                self.groups[key] = (group, file)
            elif known_group[0] != group:
                self._add(file, f"node_groups.{key}", f"declared differently in {known_group[1].display}")

    def _merge_types(self, file: _File, labels: dict[str, str]) -> None:
        for key, node_type in file.types.items():
            if key in self.types:
                self._add(file, f"node_types.{key}", f"already declared in {self.types[key][1].display}")
                continue
            if node_type.label in labels:
                self._add(
                    file,
                    f"node_types.{key}.label",
                    f"label '{node_type.label}' is already used by type '{labels[node_type.label]}'",
                )
            labels[node_type.label] = key
            self.types[key] = (node_type, file)

    def _merge_relations(self, file: _File, labels: Mapping[str, str]) -> None:
        for name, relation in file.relations.items():
            if name in self.relation_defs:
                self._add(file, f"edge_types.{name}", f"already declared in {self.relation_defs[name][1].display}")
                continue
            if name in labels:
                self._add(file, f"edge_types.{name}", "a relation may not share its name with a label")
            self.relation_defs[name] = (relation, file)

    # Check 2 and 8: names

    def _check_names(self) -> None:
        for name, (_, _, file) in self.vocabularies.items():
            self._check_key(file, f"vocabularies.{name}", name)
        for key, (group, file) in self.groups.items():
            self._check_key(file, f"node_groups.{key}", key)
            for attribute in group.attributes:
                self._check_attribute_name(file, f"node_groups.{key}.attributes.{attribute}", attribute)
        for key, (node_type, file) in self.types.items():
            self._check_key(file, f"node_types.{key}", key)
            self._check_name(file, f"node_types.{key}.label", node_type.label)
            for attribute in node_type.attributes:
                self._check_attribute_name(file, f"node_types.{key}.attributes.{attribute}", attribute)
        for name, (_, file) in self.relation_defs.items():
            self._check_name(file, f"edge_types.{name}", name)

    def _check_key(self, file: _File, where: str, key: str) -> None:
        if not KEY_PATTERN.fullmatch(key):
            self._add(file, where, f"name '{key}' must match {KEY_PATTERN.pattern}")

    def _check_name(self, file: _File, where: str, name: str) -> None:
        if not NAME_PATTERN.fullmatch(name):
            self._add(file, where, f"name '{name}' must match {NAME_PATTERN.pattern}")
        if name in BUILT_IN_NAMES:
            self._add(file, where, f"'{name}' is a built-in name")

    def _check_attribute_name(self, file: _File, where: str, name: str) -> None:
        self._check_key(file, where, name)
        if name in RESERVED_ATTRIBUTES:
            self._add(file, where, f"'{name}' is a reserved property name")

    # Keys to labels

    def _resolve_keys(self) -> None:
        members: dict[str, list[str]] = {key: [] for key in self.groups}
        for key, (node_type, _) in self.types.items():
            self.key_labels[key] = (node_type.label,)
            if node_type.group in members:
                members[node_type.group].append(node_type.label)
        for key, labels in members.items():
            self.key_labels.setdefault(key, tuple(sorted(labels)))

    def _labels_of(self, file: _File, where: str, key: str, *, evidence: bool = False) -> tuple[str, ...] | None:
        """Labels a type or group key names (check 3); built-in evidence labels when allowed."""

        labels = self.key_labels.get(key)
        if labels:
            return labels
        if evidence and key in EVIDENCE_LABELS:
            return (key,)
        if key in self.broken:
            return None
        if labels is not None:
            self._add(file, where, f"node group '{key}' has no member types")
        elif key in EVIDENCE_LABELS:
            self._add(
                file,
                where,
                f"built-in label '{key}' may only end a relation of another owner than {BUILD_OWNER}",
            )
        else:
            self._add(file, where, f"'{key}' is not a declared node type or group")
        return None

    # Node groups and types

    def _compile_groups(self) -> None:
        for key, (group, file) in self.groups.items():
            self.group_attributes[key] = self._compile_attributes(
                file, f"node_groups.{key}.attributes", group.attributes
            )

    def _compile_types(self) -> None:
        families: dict[str, str] = {}
        for key, (definition, file) in sorted(self.types.items()):
            self._compile_type(key, definition, file, families)

    def _type_traits(
        self, definition: NodeTypeDef, file: _File, where: str
    ) -> tuple[NodeGroupDef | None, str | None, str | None, str | None, IdentityDef | None]:
        """The group, origin, tier, role and identity of a type (its own value, else its group's); reports
        a missing group, origin, tier or role, and an origin the file's owner does not own."""

        group = None
        if definition.group is not None:
            entry = self.groups.get(definition.group)
            group = entry[0] if entry else None
            if group is None and definition.group not in self.broken:
                self._add(file, f"{where}.group", f"'{definition.group}' is not a declared node group")
        origin = definition.origin or (group.origin if group else None)
        tier = definition.tier or (group.tier if group else None)
        role = definition.role or (group.role if group else None)
        identity = definition.identity or (group.identity if group else None)
        if origin is None:
            self._add(file, f"{where}.origin", "required on the type or its group")
        elif origin not in OWNER_ORIGINS[file.owner]:
            self._add(file, f"{where}.origin", f"origin '{origin}' is not owned by owner '{file.owner}'")
        if tier is None:
            self._add(file, f"{where}.tier", "required on the type or its group")
        extracted = origin == EXTRACTED_ORIGIN
        if extracted and role is None:
            self._add(file, f"{where}.role", "required for extracted types")
        if not extracted and role is not None:
            self._add(file, f"{where}.role", "only extracted types have a role")
        return group, origin, tier, role, identity

    def _type_id(
        self, definition: NodeTypeDef, file: _File, where: str, key: str, families: dict[str, str], *, extracted: bool
    ) -> str | None:
        """The id prefix of a type; registers its id families (in ``families``) and reports bad ones."""

        prefix = None
        if definition.id is not None:
            match = ID_FORMAT_PATTERN.fullmatch(definition.id.format)
            if match is None:
                self._add(file, f"{where}.id.format", f"must match {ID_FORMAT_PATTERN.pattern}")
            else:
                prefix = match.group(1)
        elif extracted:
            self._add(file, f"{where}.id", "required for extracted types")
        for family in definition.id_families:
            if not ID_FAMILY_PATTERN.fullmatch(family):
                self._add(
                    file,
                    f"{where}.id_families",
                    f"family '{family}' must match {ID_FAMILY_PATTERN.pattern}",
                )
            elif family in families:
                self._add(
                    file,
                    f"{where}.id_families",
                    f"family '{family}' already names type '{families[family]}'",
                )
            else:
                families[family] = key
        return prefix

    def _compile_type(self, key: str, definition: NodeTypeDef, file: _File, families: dict[str, str]) -> None:
        where = f"node_types.{key}"
        group, origin, tier, role, identity = self._type_traits(definition, file, where)
        prefix = self._type_id(definition, file, where, key, families, extracted=origin == EXTRACTED_ORIGIN)
        key_kind = identity.key if identity else ("id" if role == "atomic" else "surface")
        scope_kind = identity.scope if identity else "type"
        scope = definition.label
        if scope_kind == "group":
            if definition.group is None:
                self._add(file, f"{where}.identity.scope", "scope 'group' needs a group")
            else:
                scope = definition.group
        fallback: Literal["span", "surface"] | None = None
        if key_kind == "id":
            fallback = "span" if role == "atomic" else "surface"
        attributes = dict(self.group_attributes.get(definition.group or "", {}))
        attributes.update(self._compile_attributes(file, f"{where}.attributes", definition.attributes))
        self.node_types[key] = NodeType(
            key=key,
            label=definition.label,
            owner=file.owner,
            origin=origin or "",
            tier=tier or "",
            role=role,
            group=definition.group,
            id_format=definition.id.format if definition.id else None,
            id_prefix=prefix,
            id_families=tuple(definition.id_families),
            identity=IdentityRule(key=key_kind, scope=scope, fallback=fallback),
            description=(definition.description or (group.description if group else "")).strip(),
            guidance=(definition.guidance or (group.guidance if group else "")).strip(),
            attributes=MappingProxyType(dict(sorted(attributes.items()))),
        )

    def _compile_attributes(
        self, file: _File, where: str, definitions: dict[str, AttributeDef]
    ) -> dict[str, AttributeSpec]:
        compiled: dict[str, AttributeSpec] = {}
        for name, definition in definitions.items():
            spec = self._compile_attribute(file, f"{where}.{name}", name, definition)
            if spec is not None:
                compiled[name] = spec
        return compiled

    def _compile_attribute(self, file: _File, here: str, name: str, definition: AttributeDef) -> AttributeSpec | None:
        """One attribute: None when it is invalid (every problem is reported)."""

        ok = True
        for extra in sorted(definition.model_fields_set - _ATTRIBUTE_FIELDS[definition.type]):
            self._add(file, f"{here}.{extra}", f"not allowed for a '{definition.type}' attribute")
            ok = False
        if getattr(definition, _REQUIRED_ATTRIBUTE_FIELD[definition.type]) is None:
            self._add(
                file,
                f"{here}.{_REQUIRED_ATTRIBUTE_FIELD[definition.type]}",
                f"required for a '{definition.type}' attribute",
            )
            return None
        values: tuple[str, ...] = ()
        texts: dict[str, str] = {}
        aliases: dict[str, str] = {}
        targets: tuple[str, ...] = ()
        if definition.type == "enum":
            resolved = self._enum_values(file, f"{here}.values", definition.values)
            if resolved is None:
                return None
            values, texts = resolved
            for alias, value in definition.aliases.items():
                if value not in values:
                    self._add(file, f"{here}.aliases.{alias}", f"'{value}' is not one of the values")
                    ok = False
                aliases[alias.strip().casefold()] = value
        elif definition.type == "ref":
            found = self._labels_of(file, f"{here}.target", definition.target or "")
            if found is None:
                return None
            targets = found
        if not ok:
            return None
        return AttributeSpec(
            name=name,
            kind=definition.type,
            description=definition.description.strip(),
            max_chars=definition.max_chars,
            values=values,
            value_descriptions=MappingProxyType(texts),
            aliases=MappingProxyType(aliases),
            target_labels=frozenset(targets),
            store=definition.store,
        )

    def _enum_values(
        self, file: _File, where: str, values: str | list[str] | dict[str, str] | None
    ) -> tuple[tuple[str, ...], dict[str, str]] | None:
        if isinstance(values, str):
            vocabulary = self.vocabularies.get(values)
            if vocabulary is None:
                if values not in self.broken:
                    self._add(file, where, f"'{values}' is not a declared vocabulary")
                return None
            return vocabulary[0], dict(vocabulary[1])
        parsed = _parse_vocabulary(values)
        if parsed is None:
            self._add(file, where, "must name a vocabulary or be a non-empty list or mapping of strings")
        return parsed

    def _node_groups(self) -> dict[str, NodeGroup]:
        return {
            key: NodeGroup(
                key=key,
                description=group.description.strip(),
                guidance=group.guidance.strip(),
                labels=self.key_labels.get(key, ()),
            )
            for key, (group, _) in sorted(self.groups.items())
        }

    # Check 5

    def _check_id_prefixes(self) -> None:
        prefixes = [(t.id_prefix, t.key) for t in self.node_types.values() if t.id_prefix]
        reserved = [(prefix, None) for prefix in BUILT_IN_ID_PREFIXES]
        for prefix, key in prefixes:
            node_file = self.types[key][1]
            for other, other_key in prefixes + reserved:
                if other_key == key:
                    continue
                if other_key is None and (prefix.startswith(other) or other.startswith(prefix)):
                    self._add(
                        node_file,
                        f"node_types.{key}.id.format",
                        f"prefix '{prefix}' collides with built-in prefix '{other}'",
                    )
                elif other_key is not None and prefix.startswith(other) and (prefix != other or key < other_key):
                    self._add(
                        node_file,
                        f"node_types.{key}.id.format",
                        f"prefix '{prefix}' "
                        + ("equals" if prefix == other else "starts with")
                        + f" the prefix of type '{other_key}'",
                    )

    # Relations: checks 3, 4, 9, 10

    def _compile_relations(self) -> None:
        defaults: dict[tuple[str, str], str] = {}
        for name, (definition, file) in sorted(self.relation_defs.items()):
            where = f"edge_types.{name}"
            if definition.origin not in OWNER_ORIGINS[file.owner]:
                self._add(
                    file,
                    f"{where}.origin",
                    f"origin '{definition.origin}' is not owned by owner '{file.owner}'",
                )
            pairs, seen = self._relation_pairs(name, definition, file, defaults)
            if any(pair.cue_required for pair in pairs) and not definition.cues:
                self._add(file, f"{where}.cues", "a relation with a cue_required pair needs cues")
            cues = []
            for index, cue in enumerate(definition.cues):
                try:
                    cues.append(re.compile(cue, re.IGNORECASE))
                except re.error as error:
                    self._add(file, f"{where}.cues[{index}]", f"invalid regular expression ({error.msg})")
            if definition.symmetric:
                missing = [f"{p.target} -> {p.source}" for p in pairs if (p.target, p.source) not in seen]
                if missing:
                    self._add(file, where, f"symmetric, but lacks reversed pairs {_listing(missing)}")
            self.legal |= {(name, pair.source, pair.target) for pair in pairs}
            self.relations[name] = Relation(
                name=name,
                owner=file.owner,
                origin=definition.origin,
                tier=definition.tier,
                description=definition.description.strip(),
                pairs=tuple(pairs),
                symmetric=definition.symmetric,
                id_references=definition.id_references,
                cues=tuple(cues),
            )

    def _relation_pairs(
        self, name: str, definition: EdgeTypeDef, file: _File, defaults: dict[tuple[str, str], str]
    ) -> tuple[list[Pair], set[tuple[str, str]]]:
        """The label pairs a relation allows (each pair definition expanded over its groups), and the
        ``(source, target)`` pairs seen; ``defaults`` records the default relation of each label pair."""

        where = f"edge_types.{name}"
        pairs: list[Pair] = []
        seen: set[tuple[str, str]] = set()
        for index, pair in enumerate(definition.pairs):
            here = f"{where}.pairs[{index}]"
            sources = self._labels_of(file, f"{here}.from", pair.source)
            targets = self._labels_of(file, f"{here}.to", pair.to, evidence=file.owner != BUILD_OWNER)
            if sources is None or targets is None:
                continue
            if definition.symmetric and pair.exclusive:
                self._add(file, here, "a symmetric relation may not have an exclusive pair")
            for source in sources:
                for target in targets:
                    if (source, target) in seen:
                        self._add(file, here, f"pair {source} -> {target} is declared more than once")
                        continue
                    seen.add((source, target))
                    pairs.append(Pair(source, target, pair.default, pair.cue_required, pair.exclusive))
                    if pair.default:
                        other = defaults.get((source, target))
                        if other is not None:
                            self._add(file, here, f"pair {source} -> {target} already has default relation {other}")
                        else:
                            defaults[(source, target)] = name
        return pairs, seen

    def _derive_rule(self, name: str, entry: str, file: _File, where: str) -> DeriveRule | None:
        """The derive rule of one ``<type or group>.<ref attribute>`` entry; None when it is invalid."""

        parts = entry.split(".")
        if len(parts) != DERIVE_PARTS:
            self._add(file, where, f"'{entry}' must be <type or group>.<ref attribute>")
            return None
        key, attribute = parts
        sources = self._labels_of(file, where, key)
        if sources is None:
            return None
        if key in self.node_types:
            spec = self.node_types[key].attributes.get(attribute)
        else:
            spec = self.group_attributes.get(key, {}).get(attribute)
        if spec is None and self._declares_attribute(key, attribute):
            return None
        if spec is None or spec.kind != "ref":
            self._add(file, where, f"'{attribute}' is not a ref attribute of '{key}'")
            return None
        for source in sources:
            for target in sorted(spec.target_labels):
                if (name, source, target) not in self.legal:
                    self._add(file, where, f"pair {source} -> {target} is not legal for {name}")
        return DeriveRule(name, frozenset(sources), attribute, spec.target_labels)

    def _compile_derive(self) -> None:
        for name, (definition, file) in sorted(self.relation_defs.items()):
            rules = [
                rule
                for index, entry in enumerate(definition.derive)
                if (rule := self._derive_rule(name, entry, file, f"edge_types.{name}.derive[{index}]")) is not None
            ]
            if name in self.relations and rules:
                relation = self.relations[name]
                self.relations[name] = Relation(
                    name=relation.name,
                    owner=relation.owner,
                    origin=relation.origin,
                    tier=relation.tier,
                    description=relation.description,
                    pairs=relation.pairs,
                    symmetric=relation.symmetric,
                    id_references=relation.id_references,
                    cues=relation.cues,
                    derive=tuple(rules),
                )

    def _declares_attribute(self, key: str, attribute: str) -> bool:
        """True when ``key`` (or its group) declares ``attribute`` in its file, valid or not."""

        if key in self.groups:
            return attribute in self.groups[key][0].attributes
        if key in self.types:
            definition = self.types[key][0]
            group = self.groups.get(definition.group or "")
            return attribute in definition.attributes or bool(group and attribute in group[0].attributes)
        return False

    def _check_relation(self, file: _File, where: str, name: str | None) -> bool:
        if name is None:
            return False
        if name in self.relations:
            return True
        if name not in self.broken:
            self._add(file, where, f"'{name}' is not a declared relation")
        return False

    # Spine: checks 3 and 6

    def _compile_spine(self, main: _File | None) -> Spine:
        if main is None or main.spine is None:
            return Spine(frozenset(), frozenset(), ())
        definition, file = main.spine, main
        before = len(self.problems.items)
        sources = self._labels_union(file, "spine.sources", definition.sources)
        roots = self._labels_union(file, "spine.roots", definition.roots)
        complete = all(key in self.key_labels for key in [*definition.sources, *definition.roots])
        paths: list[SpinePath] = []
        for index, path in enumerate(definition.paths):
            where = f"spine.paths[{index}]"
            if (path.via is None) == (path.sequence is None):
                self._add(file, where, "needs exactly one of 'via' and 'sequence'")
                continue
            relations = path.via if path.via is not None else path.sequence or []
            if not relations:
                self._add(file, where, "needs at least one relation")
                continue
            if not all(self._check_relation(file, where, name) for name in relations):
                continue
            if path.sequence is not None:
                if path.min is not None or path.max is not None:
                    self._add(file, where, "'min' and 'max' apply only to 'via'")
                paths.append(
                    SpinePath(tuple(relations), sequence=True, min_hops=len(relations), max_hops=len(relations))
                )
                continue
            low = path.min or 1
            high = path.max or low
            if high < low:
                self._add(file, where, "'max' is below 'min'")
                continue
            paths.append(SpinePath(tuple(relations), sequence=False, min_hops=low, max_hops=high))
        complete = complete and len(paths) == len(definition.paths)
        unreachable = [
            source for source in sorted(sources) if not any(self._reaches(source, roots, path) for path in paths)
        ]
        if unreachable and complete and len(self.problems.items) == before:
            self._add(
                file,
                "spine",
                f"sources {_listing(unreachable)} reach no root through a declared path",
            )
        return Spine(frozenset(sources), frozenset(roots), tuple(paths))

    def _labels_union(self, file: _File, where: str, keys: Iterable[str]) -> set[str]:
        labels: set[str] = set()
        for key in keys:
            labels |= set(self._labels_of(file, where, key) or ())
        return labels

    def _step(self, labels: set[str], relations: Iterable[str]) -> set[str]:
        names = set(relations)
        return {target for name, source, target in self.legal if name in names and source in labels}

    def _reachable(self, labels: set[str], relation: str, hops: int) -> set[str]:
        reached: set[str] = set()
        frontier = set(labels)
        for _ in range(hops):
            frontier = self._step(frontier, (relation,))
            reached |= frontier
        return reached

    def _reaches(self, source: str, roots: set[str], path: SpinePath) -> bool:
        frontier = {source}
        if path.sequence:
            for name in path.relations:
                frontier = self._step(frontier, (name,))
            return bool(frontier & roots)
        for hop in range(1, path.max_hops + 1):
            frontier = self._step(frontier, path.relations)
            if hop >= path.min_hops and frontier & roots:
                return True
        return False

    # Inference: checks 3 and 7

    def _compile_inference(self, main: _File | None) -> tuple[InferenceSpec, dict[str, str]]:
        prompts: dict[str, str] = {}
        if main is None or main.inference is None:
            return InferenceSpec(cap=0, floor=1.0, rules=()), prompts
        definition, file = main.inference, main
        rules: list[InferenceRule] = []
        ids: set[str] = set()
        for index, rule in enumerate(definition.rules):
            where = f"inference.rules[{index}]"
            if rule.id in ids:
                self._add(file, f"{where}.id", f"rule id '{rule.id}' is used twice")
            ids.add(rule.id)
            compiled = self._compile_rule(file, where, rule, prompts)
            if compiled is not None:
                rules.append(compiled)
        return InferenceSpec(cap=definition.cap, floor=definition.floor, rules=tuple(rules)), prompts

    def _check_rule_header(
        self, file: _File, where: str, rule: InferenceRuleDef, parameters: frozenset[str], prompts: dict[str, str]
    ) -> bool:
        """The template parameters, judge prompt and written relation of a rule; True when all are valid."""

        ok = True
        given = rule.model_fields_set - {"id", "template", "prompt", "writes"}
        for name in sorted(given - parameters):
            self._add(file, f"{where}.{name}", f"not a parameter of template '{rule.template}'")
            ok = False
        optional = OPTIONAL_TEMPLATE_PARAMETERS.get(rule.template, frozenset())
        for name in sorted(parameters - given - optional):
            self._add(file, f"{where}.{name}", f"required by template '{rule.template}'")
            ok = False
        if not PROMPT_REF_PATTERN.fullmatch(rule.prompt):
            self._add(file, f"{where}.prompt", f"'{rule.prompt}' must match {PROMPT_REF_PATTERN.pattern}")
            ok = False
        else:
            text = file.source.judge_prompt(rule.prompt)
            if text is None:
                self._add(
                    file,
                    f"{where}.prompt",
                    f"judge prompt file {JUDGE_PROMPT_DIR}/{rule.prompt}.md not found",
                )
                ok = False
            else:
                prompts[rule.prompt] = text
        if not self._check_relation(file, f"{where}.writes", rule.writes):
            ok = False
        elif self.relations[rule.writes].owner != file.owner:
            self._add(file, f"{where}.writes", f"relation '{rule.writes}' is not owned by '{file.owner}'")
            ok = False
        return ok

    def _compile_rule(
        self, file: _File, where: str, rule: InferenceRuleDef, prompts: dict[str, str]
    ) -> InferenceRule | None:
        parameters = TEMPLATE_PARAMETERS.get(rule.template)
        if parameters is None:
            known = ", ".join(sorted(TEMPLATE_PARAMETERS))
            self._add(file, f"{where}.template", f"unknown template '{rule.template}' (known: {known})")
            return None
        if not self._check_rule_header(file, where, rule, parameters, prompts):
            return None
        subject = self._labels_of(file, f"{where}.subject", rule.subject or "")
        if subject is None:
            return None
        targets: tuple[str, ...] | None
        if rule.template == "endpoints":
            targets = self._check_endpoints_rule(file, where, rule, subject)
        elif rule.template == "realization":
            targets = self._check_realization_rule(file, where, rule, subject)
        else:
            targets = tuple(sorted(self._labels_union(file, f"{where}.targets", rule.targets or [])))
            if not targets:
                return None
            self._require_legal(
                file,
                where,
                rule.writes,
                subject,
                targets,
                legal=lambda a, b: self._legal(rule.writes, a, b),
            )
        if targets is None:
            return None
        return InferenceRule(
            id=rule.id,
            template=rule.template,
            prompt=rule.prompt,
            writes=rule.writes,
            subject=frozenset(subject),
            targets=frozenset(targets),
            via=tuple(rule.via or ()),
            mentions=bool(rule.mentions),
            member_relation=rule.member_relation,
            container_relation=rule.container_relation,
            min_targets=rule.min_targets,
            max_targets=rule.max_targets,
            max_hops=rule.max_hops,
            max_candidates=rule.max_candidates,
        )

    def _check_endpoints_rule(
        self, file: _File, where: str, rule: InferenceRuleDef, subject: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        """The target labels of an ``endpoints`` rule; None when its targets or ``via`` relations are invalid."""

        targets = self._labels_of(file, f"{where}.target", rule.target or "")
        via = rule.via or []
        if targets is None:
            return None
        if not via:
            self._add(file, f"{where}.via", "needs at least one relation")
            return None
        if len(set(via)) < len(via):
            self._add(file, f"{where}.via", "names a relation twice")
        if not all(self._check_relation(file, f"{where}.via[{index}]", relation) for index, relation in enumerate(via)):
            return None
        if (rule.min_targets or 0) > (rule.max_targets or 0):
            self._add(file, where, "'min_targets' is above 'max_targets'")
        for index, relation in enumerate(via):
            for label in subject:
                if not any(self._legal(relation, label, target) for target in targets):
                    self._add(
                        file,
                        f"{where}.via[{index}]",
                        f"{relation} is not legal from {label} to a target",
                    )
        hubs = {node_type.label for node_type in self.node_types.values() if node_type.role in HUB_ROLES}
        if rule.mentions and not set(targets) & hubs:
            self._add(
                file,
                f"{where}.mentions",
                "no target is a hub, and only hubs are mentioned in fragments",
            )
        self._require_legal(
            file,
            where,
            rule.writes,
            targets,
            targets,
            legal=lambda a, b: self._legal(rule.writes, a, b) or self._legal(rule.writes, b, a),
        )
        return targets

    def _check_realization_rule(
        self, file: _File, where: str, rule: InferenceRuleDef, subject: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        """The container labels of a ``realization`` rule; None when its container or relations are invalid."""

        targets = self._labels_of(file, f"{where}.container", rule.container or "")
        if targets is None:
            return None
        if not self._check_relation(file, f"{where}.member_relation", rule.member_relation):
            return None
        if not self._check_relation(file, f"{where}.container_relation", rule.container_relation):
            return None
        members = {source for name, source, target in self.legal if name == rule.member_relation and target in subject}
        if not members:
            self._add(
                file,
                f"{where}.member_relation",
                f"{rule.member_relation} has no pair ending at the subject",
            )
        elif not self._reachable(members, rule.container_relation or "", rule.max_hops or 1) & set(targets):
            self._add(
                file,
                f"{where}.container_relation",
                f"{rule.container_relation} reaches no container from a member",
            )
        self._require_legal(
            file,
            where,
            rule.writes,
            subject,
            targets,
            legal=lambda a, b: self._legal(rule.writes, a, b),
        )
        return targets

    def _legal(self, relation: str | None, source: str, target: str) -> bool:
        return (relation or "", source, target) in self.legal

    def _require_legal(
        self,
        file: _File,
        where: str,
        relation: str,
        sources: Iterable[str],
        targets: Iterable[str],
        *,
        legal: Callable[[str, str], bool],
    ) -> None:
        target_list = sorted(targets)
        illegal = [
            f"{source} -> {target}" for source in sorted(sources) for target in target_list if not legal(source, target)
        ]
        if illegal:
            self._add(file, f"{where}.writes", f"{relation} is not legal for {_listing(illegal)}")


def _listing(items: Sequence[str], limit: int = 4) -> str:
    shown = ", ".join(items[:limit])
    return f"{shown} (+{len(items) - limit} more)" if len(items) > limit else shown
