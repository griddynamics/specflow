"""The gap run store: records, progress, artifacts and results, on disk and in memory with one semantics
(docs/graph/gap/architecture.md "Run steps"; FR-GRUN-07)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from graph.core.errors import ConfigurationError
from graph.gap import GapRunRecord, GapRunStore, MemoryGapRunStore, open_gap_run_store
from graph.gap.models import GapRunResult

PROJECT = "shop"
START = datetime(2026, 11, 1, tzinfo=UTC)


def record(run_id: str, minutes: int = 0) -> GapRunRecord:
    return GapRunRecord(project_id=PROJECT, run_id=run_id, mode="full", started_at=START + timedelta(minutes=minutes))


@pytest.fixture(params=["memory", "file"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> GapRunStore:
    return MemoryGapRunStore() if request.param == "memory" else open_gap_run_store(tmp_path)


async def test_runs_progress_artifacts_and_results(store: GapRunStore) -> None:
    await store.create_run(record("run-a"))
    await store.create_run(record("run-b", minutes=5))
    with pytest.raises(ConfigurationError, match="run_exists"):
        await store.create_run(record("run-a"))

    latest = await store.latest_run(PROJECT)
    assert latest is not None and latest.run_id == "run-b"
    assert await store.latest_run(PROJECT, finished_only=True) is None
    assert await store.latest_run("other") is None

    await store.put_progress(PROJECT, "run-a", "scope", "done")
    await store.put_artifact(PROJECT, "run-a", "scope", {"touched": ["REQ::A"], "moved": 1})
    found = await store.get_run(PROJECT, "run-a")
    assert found is not None and found.progress == {"scope": "done"} and not found.finished
    assert await store.get_artifact(PROJECT, "run-a", "scope") == {"touched": ["REQ::A"], "moved": 1}
    assert await store.get_artifact(PROJECT, "run-a", "detect") is None
    assert await store.get_result(PROJECT, "run-a") is None

    result = GapRunResult(run_id="run-a", mode="full", status="degraded", counts={"notes": 1})
    await store.finish_run(PROJECT, result)

    finished = await store.get_run(PROJECT, "run-a")
    assert finished is not None and finished.finished and finished.status == "degraded"
    assert await store.get_result(PROJECT, "run-a") == result
    done = await store.latest_run(PROJECT, finished_only=True)
    assert done is not None and done.run_id == "run-a"


async def test_unknown_runs_and_bad_names_are_refused(store: GapRunStore) -> None:
    bad: Any = "halfway"
    with pytest.raises(ConfigurationError, match="run_not_found"):
        await store.put_progress(PROJECT, "missing", "scope", "done")
    with pytest.raises(ConfigurationError, match="run_not_found"):
        await store.put_artifact(PROJECT, "missing", "scope", {})
    with pytest.raises(ConfigurationError, match="run_not_found"):
        await store.finish_run(PROJECT, GapRunResult(run_id="missing", mode="full", status="completed"))
    await store.create_run(record("run-a"))
    with pytest.raises(ConfigurationError, match="invalid_step_state"):
        await store.put_progress(PROJECT, "run-a", "scope", bad)
    assert await store.get_run(PROJECT, "missing") is None


async def test_the_file_store_keeps_its_layout_and_refuses_paths(tmp_path: Path) -> None:
    store = open_gap_run_store(tmp_path)
    await store.create_run(record("run-a"))
    await store.put_artifact(PROJECT, "run-a", "sites", {"sites": []})

    folder = tmp_path / PROJECT / "gap-runs" / "run-a"
    assert sorted(path.name for path in folder.iterdir()) == ["run.json.gz", "sites.json.gz"]
    (tmp_path / PROJECT / "gap-runs" / ".hidden").mkdir()
    latest = await store.latest_run(PROJECT)
    assert latest is not None and latest.run_id == "run-a"
    for run_id in ("../x", ".run", ""):
        with pytest.raises(ConfigurationError, match="invalid_run_id"):
            await store.get_run(PROJECT, run_id)
    for step in ("run", "result", "../x"):
        with pytest.raises(ConfigurationError, match="invalid_step"):
            await store.get_artifact(PROJECT, "run-a", step)
