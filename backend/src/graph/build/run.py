"""Build orchestration: mode selection, build records, the step functions, the step table and
``run_build`` (build.md "Modes" and "Steps"; api.md "Build"; tech-specs 5.3).

Only this module does ``BuildStore`` I/O for the steps. Every step function takes the build context,
reads its inputs from the build's artifacts, writes its own artifact and records its progress, so a
host can run steps one by one and resume an interrupted build. ``STEP_FUNCTIONS`` maps each step to its
function. ``infer`` runs light inference (``graph.build.infer``) and ``verify`` the verification with
repairs (``graph.build.verify``). ``estimate_build`` selects the mode and runs the Plan logic
without writing anything, then predicts the extraction calls (``graph.build.estimate``).

The step functions ``resolve``, ``conform``, ``diff`` and ``persist`` share their names with modules of
``graph.build``, so the package exports them under the api.md names on the ``steps`` namespace
(``graph.build.steps.resolve``), and ``plan`` and ``run_build`` directly.

LLM usage per step is the change of the gateway's usage counters while the step ran. When several
builds share one gateway concurrently, a step's usage also counts the other builds' calls made in the
meantime, so per-step usage is approximate in that case.

What a build reuses from earlier builds, by mode (build.md "Modes", "Reuse in update builds"):

- ``update``: everything below, plus the stored extraction results (``StoredResults.read``: nodes per
  fragment, edges per unit and the clean units' last catalogs, attribute values per node) and the
  persisted labels (Resolve, and Conform for the keys it gives, keep a continuing node's label and key
  scope; ``_persisted_identities``).
- ``rebuild``: extracts every fragment again and reads no stored extraction result and no persisted
  label, but keeps identity stable across builds by reusing what only depends on its inputs: the
  embedding vectors (vector store, by text), the hub consolidation decisions (``DecisionMemory``)
  and the requirement dedup verdicts (``conform.DedupMemory``). A ``first`` build uses the same
  stores, which are empty for a new project. Every mode writes all of these for later builds.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, get_args

import numpy as np

from graph.build import conform as conformance
from graph.build import diff as differ
from graph.build import estimate as estimation
from graph.build import extract as extraction
from graph.build import infer as inference
from graph.build import persist as writing
from graph.build import resolve as resolution
from graph.build import terms
from graph.build import verify as verification
from graph.build.changes import (
    change_set,
    dirty_ids,
    dirty_windows,
    has_persisted_graph,
    match_fragments,
    persisted_documents,
    persisted_fragments,
    plan_units,
)
from graph.build.consolidate import DecisionMemory
from graph.build.corpus import segment_revision
from graph.build.diff import EMPTY_GRAPH, DiffInput
from graph.build.extract import (
    KnownTermsSource,
    StoredResults,
    attribute_fingerprint,
    edge_fingerprint,
    node_fingerprint,
    stored_node_fragments,
)
from graph.build.models import (
    STEPS,
    ArtifactModel,
    BuildEstimate,
    BuildMetrics,
    BuildMode,
    BuildResult,
    BuildStatus,
    DiffArtifact,
    DocumentRec,
    EdgesArtifact,
    FragmentRec,
    InferenceReport,
    NodesArtifact,
    PersistCursor,
    PlanArtifact,
    ResolveArtifact,
    SpecChanges,
    StepName,
    TargetGraph,
    VerificationReport,
    VerificationSummary,
)
from graph.build.options import BuildContext, BuildOptions, StepServices
from graph.build.ports import BuildRecord, CorpusRevision
from graph.build.vectors import VectorCache, text_hash
from graph.core.db import Executor
from graph.core.errors import ConfigurationError, ConformanceError, CorpusError, GraphError
from graph.core.llm.judge import JudgeRunner
from graph.core.llm.usage import UsageCounts
from graph.core.logfields import count_fields
from graph.core.ontology import FingerprintPart, Ontology
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.run")

StepFunction = Callable[[BuildContext], Awaitable[None]]

FULL_MODES: Final[frozenset[BuildMode]] = frozenset({"first", "rebuild"})
"""Modes that extract everything; an ``update`` needs the record of the last one of these."""


# Mode and build records


def ontology_fingerprint(ontology: Ontology) -> str:
    """sha256 over the fingerprints of every prompt part of the ontology."""

    parts = {part: ontology.fingerprint(part) for part in get_args(FingerprintPart)}
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


def _executor(ctx: BuildContext) -> Executor:
    if ctx.executor is None:  # BuildContext always sets one
        raise ConformanceError("build_context_without_executor")
    return ctx.executor


async def select_mode(ctx: BuildContext) -> BuildMode:
    """``rebuild`` when the host asks; ``first`` when the project has no persisted ``Document`` and no
    spec node; else ``update`` (build.md "Modes").

    With a persisted graph, the newest ``first`` or ``rebuild`` record in the ``BuildStore`` decides:
    when it is unfinished or ``failed``, ``ConfigurationError("unfinished_build", build_id=...)`` names
    it (the host resumes that build, or asks for a rebuild); without any,
    ``ConfigurationError("rebuild_required")``. An ``update`` is checked against that record
    (``check_frozen``) before anything is recorded or any model is called.
    """

    if ctx.rebuild:
        return "rebuild"
    if not await has_persisted_graph(_executor(ctx), ctx.project_id):
        return "first"
    newest = await ctx.builds.latest_build(ctx.project_id, modes=FULL_MODES)
    if newest is None:
        raise ConfigurationError("rebuild_required")
    if not newest.finished:
        raise ConfigurationError("unfinished_build", build_id=newest.build_id)
    check_frozen(ctx, newest)
    return "update"


def relation_set(ontology: Ontology) -> tuple[str, ...]:
    """Every relation name of the ontology, sorted (recorded with each build)."""

    return tuple(sorted(ontology.relations))


def check_frozen(ctx: BuildContext, last_full: BuildRecord) -> None:
    """An ``update`` keeps what the last ``first`` or ``rebuild`` build froze (build.md "Modes";
    FR-BLD-14): its options (``frozen_options_changed``, ``fields`` names the changed options), the
    ontology's labels and relations (``ontology_changed``, ``part`` ``labels`` or ``relations``) and
    the embedding model (``embedding_model_changed``). Each raises ``ConfigurationError``; a
    ``rebuild`` is needed."""

    if ctx.options != last_full.options:
        now, then = ctx.options.model_dump(mode="json"), last_full.options.model_dump(mode="json")
        changed = sorted(name for name in now.keys() | then.keys() if now.get(name) != then.get(name))
        raise ConfigurationError("frozen_options_changed", fields=",".join(changed))
    if tuple(ctx.ontology.labels) != tuple(last_full.label_set):
        raise ConfigurationError("ontology_changed", part="labels")
    if last_full.relation_set is not None and relation_set(ctx.ontology) != tuple(last_full.relation_set):
        raise ConfigurationError("ontology_changed", part="relations")
    if ctx.gateway.config.embedding_model != last_full.embedding_model:
        raise ConfigurationError("embedding_model_changed")


async def start_build(ctx: BuildContext) -> str:
    """Select the mode, store a new ``BuildRecord`` with the frozen options and return its id."""

    mode = await select_mode(ctx)
    record = BuildRecord(
        build_id=uuid.uuid4().hex,
        project_id=ctx.project_id,
        mode=mode,
        options=ctx.options,
        ontology_fingerprint=ontology_fingerprint(ctx.ontology),
        label_set=ctx.ontology.labels,
        relation_set=relation_set(ctx.ontology),
        embedding_model=ctx.gateway.config.embedding_model,
        started_at=datetime.now(UTC),
    )
    await ctx.builds.create_build(record)
    log_event(logger, INFO, "graph.build.started", build_id=record.build_id, mode=mode)
    return record.build_id


async def require_build(ctx: BuildContext) -> BuildRecord:
    """The record of ``ctx.build_id``; ``ConfigurationError`` when unset or unknown."""

    if ctx.build_id is None:
        raise ConfigurationError("build_id_required")
    record = await ctx.builds.get_build(ctx.project_id, ctx.build_id)
    if record is None:
        raise ConfigurationError("build_not_found", build_id=ctx.build_id)
    return record


async def load_artifact[ArtifactT: ArtifactModel](
    ctx: BuildContext, step: StepName, model: type[ArtifactT]
) -> ArtifactT:
    """The stored artifact of ``step`` of the context's build; ``ConfigurationError`` when missing."""

    payload = await ctx.builds.get_artifact(ctx.project_id, _build_id(ctx), step)
    if payload is None:
        raise ConfigurationError("artifact_missing", build_id=_build_id(ctx), step=step)
    return model.model_validate(payload)


