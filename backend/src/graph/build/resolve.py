"""Resolve: raw nodes to canonical nodes."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from graph.build.consolidate import DecisionMemory
from graph.build.hubforms import fold_plural, hub_surface, hub_tokens, hub_value_words, hub_words, tokens
from graph.build.hubjudge import (
    EVIDENCE_LINES,
    MIN_ITEMS,
    Clusters,
    ConsolidationParams,
    GroupFacts,
    HeadingTree,
    HubContext,
    HubGroup,
    HubGroups,
    HubItem,
    HubJoiner,
    HubJudge,
    consolidate_hubs,
    embedding_text,
    hub_block_messages,
    hub_item_hash,
    majority_label,
    unit_rows,
)
from graph.build.models import (
    AttributeValue,
    CanonicalNode,
    NodesArtifact,
    PlanArtifact,
    RawNode,
    ResolveArtifact,
    ResolveCounters,
    SourceQuote,
)
from graph.build.vectors import VectorCache
from graph.core.identity import (
    bounded_key_value,
    identity_key,
    normalise_id,
    normalise_surface,
)
from graph.core.llm import Gateway
from graph.core.ontology import Ontology
from logger.logger import INFO, get_logger, log_event

__all__ = [
    "EVIDENCE_LINES",
    "KEY_FIELDS",
    "MAX_CLUSTER",
    "MIN_ITEMS",
    "ConsolidationParams",
    "HubGroup",
    "HubGroups",
    "IdFinder",
    "PersistedIdentity",
    "clearest_name",
    "display_name",
    "embedding_text",
    "explicit_label",
    "fold_plural",
    "hub_block_messages",
    "hub_item_hash",
    "hub_surface",
    "hub_tokens",
    "hub_value_words",
    "hub_words",
    "majority_label",
    "persisted_aliases",
    "persisted_identities",
    "resolve",
    "source_quotes",
    "surface_values",
    "tokens",
    "unit_rows",
]

logger = get_logger("graph.build.resolve")

MAX_CLUSTER: Final = 12
"""Largest number of groups one cluster may hold: exact-merged groups of a hub-consolidation cluster
(Resolve 4), requirement nodes of a dedup cluster (Conform 2)."""

_LEAD_MARKUP: Final = re.compile(r"^[\s\-*+#>|`_~\[(]*(?:\d+[.)]\s+)?[\s*_`\[(]*")
_AFTER_ID: Final = re.compile(r"^[\s:;,.*_`)\]|\-\u2013\u2014]*")


# Shared helpers (also used by Conform)

KEY_FIELDS: Final = 3
"""Fields of an identity key ``<project>|<kind>|<value>`` (the value may itself hold ``|``)."""


def surface_values(keys: Iterable[str]) -> list[str]:
    """The values of the ``surface``-kind identity keys among ``keys``, in order, each once."""

    values: list[str] = []
    for key in keys:
        parts = key.split("|", 2)
        if len(parts) == KEY_FIELDS and parts[1] == "surface" and parts[2] and parts[2] not in values:
            values.append(parts[2])
    return values


class IdFinder:
    """Finds explicit ids of the ontology's id families in text."""

    def __init__(self, ontology: Ontology) -> None:
        families = sorted(
            {family for node_type in ontology.node_types.values() for family in node_type.id_families},
            key=lambda family: (-len(family), family),
        )
        self._pattern: re.Pattern[str] | None = None
        if families:
            alternatives = "|".join(re.escape(family) for family in families)
            self._pattern = re.compile(
                rf"(?<![\w-])(?:{alternatives})(?:[-_](?:[A-Z]{{1,10}}[-_])*| )\d+(?:[-_.]\d+)*(?!\w|-\w)"
            )

    def all(self, text: str) -> list[str]:
        """Every explicit id in ``text``, normalised, in order of first occurrence."""

        if self._pattern is None or not text:
            return []
        return list(dict.fromkeys(normalise_id(match.group(0)) for match in self._pattern.finditer(text)))

    def own(self, text: str) -> str | None:
        """The id that leads ``text`` (list, heading, table and emphasis markup ignored), normalised."""

        return self.split(text)[0]

    def split(self, text: str) -> tuple[str | None, str]:
        """``(own id, text without it)``: the id as ``own`` finds it and the rest of ``text``, trimmed of the separators
        after the id.
        """

        if self._pattern is None or not text:
            return None, text
        start = _LEAD_MARKUP.match(text)
        head = self._pattern.match(text, start.end() if start else 0)
        if head is None:
            return None, text
        return normalise_id(head.group(0)), _AFTER_ID.sub("", text[head.end() :]).strip()


