"""Replay a step of a stored build with other settings (A/B replays)."""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shutil
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any, Final, Literal, get_args, override

from example_lib import run_example
from graph.build import answered as answering
from graph.build import conform as conformance
from graph.build import edges as edge_extraction
from graph.build import infer as inference
from graph.build import open_file_stores
from graph.build import resolve as resolution
from graph.build.changes import DEFAULT_EDGE_WINDOWS, EdgeWindowParams, NearnessMode
from graph.build.consolidate import DECISION_KIND, DecisionMemory
from graph.build.files import BUILDS_FOLDER, EXTRACTION_FOLDER, FileStores
from graph.build.metrics import (
    StepArtifacts,
    edge_metrics,
    edges_by_relation_basis,
    identity_metrics,
    naming_metrics,
    refused_by_guard,
)
from graph.build.models import (
    ArtifactModel,
    CatalogParts,
    EdgesArtifact,
    InferenceReport,
    NodesArtifact,
    PlanArtifact,
    ResolveArtifact,
    StepName,
    TargetGraph,
)
from graph.build.normalise import ConformJudgeMemory
from graph.build.options import StepServices
from graph.build.ports import BuildRecord
from graph.build.resolve import MIN_ITEMS, ConsolidationParams
from graph.build.run import ontology_fingerprint
from graph.build.vectors import VECTOR_KIND, VectorCache
from graph.core import Gateway, connect, load_ontology
from graph.core.db import Executor
from graph.core.identity import validate_project_id
from graph.core.llm.judge import JudgeFailure, JudgeRunner, Verdict
from graph.core.llm.usage import UsageSnapshot
from graph.core.ontology import InferenceRule, Ontology
from graph.core.ontology.model import CROSS_DOCUMENT
from graph_build import environment, llm_config, neo4j_config
from logger.logger import INFO, get_logger, log_event

logger = get_logger("examples.graph_replay")

ReplayStep = Literal["resolve", "conform", "edges", "infer"]
STATE_FOLDER: Final = "state"
METRICS_FILE: Final = "metrics.json"
REGISTRY_FILE: Final = "spec-ids.json"
USAGE_FIELDS: Final = ("calls", "requests", "failures", "input_tokens", "output_tokens", "cached_input_tokens")
DEFAULTS: Final = ConsolidationParams()
UNIT_WINDOWS: Final = "unit"
"""``--edge-window-tokens unit``: one edge window per unit (``EdgeWindowParams.target_tokens`` None)."""
CACHED_INPUT_WEIGHT: Final = 0.1
"""Default price of a cached input token relative to an uncached one (``input.price_weighted``)."""
STEP_ARTIFACTS: Final[Mapping[ReplayStep, tuple[StepName, ...]]] = {
    "resolve": ("resolve",),
    "conform": ("conform",),
    "edges": ("extract_edges", "conform"),
    "infer": ("infer",),
}
"""The build steps whose artifacts each replay replaces (and writes to ``--out``)."""
BUILD_SPEC: Final = re.compile(r"(?P<state>.+?):(?P<project>[^:]+?)(?::(?P<build_id>[0-9a-f]{32}))?")
"""``STATE:PROJECT[:BUILD_ID]`` as ``graph_report.py`` reads it: the state path may hold ``:``; the last
part is a build id only when it has the shape ``run_build`` gives one (32 hex digits)."""


@dataclass(frozen=True)
class BuildSpec:
    """Where a build is stored: state folder, project id, build id (None: latest finished)."""

    state: Path
    project: str
    build_id: str | None = None


def parse_build_spec(text: str) -> BuildSpec:
    found = BUILD_SPEC.fullmatch(text)
    if found is None:
        raise argparse.ArgumentTypeError(f"expected STATE:PROJECT[:BUILD_ID], got {text!r}")
    return BuildSpec(Path(found["state"]), found["project"], found["build_id"])


ANY_RULE: Final = ""
"""``InferSettings.sample`` key of a size for every listed rule (``--sample N``)."""


@dataclass(frozen=True)
class InferSettings:
    """``--step infer``: the rules to run (ids, ontology order kept; empty: every rule), the overrides of their ``cap``,
    ``pair_floor``, ``floor`` and ``batch`` (None: the ontology's), whether the limits are a ``first`` build's, and the
    subjects sampled per rule (``ANY_RULE``: each listed rule) with the sample's seed.
    """

    rules: tuple[str, ...] = ()
    cap: int | None = None
    pair_floor: float | None = None
    floor: float | None = None
    first: bool = False
    sample: tuple[tuple[str, int], ...] = ()
    seed: int = 0
    batch: int | None = None


