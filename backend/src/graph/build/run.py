"""Build orchestration: mode selection, build records, the step functions, the step table and ``run_build``."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Final, get_args

import numpy as np

from graph.build import answered as answering
from graph.build import conform as conformance
from graph.build import diff as differ
from graph.build import edges as edge_extraction
from graph.build import estimate as estimation
from graph.build import extract as extraction
from graph.build import infer as inference
from graph.build import normalise as normalisation
from graph.build import persist as writing
from graph.build import resolve as resolution
from graph.build import terms
from graph.build import verify as verification
from graph.build.change_report import classify as classify_changes
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
    StoredResults,
    attribute_fingerprint,
    node_fingerprint,
    node_passes,
    stored_node_fragments,
)
from graph.build.metrics import quarantine_counts, refused_by_guard
from graph.build.models import (
    STEPS,
    AnsweredElsewhereRef,
    ArtifactModel,
    BuildEstimate,
    BuildMetrics,
    BuildMode,
    BuildResult,
    BuildStatus,
    DedupRefusal,
    DiffArtifact,
    DocumentRec,
    EdgesArtifact,
    FragmentRec,
    InferenceReport,
    NodesArtifact,
    PersistCursor,
    PlanArtifact,
    QuarantinedEdge,
    ResolveArtifact,
    Settlement,
    SettlementReason,
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
from graph.core.llm.usage import UsageCounts, UsageSnapshot
from graph.core.logfields import count_fields
from graph.core.ontology import FingerprintPart, Ontology
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.run")

StepFunction = Callable[[BuildContext], Awaitable[None]]

FULL_MODES: Final[frozenset[BuildMode]] = frozenset({"first", "rebuild"})
"""Modes that extract everything; an ``update`` needs the record of the last one of these."""
ACTIVE_SPEC_ID: Final = "active"


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
    """``rebuild`` when the host asks; ``first`` when the project has no persisted ``Document`` and no spec node; else
    ``update``.
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
    """An ``update`` keeps what the last ``first`` or ``rebuild`` build froze: its options (``frozen_options_changed``,
    ``fields`` names the changed options), the ontology's labels and relations (``ontology_changed``, ``part``
    ``labels`` or ``relations``) and the embedding model (``embedding_model_changed``).
    """

    if tuple(ctx.ontology.labels) != tuple(last_full.label_set):
        raise ConfigurationError("ontology_changed", part="labels")
    if last_full.relation_set is not None and relation_set(ctx.ontology) != tuple(last_full.relation_set):
        raise ConfigurationError("ontology_changed", part="relations")
    if ctx.options != last_full.options:
        now, then = (ctx.options.model_dump(mode="json"), last_full.options.model_dump(mode="json"))
        changed = sorted(name for name in now.keys() | then.keys() if now.get(name) != then.get(name))
        raise ConfigurationError("frozen_options_changed", fields=",".join(changed))
    if ctx.gateway.config.embedding_model != last_full.embedding_model:
        raise ConfigurationError("embedding_model_changed")