def explicit_label(ontology: Ontology, explicit_id: str, scope: str) -> str | None:
    """The label the id's family names, when that label keys by id within ``scope``; else None."""

    label = ontology.label_for_id_family(explicit_id)
    if label is None:
        return None
    rule = ontology.identity_rule(label)
    return label if rule.key == "id" and rule.scope == scope else None


def clearest_name(surfaces: Sequence[str]) -> str:
    """Most frequent surface, then the longest, then the first."""

    counts = Counter(surfaces)
    first = {surface: index for index, surface in reversed(list(enumerate(surfaces)))}
    return min(counts, key=lambda surface: (-counts[surface], -len(surface), first[surface]))


def display_name(surfaces: Sequence[str], explicit_ids: Iterable[str]) -> str:
    """The clearest non-empty surface; the explicit id when every member name was the id alone."""

    named = [surface for surface in surfaces if surface]
    if named:
        return clearest_name(named)
    return min(explicit_ids, default="")


# Resolve


@dataclass
class _Group:
    """Raw nodes under one identity key (step 2)."""

    key: str
    scope: str
    provisional: bool
    explicit_ids: set[str] = field(default_factory=set)
    id_label: str | None = None
    raws: list[RawNode] = field(default_factory=list)
    surfaces: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return display_name(self.surfaces, self.explicit_ids)

    @property
    def label(self) -> str:
        return self.id_label or majority_label(self.labels)


@dataclass(frozen=True, slots=True)
class PersistedIdentity:
    """The identity key and label of a persisted spec node (Resolve step 6), and its identity aliases
    (hub mentions also match their ``surface`` values: ``persisted_aliases``)."""

    identity_key: str
    label: str
    aliases: tuple[str, ...] = ()
    attributes: tuple[tuple[str, AttributeValue], ...] = ()
    """Persisted attribute values Conform keeps for merged nodes (the spine ``exclude`` attributes, T12)."""


def persisted_identities(
    nodes: Iterable[tuple[str, Sequence[str], str]],
    attributes: Mapping[str, Mapping[str, AttributeValue]] | None = None,
) -> dict[str, PersistedIdentity]:
    """``(identity key, identity aliases, label)`` of persisted spec nodes as the ``persisted`` mapping of ``resolve``:
    every key, then every alias not already a key, each to the first node holding it.
    """

    values = attributes or {}
    rows = [
        PersistedIdentity(
            identity_key=key, label=label, aliases=tuple(aliases), attributes=tuple(sorted(values.get(key, {}).items()))
        )
        for key, aliases, label in nodes
        if key and label
    ]
    mapping: dict[str, PersistedIdentity] = {}
    for row in rows:
        mapping.setdefault(row.identity_key, row)
    for row in rows:
        for alias in row.aliases:
            mapping.setdefault(alias, row)
    return mapping


def persisted_aliases(keys: Iterable[str], persisted: Mapping[str, PersistedIdentity]) -> tuple[str, ...]:
    """The identity key and aliases of the persisted node that the first of ``keys`` (a node's own key,
    then its aliases) names; empty when none does."""

    for key in keys:
        match = persisted.get(key)
        if match is not None:
            return (match.identity_key, *match.aliases)
    return ()