@dataclass(frozen=True)
class ReplayRequest:
    """One replay: the stored build, the step, the output folder, the step's settings (``params``:
    ``resolve``; ``edges``: ``edges``; ``infer``: ``infer``), whether stored decisions are copied into
    the scratch state and the relative price of cached input."""

    spec: BuildSpec
    step: ReplayStep
    out: Path
    params: ConsolidationParams = field(default_factory=ConsolidationParams)
    keep_decisions: bool = False
    edges: EdgeWindowParams = DEFAULT_EDGE_WINDOWS
    infer: InferSettings = field(default_factory=InferSettings)
    cached_weight: float = CACHED_INPUT_WEIGHT
    persisted_from: str | None = None
    """``--step resolve``: the build whose conform nodes stand for the persisted graph (None: none)."""


@dataclass(frozen=True)
class _Source:
    """The stored build a replay reads (never written)."""

    stores: FileStores
    record: BuildRecord

    @property
    def project(self) -> str:
        return self.record.project_id

    @property
    def build_id(self) -> str:
        return self.record.build_id

    async def stored[ArtifactT: ArtifactModel](self, step: StepName, model: type[ArtifactT]) -> ArtifactT | None:
        payload = await self.stores.builds.get_artifact(self.project, self.build_id, step)
        return None if payload is None else model.model_validate(payload)

    async def artifact[ArtifactT: ArtifactModel](self, step: StepName, model: type[ArtifactT]) -> ArtifactT:
        found = await self.stored(step, model)
        if found is None:
            raise ValueError(f"build {self.build_id!r} of {self.project!r} has no {step} artifact")
        return found


def window_tokens(text: str) -> int | None:
    """``--edge-window-tokens``: a positive token count, or ``unit`` (None: one window per unit)."""

    if text == UNIT_WINDOWS:
        return None
    try:
        tokens = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a token count or {UNIT_WINDOWS!r}, got {text!r}") from None
    if tokens <= 0:
        raise argparse.ArgumentTypeError(f"the window target must be positive, got {tokens}")
    return tokens


def rule_ids(text: str) -> tuple[str, ...]:
    """``--rules``: comma-separated rule ids."""

    found = tuple(part.strip() for part in text.split(",") if part.strip())
    if not found:
        raise argparse.ArgumentTypeError("expected ID[,ID]")
    return found


def sample_sizes(text: str) -> tuple[tuple[str, int], ...]:
    """``--sample``: ``N`` (each listed rule) or ``ID=N[,ID=N]``."""

    found: list[tuple[str, int]] = []
    for part in (part.strip() for part in text.split(",") if part.strip()):
        rule, _, size = part.rpartition("=")
        if not size.isdigit():
            raise argparse.ArgumentTypeError(f"expected N or ID=N[,ID=N], got {text!r}")
        found.append((rule.strip(), int(size)))
    if not found:
        raise argparse.ArgumentTypeError("expected N or ID=N[,ID=N]")
    return tuple(found)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("build", type=parse_build_spec, help="STATE:PROJECT[:BUILD_ID] (default: latest finished)")
    parser.add_argument("--step", required=True, choices=get_args(ReplayStep), help="the step to replay")
    parser.add_argument("--out", required=True, type=Path, help="output folder (its 'state' must not exist yet)")
    parser.add_argument("--keep-decisions", action="store_true", help="reuse the state's stored decisions")
    parser.add_argument("--ontology", type=Path, help="ontology file (default: the shipped one)")
    parser.add_argument("--concurrency", type=int, help="LLMConfig.concurrency (default: GRAPH_LLM_CONCURRENCY)")
    parser.add_argument(
        "--cached-weight", type=float, default=CACHED_INPUT_WEIGHT, help="price of a cached input token (uncached 1)"
    )
    hubs = parser.add_argument_group("--step resolve")
    hubs.add_argument("--evidence-lines", type=int, default=DEFAULTS.evidence_lines, help="source lines per hub")
    hubs.add_argument("--block-size", type=int, default=DEFAULTS.block_size, help="hub items per block")
    hubs.add_argument("--block-overlap", type=int, default=DEFAULTS.block_overlap, help="items blocks share")
    hubs.add_argument(
        "--pair-pass", action=argparse.BooleanOptionalAction, default=DEFAULTS.pair_pass, help="run the pair pass"
    )
    hubs.add_argument("--pair-call-cap", type=int, default=DEFAULTS.pair_call_cap, help="most pair-pass calls")
    hubs.add_argument(
        "--persisted-from", metavar="BUILD_ID", help="persisted identities: that build's conform nodes (an update)"
    )
    edges = parser.add_argument_group("--step edges")
    edges.add_argument(
        "--edge-window-tokens",
        type=window_tokens,
        default=DEFAULT_EDGE_WINDOWS.target_tokens,
        help=f"edge window target in tokens, or {UNIT_WINDOWS!r} (one window per unit)",
    )
    edges.add_argument(
        "--nearness", choices=get_args(NearnessMode), default=DEFAULT_EDGE_WINDOWS.nearness, help="nearness mode"
    )
    edges.add_argument(
        "--section-hints",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_EDGE_WINDOWS.section_hints,
        help="local feature and module entries carry their section heading",
    )
    edges.add_argument(
        "--references",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_EDGE_WINDOWS.references,
        help="fill the catalog's referenced part",
    )
    infer = parser.add_argument_group("--step infer")
    infer.add_argument("--rules", type=rule_ids, default=(), help="inference rule ids, ID[,ID] (default: every rule)")
    infer.add_argument("--dry-run", action="store_true", help="no swap, no memory write (required)")
    infer.add_argument("--cap", type=int, help="subjects judged per listed rule (default: the ontology's)")
    infer.add_argument("--pair-floor", type=float, help="pair_floor of the listed rules (default: the ontology's)")
    infer.add_argument("--floor", type=float, help="every floor but cross_document links (default: the ontology's)")
    infer.add_argument("--first", action="store_true", help="the limits of a first build (first_cap, first_budget)")
    infer.add_argument("--sample", type=sample_sizes, default=(), help="subjects judged per rule: N or ID=N[,ID=N]")
    infer.add_argument("--seed", type=int, default=0, help="seed of --sample (default: 0)")
    infer.add_argument("--batch", type=int, help="batch: N for the attach_orphan rules (at least 2)")
    args = parser.parse_args(argv)
    if not 0 <= args.block_overlap < args.block_size:
        parser.error("--block-overlap must be at least 0 and below --block-size")
    if args.block_size < MIN_ITEMS or args.evidence_lines < 0 or args.pair_call_cap < 0:
        parser.error(f"--block-size must be at least {MIN_ITEMS}; --evidence-lines and --pair-call-cap at least 0")
    if args.step == "infer" and not args.dry_run:
        parser.error("--step infer replays only as a dry run: pass --dry-run")
    if (args.cap is not None and args.cap < 0) or args.cached_weight < 0:
        parser.error("--cap and --cached-weight must be at least 0")
    if args.floor is not None and not 0 <= args.floor <= 1:
        parser.error("--floor must lie in 0..1")
    if args.batch is not None and args.batch < inference.BATCH_MIN:
        parser.error(f"--batch must be at least {inference.BATCH_MIN}")
    return args