async def start_build(ctx: BuildContext, *, follow_up_of: str | None = None, follow_up_attempt: int = 0) -> str:
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
        follow_up_of=follow_up_of,
        follow_up_attempt=follow_up_attempt,
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
    """Plan step: start a build when ``ctx.build_id`` is None, then read the corpus snapshot, compute the change set,
    segment, select dirty fragments, group units and store the ``PlanArtifact``.
    """

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
    """``update`` builds: unchanged ``active`` fragments without stored nodes for their content under the current node
    extractor fingerprint.
    """

    if mode in FULL_MODES:
        return set()
    clean = [fragment for fragment in fragments if fragment.change == "unchanged" and fragment.status == "active"]
    if not clean:
        return set()
    stored = await stored_node_fragments(
        stored_results(ctx, mode),
        node_fingerprint(ctx.ontology, ctx.gateway, options),
        clean,
        node_passes(ctx.ontology),
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
    """Cost estimate of the build ``run_build(ctx)`` would start: the mode it would select (with the same
    ``ConfigurationError`` an ``update`` would raise), its Plan step over a dry-run corpus snapshot, the calls and input
    tokens of every step before verification and the subjects the inference limits would leave queued
    (``graph.build.estimate``; the queues of the latest finished build's infer artifact are read, exact, for an
    ``update``).
    """

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
    estimated = await estimation.estimate_steps(
        plan_artifact,
        mode=mode,
        services=StepServices(gateway=ctx.gateway, ontology=ctx.ontology, options=ctx.options),
        hubs=hubs,
        persisted=persisted,
        lookups=estimation.Lookups(
            vectors=vector_cache(ctx),
            decisions=decision_memory(ctx),
            results=stored_results(ctx, mode) if mode == "update" else None,
            attribute_fingerprint=attribute_fingerprint(ctx.ontology, ctx.gateway),
        ),
        inference=await estimation.inference_state(ctx.builds, ctx.project_id, enabled=ctx.settings.inference),
    )
    steps = estimated.steps
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
        inference_backlog=estimated.inference_backlog,
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


# 2. Extract nodes (hub pass, hub catalog and known terms, statement pass)


async def extract_nodes(ctx: BuildContext) -> None:
    """Extract nodes step: the hub pass, its hub catalog, known terms, then the statement pass, each over every window
    with a dirty fragment.
    """

    async with step_progress(ctx, "extract_nodes") as record:
        plan_artifact = await load_artifact(ctx, "plan", PlanArtifact)
        options = record.options
        persisted = (
            await terms.persisted_hubs(_executor(ctx), ctx.project_id, ctx.ontology)
            if record.mode not in FULL_MODES
            else []
        )
        artifact = await extraction.extract_nodes(
            plan_artifact,
            gateway=ctx.gateway,
            ontology=ctx.ontology,
            options=options,
            persisted_hubs=persisted,
            ranker=terms.TermRanker(plan_artifact.fragments, vector_cache(ctx), options.known_terms_limit),
            results=stored_results(ctx, record.mode),
        )
        await store_artifact(ctx, "extract_nodes", artifact)


def vector_cache(ctx: BuildContext) -> VectorCache:
    """A fresh vector cache of the context's project (one per step, so its memo lives as long as the step)."""

    return VectorCache(ctx.gateway, ctx.extraction, ctx.project_id)


def decision_memory(ctx: BuildContext) -> DecisionMemory:
    """The hub consolidation decision memory of the context's project (Resolve step 4)."""

    return DecisionMemory(ctx.extraction, ctx.project_id, ctx.gateway.config.model_for("judge"))


# 3. Resolve, 4. Extract edges, 5. Conform


async def resolve(ctx: BuildContext) -> None:
    """Resolve step."""

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
    and aliases (Resolve step 6, Conform step 1), with its values of the spine ``exclude`` attributes
    (Conform, T12), read from the current graph; None in ``first`` and ``rebuild``."""

    if record.mode != "update":
        return None
    current = await differ.load_current_graph(_executor(ctx), ctx.project_id, ctx.ontology)
    watched = sorted(ctx.ontology.spine_exclude())
    return resolution.persisted_identities(
        ((node.identity_key, node.identity_aliases, node.label) for node in current.nodes),
        {
            node.identity_key: {name: node.properties[name] for name in watched if name in node.properties}
            for node in current.nodes
            if watched
        },
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
    """Extract edges step, with the attributes pass."""

    async with step_progress(ctx, "extract_edges") as record:
        plan_artifact = await load_artifact(ctx, "plan", PlanArtifact)
        resolved = await load_artifact(ctx, "resolve", ResolveArtifact)
        nodes = await load_artifact(ctx, "extract_nodes", NodesArtifact)
        params = edge_extraction.DEFAULT_EDGE_WINDOWS
        cache = vector_cache(ctx)
        vectors = await edge_extraction.catalog_vectors(plan_artifact, resolved, ctx.ontology, cache, params)
        artifact = await edge_extraction.extract_edges(
            plan_artifact,
            resolved,
            services=StepServices(gateway=ctx.gateway, ontology=ctx.ontology, options=record.options),
            vectors=vectors,
            params=params,
            results=stored_results(ctx, record.mode),
            node_skipped=tuple(nodes.skipped),
            hub_aliases=await _persisted_hub_aliases(ctx, record, resolved),
        )
        await store_artifact(ctx, "extract_edges", artifact)


async def conform(ctx: BuildContext) -> None:
    """Conform step; statements are stamped with the build's start time."""

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
            persisted=await _persisted_identities(ctx, record),
            judging=conformance.ConformJudging(
                dedup_memory=conformance.DedupMemory(
                    ctx.extraction, ctx.project_id, ctx.gateway.config.model_for("small")
                ),
                judge_memory=_conform_judge_memory(ctx),
                answered_memory=_answered_memory(ctx),
            ),
        )
        await store_artifact(ctx, "conform", artifact)