async def resolve(
    plan: PlanArtifact,
    nodes: NodesArtifact,
    *,
    gateway: Gateway,
    ontology: Ontology,
    vectors: VectorCache,
    memory: DecisionMemory | None = None,
    persisted: Mapping[str, PersistedIdentity] | None = None,
    params: ConsolidationParams | None = None,
) -> ResolveArtifact:
    """Canonical nodes of one build from its raw nodes; name and candidate vectors come from and go to ``vectors``;
    ``memory`` holds the hub consolidation decisions (None: none are kept); ``persisted`` (``update`` builds) the
    persisted labels by identity key and alias (step 6); ``params`` the hub pass settings (internal, for replays;
    default ``ConsolidationParams``).
    """

    fragment_rank = {fragment.fragment_id: index for index, fragment in enumerate(plan.fragments)}
    groups, empty_names = _exact_groups(nodes.raw_nodes, ontology, IdFinder(ontology))
    exact_merges = len(nodes.raw_nodes) - empty_names - len(groups)

    params = params or ConsolidationParams()
    hubs = ontology.hub_labels
    headings = HeadingTree.of(plan)
    clusters = Clusters([_facts(group, headings if group.label in hubs else None) for group in groups], MAX_CLUSTER)
    chosen: dict[int, str] = {}
    items = _hub_items(groups, clusters, hubs, HubContext.of(plan, nodes.raw_nodes, hubs, params.evidence_lines))
    apart = frozenset(label for label in hubs if not ontology.merge_across_labels(label))
    continued = {item.key: persisted[item.key].identity_key for item in items if persisted and item.key in persisted}
    joiner = HubJoiner(clusters, chosen, items, hubs=hubs, apart=apart, persisted=continued)
    hub_pass = await consolidate_hubs(
        items, joiner, HubJudge(gateway, ontology, vectors, memory, params, catalog=nodes.hub_catalog)
    )
    consolidation = hub_pass.counters

    members: dict[int, list[int]] = {}
    for index in range(len(groups)):
        members.setdefault(clusters.find(index), []).append(index)
    anchors = frozenset(row.identity_key for row in (persisted or {}).values())
    drafts = [_draft(groups, indexes, fragment_rank, chosen.get(root), anchors) for root, indexes in members.items()]
    labels_kept = _keep_persisted_labels(drafts, persisted or {}, ontology)
    drafts.sort(key=lambda draft: str(draft["identity_key"]))
    texts = [embedding_text(str(draft["label"]), str(draft["name"])) for draft in drafts]
    # Stores the vector of every canonical name (a merged node may take a name not embedded yet).
    await vectors.mapping(texts)
    width = max(5, len(str(len(drafts))))
    canonical = tuple(
        CanonicalNode.model_validate({"cid": f"c{index:0{width}d}", "name_embedding_text": text, **draft})
        for index, (draft, text) in enumerate(zip(drafts, texts, strict=True))
    )
    result = ResolveArtifact(
        canonical=canonical,
        counters=ResolveCounters(
            exact_merges=exact_merges,
            empty_names=empty_names,
            labels_kept=labels_kept,
            consolidation=consolidation,
        ),
        merges=hub_pass.merges,
        pair_candidates=hub_pass.pair_candidates,
    )
    log_event(
        logger,
        INFO,
        "graph.resolve.done",
        raw_nodes=len(nodes.raw_nodes),
        canonical_nodes=len(canonical),
        provisional=sum(1 for node in canonical if node.provisional),
        **result.counters.model_dump(exclude={"consolidation"}),
        hub_merges=consolidation.merges,
        hub_cross_label_merges=consolidation.cross_label_merges,
        hub_vetoes=consolidation.vetoes,
        hub_label_splits=consolidation.label_splits,
        hub_pair_merges=consolidation.pair_merges,
    )
    return result


def _exact_groups(
    raw_nodes: Iterable[RawNode],
    ontology: Ontology,
    ids: IdFinder,
) -> tuple[list[_Group], int]:
    """Groups by identity key and the number of raw nodes dropped for an empty normalised name."""

    groups: dict[str, _Group] = {}
    empty = 0
    hubs = ontology.hub_labels
    for raw in raw_nodes:
        rule = ontology.identity_rule(raw.label)
        surface = " ".join(raw.name.split())
        explicit, rest = ids.split(surface) if rule.key == "id" else (None, surface)
        id_label = explicit_label(ontology, explicit, rule.scope) if explicit else None
        if id_label is not None and explicit is not None:
            surface = rest
            key = identity_key(rule.scope, "id", explicit)
            provisional = False
        else:
            value = (
                hub_surface(raw.name) if raw.label in hubs and rule.key == "surface" else normalise_surface(raw.name)
            )
            if not value:
                empty += 1
                continue
            provisional = rule.key == "id" and rule.fallback == "span"
            key = (
                identity_key(rule.scope, "span", bounded_key_value(value))
                if provisional
                else identity_key(rule.scope, "surface", value)
            )
        group = groups.get(key)
        if group is None:
            group = groups[key] = _Group(key=key, scope=rule.scope, provisional=provisional)
        if id_label is not None and explicit is not None:
            group.explicit_ids.add(explicit)
            group.id_label = id_label
        group.raws.append(raw)
        group.surfaces.append(surface)
        group.labels.append(raw.label)
    return list(groups.values()), empty


