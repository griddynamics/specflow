"""CI fetches the Docling models at the revisions the worker-parse image bakes in (static check, no network)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "backend" / "worker-parse.Dockerfile"
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yaml"


def _unit_job() -> dict[str, Any]:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    job: dict[str, Any] = jobs["unit-python"]
    return job


def _dockerfile_revision(name: str) -> str:
    match = re.search(rf"^ARG {name}=([0-9a-f]{{40}})$", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{name} is not pinned to a commit in {DOCKERFILE.name}"
    return match[1]


def test_the_unit_job_pins_the_same_model_revisions_as_the_image():
    env = _unit_job()["env"]
    assert env["LAYOUT_REVISION"] == _dockerfile_revision("LAYOUT_REVISION")
    assert env["TABLEFORMER_REVISION"] == _dockerfile_revision("TABLEFORMER_REVISION")


def test_the_unit_job_caches_the_models_by_revision_and_fetches_them_only_on_a_miss():
    job = _unit_job()
    cache = next(step for step in job["steps"] if step.get("id") == "docling-models")
    assert cache["uses"].startswith("actions/cache@")
    assert cache["with"]["path"] == "${{ env.DOCLING_ARTIFACTS_PATH }}"
    assert "env.LAYOUT_REVISION" in cache["with"]["key"]
    assert "env.TABLEFORMER_REVISION" in cache["with"]["key"]
    fetch = next(step for step in job["steps"] if "Fetch the Docling models" in step.get("name", ""))
    assert fetch["if"] == "steps.docling-models.outputs.cache-hit != 'true'"
    assert "snapshot_download" in fetch["run"]
    assert "model_artifacts/tableformer/fast/*" in fetch["run"]  # FAST only (D4): nothing else is fetched


def test_the_models_are_read_from_the_artifacts_path_in_the_tests_step():
    job = _unit_job()
    assert job["env"]["DOCLING_ARTIFACTS_PATH"].endswith("/.docling-models")
    steps = job["steps"]
    cache_index = next(i for i, step in enumerate(steps) if step.get("id") == "docling-models")
    test_index = next(i for i, step in enumerate(steps) if "make unit-test" in step.get("run", ""))
    assert cache_index < test_index