def _conform_judge_memory(ctx: BuildContext) -> normalisation.ConformJudgeMemory | None:
    """The project's stored conform judge verdicts (``ExtractionStore``), keyed under the ontology's judge prompt
    version (``Ontology.judge_prompt_version``) and the judge model; None when the ontology has no ``conform``
    section."""

    spec = ctx.ontology.conform_spec()
    if spec is None:
        return None
    return normalisation.ConformJudgeMemory(
        ctx.extraction,
        ctx.project_id,
        ctx.ontology.judge_prompt_version(spec.judge_prompt),
        ctx.gateway.config.model_for("judge"),
    )


def _answered_memory(ctx: BuildContext) -> answering.AnsweredMemory | None:
    """The project's stored answered-elsewhere verdicts (``ExtractionStore``), keyed under the flag's judge prompt
    version (``Ontology.judge_prompt_version``) and the small model; None when the ontology has no
    ``conform.answered_elsewhere``."""

    spec = ctx.ontology.conform_spec()
    flag = spec.answered_elsewhere if spec is not None else None
    if flag is None:
        return None
    return answering.AnsweredMemory(
        ctx.extraction,
        ctx.project_id,
        ctx.ontology.judge_prompt_version(flag.prompt),
        ctx.gateway.config.model_for(answering.ANSWERED_CLASS),
    )


def _stamp(record: BuildRecord) -> str:
    return record.started_at.astimezone(UTC).isoformat(timespec="seconds")


# 6. Diff and 7. Persist


async def diff(ctx: BuildContext) -> None:
    """Diff step against the current graph (``diff.load_current_graph``; a ``first`` build has none)."""

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
    """Persist step: apply the diff batch by batch from the stored cursor."""

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
    """Infer step: runs every ontology inference rule once and swaps in the new inferred edges (``graph.build.infer``);
    off when ``BuildSettings.inference`` is false.
    """

    async with step_progress(ctx, "infer") as record:
        if not ctx.settings.inference:
            await store_artifact(ctx, "infer", InferenceReport(enabled=False))
            return
        judge_model = ctx.gateway.config.model_for("judge")
        changes = await _optional_artifact(ctx, "diff", DiffArtifact)
        graph_changed = changes is None or bool(changes.operations) or changes.spec_changes != SpecChanges()
        scope = None
        if record.mode == "update" and changes is not None:
            spec = changes.spec_changes
            scope = inference.UpdateScope(
                added=frozenset(spec.added), changed=frozenset((*spec.added, *spec.modified, *spec.rekeyed))
            )
        report = await inference.infer(
            _executor(ctx),
            ctx.project_id,
            ctx.ontology,
            JudgeRunner(ctx.gateway, ctx.ontology),
            stamp=inference.InferenceStamp(build_id=record.build_id, at=_stamp(record), judge_model=judge_model),
            memory=inference.JudgeMemory(ctx.extraction, ctx.project_id, judge_model, ctx.ontology),
            changes=inference.BuildChanges(
                graph_changed,
                scope,
                first=record.mode in FULL_MODES,
                backlog=await inference.previous_backlog(ctx.builds, ctx.project_id),
            ),
        )
        await store_artifact(ctx, "infer", report)


async def verify(ctx: BuildContext) -> None:
    """Verify step: checks the persisted graph, repairs blocking problems (at most two rounds, the deletion valve
    applies), restoring evidence from this build's diff and conform artifacts, and stores the ``VerificationReport``.
    """

    async with step_progress(ctx, "verify") as record:
        target = await _optional_artifact(ctx, "conform", TargetGraph)
        report = await verification.verify_build(
            _executor(ctx),
            ctx.project_id,
            ctx.ontology,
            build_id=record.build_id,
            registry=ctx.registry,
            target=target,
            diff=await _optional_artifact(ctx, "diff", DiffArtifact),
            inference=await _optional_artifact(ctx, "infer", InferenceReport),
        )
        report = verification.with_node_counts(
            report,
            await _optional_artifact(ctx, "extract_nodes", NodesArtifact),
            ctx.ontology,
            resolved=await _optional_artifact(ctx, "resolve", ResolveArtifact),
            plan=await _optional_artifact(ctx, "plan", PlanArtifact),
            target=target,
        )
        await store_artifact(ctx, "verify", report)