async def _optional_artifact[ArtifactT: ArtifactModel](
    ctx: BuildContext, step: StepName, model: type[ArtifactT]
) -> ArtifactT | None:
    payload = await ctx.builds.get_artifact(ctx.project_id, _build_id(ctx), step)
    return None if payload is None else model.model_validate(payload)


async def store_artifact(ctx: BuildContext, step: StepName, artifact: ArtifactModel) -> None:
    """Store ``artifact`` as the output of ``step`` of the context's build."""

    await ctx.builds.put_artifact(ctx.project_id, _build_id(ctx), step, artifact.model_dump(mode="json"))


def _build_id(ctx: BuildContext) -> str:
    if ctx.build_id is None:
        raise ConfigurationError("build_id_required")
    return ctx.build_id


@asynccontextmanager
async def step_progress(ctx: BuildContext, step: StepName) -> AsyncIterator[BuildRecord]:
    """Mark ``step`` ``running``, then ``done`` when the block ends or ``failed`` when it raises."""

    record = await require_build(ctx)
    await ctx.builds.put_progress(ctx.project_id, record.build_id, step, "running")
    started = time.monotonic()
    try:
        yield record
    except Exception as error:
        await ctx.builds.put_progress(ctx.project_id, record.build_id, step, "failed")
        # State only: the outer handler logs the traceback.
        log_event(
            logger,
            WARNING,
            "graph.build.step_failed",
            build_id=record.build_id,
            step=step,
            error_type=type(error).__name__,
            error_code=getattr(error, "code", None),
        )
        raise
    await ctx.builds.put_progress(ctx.project_id, record.build_id, step, "done")
    log_event(
        logger,
        INFO,
        "graph.build.step_done",
        build_id=record.build_id,
        step=step,
        duration_ms=round((time.monotonic() - started) * 1000),
    )


# 1. Plan


async def plan(ctx: BuildContext) -> str:
    """Plan step (build.md step 1): start a build when ``ctx.build_id`` is None, then read the corpus
    snapshot, compute the change set, segment, select dirty fragments, group units and store the
    ``PlanArtifact``. Returns the build id."""

    if ctx.build_id is None:
        ctx = ctx.with_build_id(await start_build(ctx))
    async with step_progress(ctx, "plan") as record:
        artifact = await _plan_artifact(ctx, record.mode, record.options)
        await store_artifact(ctx, "plan", artifact)
        log_event(
            logger,
            INFO,
            "graph.build.planned",
            build_id=record.build_id,
            mode=artifact.mode,
            documents=len(artifact.documents),
            fragments=len(artifact.fragments),
            dirty=len(artifact.dirty_ids),
            units=len(artifact.units),
            windows=sum(len(unit.windows) for unit in artifact.units),
            masked_lines=sum(artifact.masked_lines.values()),
        )
    return record.build_id