# Hub consolidation (step 4)


def _hub_items(
    groups: Sequence[_Group], clusters: Clusters, hubs: Collection[str], context: HubContext
) -> list[HubItem]:
    """One consolidation item per hub group (every cluster is one group when step 4 starts)."""

    items: list[HubItem] = []
    for index, group in enumerate(groups):
        if group.label not in hubs or group.provisional:
            continue
        label = clusters.label(index, None)
        surfaces = frozenset(hub_surface(surface) for surface in group.surfaces) - {""}
        shown = context.item(label, group.name, group.surfaces, group.raws)
        items.append(
            HubItem(
                root=index,
                key=group.key,
                label=label,
                name=group.name,
                surfaces=surfaces,
                shown=shown,
                hash=hub_item_hash(label, surfaces, shown),
            )
        )
    return items


def _facts(group: _Group, headings: HeadingTree | None) -> GroupFacts:
    """The group's facts for the hub pass; ``headings``: the plan's heading tree (hub groups only)."""

    found = headings.headings(group.raws) if headings is not None else frozenset()
    return GroupFacts(
        explicit_ids=frozenset(group.explicit_ids),
        id_label=group.id_label,
        label=group.label,
        raw_labels=tuple(group.labels),
        headings=found,
        under=headings.under(found) if headings is not None else frozenset(),
    )


def _strings(value: object) -> tuple[str, ...]:
    """The items of a draft field (aliases, explicit ids) as strings."""

    return tuple(str(item) for item in value) if isinstance(value, Iterable) else ()


def _persisted_candidates(key: str, aliases: Sequence[str], *, hub: bool, hub_scopes: Sequence[str]) -> list[str]:
    """The keys a draft may continue a persisted node under: its own and aliases (a hub in every hub scope)."""

    candidates: list[str] = []
    for own in (key, *aliases):
        candidates.append(own)
        scope, _, rest = own.partition("|")
        if hub and scope in hub_scopes:
            candidates.extend(f"{other}|{rest}" for other in hub_scopes if other != scope)
    return candidates


def _kept_key(
    match: PersistedIdentity, key: str, label: str, candidates: Sequence[str], ontology: Ontology
) -> str | None:
    """The identity key a draft of ``label`` takes when it continues ``match`` of another label; None when
    the two labels do not share a key scope (a hub continues a hub only)."""

    if label in ontology.hub_labels:
        if match.label not in ontology.hub_labels:
            return None
        _, _, rest = key.partition("|")
        return (
            match.identity_key
            if match.identity_key in candidates
            else f"{ontology.identity_rule(match.label).scope}|{rest}"
        )
    if ontology.identity_rule(match.label).scope == ontology.identity_rule(label).scope:
        return key
    return None