VERIFICATION_REPORTED: Final = True
JUDGING_STEPS: Final[Mapping[StepName, tuple[str, ...]]] = {
    "resolve": ("hub_consolidation",),
    "conform": ("requirement_dedup", "conform_judge", "answered_check"),
    "infer": ("inference",),
}
"""The judging steps (``BuildMetrics.judge_calls``) of each build step that judges."""
JUDGE_FAILURE_SHARE: Final = 0.1
"""A judging step whose failed judge calls exceed this share of its calls is logged as a WARNING
(``graph.build.judge_failures``); never a gate (the build goes on with what was decided)."""
"""Whether ``BuildResult.verification`` is filled from the verify artifact."""


def _verification_summary(report: VerificationReport | None) -> VerificationSummary | None:
    """The build result's verification summary; None when verify did not run."""

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
    """``changes`` plus the nodes and inferred edges verification repairs deleted. A deleted node is listed as removed,
    and no longer as added, modified, rekeyed or carried over; a deleted inferred edge is no longer listed as added.
    """

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
    """The step functions under their names (``graph.build.steps``): ``resolve``, ``conform``, ``diff`` and ``persist``
    would shadow the modules of the same name if exported from the package.
    """

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
class StepUsage:
    """LLM usage of build steps, recorded step by step (``record``): per step (``BuildMetrics.llm``) and judge calls and
    failures per judging step (``_judge_step``; ``BuildMetrics.judge_calls``, ``judge_failed_calls``).
    """

    llm: dict[str, UsageCounts] = field(default_factory=dict)
    judges: dict[str, UsageCounts] = field(default_factory=dict)

    def record(self, step: StepName, before: UsageSnapshot, after: UsageSnapshot) -> None:
        """Add the usage between the gateway snapshots ``before`` and ``after`` of ``step``."""

        delta = _usage_delta(before.total(), after.total())
        if delta.calls or delta.requests:
            self.llm[step] = delta
        for entry in after.entries:
            name = _judge_step(step, entry.prompt, entry.call_class) if entry.call_class in ("judge", "small") else None
            if name is None:
                continue
            judged = _usage_delta(before.total(call_class=entry.call_class, prompt=entry.prompt), entry)
            if judged.calls:
                known = self.judges.get(name, UsageCounts())
                self.judges[name] = UsageCounts(
                    calls=known.calls + judged.calls, failures=known.failures + judged.failures
                )

    def judge_calls(self) -> dict[str, int]:
        """``BuildMetrics.judge_calls``: judge calls per judging step."""

        return {name: counts.calls for name, counts in sorted(self.judges.items())}

    def judge_failed_calls(self) -> dict[str, int]:
        """``BuildMetrics.judge_failed_calls``: failed judge calls per judging step."""

        return {name: counts.failures for name, counts in sorted(self.judges.items())}

    def filled(self, metrics: BuildMetrics) -> BuildMetrics:
        """``metrics`` with this usage added: its steps' ``llm`` (a step ``metrics`` has keeps its own:
        ``run_build`` ran it again) and its judge calls and failures added to those ``metrics`` has."""

        def summed(own: Mapping[str, int], more: Mapping[str, int]) -> dict[str, int]:
            return {name: own.get(name, 0) + more.get(name, 0) for name in sorted({*own, *more})}

        return metrics.model_copy(
            update={
                "llm": {**self.llm, **metrics.llm},
                "judge_calls": summed(metrics.judge_calls or {}, self.judge_calls()),
                "judge_failed_calls": summed(metrics.judge_failed_calls or {}, self.judge_failed_calls()),
            }
        )


@dataclass
class _RunStats(StepUsage):
    """Durations and LLM usage of the steps run by one ``run_build`` call."""

    duration_s: dict[str, float] = field(default_factory=dict)