async def _plan_artifact(
    ctx: BuildContext, mode: BuildMode, options: BuildOptions, *, dry_run: bool = False
) -> PlanArtifact:
    """The Plan step's artifact for ``mode`` under ``options``; reads only (the corpus snapshot is a
    dry run when ``dry_run``, so ``estimate_build`` writes nothing)."""

    revisions = await ctx.corpus.snapshot(ctx.project_id, dry_run=dry_run)
    _validate_snapshot(revisions)
    persisted = await persisted_documents(_executor(ctx), ctx.project_id)
    changes = change_set(revisions, persisted, ctx.retry_lineages)
    segmented: list[FragmentRec] = []
    masked: dict[str, int] = {}
    for revision in revisions:
        segmentation = segment_revision(revision, options)
        segmented.extend(segmentation.fragments)
        if segmentation.masked_lines:
            masked[revision.lineage_id] = segmentation.masked_lines
    old = await persisted_fragments(_executor(ctx), ctx.project_id) if mode != "first" else []
    fragments, retired = match_fragments(segmented, old, ctx.retry_lineages)
    dirty = dirty_ids(fragments, everything=mode in FULL_MODES, stale=await _stale(ctx, mode, options, fragments))
    dirty_set = set(dirty)
    tokens = {fragment.fragment_id: fragment.tokens for fragment in fragments}
    units = [
        unit.model_copy(
            update={
                "windows": tuple(dirty_windows(unit, dirty_set, max_windows=options.max_node_windows, tokens=tokens))
            }
        )
        for unit in plan_units(fragments, options)
    ]
    return PlanArtifact(
        mode=mode,
        change_set=changes,
        documents=_documents(revisions, persisted, changes.removed),
        fragments=tuple(fragments),
        retired_ids=retired,
        dirty_ids=dirty,
        units=tuple(units),
        masked_lines=masked,
    )


def _validate_snapshot(revisions: list[CorpusRevision]) -> None:
    seen: set[str] = set()
    for revision in revisions:
        if not isinstance(revision, CorpusRevision):
            raise CorpusError("corpus_invalid_revision", fields="type")
        if revision.lineage_id in seen:
            raise CorpusError("corpus_duplicate_lineage", lineage_id=revision.lineage_id)
        seen.add(revision.lineage_id)


async def _stale(ctx: BuildContext, mode: BuildMode, options: BuildOptions, fragments: list[FragmentRec]) -> set[str]:
    """``update`` builds: unchanged ``active`` fragments without stored nodes for their content under
    the current node extractor fingerprint (build.md "Modes": extracted again). A ``skipped`` one is
    extracted again only when it changes or is retried."""

    if mode in FULL_MODES:
        return set()
    clean = [fragment for fragment in fragments if fragment.change == "unchanged" and fragment.status == "active"]
    if not clean:
        return set()
    stored = await stored_node_fragments(
        stored_results(ctx, mode), node_fingerprint(ctx.ontology, ctx.gateway, options), clean
    )
    return {fragment.fragment_id for fragment in clean if fragment.fragment_id not in stored}


def stored_results(ctx: BuildContext, mode: BuildMode) -> StoredResults:
    """The project's stored extraction results; read only by ``update`` builds (a ``rebuild`` reuses nothing)."""

    return StoredResults(ctx.extraction, ctx.project_id, read=mode == "update")


def _documents(
    revisions: list[CorpusRevision], persisted: list[DocumentRec], removed: tuple[str, ...]
) -> tuple[DocumentRec, ...]:
    """Documents as they will be persisted: every snapshot lineage active, then the removed ones."""

    current = [
        DocumentRec(
            document_id=revision.lineage_id,
            revision_id=revision.revision_id,
            version=revision.version,
            path=revision.path,
            title=revision.title,
        )
        for revision in revisions
    ]
    gone = set(removed)
    return (
        *current,
        *(document.model_copy(update={"status": "removed"}) for document in persisted if document.document_id in gone),
    )


# Cost estimate


async def estimate_build(ctx: BuildContext) -> BuildEstimate:
    """Cost estimate of the build ``run_build(ctx)`` would start (build.md "Cost estimate", FR-BLD-16):
    the mode it would select (with the same ``ConfigurationError`` an ``update`` would raise), its
    Plan step over a dry-run corpus snapshot, and the calls and input tokens of every step before
    inference (``graph.build.estimate``).

    Reads only: no model call, no build record, no artifact, no store or graph write. Reuse is the
    build's own: an ``update`` plans only the fragments without stored nodes for their content as
    dirty, so an unchanged corpus estimates no call. ``units_reused``: the planned units with no
    dirty fragment (no node or edge call predicted). The estimate is of a new build: a context with
    ``build_id`` set raises ``ConfigurationError("estimate_build_id_set")`` (resuming is not estimated)."""

    if ctx.build_id is not None:
        raise ConfigurationError("estimate_build_id_set", build_id=ctx.build_id)
    mode = await select_mode(ctx)
    plan_artifact = await _plan_artifact(ctx, mode, ctx.options, dry_run=True)
    hubs = await terms.persisted_hubs(_executor(ctx), ctx.project_id, ctx.ontology) if mode != "first" else []
    # The persisted nodes stand in for what the dirty fragments will give (a rebuild reuses none of
    # them); nodes, evidence and fragments only, no edge is read.
    persisted = (
        estimation.persisted_nodes(
            plan_artifact.fragments,
            await differ.load_current_graph(_executor(ctx), ctx.project_id, ctx.ontology, edges=False),
            ctx.ontology.hub_labels,
        )
        if mode != "first"
        else estimation.PersistedNodes()
    )
    steps = await estimation.estimate_steps(
        plan_artifact,
        mode=mode,
        services=StepServices(gateway=ctx.gateway, ontology=ctx.ontology, options=ctx.options),
        revisions=active_revisions(plan_artifact) if mode in FULL_MODES else [],
        hubs=hubs,
        persisted=persisted,
        lookups=estimation.Lookups(
            vectors=vector_cache(ctx),
            decisions=decision_memory(ctx),
            results=stored_results(ctx, mode) if mode == "update" else None,
            edge_fingerprint=edge_fingerprint(ctx.ontology, ctx.gateway, ctx.options),
            attribute_fingerprint=attribute_fingerprint(ctx.ontology, ctx.gateway),
        ),
    )
    dirty = set(plan_artifact.dirty_ids)
    extracted = sum(1 for unit in plan_artifact.units if any(fid in dirty for fid in unit.fragment_ids))
    estimate = BuildEstimate(
        project_id=ctx.project_id,
        mode=mode,
        change_set=plan_artifact.change_set,
        fragments_by_change=dict(sorted(Counter(fragment.change for fragment in plan_artifact.fragments).items())),
        fragments_by_status=dict(sorted(Counter(fragment.status for fragment in plan_artifact.fragments).items())),
        dirty_fragments=len(dirty),
        units_to_extract=extracted,
        units_reused=len(plan_artifact.units) - extracted,
        steps=steps,
    )
    log_event(
        logger,
        INFO,
        "graph.build.estimated",
        mode=mode,
        fragments=len(plan_artifact.fragments),
        dirty=len(dirty),
        units=len(plan_artifact.units),
        units_to_extract=extracted,
        calls=sum(step.calls for step in steps.values()),
        input_tokens=sum(step.input_tokens for step in steps.values()),
    )
    return estimate


