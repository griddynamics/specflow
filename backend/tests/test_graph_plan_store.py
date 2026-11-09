"""Plan store contract and the file implementation."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from graph.core.errors import ConfigurationError, GraphError, InfrastructureUnavailableError
from graph.plan.files import FilePlanStore, open_plan_store
from graph.plan.models import MeaningRevisions, PlanOptions, PlanRunRecord, PlanStepName, StepState
from graph.plan.names import PlanMode
from graph.plan.ports import PlanAnswerKind, PlanStore

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
STEPS: tuple[PlanStepName, ...] = (
    "snapshot",
    "partition",
    "describe",
    "project",
    "tasks",
    "stitch",
    "review",
    "diff",
    "persist",
    "check",
)


def all_files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def record(
    run_id: str = "r1",
    *,
    project_id: str = "proj",
    mode: PlanMode = "first",
    minutes: int = 0,
) -> PlanRunRecord:
    return PlanRunRecord(
        project_id=project_id,
        run_id=run_id,
        mode=mode,
        replan=False,
        build_id="build-1",
        options=PlanOptions(),
        rules_version="2.0",
        plan_ontology_version="1",
        fingerprint=None,
        started_at=T0 + timedelta(minutes=minutes),
        progress={},
        status="running",
        failure=None,
    )


def result_of(
    run_id: str = "r1",
    *,
    project_id: str = "proj",
    status: str = "succeeded",
    failure: str | None = None,
) -> dict[str, Any]:
    return {"project_id": project_id, "run_id": run_id, "status": status, "failure": failure}


async def start(store: PlanStore, *runs: PlanRunRecord) -> None:
    for run in runs:
        await store.create_run(run)


class PlanStoreContract:
    """The plan store contract; subclasses provide the fixture ``store``."""

    async def test_run_round_trips(self, store: PlanStore) -> None:
        await store.create_run(record())
        assert await store.get_run("proj", "r1") == record()

    async def test_unknown_runs_reads_and_results_are_none(self, store: PlanStore) -> None:
        assert await store.get_run("proj", "ghost") is None
        assert await store.get_result("proj", "ghost") is None
        assert await store.get_artifact("proj", "ghost", "snapshot") is None

    async def test_duplicate_run_ids_are_refused(self, store: PlanStore) -> None:
        await store.create_run(record())
        with pytest.raises(GraphError):
            await store.create_run(record())
        assert await store.get_run("proj", "r1") == record()

    async def test_run_ids_are_project_scoped(self, store: PlanStore) -> None:
        await start(store, record(project_id="one"), record(project_id="two"))
        one = await store.get_run("one", "r1")
        two = await store.get_run("two", "r1")
        assert one is not None and one.project_id == "one"
        assert two is not None and two.project_id == "two"

    async def test_latest_run_orders_by_started_at_then_run_id(self, store: PlanStore) -> None:
        await start(store, record("a", minutes=1), record("c", minutes=1), record("b", minutes=2))
        latest = await store.latest_run("proj")
        assert latest is not None
        assert latest.run_id == "b"

    async def test_latest_run_filters_finished_only_and_modes(self, store: PlanStore) -> None:
        await start(
            store,
            record("first", mode="first", minutes=1),
            record("update", mode="update", minutes=2),
            record("replan", mode="replan", minutes=3),
        )
        await store.finish_run(result_of("first", status="failed"))
        latest = await store.latest_run("proj")
        finished = await store.latest_run("proj", finished_only=True)
        mode = await store.latest_run("proj", modes={"first", "replan"})
        assert latest is not None and latest.run_id == "replan"
        assert finished is not None and finished.run_id == "first"
        assert mode is not None and mode.run_id == "replan"

    async def test_progress_is_recorded(self, store: PlanStore) -> None:
        await store.create_run(record())
        await store.put_progress("proj", "r1", "snapshot", "running")
        await store.put_progress("proj", "r1", "snapshot", "done")
        await store.put_progress("proj", "r1", "partition", "failed")
        stored = await store.get_run("proj", "r1")
        assert stored is not None
        assert stored.progress == {"snapshot": "done", "partition": "failed"}

    async def test_concurrent_progress_updates_are_kept(self, store: PlanStore) -> None:
        await store.create_run(record())
        states: dict[PlanStepName, StepState] = dict.fromkeys(STEPS, "done")
        await asyncio.gather(*(store.put_progress("proj", "r1", step, state) for step, state in states.items()))
        stored = await store.get_run("proj", "r1")
        assert stored is not None
        assert stored.progress == states

    async def test_finish_run_stores_result_and_updates_the_record(self, store: PlanStore) -> None:
        await store.create_run(record())
        await store.put_progress("proj", "r1", "snapshot", "done")
        await store.finish_run(result_of(status="partially_succeeded", failure="llm_error"))
        stored = await store.get_run("proj", "r1")
        assert stored is not None
        assert (stored.status, stored.failure, stored.finished) == ("partially_succeeded", "llm_error", True)
        assert await store.get_result("proj", "r1") == result_of(status="partially_succeeded", failure="llm_error")

    async def test_artifacts_round_trip_and_replace(self, store: PlanStore) -> None:
        await store.create_run(record())
        await store.put_artifact("proj", "r1", "snapshot", {"v": 1, "old": True})
        await store.put_artifact("proj", "r1", "snapshot", {"v": 2})
        await store.put_artifact("proj", "r1", "partition", {})
        assert await store.get_artifact("proj", "r1", "snapshot") == {"v": 2}
        assert await store.get_artifact("proj", "r1", "partition") == {}

    async def test_artifacts_are_project_and_run_scoped(self, store: PlanStore) -> None:
        await start(store, record("r1"), record("r2"), record("r1", project_id="other"))
        await store.put_artifact("proj", "r1", "snapshot", {"owner": "proj/r1"})
        await store.put_artifact("proj", "r2", "snapshot", {"owner": "proj/r2"})
        await store.put_artifact("other", "r1", "snapshot", {"owner": "other/r1"})
        assert await store.get_artifact("proj", "r1", "snapshot") == {"owner": "proj/r1"}
        assert await store.get_artifact("proj", "r2", "snapshot") == {"owner": "proj/r2"}
        assert await store.get_artifact("other", "r1", "snapshot") == {"owner": "other/r1"}

    async def test_answers_round_trip_by_reuse_key(self, store: PlanStore) -> None:
        await store.put_answer("proj", "describe", "key", "reuse", "r1", payload={"answer": 1})
        assert await store.get_answer("proj", "describe", "key", "reuse") == {"answer": 1}
        assert await store.get_answer("proj", "describe", "key", "other") is None

    async def test_answers_can_be_scoped_to_a_run(self, store: PlanStore) -> None:
        await store.put_answer("proj", "tasks", "key", "reuse", "r1", payload={"answer": 1})
        assert await store.get_answer("proj", "tasks", "key", "reuse", run_id="r1") == {"answer": 1}
        assert await store.get_answer("proj", "tasks", "key", "reuse", run_id="r2") is None

    async def test_revisions_round_trip(self, store: PlanStore) -> None:
        assert await store.get_revisions("proj") == MeaningRevisions()
        revisions = MeaningRevisions(nodes={"REQ-1": "build-2"}, consumed=("build-1", "build-2"))
        await store.put_revisions("proj", revisions)
        assert await store.get_revisions("proj") == revisions


class MemoryPlanStore(PlanStore):
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], PlanRunRecord] = {}
        self.results: dict[tuple[str, str], dict[str, Any]] = {}
        self.artifacts: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.answers: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.revisions: dict[str, MeaningRevisions] = {}

    async def create_run(self, record: PlanRunRecord) -> None:
        key = (record.project_id, record.run_id)
        if key in self.records:
            raise ConfigurationError("run_exists")
        self.records[key] = record

    async def get_run(self, project_id: str, run_id: str) -> PlanRunRecord | None:
        return self.records.get((project_id, run_id))

    async def latest_run(
        self, project_id: str, *, finished_only: bool = False, modes: Any = None
    ) -> PlanRunRecord | None:
        candidates = [
            run
            for (project, _), run in self.records.items()
            if project == project_id and (not finished_only or run.finished) and (modes is None or run.mode in modes)
        ]
        return max(candidates, key=lambda run: (run.started_at, run.run_id), default=None)

    async def put_progress(self, project_id: str, run_id: str, step: PlanStepName, state: StepState) -> None:
        current = self.records.get((project_id, run_id))
        if current is None:
            raise ConfigurationError("run_not_found")
        self.records[(project_id, run_id)] = current.model_copy(update={"progress": {**current.progress, step: state}})

    async def finish_run(self, result: Mapping[str, Any]) -> None:
        key = (result["project_id"], result["run_id"])
        current = self.records.get(key)
        if current is None:
            raise ConfigurationError("run_not_found")
        self.results[key] = dict(result)
        self.records[key] = current.model_copy(update={"status": result["status"], "failure": result.get("failure")})

    async def get_result(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        return self.results.get((project_id, run_id))

    async def put_artifact(self, project_id: str, run_id: str, step: PlanStepName, payload: Mapping[str, Any]) -> None:
        self.artifacts[(project_id, run_id, step)] = dict(payload)

    async def get_artifact(self, project_id: str, run_id: str, step: PlanStepName) -> dict[str, Any] | None:
        return self.artifacts.get((project_id, run_id, step))

    async def get_answer(
        self,
        project_id: str,
        kind: PlanAnswerKind,
        key: str,
        reuse_key: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        found = self.answers.get((project_id, kind, key))
        if found is None or found["reuse_key"] != reuse_key:
            return None
        if run_id is not None and found["run_id"] != run_id:
            return None
        return found["payload"]

    async def put_answer(
        self,
        project_id: str,
        kind: PlanAnswerKind,
        key: str,
        reuse_key: str,
        run_id: str,
        *,
        payload: Mapping[str, Any],
    ) -> None:
        self.answers[(project_id, kind, key)] = {
            "reuse_key": reuse_key,
            "run_id": run_id,
            "payload": dict(payload),
        }

    async def get_revisions(self, project_id: str) -> MeaningRevisions:
        return self.revisions.get(project_id, MeaningRevisions())

    async def put_revisions(self, project_id: str, revisions: MeaningRevisions) -> None:
        self.revisions[project_id] = revisions


def damage_only_gzip_file(root: Path, pattern: str) -> None:
    next(root.rglob(pattern)).write_bytes(b"garbage")


class TestMemoryPlanStore(PlanStoreContract):
    @pytest.fixture
    def store(self) -> PlanStore:
        return MemoryPlanStore()


class TestFilePlanStore(PlanStoreContract):
    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        return tmp_path / "root"

    @pytest.fixture
    def store(self, root: Path) -> FilePlanStore:
        return open_plan_store(root)

    async def test_the_layout_is_gzip_files_under_the_project(self, store: FilePlanStore, root: Path) -> None:
        await store.create_run(record())
        await store.put_artifact("proj", "r1", "snapshot", {"v": 1})
        await store.put_answer("proj", "describe", "answer-key", "reuse", "r1", payload={"answer": 1})
        await store.put_revisions("proj", MeaningRevisions(nodes={"REQ-1": "build-2"}))
        await store.finish_run(result_of())

        assert all_files(root) == [
            f"proj/plan-answers/describe/{hashlib.sha256(b'answer-key').hexdigest()}.json.gz",
            "proj/plan-revisions.json.gz",
            "proj/plan-runs/r1/result.json.gz",
            "proj/plan-runs/r1/run.json.gz",
            "proj/plan-runs/r1/snapshot.json.gz",
        ]

    async def test_the_same_data_writes_the_same_bytes(self, tmp_path: Path) -> None:
        first = open_plan_store(tmp_path / "one")
        second = open_plan_store(tmp_path / "two")
        await first.create_run(record())
        await second.create_run(record())
        first_file = next((tmp_path / "one").rglob("run.json.gz"))
        second_file = next((tmp_path / "two").rglob("run.json.gz"))
        assert first_file.read_bytes() == second_file.read_bytes()

    async def test_a_corrupt_file_is_unavailable(self, store: FilePlanStore, root: Path) -> None:
        await store.create_run(record())
        damage_only_gzip_file(root, "run.json.gz")
        with pytest.raises(InfrastructureUnavailableError, match="file_store_unreadable"):
            await store.get_run("proj", "r1")

    async def test_a_corrupt_answer_file_is_unavailable(self, store: FilePlanStore, root: Path) -> None:
        await store.put_answer("proj", "describe", "key", "reuse", "r1", payload={"answer": 1})
        damage_only_gzip_file(root, "*.json.gz")
        with pytest.raises(InfrastructureUnavailableError, match="file_store_unreadable"):
            await store.get_answer("proj", "describe", "key", "reuse")

    async def test_invalid_project_and_run_ids_are_refused(self, store: FilePlanStore) -> None:
        with pytest.raises(ConfigurationError, match="invalid_project_id"):
            await store.get_run("../bad", "r1")
        with pytest.raises(ConfigurationError, match="invalid_run_id"):
            await store.get_run("proj", "../bad")

    async def test_answer_files_carry_the_key_reuse_key_run_and_payload(self, store: FilePlanStore, root: Path) -> None:
        await store.put_answer("proj", "project", "key", "reuse", "r1", payload={"answer": 1})
        path = next((root / "proj" / "plan-answers").rglob("*.json.gz"))
        assert json.loads(gzip.decompress(path.read_bytes())) == {
            "key": "key",
            "reuse_key": "reuse",
            "run_id": "r1",
            "payload": {"answer": 1},
        }