def _usage_delta(before: UsageCounts, after: UsageCounts) -> UsageCounts:
    return UsageCounts(
        calls=after.calls - before.calls,
        failures=after.failures - before.failures,
        requests=after.requests - before.requests,
        input_tokens=after.input_tokens - before.input_tokens,
        output_tokens=after.output_tokens - before.output_tokens,
        cached_input_tokens=after.cached_input_tokens - before.cached_input_tokens,
        duration_s=round(after.duration_s - before.duration_s, 3),
    )


MAX_FOLLOW_UPS: Final = 2
"""Follow-up ``update`` builds ``run_build`` chains after a build at most."""
FOLLOW_UP_CODE: Final = "fragment_state"
"""``Settlement.document_codes`` code of a Verify follow-up lineage (its ``fragment_state`` problem)."""
NON_RETRYABLE_CODES: Final = frozenset({"llm_refused", "llm_truncated", "llm_malformed", "llm_overflow"})
"""Skipped-fragment error codes a follow-up build would meet again on the same content, so a lineage holding only these
starts none: ``llm_refused`` (content filter or refusal), ``llm_truncated`` (still cut after halving the window
``MAX_SPLIT_DEPTH`` times, or a window of one fragment), ``llm_malformed`` (still malformed after the repair request)
and ``llm_overflow`` (the request exceeds the model's context).
"""


async def run_build(ctx: BuildContext) -> BuildResult:
    """Run every step: a new build when ``ctx.build_id`` is None, else resume that build from its first step not
    ``done`` (a finished build returns its stored result).
    """

    if ctx.build_id is None:
        ctx = ctx.with_build_id(await start_build(ctx))
    record = await require_build(ctx)
    first = record.follow_up_of or record.build_id
    attempt = record.follow_up_attempt
    stored = await ctx.builds.get_result(ctx.project_id, record.build_id) if record.finished else None
    chain = await _chain(ctx, record, stored)
    previous = await _previous_result(ctx, record, chain)
    if stored is not None:
        latest = await ctx.builds.latest_build(ctx.project_id)
        if latest is None or latest.build_id != record.build_id:
            return stored
        result = stored
    else:
        result = await _run_steps(ctx, first_open_step(record), chain)
    while (retry := _retry_lineages(result, previous, attempt)) and attempt < MAX_FOLLOW_UPS:
        attempt += 1
        retry_ctx = replace(ctx, rebuild=False, retry_lineages=retry, build_id=None)
        build_id = await start_build(retry_ctx, follow_up_of=first, follow_up_attempt=attempt)
        chain = (*chain, build_id)
        log_event(
            logger,
            INFO,
            "graph.build.follow_up_started",
            build_id=build_id,
            follow_up_of=first,
            follow_up_attempt=attempt,
            retry_lineages=len(retry),
        )
        previous, result = result, await _run_steps(retry_ctx.with_build_id(build_id), 0, chain)
    return result


def _retry_lineages(result: BuildResult, previous: BuildResult | None, attempt: int) -> tuple[str, ...]:
    """The lineages a follow-up after ``result`` retries: those of ``Settlement.documents`` with a retryable code (not
    in ``NON_RETRYABLE_CODES``; a lineage without recorded codes counts as retryable).
    """

    settlement = result.settlement
    if settlement is None or not settlement.documents:
        return ()
    if previous is not None and previous.settlement is not None:
        before = (previous.settlement.documents, previous.skipped_fragments)
        if (settlement.documents, result.skipped_fragments) == before:
            log_event(
                logger,
                INFO,
                "graph.build.follow_up_stopped",
                build_id=result.build_id,
                follow_up_attempt=attempt,
                reason="nothing_recovered",
                documents=len(settlement.documents),
            )
            return ()
    codes = settlement.document_codes
    retry = tuple(
        lineage
        for lineage in settlement.documents
        if any(code not in NON_RETRYABLE_CODES for code in codes.get(lineage, (FOLLOW_UP_CODE,)))
    )
    if len(retry) < len(settlement.documents):
        log_event(
            logger,
            INFO,
            "graph.build.follow_up_not_retryable",
            build_id=result.build_id,
            documents=len(settlement.documents) - len(retry),
        )
    return retry