# 2. Extract nodes (with the hub catalog and known terms)


async def extract_nodes(ctx: BuildContext) -> None:
    """Extract nodes step (build.md step 2): hub catalog and known terms, then node extraction per window."""

    async with step_progress(ctx, "extract_nodes") as record:
        plan_artifact = await load_artifact(ctx, "plan", PlanArtifact)
        options = record.options
        hubs, source = await _hubs(ctx, record, plan_artifact)
        known: dict[str, list[str]] = {}
        # Known terms rank only units that are extracted (a dirty fragment in a window), so a build
        # without one embeds nothing here (no fragment or hub vector is even looked up).
        dirty = set(plan_artifact.dirty_ids)
        extracted = [
            unit
            for unit in plan_artifact.units
            if unit.windows and any(fragment_id in dirty for fragment_id in unit.fragment_ids)
        ]
        if hubs and extracted:
            cache = vector_cache(ctx)
            vectors = await terms.fragment_vectors(plan_artifact.fragments, cache)
            names = await terms.hub_vectors(hubs, cache)
            by_id = {fragment.fragment_id: fragment for fragment in plan_artifact.fragments}
            known = {
                unit.unit_id: terms.known_terms(unit, by_id, hubs, names, vectors, limit=options.known_terms_limit)
                for unit in extracted
            }
        artifact = await extraction.extract_nodes(
            plan_artifact,
            gateway=ctx.gateway,
            ontology=ctx.ontology,
            options=options,
            known_terms=known,
            known_terms_source=source,
            results=stored_results(ctx, record.mode),
        )
        await store_artifact(ctx, "extract_nodes", artifact)


async def _hubs(
    ctx: BuildContext, record: BuildRecord, plan_artifact: PlanArtifact
) -> tuple[list[terms.HubName], KnownTermsSource]:
    """Known-term hubs (build.md step 2, FR-BLD-23): the hub catalog in ``first`` and ``rebuild``
    builds, the persisted hub nodes in ``update`` builds."""

    if record.mode not in FULL_MODES:
        hubs = await terms.persisted_hubs(_executor(ctx), ctx.project_id, ctx.ontology)
        return hubs, ("persisted" if hubs else "none")
    if not record.options.hub_catalog:
        return [], "none"
    hubs = await terms.hub_catalog(
        active_revisions(plan_artifact), plan_artifact.fragments, ctx.gateway, ctx.ontology, record.options
    )
    return hubs, ("hub_catalog" if hubs else "none")


def active_revisions(plan_artifact: PlanArtifact) -> list[CorpusRevision]:
    """The plan's active documents as revisions without text (what the hub catalog outlines)."""

    return [
        CorpusRevision(
            lineage_id=document.document_id,
            revision_id=document.revision_id,
            version=document.version,
            path=document.path,
            title=document.title,
            markdown="",
        )
        for document in plan_artifact.documents
        if document.status == "active"
    ]


def vector_cache(ctx: BuildContext) -> VectorCache:
    """A fresh vector cache of the context's project (one per step, so its memo lives as long as the step)."""

    return VectorCache(ctx.gateway, ctx.extraction, ctx.project_id)


def decision_memory(ctx: BuildContext) -> DecisionMemory:
    """The hub consolidation decision memory of the context's project (Resolve step 4)."""

    return DecisionMemory(ctx.extraction, ctx.project_id, ctx.gateway.config.model_for("judge"))


# 3. Resolve, 4. Extract edges, 5. Conform


async def resolve(ctx: BuildContext) -> None:
    """Resolve step (build.md step 3)."""

    async with step_progress(ctx, "resolve") as record:
        plan_artifact = await load_artifact(ctx, "plan", PlanArtifact)
        nodes = await load_artifact(ctx, "extract_nodes", NodesArtifact)
        artifact = await resolution.resolve(
            plan_artifact,
            nodes,
            gateway=ctx.gateway,
            ontology=ctx.ontology,
            vectors=vector_cache(ctx),
            memory=decision_memory(ctx),
            persisted=await _persisted_identities(ctx, record),
        )
        await store_artifact(ctx, "resolve", artifact)


async def _persisted_identities(
    ctx: BuildContext, record: BuildRecord
) -> dict[str, resolution.PersistedIdentity] | None:
    """``update`` builds: the persisted label and identity key of every spec node by its identity key
    and aliases (Resolve step 6, Conform step 1), read from the current graph; None in ``first`` and
    ``rebuild``."""

    if record.mode != "update":
        return None
    current = await differ.load_current_graph(_executor(ctx), ctx.project_id, ctx.ontology)
    return resolution.persisted_identities(
        (node.identity_key, node.identity_aliases, node.label) for node in current.nodes
    )


