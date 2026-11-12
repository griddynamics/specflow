"""Build the knowledge graph of a folder of markdown documents into Neo4j."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from example_lib import build_result_summary, run_example
from graph.build import BuildContext, BuildResult, FileStores, open_file_stores, plan, run_build
from graph.build.models import PlanArtifact
from graph.core import LLMConfig, ensure_schema, load_ontology
from graph.core.llm import Gateway
from graph.stores import StoreConfig, build_store
from logger.logger import INFO, get_logger, log_event
from settings import ConfigurationError, GraphBuildSettings, load_graph_store_settings

logger = get_logger("examples.graph_build")

REQUIRED_ENVIRONMENT = (
    "LLM_GATEWAY_URL",
    "LLM_GATEWAY_API_KEY",
    "GRAPH_LLM_MODEL",
    "GRAPH_SMALL_MODEL",
    "GRAPH_EMBEDDING_MODEL",
    "GRAPH_EMBEDDING_DIM",
)
OPTIONAL_LLM_ENVIRONMENT = ("GRAPH_LLM_CONCURRENCY",)


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


def store_config() -> StoreConfig:
    """The graph store settings of the environment, through the host's own settings loader."""

    try:
        return load_graph_store_settings().to_store_config()
    except ConfigurationError as error:
        raise SystemExit(f"{error} (run with `uv run --env-file ../.env.local`)") from None


def llm_config(env: dict[str, str]) -> LLMConfig:
    """The API's mapping (``settings.GraphBuildSettings``); ``GRAPH_LLM_CONCURRENCY`` is optional (blank: 8)."""

    optional = {name: os.environ[name].strip() for name in OPTIONAL_LLM_ENVIRONMENT if os.environ.get(name, "").strip()}
    try:
        return GraphBuildSettings.model_validate({**env, **optional}).to_llm_config()
    except ValidationError as error:
        names = sorted({str(item["loc"][0]) for item in error.errors(include_url=False, include_input=False)})
        raise SystemExit("invalid environment variables: " + ", ".join(names)) from None


def result_summary(result: BuildResult) -> dict[str, Any]:
    summary = build_result_summary(result)
    duration_s = summary.pop("duration_s")
    summary["llm"] = {step: usage.model_dump() for step, usage in result.metrics.llm.items()}
    summary["duration_s"] = duration_s
    return summary


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
    store_settings = store_config()
    llm = llm_config(env)
    ontology = load_ontology()
    stores = open_file_stores(args.state, args.corpus)
    if stores.corpus is None:
        raise RuntimeError("file stores were opened without a corpus")
    store = build_store(store_settings)
    gateway = Gateway(llm)
    try:
        retry = await retry_lineages(stores, args.project, args)
        log_event(logger, INFO, "example.graph_build.retry", lineages=len(retry))
        created = await ensure_schema(store, ontology, llm)
        log_event(logger, INFO, "example.graph_build.schema", created=len(created))
        ctx = BuildContext(
            project_id=args.project,
            ontology=ontology,
            driver=store,
            gateway=gateway,
            registry=stores.registry,
            corpus=stores.corpus,
            extraction=stores.extraction,
            builds=stores.builds,
            rebuild=args.rebuild,
            retry_lineages=retry,
            build_id=args.build_id,
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
        await store.close()


if __name__ == "__main__":
    raise SystemExit(run_example(main))
