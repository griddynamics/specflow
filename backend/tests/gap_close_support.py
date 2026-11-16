"""Simulated apply builds of the shop gap corpus for offline close-check tests (docs/graph/gap/lifecycle.md "Close
check", reconcile.md "Applying amendments").

No model builds the corpus in offline tests, so ``apply_build`` stands in for an update build: it reads the corpus the
way the host's corpus reader hands it to a build (``api.graph_build.corpus.with_answers`` over the stored corpus files,
the world's active amendments and its clarifications document), re-segments only the sections whose text changed
(one fragment per ``##`` section, as ``graph.yaml``'s build options do; unchanged sections keep their fragment ids),
then applies the extraction outcome the test names (nodes removed, added, merged; provenance moved to the new
fragments). The world's reader and graph executor follow (old fragments stay in the executor as the store keeps them
inactive, so their ``CITES`` are read until the close check removes them). It returns the gap-side change report of
that build and the revisions it read.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from api.graph_build.corpus import CorpusAnswersRead, with_answers
from gap_acceptance_support import Planted, corpus_revisions, gap_corpus_graph
from gap_answer_support import MEMBER, ORDERS_LINEAGE, AnswerWorld, answer_world
from gap_detection_support import TracingReader
from gap_port_fakes import SeedInvestigator
from gap_run_support import CountingInvestigator, GapWorld
from graph.build import CorpusRevision
from graph.gap import run_gap
from graph.gap.corpus import document_clarifications
from graph.gap.gaps import read_gaps
from graph.gap.models import AnswerInput, AnswerResult, BuildFacts, GapChangeReport, GapRunResult, GapView, RefusedPair
from graph.gap.names import GRAPH_STATUSES
from graph.gap.reconcile import APPLIED_STATUSES, Judge, LineageText, ReconcileInputs, reconcile_corpus
from graph.plan.diff import DiffInputs, diff
from graph.plan.files import open_plan_store
from graph.plan.models import DiffArtifact
from graph.plan.rules import load_plan_rules
from graph.plan.stitch import stitch
from graph_plan_support import (
    applied_plan,
    fixture_graph,
    fixture_view,
    generic_planners,
    next_view,
    planned_shop,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


@dataclass(frozen=True)
class Section:
    """One ``##`` section of a revision: its heading path, text (heading line included) and line range."""

    heading_path: tuple[str, ...]
    text: str
    line_start: int
    line_end: int


def sections(markdown: str) -> list[Section]:
    """The ``##`` sections of ``markdown`` (the title ``#`` line opens no section of its own)."""

    lines = markdown.split("\n")
    title = ""
    found: list[Section] = []
    start: int | None = None
    heading = ""

    def close(end: int) -> None:
        if start is None:
            return
        body = lines[start:end]
        while body and not body[-1].strip():
            body.pop()
        found.append(Section((title, heading), "\n".join(body), start + 1, start + len(body)))

    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match is None:
            continue
        if len(match.group(1)) == 1:
            title = match.group(2)
            continue
        close(index)
        start, heading = index, match.group(2)
    close(len(lines))
    return found


def world_data(world: GapWorld) -> dict[str, Any]:
    """The fixture data the world's graph was last built from (``graph.yaml`` before the first ``apply_build``)."""

    if world.corpus_data is None:
        world.corpus_data = copy.deepcopy(dict(gap_corpus_graph().data))
    return world.corpus_data


async def corpus_answers(
    world: GapWorld,
    revisions: Sequence[CorpusRevision] = (),
    *,
    judge: Judge | None = None,
    dry_run: bool = False,
) -> CorpusAnswersRead:
    """What the gap runner hands the corpus reader for the world's project (``GapRunner.corpus_answers``): with
    ``revisions`` (a build's snapshot) the lineages are reconciled first, with ``judge`` as the same-meaning call and
    the world's active fragments as the previous text."""

    if revisions:
        reader = world.reader

        async def previous_text(lineage: str) -> str | None:
            fragments = [
                item for item in await reader.fragments(world.project_id, lineage) if item.status in {None, "active"}
            ]
            fragments.sort(key=lambda item: (item.line_start or 0, item.ordinal or 0, item.fragment_id))
            return "\n\n".join(item.text for item in fragments) or None

        async def gap_ids() -> Mapping[str, str]:
            gaps = await read_gaps(world.executor, world.project_id, world.ontology, statuses=GRAPH_STATUSES)
            return {str(props.get("clr_id")): gap.gap_id for gap in gaps for props in gap.clarifications}

        inputs = ReconcileInputs(
            amendments=world.amendments,
            records=world.records,
            judge=judge,
            previous_text=previous_text,
            gap_ids=gap_ids,
        )
        lineages = [LineageText(item.lineage_id, item.revision_id, item.markdown) for item in revisions]
        by_lineage = dict((await reconcile_corpus(world.project_id, lineages, inputs, dry_run=dry_run)).rows)
    else:
        by_lineage = {
            lineage: tuple(await world.amendments.for_lineage(world.project_id, lineage, statuses=APPLIED_STATUSES))
            for lineage in await world.amendments.pending_lineages(world.project_id)
        }
    clarifications = await document_clarifications(world.executor, world.project_id, world.ontology)
    return CorpusAnswersRead(amendments=by_lineage, clarifications=clarifications)