def _keep_persisted_labels(
    drafts: list[dict[str, object]], persisted: Mapping[str, PersistedIdentity], ontology: Ontology
) -> int:
    """Step 6 (module docstring): give each draft that continues a persisted node of another label
    that label and its key scope, in place; returns the number of drafts changed."""

    if not persisted:
        return 0
    # Hubs of a type with merge_across_labels: false are never rescoped, nor rescoped into.
    hubs = frozenset(label for label in ontology.hub_labels if ontology.merge_across_labels(label))
    extracted = set(ontology.extracted_labels)
    hub_scopes = sorted({ontology.identity_rule(label).scope for label in hubs})
    taken = {str(draft["identity_key"]) for draft in drafts}
    kept = 0
    for draft in drafts:
        label, key = str(draft["label"]), str(draft["identity_key"])
        aliases = _strings(draft["identity_aliases"])
        if any(ontology.label_for_id_family(value) for value in _strings(draft["explicit_ids"])):
            continue
        candidates = _persisted_candidates(key, aliases, hub=label in hubs, hub_scopes=hub_scopes)
        match = next((persisted[candidate] for candidate in candidates if candidate in persisted), None)
        if match is None or match.label == label or match.label not in extracted:
            continue
        if not (ontology.merge_across_labels(label) and ontology.merge_across_labels(match.label)):
            continue
        new_key = _kept_key(match, key, label, candidates, ontology)
        if new_key is None or (new_key != key and new_key in taken):
            continue
        taken.discard(key)
        taken.add(new_key)
        draft["label"] = match.label
        draft["identity_key"] = new_key
        draft["identity_aliases"] = tuple(sorted({key, *aliases} - {new_key}))
        draft["label_disagreement"] = label
        kept += 1
    if kept:
        log_event(logger, INFO, "graph.resolve.labels_kept", nodes=kept)
    return kept


def _draft(
    groups: Sequence[_Group],
    indexes: Sequence[int],
    fragment_rank: Mapping[str, int],
    chosen_label: str | None = None,
    anchors: Collection[str] = frozenset(),
) -> dict[str, object]:
    """Fields of one canonical node (all but ``cid`` and ``name_embedding_text``) from its groups (step 5);
    ``chosen_label``: the label step 3 or 4 chose for the cluster; ``anchors``: the identity keys of the persisted nodes
    (``update`` builds).
    """

    members = [groups[index] for index in indexes]
    raws = [raw for group in members for raw in group.raws]
    explicit = sorted({value for group in members for value in group.explicit_ids})
    id_labels = [group.id_label for group in members if group.id_label]
    label = (
        id_labels[0]
        if id_labels
        else chosen_label or majority_label([label for group in members for label in group.labels])
    )
    own = [group for group in members if group.label == label]
    if own:
        members = own + [group for group in members if group.label != label]
    naming = [group for group in own or members if group.key in anchors] or own or members
    name = display_name([surface for group in naming for surface in group.surfaces], explicit)
    lead = next(
        (group for group in [*naming, *members] if name in group.surfaces and group.label == label),
        next((group for group in members if name in group.surfaces), members[0]),
    )
    fragments = sorted(
        {fragment_id for raw in raws for fragment_id in raw.fragment_ids},
        key=lambda fragment_id: _fragment_order(fragment_rank, fragment_id),
    )
    return {
        "identity_key": lead.key,
        "identity_aliases": tuple(sorted(group.key for group in members if group is not lead)),
        "provisional": lead.provisional,
        "name": name,
        "label": label,
        "member_ids": tuple(raw.raw_id for raw in raws),
        "explicit_ids": tuple(explicit),
        "fragment_ids": tuple(fragments),
        "quotes": source_quotes(raws, fragment_rank),
    }


def _fragment_order(fragment_rank: Mapping[str, int], fragment_id: str) -> tuple[int, str]:
    return fragment_rank.get(fragment_id, len(fragment_rank)), fragment_id


def source_quotes(raws: Iterable[RawNode], fragment_rank: Mapping[str, int]) -> tuple[SourceQuote, ...]:
    """The distinct quotes of ``raws`` (equal texts once), each with the union of the fragments of
    the raw nodes that gave it in plan order (``fragment_rank``); ordered by those fragments' plan
    order, then by text. Raw nodes without a quote add none."""

    fragments: dict[str, set[str]] = {}
    for raw in raws:
        if raw.quote:
            fragments.setdefault(raw.quote, set()).update(raw.fragment_ids)
    quotes = [
        SourceQuote(
            quote=quote,
            fragment_ids=tuple(sorted(found, key=lambda fragment_id: _fragment_order(fragment_rank, fragment_id))),
        )
        for quote, found in fragments.items()
    ]
    return tuple(
        sorted(
            quotes,
            key=lambda item: (
                [_fragment_order(fragment_rank, fragment_id) for fragment_id in item.fragment_ids],
                item.quote,
            ),
        )
    )