async def _persisted_hub_aliases(
    ctx: BuildContext, record: BuildRecord, resolved: ResolveArtifact
) -> dict[str, tuple[str, ...]]:
    """``update`` builds: the identity key and aliases of the persisted node each hub continues, by
    cid, so edge catalogs find a hub by a name folded into it earlier (``terms.EdgeCatalog``)."""

    persisted = await _persisted_identities(ctx, record)
    if not persisted:
        return {}
    hubs = ctx.ontology.hub_labels
    found = {
        node.cid: resolution.persisted_aliases((node.identity_key, *node.identity_aliases), persisted)
        for node in resolved.canonical
        if node.label in hubs
    }
    return {cid: keys for cid, keys in found.items() if keys}


async def extract_edges(ctx: BuildContext) -> None:
    """Extract edges step (build.md step 4), with the attributes pass."""

    async with step_progress(ctx, "extract_edges") as record:
        plan_artifact = await load_artifact(ctx, "plan", PlanArtifact)
        resolved = await load_artifact(ctx, "resolve", ResolveArtifact)
        nodes = await load_artifact(ctx, "extract_nodes", NodesArtifact)
        cache = vector_cache(ctx)
        vectors = await terms.fragment_vectors(plan_artifact.fragments, cache)
        named = [node for node in resolved.canonical if node.name_embedding_text is not None]
        name_vectors = await cache.get_or_embed([node.name_embedding_text or "" for node in named])
        artifact = await extraction.extract_edges(
            plan_artifact,
            resolved,
            services=StepServices(gateway=ctx.gateway, ontology=ctx.ontology, options=record.options),
            fragment_vectors=vectors,
            name_vectors={node.cid: vector for node, vector in zip(named, name_vectors, strict=True)},
            results=stored_results(ctx, record.mode),
            node_skipped=tuple(nodes.skipped),
            hub_aliases=await _persisted_hub_aliases(ctx, record, resolved),
        )
        await store_artifact(ctx, "extract_edges", artifact)


async def conform(ctx: BuildContext) -> None:
    """Conform step (build.md step 5); statements are stamped with the build's start time."""

    async with step_progress(ctx, "conform") as record:
        plan_artifact = await load_artifact(ctx, "plan", PlanArtifact)
        resolved = await load_artifact(ctx, "resolve", ResolveArtifact)
        edges = await load_artifact(ctx, "extract_edges", EdgesArtifact)
        artifact = await conformance.conform(
            plan_artifact,
            resolved,
            edges,
            services=StepServices(gateway=ctx.gateway, ontology=ctx.ontology, options=record.options),
            vectors=vector_cache(ctx),
            at=_stamp(record),
            memory=conformance.DedupMemory(ctx.extraction, ctx.project_id, ctx.gateway.config.model_for("small")),
            persisted=await _persisted_identities(ctx, record),
        )
        await store_artifact(ctx, "conform", artifact)


def _stamp(record: BuildRecord) -> str:
    return record.started_at.astimezone(UTC).isoformat(timespec="seconds")


# 6. Diff and 7. Persist


async def diff(ctx: BuildContext) -> None:
    """Diff step (build.md step 6) against the current graph (``diff.load_current_graph``; a ``first``
    build has none).

    A new diff never resumes an old persist: a persist cursor of the build is reset first and the
    steps after diff marked ``done`` are reopened (``failed``: the port has no pending state),
    so a resumed ``run_build`` persists this diff. Fragments the edge step ``recovered`` are passed as
    extracted this build (with the dirty ones), so they are persisted ``active`` at their content.
    """

    async with step_progress(ctx, "diff") as record:
        await _reopen_after_diff(ctx, record)
        current = (
            EMPTY_GRAPH
            if record.mode == "first"
            else await differ.load_current_graph(_executor(ctx), ctx.project_id, ctx.ontology)
        )
        plan_artifact = await load_artifact(ctx, "plan", PlanArtifact)
        target = await load_artifact(ctx, "conform", TargetGraph)
        nodes = await load_artifact(ctx, "extract_nodes", NodesArtifact)
        edges = await load_artifact(ctx, "extract_edges", EdgesArtifact)
        given = DiffInput(
            project_id=ctx.project_id,
            build_id=record.build_id,
            embedding_model=record.embedding_model,
            at=_stamp(record),
            taken=await ctx.registry.taken(ctx.project_id),
            skipped={**nodes.skipped, **edges.skipped},
            current=current,
            edge_failed_fragments=edges.edge_failed_fragments,
        )
        extracted = plan_artifact
        if edges.recovered:
            extracted = plan_artifact.model_copy(update={"dirty_ids": (*plan_artifact.dirty_ids, *edges.recovered)})
        artifact = differ.diff(extracted, target, ctx.ontology, given)
        await store_artifact(ctx, "diff", artifact)
        log_event(
            logger, INFO, "graph.build.diffed", build_id=record.build_id, **count_fields(differ.diff_summary(artifact))
        )


async def _reopen_after_diff(ctx: BuildContext, record: BuildRecord) -> None:
    """Reset the build's persist cursor and reopen the steps after diff that have a recorded state."""

    if await _optional_artifact(ctx, "persist", PersistCursor) is not None:
        await store_artifact(ctx, "persist", PersistCursor())
    for step in STEPS[STEPS.index("diff") + 1 :]:
        if record.progress.get(step) == "done":
            await ctx.builds.put_progress(ctx.project_id, record.build_id, step, "failed")