def request_of(args: argparse.Namespace) -> ReplayRequest:
    params = ConsolidationParams(
        evidence_lines=args.evidence_lines,
        block_size=args.block_size,
        block_overlap=args.block_overlap,
        pair_pass=args.pair_pass,
        pair_call_cap=args.pair_call_cap,
    )
    edges = dataclasses.replace(
        DEFAULT_EDGE_WINDOWS,
        target_tokens=args.edge_window_tokens,
        nearness=args.nearness,
        section_hints=args.section_hints,
        references=args.references,
    )
    return ReplayRequest(
        args.build,
        args.step,
        args.out,
        params,
        keep_decisions=args.keep_decisions,
        edges=edges,
        infer=InferSettings(
            tuple(args.rules),
            args.cap,
            args.pair_floor,
            floor=args.floor,
            first=args.first,
            sample=tuple(args.sample),
            seed=args.seed,
            batch=args.batch,
        ),
        cached_weight=args.cached_weight,
        persisted_from=args.persisted_from,
    )


# Scratch state


def scratch_folder(request: ReplayRequest) -> Path:
    """``<out>/state``; refused when it exists or when the output and the source state overlap."""

    state, out = request.spec.state.resolve(), request.out.resolve()
    if out.is_relative_to(state) or state.is_relative_to(out):
        raise ValueError(f"--out {request.out} and the state {request.spec.state} overlap")
    scratch = request.out / STATE_FOLDER
    if scratch.exists():
        raise ValueError(f"{scratch} exists; give a new --out")
    return scratch


async def open_source(spec: BuildSpec) -> _Source:
    """The stored build ``spec`` names (its latest finished build when it names none)."""

    stores = open_file_stores(spec.state)
    project = validate_project_id(spec.project)
    if spec.build_id is None:
        record = await stores.builds.latest_build(project, finished_only=True)
    else:
        record = await stores.builds.get_build(project, spec.build_id)
    if record is None:
        raise ValueError(f"no such build of {project!r} under {spec.state}: {spec.build_id or 'latest finished'}")
    return _Source(stores, record)


def copy_state(source: Path, scratch: Path, record: BuildRecord, *, keep_decisions: bool) -> list[str]:
    """Copy what a replay reads into ``scratch``: the cached vectors (and the stored decisions with
    ``keep_decisions``), the project's spec-id registry and the build's folder. Returns the extraction
    kinds copied."""

    kinds = [VECTOR_KIND, *([DECISION_KIND] if keep_decisions else [])]
    copied: list[str] = []
    for kind in kinds:
        folder = source / EXTRACTION_FOLDER / kind
        if folder.is_dir():
            shutil.copytree(folder, scratch / EXTRACTION_FOLDER / kind)
            copied.append(kind)
    project = record.project_id
    registry = source / project / REGISTRY_FILE
    (scratch / project).mkdir(parents=True, exist_ok=True)
    if registry.is_file():
        shutil.copy2(registry, scratch / project / REGISTRY_FILE)
    build = Path(project, BUILDS_FOLDER, record.build_id)
    shutil.copytree(source / build, scratch / build)
    return copied


