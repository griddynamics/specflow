"""Evaluate graph builds over corpus snapshots: speed, calls, tokens and the estimate per step."""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import resource
import shutil
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, override

import openai

from example_lib import capture_failure, run_example
from graph.build import (
    STEPS,
    BuildContext,
    BuildResult,
    BuildSettings,
    estimate_build,
    open_file_stores,
    run_build,
    steps,
)
from graph.build.conform import DEDUP_PROMPT
from graph.build.consolidate import HUBS_PROMPT
from graph.build.hubjudge import TWINS_PROMPT
from graph.build.prompts import (
    ATTRIBUTES_PROMPT,
    EDGE_WINDOW_PROMPT,
    EDGE_WINDOW_WARM_UP_PROMPT,
    NODE_HUBS_PROMPT,
    NODE_STATEMENTS_PROMPT,
    NODE_WARM_UP_PROMPT,
)
from graph.build.run import StepUsage
from graph.core import Gateway, GraphReader, Query, ensure_schema, load_ontology
from graph.core.ontology import Ontology
from graph.core.ontology.model import CROSS_DOCUMENT
from graph.core.store import Executor
from graph.stores import build_store
from graph_build import environment, llm_config, store_config

ESTIMATE_STEP = {
    NODE_HUBS_PROMPT: "node_extraction",
    NODE_STATEMENTS_PROMPT: "node_extraction",
    NODE_WARM_UP_PROMPT: "node_extraction",
    HUBS_PROMPT: "hub_consolidation",
    TWINS_PROMPT: "hub_consolidation",
    EDGE_WINDOW_PROMPT: "edge_extraction",
    EDGE_WINDOW_WARM_UP_PROMPT: "edge_extraction",
    ATTRIBUTES_PROMPT: "attributes",
    DEDUP_PROMPT: "requirement_dedup",
    "embed": "embeddings",
}
"""Estimate step of each prompt the build calls; the conform judge's, the answered check's and the
inference rules' prompts come from the ontology (``estimate_steps_of``)."""
TOP_K = 5

CALL: contextvars.ContextVar[str] = contextvars.ContextVar("call", default="other")


class Probe:
    """Per-request records (step, call class, status, latency) and backoff waits; the current step is set
    by the runner. One build at a time."""

    def __init__(self, progress: Path | None) -> None:
        self.step, self.build = "-", "-"
        self.requests: list[dict[str, Any]] = []
        self.waits: list[tuple[str, float]] = []
        self.progress = progress

    def log(self, event: str, **fields: Any) -> None:
        if self.progress is not None:
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            extra = " ".join(f"{key}={value}" for key, value in fields.items())
            with self.progress.open("a") as file:
                file.write(f"{stamp} {self.build} {self.step} {event} {extra}".rstrip() + "\n")

    async def sleep(self, delay: float) -> None:
        self.waits.append((self.step, delay))
        await asyncio.sleep(delay)

    def client(self) -> openai.DefaultAsyncHttpxClient:
        probe = self

        class TimedClient(openai.DefaultAsyncHttpxClient):
            @override
            async def send(self, request: Any, **kwargs: Any) -> Any:
                started, status = time.monotonic(), "error"
                try:
                    response = await super().send(request, **kwargs)
                except Exception as error:
                    status = type(error).__name__
                    raise
                else:
                    status = str(response.status_code)
                    return response
                finally:
                    probe.requests.append(
                        {
                            "step": probe.step,
                            "call": CALL.get(),
                            "status": status,
                            "start": started,
                            "s": time.monotonic() - started,
                        }
                    )
                    if len(probe.requests) % 50 == 0:
                        probe.log("requests", total=len(probe.requests))

        return TimedClient()