async def persist(ctx: BuildContext, *, on_batch: Callable[[int], None] | None = None) -> None:
    """Persist step (build.md step 7): apply the diff batch by batch from the stored cursor.

    The diff's ``spec_id`` rows are recorded in the registry before the first batch: the ids are
    reserved even when persist is interrupted, and a re-run allocates the same ids for the same keys.
    Node vectors are read per batch from the vector store; one missing there is embedded again from
    its text in the conform artifact (or the diff's ``texts``). The diff's ``texts`` (a continuing
    node's texts under its persisted label) go through the vector store first, so they are embedded
    once, here. ``on_batch(index)`` runs after each committed batch (tech-specs 5.3 test hook).
    """

    async with step_progress(ctx, "persist") as record:
        artifact = await load_artifact(ctx, "diff", DiffArtifact)
        cursor = await _optional_artifact(ctx, "persist", PersistCursor) or PersistCursor()
        if cursor.batch_index < 0 and not cursor.done and artifact.registry_rows:
            await ctx.registry.record(ctx.project_id, record.build_id, artifact.registry_rows)

        async def save(state: PersistCursor) -> None:
            await store_artifact(ctx, "persist", state)

        cache = vector_cache(ctx)
        if artifact.texts and not cursor.done:
            await cache.mapping(artifact.texts.values())

        async def target_texts() -> dict[str, str]:
            target = await load_artifact(ctx, "conform", TargetGraph)
            return {
                **artifact.texts,
                **{
                    text_hash(text): text
                    for node in target.nodes
                    for text in (node.embedding_text, node.name_embedding_text)
                    if text is not None
                },
            }

        async def fetch_vectors(digests: Collection[str]) -> Mapping[str, np.ndarray]:
            return await cache.by_hash(digests, target_texts)

        final = await writing.persist(
            artifact,
            cursor,
            target=writing.PersistTarget(executor=_executor(ctx), project_id=ctx.project_id, build_id=record.build_id),
            ontology=ctx.ontology,
            save=save,
            fetch_vectors=fetch_vectors,
            on_batch=on_batch,
        )
        log_event(
            logger,
            INFO,
            "graph.build.persisted",
            build_id=record.build_id,
            operations=len(artifact.operations),
            batches=final.batches,
        )


# 8. Infer and 9. Verify (M4)


async def infer(ctx: BuildContext) -> None:
    """Infer step (build.md step 8, analysis.md "Light inference"): runs every ontology inference rule
    once and swaps in the new inferred edges (``graph.build.infer``); off when
    ``BuildSettings.inference`` is false. Judgements are remembered in the ``ExtractionStore`` (kind
    ``decisions``), so unchanged subjects are not judged again. When this build's diff changed nothing
    (no operation and no spec change), no subject is judged: the never-judged backlog waits for a
    build that changes something. A non-fatal model error is reported in the stored
    ``InferenceReport`` (FR-INF-05); fatal errors fail the step with the previous inferred edges
    intact."""

    async with step_progress(ctx, "infer") as record:
        if not ctx.settings.inference:
            await store_artifact(ctx, "infer", InferenceReport(enabled=False))
            return
        judge_model = ctx.gateway.config.model_for("judge")
        changes = await _optional_artifact(ctx, "diff", DiffArtifact)
        graph_changed = changes is None or bool(changes.operations) or changes.spec_changes != SpecChanges()
        report = await inference.infer(
            _executor(ctx),
            ctx.project_id,
            ctx.ontology,
            JudgeRunner(ctx.gateway, ctx.ontology),
            stamp=inference.InferenceStamp(build_id=record.build_id, at=_stamp(record), judge_model=judge_model),
            memory=inference.JudgeMemory(ctx.extraction, ctx.project_id, judge_model),
            graph_changed=graph_changed,
        )
        await store_artifact(ctx, "infer", report)


async def verify(ctx: BuildContext) -> None:
    """Verify step (build.md step 9, analysis.md "Verification"): checks the persisted graph, repairs
    blocking problems (at most two rounds, the deletion valve applies), restoring evidence from this
    build's diff and conform artifacts, and stores the ``VerificationReport``. Its outcome never
    changes the build status; a requested follow-up build makes the result not ``settled``."""

    async with step_progress(ctx, "verify") as record:
        report = await verification.verify_build(
            _executor(ctx),
            ctx.project_id,
            ctx.ontology,
            build_id=record.build_id,
            registry=ctx.registry,
            target=await _optional_artifact(ctx, "conform", TargetGraph),
            diff=await _optional_artifact(ctx, "diff", DiffArtifact),
        )
        await store_artifact(ctx, "verify", report)


VERIFICATION_REPORTED: Final = True
"""Whether ``BuildResult.verification`` is filled from the verify artifact."""


def _verification_summary(report: VerificationReport | None) -> VerificationSummary | None:
    """The build result's verification summary (build.md "Build result"); None when verify did not run."""

    if report is None:
        return None
    return VerificationSummary(
        ok=report.ok,
        found=report.found,
        problems=dict(Counter(problem.check for problem in report.problems)),
        repairs=len(report.repairs),
        rounds=report.rounds,
        blocked_rounds=report.blocked_rounds,
        follow_up_lineages=report.follow_up_lineages,
        metrics=report.metrics,
    )


def _edges_after(diff_artifact: DiffArtifact | None, report: InferenceReport | None) -> dict[str, int]:
    """Domain edges per relation the graph holds after this build: the diff's count once persisted
    (``DiffArtifact.edges_by_relation``), with the inferred edges the infer step added and removed
    (``edge_ref`` form, ``<source>|<RELATION>|<target>``)."""

    if diff_artifact is None:
        return {}
    counts = Counter(diff_artifact.edges_by_relation)
    if report is not None:
        counts.update(ref.split("|")[1] for ref in report.inferred_added)
        counts.subtract(ref.split("|")[1] for ref in report.inferred_removed)
    return {relation: count for relation, count in sorted(counts.items()) if count > 0}