# Replays


def stamp(record: BuildRecord) -> str:
    """The statements' stamp of the build (its start time, as the Conform step gives it)."""

    return record.started_at.astimezone(UTC).isoformat(timespec="seconds")


def services_of(source: _Source, gateway: Gateway, ontology: Ontology) -> StepServices:
    return StepServices(gateway=gateway, ontology=ontology, options=source.record.options)


async def persisted_from(source: _Source, build_id: str) -> dict[str, resolution.PersistedIdentity]:
    """The identities of ``build_id``'s conform nodes as Resolve's ``persisted`` mapping."""

    payload = await source.stores.builds.get_artifact(source.project, build_id, "conform")
    if payload is None:
        raise ValueError(f"build {build_id!r} of {source.project!r} has no conform artifact")
    nodes = payload.get("nodes")
    rows = nodes if isinstance(nodes, list) else []
    return resolution.persisted_identities(
        (str(row["identity_key"]), tuple(row.get("identity_aliases") or ()), str(row["label"]))
        for row in rows
        if isinstance(row, dict)
    )


async def replay_resolve(
    source: _Source,
    scratch: FileStores,
    gateway: Gateway,
    ontology: Ontology,
    params: ConsolidationParams,
    *,
    persisted: Mapping[str, resolution.PersistedIdentity] | None = None,
) -> ResolveArtifact:
    plan = await source.artifact("plan", PlanArtifact)
    nodes = await source.artifact("extract_nodes", NodesArtifact)
    return await resolution.resolve(
        plan,
        nodes,
        gateway=gateway,
        ontology=ontology,
        vectors=VectorCache(gateway, scratch.extraction, source.project),
        memory=DecisionMemory(scratch.extraction, source.project, gateway.config.model_for("judge")),
        persisted=persisted,
        params=params,
    )


async def replay_conform(
    source: _Source, scratch: FileStores, gateway: Gateway, ontology: Ontology, edges: EdgesArtifact | None = None
) -> TargetGraph:
    """Conform on the build's plan and resolve artifacts and ``edges`` (default: the build's), its judge
    memories over the scratch state."""

    spec = ontology.conform_spec()
    judge_model = gateway.config.model_for("judge")
    answered = spec.answered_elsewhere if spec is not None else None
    return await conformance.conform(
        await source.artifact("plan", PlanArtifact),
        await source.artifact("resolve", ResolveArtifact),
        edges if edges is not None else await source.artifact("extract_edges", EdgesArtifact),
        services=services_of(source, gateway, ontology),
        vectors=VectorCache(gateway, scratch.extraction, source.project),
        at=stamp(source.record),
        judging=conformance.ConformJudging(
            dedup_memory=conformance.DedupMemory(scratch.extraction, source.project, gateway.config.model_for("small")),
            judge_memory=None
            if spec is None
            else ConformJudgeMemory(scratch.extraction, source.project, spec.judge_prompt, judge_model),
            answered_memory=None
            if answered is None
            else answering.AnsweredMemory(
                scratch.extraction,
                source.project,
                answered.prompt,
                gateway.config.model_for(answering.ANSWERED_CLASS),
            ),
        ),
    )


async def replay_edges(
    source: _Source, scratch: FileStores, gateway: Gateway, ontology: Ontology, params: EdgeWindowParams
) -> EdgesArtifact:
    """``extract_edges`` without a store (every window called) with the build's attributes."""

    plan = await source.artifact("plan", PlanArtifact)
    resolved = await source.artifact("resolve", ResolveArtifact)
    built = await source.artifact("extract_edges", EdgesArtifact)
    vectors = await edge_extraction.catalog_vectors(
        plan, resolved, ontology, VectorCache(gateway, scratch.extraction, source.project), params
    )
    if source.record.mode == "update":
        plan = plan.model_copy(update={"dirty_ids": tuple(fragment.fragment_id for fragment in plan.fragments)})
    return await edge_extraction.replay_edges(
        plan,
        resolved,
        services=services_of(source, gateway, ontology),
        vectors=vectors,
        attributes=edge_extraction.attributes_from(built),
        params=params,
    )


class CountingJudge(JudgeRunner):
    """A ``JudgeRunner`` that counts, per prompt, the member ``pairs`` and orphan ``owners`` its
    payloads show (``cross_document`` rules; other payloads count none)."""

    def __init__(self, gateway: Gateway, ontology: Ontology) -> None:
        super().__init__(gateway, ontology)
        self.pairs: Counter[str] = Counter()
        self.owners: Counter[str] = Counter()

    @override
    async def judge[VerdictT: Verdict](
        self, prompt: str, payload: Mapping[str, Any], verdict_type: type[VerdictT]
    ) -> VerdictT | JudgeFailure:
        for name, counter in (("pairs", self.pairs), ("owners", self.owners)):
            shown = payload.get(name)
            if isinstance(shown, list):
                counter[prompt] += len(shown)
        return await super().judge(prompt, payload, verdict_type)