async def _chain(ctx: BuildContext, record: BuildRecord, stored: BuildResult | None) -> tuple[str, ...]:
    """The follow-up builds of ``record``'s chain up to it (``Settlement.follow_up_builds``): a stored result's own
    list; none for a chain's first build; for follow-up ``n`` > 1 the list of the project's latest finished build when
    that is follow-up ``n - 1`` of the same chain (``BuildRecord.follow_up_of``), with ``record`` after it; else
    ``record`` alone.
    """

    if stored is not None and stored.settlement is not None:
        return stored.settlement.follow_up_builds
    if not record.follow_up_attempt:
        return ()
    if record.follow_up_attempt > 1:
        earlier = await ctx.builds.latest_build(ctx.project_id, finished_only=True)
        if (
            earlier is not None
            and earlier.follow_up_of == record.follow_up_of
            and earlier.follow_up_attempt == record.follow_up_attempt - 1
        ):
            found = await ctx.builds.get_result(ctx.project_id, earlier.build_id)
            if found is not None and found.settlement is not None:
                return (*found.settlement.follow_up_builds, record.build_id)
    return (record.build_id,)


async def _previous_result(ctx: BuildContext, record: BuildRecord, chain: tuple[str, ...]) -> BuildResult | None:
    """The result of the build before ``record`` in its chain (the first build for follow-up 1, the
    follow-up before it in ``chain`` for a later one); None for a chain's first build or when unknown."""

    if not record.follow_up_attempt:
        return None
    if record.follow_up_attempt == 1:
        before = record.follow_up_of
    else:
        before = chain[-2] if len(chain) > 1 and chain[-1] == record.build_id else None
    return await ctx.builds.get_result(ctx.project_id, before) if before is not None else None


async def _run_steps(ctx: BuildContext, start: int, follow_ups: tuple[str, ...]) -> BuildResult:
    """Run the steps of the context's build from ``STEPS[start]`` on and finish it (``run_build``)."""

    stats = _RunStats()
    for step in STEPS[start:]:
        before = ctx.gateway.usage()
        started = time.monotonic()
        try:
            await STEP_FUNCTIONS[step](ctx)
        except Exception as error:
            _record_step(stats, step, started, before, ctx)
            await _finish(ctx, stats, failed_step=step, error=error)
            raise
        _record_step(stats, step, started, before, ctx)
    return await _finish(ctx, stats, follow_ups=follow_ups)


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


def _record_step(stats: _RunStats, step: StepName, started: float, before: UsageSnapshot, ctx: BuildContext) -> None:
    stats.duration_s[step] = round(time.monotonic() - started, 3)
    stats.record(step, before, ctx.gateway.usage())
    _warn_judge_failures(stats, step, ctx)


def _judge_step(step: StepName, prompt: str, call_class: str = "judge") -> str | None:
    """The judging step a judge (or small-model) call of ``step`` with ``prompt`` belongs to; None for a step that
    judges nothing (the attributes calls of extract_edges are no judge).
    """

    if step == "resolve":
        return "hub_consolidation"
    if step == "conform" and prompt == conformance.DEDUP_PROMPT:
        return "requirement_dedup"
    if step == "conform":
        return "answered_check" if call_class == answering.ANSWERED_CLASS else "conform_judge"
    if step == "infer":
        return "inference"
    return None


def _warn_judge_failures(stats: _RunStats, step: StepName, ctx: BuildContext) -> None:
    """``graph.build.judge_failures`` (WARNING) for each judging step of ``step`` whose failed calls
    exceed ``JUDGE_FAILURE_SHARE`` of its calls; counts only, never a gate."""

    for name in JUDGING_STEPS.get(step, ()):
        counts = stats.judges.get(name)
        if counts is not None and counts.failures > JUDGE_FAILURE_SHARE * counts.calls:
            log_event(
                logger,
                WARNING,
                "graph.build.judge_failures",
                build_id=ctx.build_id,
                step=name,
                calls=counts.calls,
                failed_calls=counts.failures,
            )


async def _finish(
    ctx: BuildContext,
    stats: _RunStats,
    *,
    failed_step: StepName | None = None,
    error: Exception | None = None,
    follow_ups: tuple[str, ...] = (),
) -> BuildResult:
    """Store the build result and finish the record; a failure here never hides the step's error.
    ``follow_ups``: the follow-up builds of the chain so far, this one included when it is one
    (``Settlement.follow_up_builds``)."""

    record = await require_build(ctx)
    try:
        result = await _result(ctx, record, stats, failed_step, error)
        if error is None:
            result = result.model_copy(update={"settlement": await _settlement(ctx, record, result, follow_ups)})
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
        cached_input_tokens=sum(usage.cached_input_tokens for usage in metrics.llm.values()),
        duration_s=round(sum(metrics.duration_s.values()), 3),
    )
    return result