async def effective_corpus(
    world: GapWorld,
    uploads: Mapping[str, str] | None = None,
    *,
    judge: Judge | None = None,
    reconcile: bool = True,
) -> list[CorpusRevision]:
    """The corpus as the next build reads it; ``uploads`` replaces the stored text of a lineage (a new upload, a new
    revision id) and, with ``reconcile``, the snapshot is reconciled like the host's corpus reader does."""

    stored = [
        revision.model_copy(update={"markdown": uploads[revision.lineage_id], "revision_id": "REV::UPLOADED"})
        if uploads and revision.lineage_id in uploads
        else revision
        for revision in corpus_revisions()
    ]
    answers = await corpus_answers(world, stored if reconcile else (), judge=judge)
    return with_answers(world.project_id, stored, answers)


@dataclass(frozen=True)
class NewNode:
    """A node the simulated build extracts from a changed section, its evidence one quoted line there."""

    spec_id: str
    label: str
    name: str
    lineage: str
    heading: str
    quote: str
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rebuilt:
    """A simulated build: its change report, the corpus it read and the fragment of each changed section."""

    report: GapChangeReport
    revisions: tuple[CorpusRevision, ...]
    fragments: Mapping[tuple[str, str], str]
    """(lineage, heading) of each changed or new section to its new fragment id."""
    build: BuildFacts

    def fragment(self, lineage: str, heading: str) -> str:
        return self.fragments[(lineage, heading)]


def _new_fragment(lineage: str, revision: CorpusRevision, section: Section, build_id: str) -> dict[str, Any]:
    digest = hashlib.sha256(section.text.encode("utf-8")).hexdigest()
    path = "/".join(_slug(part) for part in section.heading_path)
    return {
        "fragment_id": f"{lineage}#{path}#{digest[:8]}",
        "document_id": lineage,
        "spec_id": f"FRAG::{digest[:16].upper()}",
        "text": section.text,
        "heading_path": list(section.heading_path),
        "ordinal": 0,
        "line_start": section.line_start,
        "line_end": section.line_end,
        "content_hash": digest,
        "status": "active",
        "last_build_id": build_id,
        "_revision": revision.revision_id,
    }


def _resegment(data: dict[str, Any], revision: CorpusRevision, build_id: str) -> dict[tuple[str, str], tuple[str, str]]:
    """Replace the fragments of the revision's sections whose text changed; (lineage, heading) to (old id, new id)."""

    lineage = revision.lineage_id
    known = [item for item in data["fragments"] if item["document_id"] == lineage]
    by_heading = {item["heading_path"][-1]: item for item in known}
    moved: dict[tuple[str, str], tuple[str, str]] = {}
    ordinal = 0
    for section in sections(revision.markdown):
        ordinal += 1
        old = by_heading.get(section.heading_path[-1])
        if old is not None and old["text"] == section.text:
            continue
        fresh = _new_fragment(lineage, revision, section, build_id)
        fresh.pop("_revision")
        fresh["ordinal"] = ordinal
        if old is not None:
            data["fragments"].remove(old)
        data["fragments"].append(fresh)
        moved[(lineage, section.heading_path[-1])] = (old["fragment_id"] if old else "", fresh["fragment_id"])
    documents = {item["document_id"]: item for item in data["documents"]}
    if lineage not in documents:
        data["documents"].append(
            {
                "document_id": lineage,
                "spec_id": f"DOC::{_slug(revision.title).upper().replace('-', '_')}",
                "revision_id": revision.revision_id,
                "version": revision.version,
                "path": revision.path,
                "title": revision.title,
                "status": "active",
                "last_build_id": build_id,
            }
        )
    else:
        documents[lineage]["revision_id"] = revision.revision_id
    return moved