def selected_ontology(ontology: Ontology, settings: InferSettings) -> Ontology:
    """``ontology`` with only the rules ``settings`` lists (ontology order), their overrides applied:
    ``cap``, ``pair_floor``, ``floor`` (every rule's and ``inference.floor``; a ``cross_document`` rule
    keeps its link floor) and ``batch`` (rules that name a ``batch_prompt``)."""

    spec = ontology.inference
    rules = spec.rules
    known = {rule.id for rule in rules}
    unknown = [rule_id for rule_id in settings.rules if rule_id not in known]
    if unknown:
        raise ValueError(f"unknown inference rules {unknown}; the ontology has {sorted(known)}")
    wanted = set(settings.rules) or known
    overrides: dict[str, Any] = {}
    if settings.cap is not None:
        overrides["cap"] = settings.cap
    if settings.pair_floor is not None:
        overrides["pair_floor"] = settings.pair_floor

    def overridden(rule: InferenceRule) -> InferenceRule:
        own: dict[str, Any] = dict(overrides)
        if settings.floor is not None:
            own["floor"] = spec.floor_of(rule) if rule.template == CROSS_DOCUMENT else settings.floor
        if settings.batch is not None and rule.batch_prompt is not None:
            own["batch"] = settings.batch
        return dataclasses.replace(rule, **own)

    chosen = tuple(overridden(rule) for rule in rules if rule.id in wanted)
    floor = spec.floor if settings.floor is None else settings.floor
    return dataclasses.replace(ontology, inference=dataclasses.replace(spec, rules=chosen, floor=floor))


def changes_of(ontology: Ontology, settings: InferSettings) -> inference.BuildChanges:
    """What the dry run judges under: a ``first`` build's limits (``--first``) and the subjects sampled
    per rule of ``ontology`` (``selected_ontology``; ``--sample``, ``--seed``)."""

    ids = [rule.id for rule in ontology.inference.rules]
    sizes: dict[str, int] = {}
    for rule_id, size in settings.sample:
        if rule_id == ANY_RULE:
            sizes.update(dict.fromkeys(ids, size))
        elif rule_id not in ids:
            raise ValueError(f"--sample names {rule_id!r}, not a rule of the replay ({', '.join(ids)})")
        else:
            sizes[rule_id] = size
    sample = inference.SubjectSample(sizes, seed=settings.seed) if sizes else None
    return inference.BuildChanges(first=settings.first, sample=sample)


async def replay_infer(
    source: _Source,
    scratch: FileStores,
    judge: JudgeRunner,
    ontology: Ontology,
    executor: Executor,
    *,
    judge_model: str,
    keep_decisions: bool,
    changes: inference.BuildChanges | None = None,
) -> InferenceReport:
    """A dry run of ``infer`` over the build's project (``write=False``) under ``changes``
    (``changes_of``; default: an ``update``'s limits, every subject)."""

    return await inference.infer(
        executor,
        source.project,
        ontology,
        judge,
        stamp=inference.InferenceStamp(build_id=source.build_id, at=stamp(source.record), judge_model=judge_model),
        memory=inference.JudgeMemory(scratch.extraction if keep_decisions else None, source.project, judge_model),
        changes=changes,
        write=False,
    )


# Metrics


def usage_counts(snapshot: UsageSnapshot) -> dict[str, dict[str, dict[str, int]]]:
    """Calls, requests, failures and tokens per prompt and per call class."""

    out: dict[str, dict[str, dict[str, int]]] = {"by_prompt": {}, "by_class": {}}
    for entry in snapshot.entries:
        for group, key in (("by_prompt", entry.prompt), ("by_class", entry.call_class)):
            slot = out[group].setdefault(key, dict.fromkeys(USAGE_FIELDS, 0))
            for name in USAGE_FIELDS:
                slot[name] += getattr(entry, name)
    return out


def input_costs(counts: Iterable[Mapping[str, int]], cached_weight: float) -> dict[str, float]:
    """Uncached, cached and price-weighted input tokens (cached weighted ``cached_weight``) of ``counts``."""

    total = cached = 0
    for item in counts:
        total += item.get("input_tokens", 0)
        cached += item.get("cached_input_tokens", 0)
    uncached = total - cached
    return {"uncached": uncached, "cached": cached, "price_weighted": round(uncached + cached_weight * cached, 1)}