def _with_inference(changes: SpecChanges, report: InferenceReport | None) -> SpecChanges:
    """``changes`` plus the inferred edges the infer step added and removed (``edge_ref`` form, as diff
    names the inferred edges it removes)."""

    if report is None or not (report.inferred_added or report.inferred_removed):
        return changes
    added = set(report.inferred_added)
    return changes.model_copy(
        update={
            "inferred_added": tuple(sorted({*changes.inferred_added, *added})),
            "inferred_removed": tuple(sorted({*changes.inferred_removed, *report.inferred_removed} - added)),
        }
    )


def _with_repairs(changes: SpecChanges, report: VerificationReport | None) -> SpecChanges:
    """``changes`` plus the nodes and inferred edges verification repairs deleted (W4: a deleted node
    is listed as removed, and no longer as added, modified, rekeyed or carried over; a deleted inferred
    edge is no longer listed as added). Inferred edges are in the ``spec_id`` form
    ``<source spec_id>|<RELATION>|<target spec_id>``."""

    if report is None or not (report.removed_spec_ids or report.removed_inferred_edges):
        return changes
    removed, gone = set(report.removed_spec_ids), set(report.removed_inferred_edges)

    def kept(values: tuple[str, ...], dropped: set[str]) -> tuple[str, ...]:
        return tuple(value for value in values if value not in dropped)

    return changes.model_copy(
        update={
            "added": kept(changes.added, removed),
            "modified": kept(changes.modified, removed),
            "rekeyed": kept(changes.rekeyed, removed),
            "carried_over": kept(changes.carried_over, removed),
            "removed": tuple(sorted({*changes.removed, *removed})),
            "inferred_added": kept(changes.inferred_added, gone),
            "inferred_removed": tuple(sorted({*changes.inferred_removed, *gone})),
        }
    )


# Step table and run_build


async def _plan_step(ctx: BuildContext) -> None:
    await plan(ctx)


async def _persist_step(ctx: BuildContext) -> None:
    await persist(ctx)


STEP_FUNCTIONS: dict[StepName, StepFunction] = {
    "plan": _plan_step,
    "extract_nodes": extract_nodes,
    "resolve": resolve,
    "extract_edges": extract_edges,
    "conform": conform,
    "diff": diff,
    "persist": _persist_step,
    "infer": infer,
    "verify": verify,
}
"""The step function of each step, run in ``STEPS`` order by ``run_build``."""


@dataclass(frozen=True, slots=True)
class StepFunctions:
    """The step functions under their api.md names (``graph.build.steps``): ``resolve``, ``conform``,
    ``diff`` and ``persist`` would shadow the modules of the same name if exported from the package."""

    plan: Callable[[BuildContext], Awaitable[str]]
    extract_nodes: StepFunction
    resolve: StepFunction
    extract_edges: StepFunction
    conform: StepFunction
    diff: StepFunction
    persist: Callable[..., Awaitable[None]]
    infer: StepFunction
    verify: StepFunction


steps: Final = StepFunctions(
    plan=plan,
    extract_nodes=extract_nodes,
    resolve=resolve,
    extract_edges=extract_edges,
    conform=conform,
    diff=diff,
    persist=persist,
    infer=infer,
    verify=verify,
)


def first_open_step(record: BuildRecord) -> int:
    """Index in ``STEPS`` of the first step not ``done`` (``len(STEPS)`` when all are)."""

    for index, step in enumerate(STEPS):
        if record.progress.get(step) != "done":
            return index
    return len(STEPS)


@dataclass
class _RunStats:
    """Durations and LLM usage of the steps run by one ``run_build`` call."""

    duration_s: dict[str, float] = field(default_factory=dict)
    llm: dict[str, UsageCounts] = field(default_factory=dict)


def _usage_delta(before: UsageCounts, after: UsageCounts) -> UsageCounts:
    return UsageCounts(
        calls=after.calls - before.calls,
        failures=after.failures - before.failures,
        requests=after.requests - before.requests,
        input_tokens=after.input_tokens - before.input_tokens,
        output_tokens=after.output_tokens - before.output_tokens,
        duration_s=round(after.duration_s - before.duration_s, 3),
    )


async def run_build(ctx: BuildContext) -> BuildResult:
    """Run every step (api.md "Build"): a new build when ``ctx.build_id`` is None, else resume that
    build from its first step not ``done`` (a finished build returns its stored result).

    A step that raises finishes the build as ``failed`` (the record stays resumable), then the error
    propagates. Durations and LLM usage in the metrics cover the steps this call ran.
    """

    if ctx.build_id is None:
        ctx = ctx.with_build_id(await start_build(ctx))
        start = 0
    else:
        record = await require_build(ctx)
        if record.finished:
            stored = await ctx.builds.get_result(ctx.project_id, record.build_id)
            if stored is not None:
                return stored
        start = first_open_step(record)
    stats = _RunStats()
    for step in STEPS[start:]:
        before = ctx.gateway.usage().total()
        started = time.monotonic()
        try:
            await STEP_FUNCTIONS[step](ctx)
        except Exception as error:
            _record_step(stats, step, started, before, ctx)
            await _finish(ctx, stats, failed_step=step, error=error)
            raise
        _record_step(stats, step, started, before, ctx)
    return await _finish(ctx, stats)


def _skipped_fragments(
    plan_artifact: PlanArtifact | None, nodes: NodesArtifact | None, edges: EdgesArtifact | None
) -> dict[str, str]:
    """Fragments persisted ``skipped`` after this build, with their error code, in plan order: those
    this build skipped, and those still ``skipped`` from earlier builds (not extracted again now, not
    ``recovered``)."""

    now = {**(nodes.skipped if nodes else {}), **(edges.skipped if edges else {})}
    if plan_artifact is None:
        return now
    extracted = {*plan_artifact.dirty_ids, *(edges.recovered if edges else ())}
    result: dict[str, str] = {}
    for fragment in plan_artifact.fragments:
        fid = fragment.fragment_id
        if fid in now:
            result[fid] = now[fid]
        elif fragment.status == "skipped" and fid not in extracted:
            result[fid] = fragment.error_code or "skipped"
    result.update({fid: code for fid, code in now.items() if fid not in result})
    return result