async def _settlement(
    ctx: BuildContext, record: BuildRecord, result: BuildResult, follow_ups: tuple[str, ...]
) -> Settlement:
    """The build's ``Settlement``: the lineages left incomplete (Verify follow-up lineages and the lineages of skipped
    fragments, sorted) with their codes (``FOLLOW_UP_CODE``, the skipped fragments' error codes), the subjects queued
    per rule (``InferenceReport.backlog``; none when the report has no queue) and whether a rule failed.
    """

    plan_artifact = await _optional_artifact(ctx, "plan", PlanArtifact)
    inferred = (
        await _optional_artifact(ctx, "infer", InferenceReport) if record.progress.get("infer") == "done" else None
    )
    codes: dict[str, set[str]] = {
        lineage: {FOLLOW_UP_CODE} for lineage in (result.verification.follow_up_lineages if result.verification else ())
    }
    for fragment in plan_artifact.fragments if plan_artifact is not None else ():
        code = result.skipped_fragments.get(fragment.fragment_id)
        if code is not None:
            codes.setdefault(fragment.document_id, set()).add(code)
    lineages = set(codes)
    backlog = inferred.backlog_counts() if inferred is not None else {}
    failed = inferred is not None and (
        inferred.error_code is not None or any(rule.error_code is not None for rule in inferred.rules)
    )
    reasons: list[SettlementReason] = []
    if lineages:
        reasons.append("documents_incomplete")
    if backlog:
        reasons.append("inference_backlog")
    if failed:
        reasons.append("inference_rule_failed")
    return Settlement(
        state="not_settled" if reasons else "settled",
        reasons=tuple(reasons),
        documents=tuple(sorted(lineages)),
        document_codes={lineage: tuple(sorted(found)) for lineage, found in sorted(codes.items())},
        inference_backlog=backlog,
        follow_up_builds=follow_ups,
    )


async def _spec_ids(ctx: BuildContext, diff_artifact: DiffArtifact | None) -> dict[str, str]:
    """The ``spec_id`` of each identity key, for the result's lists: the active ``spec_id`` the build's diff rows give
    the key, else the registry's (lowest active one); a key with neither is absent.
    """

    spec_ids: dict[str, str] = {}
    for row in sorted((await ctx.registry.taken(ctx.project_id)).values(), key=lambda row: row.spec_id):
        if row.state == ACTIVE_SPEC_ID:
            spec_ids.setdefault(row.identity_key, row.spec_id)
    for row in diff_artifact.registry_rows if diff_artifact else ():
        if row.state == ACTIVE_SPEC_ID:
            spec_ids[row.identity_key] = row.spec_id
        elif spec_ids.get(row.identity_key) == row.spec_id:
            del spec_ids[row.identity_key]
    return spec_ids


def _quarantine(target: TargetGraph | None, spec_ids: Mapping[str, str]) -> tuple[QuarantinedEdge, ...]:
    """The conform artifact's quarantine with its ends by ``spec_id`` (``_spec_ids``; None for a key
    without one)."""

    if target is None:
        return ()
    return tuple(
        QuarantinedEdge(
            reason=entry.reason,
            relation=entry.relation,
            source_label=entry.source_label,
            target_label=entry.target_label,
            basis=entry.basis,
            fragment_ids=entry.fragment_ids,
            source_spec_id=spec_ids.get(entry.source_key),
            target_spec_id=spec_ids.get(entry.target_key),
        )
        for entry in target.quarantine
    )


def _dedup_refused(target: TargetGraph | None, spec_ids: Mapping[str, str]) -> tuple[DedupRefusal, ...]:
    """The conform artifact's guard-refused dedup pairs with their ends by ``spec_id`` (``_spec_ids``;
    an end that is an identity alias, as a collapsed pair's are, by its node's; None for a key without
    one)."""

    if target is None:
        return ()
    owner = {alias: node.identity_key for node in target.nodes for alias in node.identity_aliases}

    def spec_id(key: str) -> str | None:
        return spec_ids.get(key) or spec_ids.get(owner.get(key, key))

    return tuple(
        DedupRefusal(
            first_spec_id=spec_id(pair.first_key),
            second_spec_id=spec_id(pair.second_key),
            label=pair.label,
            guard=pair.guard,
            name_cosine=pair.name_cosine,
            rich_cosine=pair.rich_cosine,
            collapsed=pair.collapsed,
        )
        for pair in target.dedup_refused
    )