def input_of(usage: Mapping[str, Mapping[str, Mapping[str, int]]], cached_weight: float) -> dict[str, Any]:
    """``input_costs`` of the whole usage and per prompt."""

    prompts = usage.get("by_prompt", {})
    return {
        "cached_weight": cached_weight,
        "total": input_costs(prompts.values(), cached_weight),
        "by_prompt": {prompt: input_costs([counts], cached_weight) for prompt, counts in sorted(prompts.items())},
    }


def resolve_side(
    nodes: NodesArtifact, plan: PlanArtifact, resolved: ResolveArtifact, ontology: Ontology
) -> dict[str, Any]:
    return {
        "canonical": len(resolved.canonical),
        "merges": len(resolved.merges),
        "pair_candidates": len(resolved.pair_candidates),
        "counters": resolved.counters.model_dump(mode="json"),
        "identity": identity_metrics(resolved, None),
        "naming": naming_metrics(nodes, resolved, ontology, plan),
    }


def conform_side(resolved: ResolveArtifact, target: TargetGraph) -> dict[str, Any]:
    return {
        "nodes": len(target.nodes),
        "edges": len(target.edges),
        "dedup_merges": target.counters.dedup_merges,
        "dedup_refused": len(target.dedup_refused),
        "counters": target.counters.model_dump(mode="json"),
        "identity": identity_metrics(resolved, target),
    }


def _sizes(counts: Iterable[int]) -> dict[str, int]:
    found = list(counts)
    return {"total": sum(found), "max": max(found, default=0)}


def catalog_sizes(edges: EdgesArtifact) -> dict[str, Any]:
    """Catalog entries over the units (total, largest unit): the whole catalog and each foreign part
    (``CatalogParts``; None each in an artifact that does not record them)."""

    parts = edges.catalog_parts
    out: dict[str, Any] = {"units": len(edges.catalogs), "catalog": _sizes(len(c) for c in edges.catalogs.values())}
    for name in CatalogParts.model_fields:
        out[name] = None if parts is None else _sizes(len(getattr(part, name)) for part in parts.values())
    return out


def edges_side(
    plan: PlanArtifact, resolved: ResolveArtifact, edges: EdgesArtifact, target: TargetGraph, ontology: Ontology
) -> dict[str, Any]:
    """Raw and target edges, edges per relation and basis, the edge metrics of ``graph.build.metrics``
    (requirements without an edge, foreign edges by path, window counters, ...), catalog sizes, the
    extract-edges counters and Conform's side."""

    shape, _ = edge_metrics(target, None, ontology, plan=plan, steps=StepArtifacts(resolved=resolved, edges=edges))
    return {
        "raw_edges": len(edges.raw_edges),
        "edges": len(target.edges),
        "edges_by_relation": edges_by_relation_basis(target),
        "edge_metrics": shape,
        "catalog_sizes": catalog_sizes(edges),
        "edge_counters": edges.counters.model_dump(mode="json"),
        "conform": conform_side(resolved, target),
    }


def rule_side(
    report: InferenceReport,
    ontology: Ontology,
    judge: CountingJudge,
    usage: Mapping[str, Mapping[str, Mapping[str, int]]],
    cached_weight: float,
) -> dict[str, Any]:
    """Per rule of the dry run: its report, its calls (``judged`` + ``failed``), its prompt, and the member pairs and
    owner questions its payloads showed and its input, counted by prompt: when rules share a prompt those are the
    prompt's (``prompt_rules`` lists the rules sharing it).
    """

    prompts = {rule.id: rule.prompt for rule in ontology.inference.rules}
    sharing: dict[str, list[str]] = {}
    for rule in report.rules:
        sharing.setdefault(prompts.get(rule.rule, ""), []).append(rule.rule)
    by_prompt = usage.get("by_prompt", {})
    out: dict[str, Any] = {}
    for rule in report.rules:
        prompt = prompts.get(rule.rule, "")
        out[rule.rule] = {
            **rule.model_dump(mode="json"),
            "calls": rule.judged + rule.failed,
            "prompt": prompt,
            "prompt_rules": sharing[prompt],
            "pairs": judge.pairs[prompt],
            "owner_questions": judge.owners[prompt],
            "input": input_costs([by_prompt.get(prompt, {})], cached_weight),
        }
    return out


def warnings_of(
    record: BuildRecord, gateway: Gateway, ontology: Ontology, persisted_build: str | None = None
) -> list[str]:
    found = []
    if persisted_build is not None:
        found.append(f"persisted graph: the conform nodes of build {persisted_build} (identities only)")
    elif record.mode == "update":
        found.append("update build replayed without the persisted graph (first-build assumption)")
    if record.ontology_fingerprint != ontology_fingerprint(ontology):
        found.append("ontology differs from the build's")
    if record.embedding_model != gateway.config.embedding_model:
        found.append("embedding model differs from the build's (no cached vector is reused)")
    return found