class TimedGateway(Gateway):
    """Tags every request with its call class (read by the HTTP client) and times logical calls."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.logical: list[dict[str, Any]] = []

    @override
    async def structured(self, call_class: Any, model: Any, messages: Any, output_type: Any, *, prompt: str) -> Any:
        token, started = CALL.set(call_class), time.monotonic()
        try:
            return await super().structured(call_class, model, messages, output_type, prompt=prompt)
        finally:
            self.logical.append({"call": call_class, "s": time.monotonic() - started})
            CALL.reset(token)

    @override
    async def warm_up(self, call_class: Any, model: Any, messages: Any, output_type: Any, *, prompt: str) -> None:
        token = CALL.set(call_class)
        try:
            await super().warm_up(call_class, model, messages, output_type, prompt=prompt)
        finally:
            CALL.reset(token)

    @override
    async def embed(self, texts: Any) -> list[list[float]]:
        token = CALL.set("embedding")
        try:
            return await super().embed(texts)
        finally:
            CALL.reset(token)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshots", nargs="*", help="corpus folders in build order; 'rebuild:<folder>' rebuilds")
    parser.add_argument("--project", required=True, help="project id (use a throwaway one)")
    parser.add_argument("--state", required=True, type=Path, help="folder of the file stores")
    parser.add_argument("--queries", type=Path, help="JSON list of {text, spec_id}, run after the last build")
    parser.add_argument("--out", type=Path, help="write everything as JSON here")
    parser.add_argument("--progress", type=Path, help="append one line per event here")
    parser.add_argument("--label", default="B", help="build label (prefix of a numbered one for several snapshots)")
    parser.add_argument("--concurrency", type=int, help="LLMConfig.concurrency (default: GRAPH_LLM_CONCURRENCY)")
    parser.add_argument("--no-inference", action="store_true", help="BuildSettings(inference=False)")
    parser.add_argument("--estimate-only", action="store_true", help="run only estimate_build per snapshot")
    parser.add_argument("--cleanup", action="store_true", help="delete the project and the state folder at the end")
    return parser.parse_args()


def latency(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def pick(share: float) -> float:
        return ordered[min(len(ordered) - 1, int(share * len(ordered)))]

    return {
        "n": len(ordered),
        "mean": round(statistics.fmean(ordered), 2),
        "p50": round(pick(0.5), 2),
        "p95": round(pick(0.95), 2),
        "max": round(ordered[-1], 2),
    }


def usage_delta(before: Any, after: Any) -> dict[str, dict[str, dict[str, int]]]:
    """Calls, requests, failures and tokens (input, output and cached input) per call class and per prompt
    between two usage snapshots."""

    old = {(entry.call_class, entry.prompt): entry for entry in before.entries}
    out: dict[str, dict[str, dict[str, int]]] = {"by_class": {}, "by_prompt": {}}
    for entry in after.entries:
        prior = old.get((entry.call_class, entry.prompt))
        delta = {
            name: getattr(entry, name) - (getattr(prior, name) if prior else 0)
            for name in ("calls", "requests", "failures", "input_tokens", "output_tokens", "cached_input_tokens")
        }
        if not any(delta.values()):
            continue
        for key, group in ((entry.call_class, "by_class"), (entry.prompt, "by_prompt")):
            slot = out[group].setdefault(key, Counter())
            slot.update(delta)
    return {group: {key: dict(value) for key, value in items.items()} for group, items in out.items()}


def step_stats(
    probe: Probe, gateway: TimedGateway, step: str, wall: float, marks: tuple[int, int, int]
) -> dict[str, Any]:
    requests = [item for item in probe.requests[marks[0] :] if item["step"] == step]
    by_class: dict[str, list[float]] = defaultdict(list)
    for item in requests:
        by_class[item["call"]].append(item["s"])
    return {
        "wall_s": round(wall, 2),
        "http_latency": {name: latency(values) for name, values in sorted(by_class.items())},
        "logical_latency": latency([item["s"] for item in gateway.logical[marks[1] :]]),
        "statuses": dict(Counter(item["status"] for item in requests)),
        "backoff_waits": {"n": len(probe.waits) - marks[2], "s": round(sum(d for _, d in probe.waits[marks[2] :]), 1)},
        "in_flight_mean": round(sum(item["s"] for item in requests) / wall, 2) if wall else 0.0,
    }


def counters(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    out: dict[str, Any] = {
        key: payload[key] for key in ("counters", "batches", "rules", "found", "rounds") if key in payload
    }
    if payload.get("skipped"):
        out["skipped"] = dict(Counter(payload["skipped"].values()))
    return out


def estimate_steps_of(ontology: Ontology) -> dict[str, str]:
    """``ESTIMATE_STEP`` plus the ontology's conform judge prompt (``conform.judge_prompt``) as ``conform_judge``, its
    answered-elsewhere prompt (``conform.answered_elsewhere.prompt``) as ``answered_check`` and each inference rule's
    prompt (and batch prompt) as ``cross_document_inference`` (template ``cross_document``) or ``inference`` (every
    other template).
    """

    inference = {
        prompt: "cross_document_inference" if rule.template == CROSS_DOCUMENT else "inference"
        for rule in ontology.inference.rules
        for prompt in (rule.prompt, rule.batch_prompt)
        if prompt is not None
    }
    spec = ontology.conform_spec()
    if spec is None:
        return {**ESTIMATE_STEP, **inference}
    answered = {spec.answered_elsewhere.prompt: "answered_check"} if spec.answered_elsewhere else {}
    return {**ESTIMATE_STEP, spec.judge_prompt: "conform_judge", **answered, **inference}


def estimate_vs_actual(
    estimate: Any, by_prompt: dict[str, dict[str, int]], step_of: dict[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    step_of = step_of or ESTIMATE_STEP
    actual: dict[str, Counter[str]] = defaultdict(Counter)
    for prompt, counts in by_prompt.items():
        actual[step_of.get(prompt, "not_estimated:" + prompt)].update(counts)
    names = sorted({*estimate.steps, *actual})
    return {
        name: {
            "estimate": [estimate.steps[name].calls, estimate.steps[name].input_tokens]
            if name in estimate.steps
            else None,
            "actual": [actual[name]["calls"], actual[name]["input_tokens"]],
        }
        for name in names
    }


async def run_step(ctx: BuildContext, step: str, batches: list[float]) -> BuildContext:
    """Run one build step; returns the context to use for the next step."""

    if step == "plan":
        return ctx.with_build_id(await steps.plan(ctx))
    if step == "persist":
        await steps.persist(ctx, on_batch=lambda _: batches.append(time.monotonic()))
    else:
        await getattr(steps, step)(ctx)
    return ctx


async def build_one(
    ctx: BuildContext, stores: Any, probe: Probe, gateway: TimedGateway, out: dict[str, Any], *, estimate_only: bool
) -> None:
    """Estimate, then every step one by one, then ``run_build`` to finish the record; fills ``out``."""

    probe.step = "estimate"
    started = time.monotonic()
    estimate = await estimate_build(ctx)
    out |= {"estimate_s": round(time.monotonic() - started, 2), "estimate": estimate.model_dump()}
    probe.log(
        "estimated",
        mode=estimate.mode,
        calls=sum(s.calls for s in estimate.steps.values()),
        input_tokens=sum(s.input_tokens for s in estimate.steps.values()),
    )
    if estimate_only:
        return
    begin, total_before, per_step = time.monotonic(), gateway.usage(), {}
    batches: list[float] = []
    usage = StepUsage()  # run_build below runs no step, so it records none: recorded here as it would
    for step in STEPS:
        probe.step, before = step, gateway.usage()
        marks = (len(probe.requests), len(gateway.logical), len(probe.waits))
        probe.log("start")
        started = time.monotonic()
        next_ctx, error = await capture_failure(run_step(ctx, step, batches))
        usage.record(step, before, gateway.usage())
        if error is not None:
            probe.log("failed", error=type(error).__name__, code=getattr(error, "code", None))
            out["failed_step"] = step
            break
        ctx = cast(BuildContext, next_ctx)
        wall = time.monotonic() - started
        stats = step_stats(probe, gateway, step, wall, marks) | usage_delta(before, gateway.usage())
        stats["artifact"] = counters(await stores.builds.get_artifact(ctx.project_id, ctx.build_id, step))
        if step == "persist":
            stats["batch_s"] = [round(b - a, 2) for a, b in zip([started, *batches], batches, strict=False)]
        per_step[step] = stats
        probe.log(
            "done",
            s=round(wall, 1),
            calls=sum(v["calls"] for v in stats["by_class"].values()),
            input_tokens=sum(v["input_tokens"] for v in stats["by_class"].values()),
            cached_input_tokens=sum(v.get("cached_input_tokens", 0) for v in stats["by_class"].values()),
        )
    probe.step = "finish"
    out["steps"] = per_step
    # Finishes the record (resumes the failed step once when one failed) and runs the follow-up builds
    # it asks for; returns the chain's last result.
    finishing = gateway.usage()
    head_usage = usage_delta(total_before, finishing)  # the estimated build's steps only
    last, stepped = await run_build(ctx), ctx.build_id
    stored = last if stepped in (None, last.build_id) else await ctx.builds.get_result(ctx.project_id, stepped)
    if stored is None:
        raise RuntimeError("the stepped build has no stored result")
    result = stored.model_copy(update={"metrics": usage.filled(stored.metrics)})
    await ctx.builds.finish_build(result)  # stored with the steps' usage, so graph_report reads it too
    follow_ups = [await ctx.builds.get_result(ctx.project_id, build_id) for build_id in followed(last)]
    total = usage_delta(total_before, gateway.usage())  # the follow-up builds' calls included
    out |= {
        "wall_s": round(time.monotonic() - begin, 1),
        "steps": per_step,
        "total": total,
        "head_usage": head_usage,
        "follow_up_usage": usage_delta(finishing, gateway.usage()),
        "result": result.model_dump(mode="json"),
        "settlement": None if last.settlement is None else last.settlement.model_dump(mode="json"),
        "follow_ups": [build.model_dump(mode="json") for build in follow_ups if build is not None],
        "estimate_vs_actual": estimate_vs_actual(estimate, head_usage["by_prompt"], estimate_steps_of(ctx.ontology)),
        "max_rss_mb": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 2**10), 1
        ),
    }


def followed(last: BuildResult) -> tuple[str, ...]:
    """The follow-up builds ``run_build`` chained after the stepped build (``Settlement.follow_up_builds``)."""

    return last.settlement.follow_up_builds if last.settlement is not None else ()


def summary(label: str, build: dict[str, Any]) -> dict[str, Any]:
    result, verification = build["result"], build["result"]["verification"] or {}
    return {
        "build": label,
        "mode": result["mode"],
        "status": result["status"],
        "settled": result["settled"],
        "settlement": build["settlement"],
        "follow_ups": [{"build_id": b["build_id"], "status": b["status"]} for b in build["follow_ups"]],
        "failed_step": result["failed_step"],
        "verification_ok": verification.get("ok"),
        "verification": {key: verification.get(key) for key in ("found", "problems", "repairs", "metrics")},
        "wall_s": build["wall_s"],
        "max_rss_mb": build["max_rss_mb"],
        "steps": {
            name: {
                "s": s["wall_s"],
                "calls": {k: v["calls"] for k, v in s["by_class"].items()},
                "p50_p95": {k: [v["p50"], v["p95"]] for k, v in s["http_latency"].items()},
                "in_flight": s["in_flight_mean"],
                "non_200": {k: v for k, v in s["statuses"].items() if k != "200"},
                "waits": s["backoff_waits"],
            }
            for name, s in build["steps"].items()
        },
        "by_class": build["total"]["by_class"],
        "follow_up_by_class": (build.get("follow_up_usage") or {}).get("by_class", {}),
        "estimate_vs_actual": build["estimate_vs_actual"],
        "spec_changes": {name: len(values) for name, values in result["spec_changes"].items() if values},
        "nodes_by_label": result["metrics"]["nodes_by_label"],
        "edges_by_relation": result["metrics"]["edges_by_relation"],
    }


async def run_queries(reader: GraphReader, fulltext: GraphReader, project: str, path: Path) -> dict[str, Any]:
    queries = json.loads(await asyncio.to_thread(path.read_text))
    out: dict[str, Any] = {}
    for name, current in (("fused", reader), ("fulltext", fulltext)):
        ranks, times = [], []
        for query in queries:
            started = time.monotonic()
            hits = await current.find_nodes(project, query["text"], limit=20)
            times.append(time.monotonic() - started)
            found = [hit.node.spec_id for hit in hits]
            ranks.append(found.index(query["spec_id"]) + 1 if query["spec_id"] in found else None)
        out[name] = {
            "queries": len(queries),
            "top1": sum(rank == 1 for rank in ranks),
            "top5": sum(rank is not None and rank <= TOP_K for rank in ranks),
            "mrr": round(sum(1 / rank for rank in ranks if rank) / max(1, len(ranks)), 3),
            "latency_ms": {k: round(v * 1000) for k, v in latency(times).items() if k != "n"},
            "ranks": ranks,
        }
    return out


# The only raw Cypher outside the library's query files (tests/test_graph_probes_structure.py allows exactly
# this statement): the eval runs on Neo4j only until the library has a delete-project query.
CLEANUP = Query.literal(
    "example.cleanup",
    "MATCH (n {group_id: $project_id}) WITH n LIMIT 2000 DETACH DELETE n RETURN count(*) AS c",
)


async def cleanup(executor: Executor, project: str, state: Path) -> int:
    """Delete the project's nodes in batches (a Cypher store only: the query is literal Cypher)."""

    deleted: int = 0
    while True:
        records = await executor.write(CLEANUP, project)
        deleted += int(records[0]["c"])
        if not records[0]["c"]:
            break
    shutil.rmtree(state, ignore_errors=True)
    return deleted