async def _answered_elsewhere(
    ctx: BuildContext, target: TargetGraph | None, spec_ids: Mapping[str, str]
) -> tuple[AnsweredElsewhereRef, ...]:
    """The conform artifact's answered-elsewhere drops by ``spec_id``: the active one ``spec_ids``
    gives the key, else the one the registry issued for it (a persisted node the build deleted; never
    an alias row); None for a node never persisted."""

    if target is None or not target.answered_elsewhere:
        return ()
    issued: dict[str, str] = {}
    for row in sorted((await ctx.registry.taken(ctx.project_id)).values(), key=lambda row: row.spec_id):
        if row.state != "alias":
            issued.setdefault(row.identity_key, row.spec_id)
    return tuple(
        AnsweredElsewhereRef(
            spec_id=spec_ids.get(item.key) or issued.get(item.key), fragment_id=item.fragment, quote=item.quote
        )
        for item in target.answered_elsewhere
    )


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
    listed = target is not None and bool(target.quarantine or target.dedup_refused or target.answered_elsewhere)
    spec_ids = await _spec_ids(ctx, diff_artifact) if listed else {}
    # Conform's quarantine, then inference's (invalid proposals, ends already by spec_id).
    quarantine = _quarantine(target, spec_ids)
    quarantine = (*quarantine, *(inferred.quarantine if inferred is not None else ()))
    by_reason, by_pair = quarantine_counts(quarantine)
    recorded = (target is not None and "quarantine" in target.model_fields_set) or bool(
        inferred is not None and inferred.quarantine
    )
    metrics = BuildMetrics(
        fragments_by_status={key: value for key, value in sorted(by_status.items()) if value},
        units_extracted=units_extracted,
        units_reused=units_total - units_extracted,
        nodes_by_label=diff_artifact.nodes_by_label if diff_artifact else {},
        edges_by_relation=_edges_after(diff_artifact, inferred),
        dropped_edges=dropped,
        exclusive_conflicts=target.counters.exclusive_conflicts if target else 0,
        spine_unreachable=report.metrics.spine_unreachable if report else None,
        attribution_fallbacks=nodes.counters.window_fallbacks if nodes else 0,
        attribute_batches_failed=edges.counters.attribute_batches_failed if edges else 0,
        duration_s=stats.duration_s,
        quarantine_by_reason=by_reason if recorded else None,
        quarantine_by_pair=by_pair if recorded else None,
        conform_judge_calls=target.counters.conform_judge_calls if target else 0,
        dedup_refused_by_guard=refused_by_guard(target, None),
        llm=stats.llm,
        judge_calls=stats.judge_calls(),
        judge_failed_calls=stats.judge_failed_calls(),
    )
    # The change report: none for a failed or unpersisted build.
    change_report = None
    if error is None and persisted and diff_artifact is not None:
        taken = await ctx.registry.taken(ctx.project_id)
        change_report = classify_changes(diff_artifact, inferred, report, ctx.ontology, registry=taken)
    return BuildResult(
        project_id=ctx.project_id,
        build_id=record.build_id,
        mode=record.mode,
        status=status,
        settled=error is None and persisted and not (report and report.follow_up_lineages),
        spec_changes=_with_repairs(
            _with_inference(diff_artifact.spec_changes if diff_artifact else SpecChanges(), inferred), report
        ),
        quarantine=quarantine,
        dedup_refused=_dedup_refused(target, spec_ids),
        answered_elsewhere=await _answered_elsewhere(ctx, target, spec_ids),
        verification=verification,
        metrics=metrics,
        skipped_fragments=skipped,
        failed_step=failed_step,
        error_code=None if error is None else error.code if isinstance(error, GraphError) else type(error).__name__,
        started_at=record.started_at,
        finished_at=datetime.now(UTC),
        change_report=change_report,
    )