def _record_step(stats: _RunStats, step: StepName, started: float, before: UsageCounts, ctx: BuildContext) -> None:
    stats.duration_s[step] = round(time.monotonic() - started, 3)
    delta = _usage_delta(before, ctx.gateway.usage().total())
    if delta.calls or delta.requests:
        stats.llm[step] = delta


async def _finish(
    ctx: BuildContext,
    stats: _RunStats,
    *,
    failed_step: StepName | None = None,
    error: Exception | None = None,
) -> BuildResult:
    """Store the build result and finish the record; a failure here never hides the step's error."""

    record = await require_build(ctx)
    try:
        result = await _result(ctx, record, stats, failed_step, error)
        await ctx.builds.finish_build(result)
    except Exception as finish_error:
        if error is None:
            raise
        # The step's error propagates; this one is reported by type and code only, never its text.
        log_event(
            logger,
            WARNING,
            "graph.build.finish_failed",
            build_id=record.build_id,
            error_type=type(finish_error).__name__,
            error_code=getattr(finish_error, "code", None),
        )
        return BuildResult(
            project_id=ctx.project_id,
            build_id=record.build_id,
            mode=record.mode,
            status="failed",
            failed_step=failed_step,
            started_at=record.started_at,
            finished_at=datetime.now(UTC),
        )
    metrics = result.metrics
    log_event(
        logger,
        INFO if error is None else WARNING,
        "graph.build.finished",
        build_id=record.build_id,
        mode=record.mode,
        status=result.status,
        failed_step=failed_step,
        error_code=result.error_code,
        fragments=dict(metrics.fragments_by_status),
        units_extracted=metrics.units_extracted,
        nodes=sum(metrics.nodes_by_label.values()),
        edges=sum(metrics.edges_by_relation.values()),
        llm_calls=sum(usage.calls for usage in metrics.llm.values()),
        input_tokens=sum(usage.input_tokens for usage in metrics.llm.values()),
        output_tokens=sum(usage.output_tokens for usage in metrics.llm.values()),
        duration_s=round(sum(metrics.duration_s.values()), 3),
    )
    return result


async def _result(
    ctx: BuildContext,
    record: BuildRecord,
    stats: _RunStats,
    failed_step: StepName | None,
    error: Exception | None,
) -> BuildResult:
    plan_artifact = await _optional_artifact(ctx, "plan", PlanArtifact)
    nodes = await _optional_artifact(ctx, "extract_nodes", NodesArtifact)
    edges = await _optional_artifact(ctx, "extract_edges", EdgesArtifact)
    target = await _optional_artifact(ctx, "conform", TargetGraph)
    diff_artifact = await _optional_artifact(ctx, "diff", DiffArtifact)
    cursor = await _optional_artifact(ctx, "persist", PersistCursor)
    # Only a verify run that finished in this build's current attempt (a re-run diff reopens it).
    verified = VERIFICATION_REPORTED and record.progress.get("verify") == "done"
    report = await _optional_artifact(ctx, "verify", VerificationReport) if verified else None
    inferred = (
        await _optional_artifact(ctx, "infer", InferenceReport) if record.progress.get("infer") == "done" else None
    )
    skipped = _skipped_fragments(plan_artifact, nodes, edges)
    status: BuildStatus = "failed" if error is not None else "partially_succeeded" if skipped else "succeeded"
    by_status: Counter[str] = Counter()
    units_extracted = units_total = 0
    if plan_artifact is not None:
        extracted = {*plan_artifact.dirty_ids, *(edges.recovered if edges else ())}
        for fragment in plan_artifact.fragments:
            # A dirty fragment's planned status is the persisted one; extracted now, it is active.
            fid = fragment.fragment_id
            by_status["skipped" if fid in skipped else "active" if fid in extracted else fragment.status] += 1
        by_status["retired"] += len(plan_artifact.retired_ids)
        units_total = len(plan_artifact.units)
        called = set(edges.called_units) if edges else set()
        # A unit is extracted when its node windows or its edge call ran; reused when neither did.
        units_extracted = sum(1 for unit in plan_artifact.units if unit.windows or unit.unit_id in called)
    dropped: dict[str, int] = {}
    if target is not None:
        for relation, reasons in target.counters.dropped_edges.items():
            for reason, count in reasons.items():
                dropped[f"{relation}:{reason}"] = count
    verification = _verification_summary(report)
    persisted = cursor is not None and cursor.done
    metrics = BuildMetrics(
        fragments_by_status={key: value for key, value in sorted(by_status.items()) if value},
        units_extracted=units_extracted,
        units_reused=units_total - units_extracted,
        llm=stats.llm,
        nodes_by_label=diff_artifact.nodes_by_label if diff_artifact else {},
        edges_by_relation=_edges_after(diff_artifact, inferred),
        dropped_edges=dropped,
        exclusive_conflicts=target.counters.exclusive_conflicts if target else 0,
        spine_unreachable=report.metrics.spine_unreachable if report else None,
        attribution_fallbacks=nodes.counters.window_fallbacks if nodes else 0,
        attribute_batches_failed=edges.counters.attribute_batches_failed if edges else 0,
        duration_s=stats.duration_s,
    )
    return BuildResult(
        project_id=ctx.project_id,
        build_id=record.build_id,
        mode=record.mode,
        status=status,
        settled=error is None and persisted and not (report and report.follow_up_lineages),
        spec_changes=_with_repairs(
            _with_inference(diff_artifact.spec_changes if diff_artifact else SpecChanges(), inferred), report
        ),
        verification=verification,
        metrics=metrics,
        skipped_fragments=skipped,
        failed_step=failed_step,
        error_code=None if error is None else error.code if isinstance(error, GraphError) else type(error).__name__,
        started_at=record.started_at,
        finished_at=datetime.now(UTC),
    )