async def infer_warnings(source: _Source, ontology: Ontology, settings: InferSettings) -> list[str]:
    """What a dry run reads besides the build: a later build's graph; rules left out."""

    found = []
    latest = await source.stores.builds.latest_build(source.project, finished_only=True)
    if latest is not None and latest.build_id != source.build_id:
        found.append(f"the project's graph is that of a later build ({latest.build_id})")
    left_out = [rule.id for rule in ontology.inference.rules if settings.rules and rule.id not in settings.rules]
    if left_out:
        found.append(f"rules left out (their accepted owners are not counted): {', '.join(left_out)}")
    return found


def dumped(artifact: ArtifactModel | None) -> Any:
    return None if artifact is None else artifact.model_dump(mode="json")


async def _patch_result(scratch: FileStores, record: BuildRecord, target: TargetGraph) -> None:
    """The copied result with ``dedup_refused_by_guard`` of the replayed conform artifact."""

    result = await scratch.builds.get_result(record.project_id, record.build_id)
    if result is None or result.metrics.dedup_refused_by_guard is None:
        return
    metrics = result.metrics.model_copy(update={"dedup_refused_by_guard": refused_by_guard(target)})
    await scratch.builds.finish_build(result.model_copy(update={"metrics": metrics}))


@dataclass
class _Replayed:
    """What one replay gives: its artifacts by step, the build's, both sides' metrics and more."""

    new: dict[StepName, ArtifactModel]
    old: dict[StepName, ArtifactModel | None]
    sides: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)


async def _replay_resolve_step(
    request: ReplayRequest, source: _Source, scratch: FileStores, gateway: Gateway, ontology: Ontology
) -> _Replayed:
    plan = await source.artifact("plan", PlanArtifact)
    nodes = await source.artifact("extract_nodes", NodesArtifact)
    persisted = await persisted_from(source, request.persisted_from) if request.persisted_from else None
    new = await replay_resolve(source, scratch, gateway, ontology, request.params, persisted=persisted)
    old = await source.stored("resolve", ResolveArtifact)
    sides = {
        "build": None if old is None else resolve_side(nodes, plan, old, ontology),
        "replay": resolve_side(nodes, plan, new, ontology),
    }
    return _Replayed({"resolve": new}, {"resolve": old}, sides)


async def _replay_conform_step(source: _Source, scratch: FileStores, gateway: Gateway, ontology: Ontology) -> _Replayed:
    resolved = await source.artifact("resolve", ResolveArtifact)
    new = await replay_conform(source, scratch, gateway, ontology)
    old = await source.stored("conform", TargetGraph)
    sides = {
        "build": None if old is None else conform_side(resolved, old),
        "replay": conform_side(resolved, new),
    }
    await _patch_result(scratch, source.record, new)
    return _Replayed({"conform": new}, {"conform": old}, sides)


async def _replay_edges_step(
    request: ReplayRequest, source: _Source, scratch: FileStores, gateway: Gateway, ontology: Ontology
) -> _Replayed:
    plan = await source.artifact("plan", PlanArtifact)
    resolved = await source.artifact("resolve", ResolveArtifact)
    edges = await replay_edges(source, scratch, gateway, ontology, request.edges)
    target = await replay_conform(source, scratch, gateway, ontology, edges)
    old_edges = await source.artifact("extract_edges", EdgesArtifact)
    old_target = await source.stored("conform", TargetGraph)
    sides = {
        "build": None if old_target is None else edges_side(plan, resolved, old_edges, old_target, ontology),
        "replay": edges_side(plan, resolved, edges, target, ontology),
    }
    await _patch_result(scratch, source.record, target)
    return _Replayed(
        {"extract_edges": edges, "conform": target}, {"extract_edges": old_edges, "conform": old_target}, sides
    )


async def _replay_infer_step(
    request: ReplayRequest,
    source: _Source,
    scratch: FileStores,
    gateway: Gateway,
    ontology: Ontology,
    *,
    executor: Executor | None,
) -> _Replayed:
    if executor is None:
        raise ValueError("--step infer needs Neo4j (an executor)")
    chosen = selected_ontology(ontology, request.infer)
    judge = CountingJudge(gateway, chosen)
    before = usage_counts(gateway.usage())
    report = await replay_infer(
        source,
        scratch,
        judge,
        chosen,
        executor,
        judge_model=gateway.config.model_for("judge"),
        keep_decisions=request.keep_decisions,
        changes=changes_of(chosen, request.infer),
    )
    usage = _usage_delta(before, usage_counts(gateway.usage()))
    old = await source.stored("infer", InferenceReport)
    sides = {
        "build": None if old is None else {rule.rule: rule.model_dump(mode="json") for rule in old.rules},
        "replay": rule_side(report, chosen, judge, usage, request.cached_weight),
    }
    extra = {
        "dry_run": True,
        "judged_items": None if report.judged_items is None else len(report.judged_items),
        "would_add": len(report.inferred_added),
        "would_remove": len(report.inferred_removed),
        "quarantine": len(report.quarantine),
        "spine_unreachable_before": report.spine_unreachable_before,
        "spine_unreachable_after": report.spine_unreachable_after,
        "warnings": await infer_warnings(source, ontology, request.infer),
    }
    return _Replayed({"infer": report}, {"infer": old}, sides, extra)