def _replace_ids(value: Any, ids: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return ids.get(value, value)
    if isinstance(value, list):
        return [_replace_ids(item, ids) for item in value]
    if isinstance(value, dict):
        return {key: _replace_ids(item, ids) for key, item in value.items()}
    return value


def _evidence(fragment: Mapping[str, Any], quote: str, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    offset = next(i for i, line in enumerate(fragment["text"].split("\n")) if quote in line)
    line = fragment["line_start"] + offset
    statement = {
        "fragment_id": fragment["fragment_id"],
        "document_id": fragment["document_id"],
        "revision_id": "REV::REBUILT",
        "version": 1,
        "text": quote,
    }
    provenance = {
        "document_id": fragment["document_id"],
        "path": path,
        "fragment_id": fragment["fragment_id"],
        "line_start": line,
        "line_end": line,
        "quote": quote,
    }
    return statement, provenance


@dataclass(frozen=True)
class Extraction:
    """What the simulated build extracts (``apply_build``): ``remove`` drops nodes, ``add`` extracts new ones, ``edges``
    adds relations (fixture format), ``merge`` merges a node into another (the survivor takes its evidence and
    relations), ``keep`` moves a node's evidence to a quoted line ``(lineage, heading, quote)`` of a changed section,
    ``refused`` replaces the build's dedup refusals (``graph.yaml``'s of the nodes left when None), ``attributes`` sets
    attributes of kept nodes (``None`` removes one)."""

    remove: Sequence[str] = ()
    add: Sequence[NewNode] = ()
    edges: Sequence[Mapping[str, Any]] = ()
    merge: Mapping[str, str] = field(default_factory=dict)
    keep: Mapping[str, tuple[str, str, str]] = field(default_factory=dict)
    refused: Sequence[RefusedPair] | None = None
    attributes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass
class _Build:
    """One simulated build over the fixture data: the sections it re-segmented and where nodes are evidenced."""

    data: dict[str, Any]
    build_id: str
    moved: dict[tuple[str, str], tuple[str, str]]

    @property
    def fragments(self) -> dict[str, Mapping[str, Any]]:
        return {item["fragment_id"]: item for item in self.data["fragments"]}

    def evidence(self, lineage: str, heading: str, quote: str) -> tuple[dict[str, Any], dict[str, Any]]:
        paths = {item["document_id"]: item["path"] for item in self.data["documents"]}
        return _evidence(self.fragments[self.moved[(lineage, heading)][1]], quote, paths[lineage])

    def node(self, spec_id: str) -> dict[str, Any]:
        return next(node for node in self.data["nodes"] if node["spec_id"] == spec_id)

    def extract(self, extraction: Extraction) -> list[str]:
        """Apply ``extraction`` to the data; the ids whose evidence moved to a new fragment."""

        data, gone = self.data, set(extraction.remove)
        renamed = {old: new for old, new in self.moved.values() if old}
        data["nodes"] = [node for node in data["nodes"] if node["spec_id"] not in gone]
        data["edges"] = [edge for edge in data["edges"] if not {edge["source"], edge["target"]} & gone]
        for spec_id, (lineage, heading, quote) in extraction.keep.items():
            statement, provenance = self.evidence(lineage, heading, quote)
            self.node(spec_id)["statements"], self.node(spec_id)["provenance"] = [statement], [provenance]
        moved = {
            node["spec_id"]
            for node in data["nodes"]
            for item in node.get("provenance", ())
            if item["fragment_id"] in renamed
        }
        data["nodes"] = _replace_ids(data["nodes"], renamed)
        data["edges"] = _replace_ids(data["edges"], renamed)
        for new in extraction.add:
            statement, provenance = self.evidence(new.lineage, new.heading, new.quote)
            data["nodes"].append(
                {
                    "spec_id": new.spec_id,
                    "uuid": f"gap-node-{new.spec_id}",
                    "label": new.label,
                    "name": new.name,
                    "last_build_id": self.build_id,
                    "attributes": dict(new.attributes),
                    "statements": [statement],
                    "provenance": [provenance],
                }
            )
        data["edges"] += [dict(edge) for edge in extraction.edges]
        for old, survivor in extraction.merge.items():
            self.merge(old, survivor)
        for spec_id, values in extraction.attributes.items():
            attributes = self.node(spec_id).setdefault("attributes", {})
            for name, value in values.items():
                if value is None:
                    attributes.pop(name, None)
                else:
                    attributes[name] = value
        data["build_id"] = self.build_id
        return sorted((moved | set(extraction.keep)) - gone)

    def merge(self, old: str, survivor: str) -> None:
        node, target = self.node(old), self.node(survivor)
        target["provenance"] = [*target.get("provenance", ()), *node.get("provenance", ())]
        target["statements"] = [*target.get("statements", ()), *node.get("statements", ())]
        self.data["nodes"].remove(node)
        self.data["edges"] = [
            {
                **edge,
                "source": survivor if edge["source"] == old else edge["source"],
                "target": survivor if edge["target"] == old else edge["target"],
            }
            for edge in self.data["edges"]
            if {edge["source"], edge["target"]} != {old, survivor}
        ]


async def _follow(world: GapWorld, data: Mapping[str, Any], extraction: Extraction) -> None:
    """The world's reader and graph executor after the build."""

    reader = TracingReader(fixture_graph(data, world.ontology), world.ontology)
    world.reader = reader
    executor = world.executor
    for spec_id in sorted(extraction.remove):
        executor.delete_spec(spec_id)
    for old, survivor in extraction.merge.items():
        executor.merge_spec(old, survivor)
    for node in await reader.nodes(world.project_id):
        if node.spec_id not in executor.specs:
            documents = sorted({item.document_id for item in node.provenance if item.document_id})
            executor.spec(node.spec_id, node.label, name=node.name, documents=documents)
    for fragment in data["fragments"]:
        executor.fragment(fragment["fragment_id"], fragment["document_id"])


async def apply_build(
    world: GapWorld,
    *,
    build_id: str = "build-2",
    uploads: Mapping[str, str] | None = None,
    judge: Judge | None = None,
    **outcome: Any,
) -> Rebuilt:
    """Build the world's corpus as an update build would (module docstring), with the extraction outcome ``outcome``
    (the fields of ``Extraction``); ``uploads`` is new source text by lineage, reconciled with ``judge``."""

    extraction = Extraction(**outcome)
    data = world_data(world)
    revisions = await effective_corpus(world, uploads, judge=judge)
    moved: dict[tuple[str, str], tuple[str, str]] = {}
    for revision in revisions:
        moved |= _resegment(data, revision, build_id)
    evidence_moved = _Build(data, build_id, moved).extract(extraction)
    await _follow(world, data, extraction)
    refused = extraction.refused
    facts = BuildFacts(build_id=build_id, refused_pairs=tuple(refused) if refused is not None else _refused(data))
    changed: dict[str, list[str]] = {}
    for (lineage, _), (_, new) in sorted(moved.items()):
        changed.setdefault(lineage, []).append(new)
    report = GapChangeReport(
        build_id=build_id,
        added=tuple(new.spec_id for new in extraction.add),
        removed=tuple(sorted(extraction.remove)),
        merged=dict(extraction.merge),
        changed_evidence=tuple(evidence_moved),
        fragments_changed={lineage: tuple(ids) for lineage, ids in changed.items()},
    )
    return Rebuilt(report, tuple(revisions), {key: new for key, (_, new) in moved.items()}, facts)


def _refused(data: Mapping[str, Any]) -> tuple[RefusedPair, ...]:
    ids = {node["spec_id"] for node in data["nodes"]}
    return tuple(
        RefusedPair(
            first_spec_id=item["first_spec_id"],
            second_spec_id=item["second_spec_id"],
            label=item["label"],
            guard=item["guard"],
        )
        for item in data.get("dedup_refused", ())
        if item.get("first_spec_id") in ids and item.get("second_spec_id") in ids
    )


# Scenarios of the shop corpus

CANCEL_REQUIREMENT = "REQ::FR::CANCEL_PAID_ORDER"
PLACE_HEADING = "Place order"
REVIEW_HEADING = "Order review"
PLACE_STORY = "STORY::ORDERS_SERVICE::PLACE_ORDER"


def seed_investigator(world: GapWorld) -> CountingInvestigator:
    """Investigate the sites a build changed without a model: the seed report, under the prompt version of the
    world's investigator (so every unchanged site keeps its cached verdict)."""

    version = getattr(world.investigator, "prompt_version", None)
    counting = CountingInvestigator(SeedInvestigator())
    if version is not None:
        counting.prompt_version = version
    world.investigator = counting
    return counting


async def update_run(world: GapWorld, rebuilt: Rebuilt) -> GapRunResult:
    """The gap run the host queues after the build (mode ``update``, the build's change report and facts)."""

    seed_investigator(world)
    return await run_gap(world.context(changes=[rebuilt.report], build=rebuilt.build))


def option_text(gap: GapView, rank: int) -> str:
    """The text of the first edit of option ``rank``."""

    return next(option for option in gap.options if option.rank == rank).edits[0].text


async def answer_and_apply(rig: AnswerWorld, gap: GapView, **answer: Any) -> AnswerResult:
    """Answer ``gap`` (``option_rank`` or ``own_text`` and ``placement``) and apply the answers at once."""

    result = await rig.service.answer_gap(rig.project_id, AnswerInput(gap_id=gap.gap_id, answered_by=MEMBER, **answer))
    await rig.service.apply_now(rig.project_id, by=MEMBER)
    return result


def cancel_requirement(text: str) -> NewNode:
    """The requirement a build extracts from the cancel-paid-order answer line ``text``."""

    return NewNode(
        CANCEL_REQUIREMENT,
        "FunctionalRequirement",
        "Customer can cancel a paid order",
        ORDERS_LINEAGE,
        PLACE_HEADING,
        text,
    )


CANCEL_EDGE: Final = {
    "relation": "PART_OF",
    "source": CANCEL_REQUIREMENT,
    "target": Planted.PLACE_ORDER_FEATURE,
    "edge_key": "REQ_FR_CANCEL_PAID_ORDER|PART_OF|FEAT_PLACE_ORDER",
    "basis": "stated",
    "source_fragment_ids": [],
}


async def cancel_answered_and_built(rank: int = 1) -> tuple[AnswerWorld, GapView, AnswerResult, Rebuilt]:
    """The cancel-paid-order gap answered with option ``rank`` and applied, and the update build that read it.

    Option A or B: the build extracts the answer as a requirement of the Place order feature and drops the question.
    Option C (a fee with no rule): the build keeps the open question, now evidenced by the answer line."""

    rig = await answer_world()
    gap = await rig.cancel_gap()
    result = await answer_and_apply(rig, gap, option_rank=rank)
    line = option_text(gap, rank)
    if rank == 3:
        rebuilt = await apply_build(rig.world, keep={Planted.STATED_QUESTION: (ORDERS_LINEAGE, PLACE_HEADING, line)})
    else:
        rebuilt = await apply_build(
            rig.world, remove=[Planted.STATED_QUESTION], add=[cancel_requirement(line)], edges=[CANCEL_EDGE]
        )
    return rig, gap, result, rebuilt


# The plan after an apply build (FR-GCOR-04, FR-GPLN-05)

PLAN_INPUTS: Final = DiffInputs(ontology_version="2.0", plan_ontology_version="2.0", rules_version="2.0")


@dataclass(frozen=True)
class PlannedUpdate:
    """The plan's ``update`` run over an apply build: the task operations and the stories of the tasks it wrote."""

    tasks: Mapping[str, str]
    stories: frozenset[str]
    kept_stories: frozenset[str]


async def _plan(view: Any, store: Path) -> DiffArtifact:
    rules = view.rules
    shop = await planned_shop(view, open_plan_store(store), generic_planners(rules))
    stitched = stitch(view, shop.project, shop.tasks)
    return diff(
        view, describe=shop.describe, project=shop.project, tasks=shop.tasks, stitch=stitched, inputs=PLAN_INPUTS
    )


async def plan_update(world: GapWorld, rebuilt: Rebuilt, root: Path) -> PlannedUpdate:
    """Plan the corpus graph before the build with planners that answer from the request (one task per story and
    phase), then run the plan's ``update`` over the graph the build left and the build's changes (added, removed and
    changed nodes, as the host's plan-side change report lists them)."""

    rules = load_plan_rules(ontology=world.ontology)
    before = await fixture_view(gap_corpus_graph().reader, world.ontology, rules)
    current = applied_plan(before, await _plan(before, root / "first"))
    report = rebuilt.report
    changed = (
        dict.fromkeys(report.added, "added")
        | dict.fromkeys(report.removed, "removed")
        | dict.fromkeys(report.changed_evidence, "changed_evidence")
        | dict.fromkeys(report.changed_meaning, "changed_meaning")
    )
    after = next_view(await fixture_view(world.reader.base, world.ontology, rules), current, changed=changed)
    artifact = await _plan(after, root / "update")
    in_story = {link.source: link.target for link in current.links if link.type == "IN_STORY"}
    in_story |= {
        link.key.split("|")[0]: link.key.split("|")[-1] for link in artifact.links_create if link.type == "IN_STORY"
    }
    tasks = {op.id: op.op for op in artifact.tasks}
    stories = frozenset(in_story[task] for task in tasks)
    return PlannedUpdate(tasks, stories, frozenset(story.story_id for story in current.stories) - stories)