async def main() -> None:
    args = parse_args()
    env = environment()
    store_settings, llm = store_config(), llm_config(env)
    if args.concurrency is not None:
        llm = llm.model_copy(update={"concurrency": args.concurrency})
    if store_settings.backend != "neo4j":
        raise SystemExit(
            f"graph_eval.py runs on a Neo4j store only (GRAPH_STORE={store_settings.backend}): "
            "its cleanup is a raw Cypher statement"
        )
    probe = Probe(args.progress)
    ontology, store = load_ontology(), build_store(store_settings)
    gateway = TimedGateway(llm, http_client=probe.client(), sleep=probe.sleep)
    report: dict[str, Any] = {"project_id": args.project, "concurrency": llm.concurrency, "builds": {}}
    try:
        await ensure_schema(store, ontology, llm)
        for index, snapshot in enumerate(args.snapshots, start=1):
            rebuild = snapshot.startswith("rebuild:")
            folder = Path(snapshot.removeprefix("rebuild:"))
            stores = open_file_stores(args.state, folder)
            if stores.corpus is None:
                raise RuntimeError("file stores were opened without a corpus")
            probe.build = label = args.label if len(args.snapshots) == 1 else f"{args.label}{index}"
            ctx = BuildContext(
                project_id=args.project,
                ontology=ontology,
                driver=store,
                gateway=gateway,
                registry=stores.registry,
                corpus=stores.corpus,
                extraction=stores.extraction,
                builds=stores.builds,
                rebuild=rebuild,
                settings=BuildSettings(inference=not args.no_inference),
            )
            build = report["builds"][label] = {"snapshot": str(folder), "rebuild": rebuild}
            await build_one(ctx, stores, probe, gateway, build, estimate_only=args.estimate_only)
            if args.estimate_only:
                print(json.dumps({"build": label, **build}))
                continue
            probe.log("finished", status=build["result"]["status"], wall_s=build["wall_s"])
            print(json.dumps(summary(label, build), sort_keys=False))
            if args.out:
                args.out.write_text(json.dumps(report, indent=1, default=str))
        if args.queries:
            probe.step = "queries"
            report["queries"] = await run_queries(
                GraphReader(store, ontology, gateway),
                GraphReader(store, ontology, None),
                args.project,
                args.queries,
            )
            print(
                json.dumps(
                    {key: {k: v for k, v in value.items() if k != "ranks"} for key, value in report["queries"].items()}
                )
            )
    finally:
        if args.cleanup:
            report["cleanup_deleted_nodes"] = await cleanup(store.executor(), args.project, args.state)
            probe.log("cleanup", deleted=report["cleanup_deleted_nodes"])
        if args.out:
            args.out.write_text(json.dumps(report, indent=1, default=str))
        await gateway.aclose()
        await store.close()


if __name__ == "__main__":
    raise SystemExit(run_example(main))