def _settings(request: ReplayRequest) -> dict[str, Any] | None:
    if request.step == "resolve":
        return asdict(request.params)
    if request.step == "edges":
        return asdict(request.edges)
    if request.step == "infer":
        return asdict(request.infer)
    return None


async def replay(
    request: ReplayRequest, gateway: Gateway, ontology: Ontology, *, executor: Executor | None = None
) -> dict[str, Any]:
    """Run one replay (module docstring); returns what ``metrics.json`` holds. ``executor``: the Neo4j
    the ``infer`` step reads (only it)."""

    scratch_root = scratch_folder(request)
    source = await open_source(request.spec)
    copied = copy_state(request.spec.state, scratch_root, source.record, keep_decisions=request.keep_decisions)
    scratch = open_file_stores(scratch_root)
    before = usage_counts(gateway.usage())
    if request.step == "resolve":
        replayed = await _replay_resolve_step(request, source, scratch, gateway, ontology)
    elif request.step == "conform":
        replayed = await _replay_conform_step(source, scratch, gateway, ontology)
    elif request.step == "edges":
        replayed = await _replay_edges_step(request, source, scratch, gateway, ontology)
    else:
        replayed = await _replay_infer_step(request, source, scratch, gateway, ontology, executor=executor)
    for step, artifact in replayed.new.items():
        await scratch.builds.put_artifact(source.project, source.build_id, step, dumped(artifact))
        (request.out / f"{step}.json").write_text(json.dumps(dumped(artifact), indent=1, ensure_ascii=False))
    usage = _usage_delta(before, usage_counts(gateway.usage()))
    extra = dict(replayed.extra)
    persisted_build = request.persisted_from if request.step == "resolve" else None
    warnings = [*warnings_of(source.record, gateway, ontology, persisted_build), *extra.pop("warnings", [])]
    metrics = {
        "source": {
            "state": str(request.spec.state),
            "project": source.project,
            "build_id": source.build_id,
            "mode": source.record.mode,
        },
        "step": request.step,
        "params": _settings(request),
        "extraction_copied": copied,
        "warnings": warnings,
        "same_as_build": all(dumped(replayed.old[step]) == dumped(new) for step, new in replayed.new.items()),
        **extra,
        **replayed.sides,
        "usage": usage,
        "input": input_of(usage, request.cached_weight),
        "report_state": f"{scratch_root}:{source.project}:{source.build_id}",
    }
    (request.out / METRICS_FILE).write_text(json.dumps(metrics, indent=1, ensure_ascii=False))
    log_event(logger, INFO, "example.graph_replay.done", step=request.step, build_id=source.build_id)
    return metrics


def _usage_delta(
    before: Mapping[str, Mapping[str, Mapping[str, int]]], after: Mapping[str, Mapping[str, Mapping[str, int]]]
) -> dict[str, dict[str, dict[str, int]]]:
    out: dict[str, dict[str, dict[str, int]]] = {}
    for group, items in after.items():
        out[group] = {}
        for key, counts in items.items():
            prior = before.get(group, {}).get(key, {})
            delta = {name: counts[name] - prior.get(name, 0) for name in USAGE_FIELDS}
            if any(delta.values()):
                out[group][key] = delta
    return out


def summary_of(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """What ``main`` prints: the step, whether it equals the build, warnings, report state, usage by
    class, total input and the replay side's scalar values (per rule for ``infer``)."""

    summary = {key: metrics[key] for key in ("step", "same_as_build", "warnings", "report_state")}
    summary["usage_by_class"] = metrics["usage"].get("by_class", {})
    summary["input"] = metrics["input"]["total"]
    side: Mapping[str, Any] = metrics["replay"]
    if metrics["step"] == "infer":
        summary["replay"] = {
            rule: {key: value for key, value in found.items() if not isinstance(value, dict)}
            for rule, found in side.items()
        }
    else:
        summary["replay"] = {key: value for key, value in side.items() if not isinstance(value, dict)}
    return summary


async def main() -> None:
    args = parse_args()
    env = environment()
    llm = llm_config(env)
    if args.concurrency:
        llm = llm.model_copy(update={"concurrency": args.concurrency})
    ontology = load_ontology(args.ontology)
    gateway = Gateway(llm)
    driver = None
    try:
        executor = None
        if args.step == "infer":
            neo4j = neo4j_config(env)
            driver = connect(neo4j)
            executor = Executor(driver, neo4j.database)
        metrics = await replay(request_of(args), gateway, ontology, executor=executor)
    finally:
        await gateway.aclose()
        if driver is not None:
            await driver.close()
    print(json.dumps(summary_of(metrics), indent=1))


if __name__ == "__main__":
    raise SystemExit(run_example(main))
