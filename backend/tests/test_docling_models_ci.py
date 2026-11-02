"""CI fetches the Docling models at the revisions the worker-parse image bakes in (static check, no network)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from scripts import fetch_docling_models as fetch_script

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "backend" / "docker" / "worker-parse" / "Dockerfile"
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yaml"


def _unit_job() -> dict[str, Any]:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    job: dict[str, Any] = jobs["unit-python"]
    return job


def _dockerfile_revision(name: str) -> str:
    match = re.search(rf"^ARG {name}=([0-9a-f]{{40}})$", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{name} is not pinned to a commit in {DOCKERFILE.name}"
    return match[1]


def test_the_dockerfile_pins_the_revisions_of_the_fetch_script():
    """The script is the single source; the image build keeps its own ARGs, equal to it."""

    assert fetch_script.LAYOUT.revision == _dockerfile_revision("LAYOUT_REVISION")
    assert fetch_script.TABLEFORMER.revision == _dockerfile_revision("TABLEFORMER_REVISION")
    patterns = {model.repo_id: model.patterns for model in fetch_script.MODELS}
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    for repo, repo_patterns in patterns.items():
        assert repo in dockerfile
        assert all(pattern in dockerfile for pattern in repo_patterns), (
            f"{repo}: pattern list differs from the Dockerfile"
        )


def test_the_unit_job_caches_the_models_by_the_script_hash_and_fetches_them_only_on_a_miss():
    job = _unit_job()
    cache = next(step for step in job["steps"] if step.get("id") == "docling-models")
    assert cache["uses"].startswith("actions/cache@")
    assert cache["with"]["path"] == "${{ env.DOCLING_ARTIFACTS_PATH }}"
    assert "hashFiles('backend/scripts/fetch_docling_models.py')" in cache["with"]["key"]
    assert (ROOT / "backend" / "scripts" / "fetch_docling_models.py").is_file()
    fetch = next(step for step in job["steps"] if "Fetch the Docling models" in step.get("name", ""))
    assert fetch["if"] == "steps.docling-models.outputs.cache-hit != 'true'"
    assert "scripts/fetch_docling_models.py" in fetch["run"]
    assert "--target" in fetch["run"]
    assert "REVISION" not in str(job.get("env", {})), "the revisions live in the script only"
    assert [m.patterns for m in fetch_script.MODELS if m is fetch_script.TABLEFORMER] == [
        ("model_artifacts/tableformer/fast/*",)
    ]


def test_a_second_fetch_makes_no_download(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_script, "snapshot_download", lambda **kwargs: (calls.append(kwargs["repo_id"]), None)[1])
    assert fetch_script.fetch(tmp_path) == [m.repo_id for m in fetch_script.MODELS]
    assert fetch_script.fetch(tmp_path) == []
    assert calls == [m.repo_id for m in fetch_script.MODELS]
    marker = tmp_path / fetch_script.LAYOUT.folder / fetch_script.MARKER
    marker.write_text("an older revision\n", encoding="utf-8")
    assert fetch_script.fetch(tmp_path) == [fetch_script.LAYOUT.repo_id]


def test_the_models_are_read_from_the_artifacts_path_in_the_tests_step():
    job = _unit_job()
    assert job["env"]["DOCLING_ARTIFACTS_PATH"].endswith("/.docling-models")
    steps = job["steps"]
    cache_index = next(i for i, step in enumerate(steps) if step.get("id") == "docling-models")
    test_index = next(i for i, step in enumerate(steps) if "make unit-test" in step.get("run", ""))
    assert cache_index < test_index
