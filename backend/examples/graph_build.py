"""Build the knowledge graph of a folder of markdown documents into Neo4j (graph library, plan 2.20).

Run from backend/:

    uv run --env-file ../.env.local python examples/graph_build.py \
        --project shop-demo --corpus tests/fixtures/graph/shop/1 --state ../.graph-build/state

Host-style wiring: this script reads the environment (the library never does) and passes configs:
NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, NEO4J_DATABASE (optional), LLM_GATEWAY_URL, LLM_GATEWAY_API_KEY,
GRAPH_LLM_MODEL, GRAPH_SMALL_MODEL, GRAPH_EMBEDDING_MODEL, GRAPH_EMBEDDING_DIM. Build records,
artifacts, reusable extraction results, the spec_id registry and the corpus index live under
--state (file stores). It runs ensure_schema, then run_build (or only the plan step with
--plan-only), and prints a summary: ids, counts, status and LLM usage, never document text or keys.

A build whose verification requested a follow-up (``follow_up_lineages`` in its summary, not
``settled``) is settled by a next build that retries those lineages: pass --retry-follow-ups to take
them from the project's latest finished build result, or name them with --retry-lineage.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from example_lib import run_example
from graph.build import BuildContext, BuildResult, FileStores, open_file_stores, plan, run_build
from graph.build.models import PlanArtifact
from graph.core import Gateway, LLMConfig, Neo4jConfig, connect, ensure_schema, load_ontology
from logger.logger import INFO, get_logger, log_event

logger = get_logger("examples.graph_build")

REQUIRED_ENVIRONMENT = (
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "LLM_GATEWAY_URL",
    "LLM_GATEWAY_API_KEY",
    "GRAPH_LLM_MODEL",
    "GRAPH_SMALL_MODEL",
    "GRAPH_EMBEDDING_MODEL",
    "GRAPH_EMBEDDING_DIM",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True, help="project id (graph partition)")
    parser.add_argument("--corpus", required=True, type=Path, help="folder of .md documents")
    parser.add_argument("--state", required=True, type=Path, help="folder of the file stores")
    parser.add_argument("--rebuild", action="store_true", help="ask for a rebuild")
    parser.add_argument("--plan-only", action="store_true", help="run only the plan step")
    parser.add_argument("--build-id", help="resume this build instead of starting a new one")
    parser.add_argument(
        "--retry-follow-ups",
        action="store_true",
        help="retry the follow_up_lineages of the project's latest finished build",
    )
    parser.add_argument(
        "--retry-lineage",
        action="append",
        default=[],
        metavar="LINEAGE",
        help="retry this document lineage (repeatable)",
    )
    return parser.parse_args()


def environment() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name, "").strip()]
    if missing:
        raise SystemExit(
            "missing environment variables: " + ", ".join(missing) + " (run with `uv run --env-file ../.env.local`)"
        )
    return {name: os.environ[name].strip() for name in REQUIRED_ENVIRONMENT}


def neo4j_config(env: dict[str, str]) -> Neo4jConfig:
    return Neo4jConfig(
        uri=env["NEO4J_URI"],
        user=env["NEO4J_USER"],
        password=SecretStr(env["NEO4J_PASSWORD"]),
        database=os.environ.get("NEO4J_DATABASE", "").strip() or None,
    )


def llm_config(env: dict[str, str]) -> LLMConfig:
    return LLMConfig(
        endpoint=env["LLM_GATEWAY_URL"],
        api_key=SecretStr(env["LLM_GATEWAY_API_KEY"]),
        main_model=env["GRAPH_LLM_MODEL"],
        small_model=env["GRAPH_SMALL_MODEL"],
        embedding_model=env["GRAPH_EMBEDDING_MODEL"],
        embedding_dim=int(env["GRAPH_EMBEDDING_DIM"]),
    )


def result_summary(result: BuildResult) -> dict[str, Any]:
    metrics = result.metrics
    return {
        "project_id": result.project_id,
        "build_id": result.build_id,
        "mode": result.mode,
        "status": result.status,
        "settled": result.settled,
        "failed_step": result.failed_step,
        "error_code": result.error_code,
        "spec_changes": {name: len(values) for name, values in result.spec_changes.model_dump().items() if values},
        "skipped_fragments": dict(sorted(result.skipped_fragments.items())),
        "follow_up_lineages": list(result.verification.follow_up_lineages) if result.verification else [],
        "fragments_by_status": metrics.fragments_by_status,
        "units_extracted": metrics.units_extracted,
        "units_reused": metrics.units_reused,
        "nodes_by_label": metrics.nodes_by_label,
        "edges_by_relation": metrics.edges_by_relation,
        "dropped_edges": metrics.dropped_edges,
        "exclusive_conflicts": metrics.exclusive_conflicts,
        "attribution_fallbacks": metrics.attribution_fallbacks,
        "llm": {step: usage.model_dump() for step, usage in metrics.llm.items()},
        "duration_s": metrics.duration_s,
    }


def plan_summary(project_id: str, build_id: str, artifact: PlanArtifact) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "build_id": build_id,
        "mode": artifact.mode,
        "change_set": {name: len(values) for name, values in artifact.change_set.model_dump().items() if values},
        "documents": len(artifact.documents),
        "fragments": len(artifact.fragments),
        "dirty_fragments": len(artifact.dirty_ids),
        "fragment_tokens": sum(fragment.tokens for fragment in artifact.fragments),
        "units": len(artifact.units),
        "windows": sum(len(unit.windows) for unit in artifact.units),
    }


async def retry_lineages(stores: FileStores, project_id: str, args: argparse.Namespace) -> tuple[str, ...]:
    """The lineages to retry: --retry-lineage values, then with --retry-follow-ups the follow-ups the
    latest finished build's verification requested."""

    lineages = list(args.retry_lineage)
    if args.retry_follow_ups:
        record = await stores.builds.latest_build(project_id, finished_only=True)
        result = await stores.builds.get_result(project_id, record.build_id) if record else None
        if result is not None and result.verification is not None:
            lineages.extend(result.verification.follow_up_lineages)
    return tuple(dict.fromkeys(lineages))


async def main() -> None:
    args = parse_args()
    env = environment()
    neo4j = neo4j_config(env)
    llm = llm_config(env)
    ontology = load_ontology()
    stores = open_file_stores(args.state, args.corpus)
    if stores.corpus is None:
        raise RuntimeError("file stores were opened without a corpus")
    driver = connect(neo4j)
    gateway = Gateway(llm)
    try:
        retry = await retry_lineages(stores, args.project, args)
        log_event(logger, INFO, "example.graph_build.retry", lineages=len(retry))
        created = await ensure_schema(driver, ontology, llm, neo4j.database)
        log_event(logger, INFO, "example.graph_build.schema", created=len(created))
        ctx = BuildContext(
            project_id=args.project,
            ontology=ontology,
            driver=driver,
            gateway=gateway,
            registry=stores.registry,
            corpus=stores.corpus,
            extraction=stores.extraction,
            builds=stores.builds,
            rebuild=args.rebuild,
            retry_lineages=retry,
            build_id=args.build_id,
            database=neo4j.database,
        )
        if args.plan_only:
            build_id = await plan(ctx)
            payload = await stores.builds.get_artifact(args.project, build_id, "plan")
            summary = plan_summary(args.project, build_id, PlanArtifact.model_validate(payload))
        else:
            summary = result_summary(await run_build(ctx))
        print(json.dumps(summary, indent=2, sort_keys=False))
    finally:
        await gateway.aclose()
        await driver.close()


if __name__ == "__main__":
    raise SystemExit(run_example(main))
