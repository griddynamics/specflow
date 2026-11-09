"""File implementation of the plan store.

Layout under the root given to ``open_plan_store``:

- ``<root>/<project_id>/plan-runs/<run_id>/{run,result,<step>}.json.gz``: run records, results and
  step artifacts;
- ``<root>/<project_id>/plan-answers/<kind>/<sha256(key)>.json.gz``: planner answers with their
  reuse keys, the run that wrote them and the payload;
- ``<root>/<project_id>/plan-revisions.json.gz``: meaning revisions.

Every file is written atomically through ``graph.core.files.write_atomic``; ``.json.gz`` files are
gzip JSON without a timestamp. I/O failures raise ``InfrastructureUnavailableError``.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from graph.core.errors import ConfigurationError, InfrastructureUnavailableError
from graph.core.files import encode_gzip_json, read_gzip_json, write_store_file
from graph.core.identity import validate_project_id
from graph.plan.models import MeaningRevisions, PlanRunRecord, PlanStepName, StepState
from graph.plan.names import PlanMode
from graph.plan.ports import PlanAnswerKind

__all__ = ["FilePlanStore", "open_plan_store"]

RUNS_FOLDER = "plan-runs"
ANSWERS_FOLDER = "plan-answers"
REVISIONS_FILE = "plan-revisions.json.gz"
RECORD_FILE = "run.json.gz"
RESULT_FILE = "result.json.gz"
ARTIFACT_SUFFIX = ".json.gz"


def _list_folders(folder: Path) -> list[str]:
    try:
        return sorted(entry.name for entry in folder.iterdir() if entry.is_dir() and not entry.name.startswith("."))
    except FileNotFoundError:
        return []
    except OSError as error:
        raise InfrastructureUnavailableError("file_store_unreadable", error_type=type(error).__name__) from None


class FilePlanStore:
    """``PlanStore`` on disk: runs, artifacts, planner answers and meaning revisions."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._lock = asyncio.Lock()

    def _project(self, project_id: str) -> Path:
        return self._root / validate_project_id(project_id)

    def _folder(self, project_id: str, run_id: str) -> Path:
        if not run_id or run_id != Path(run_id).name or run_id.startswith("."):
            raise ConfigurationError("invalid_run_id", run_id=run_id)
        return self._project(project_id) / RUNS_FOLDER / run_id

    def _answer_path(self, project_id: str, kind: PlanAnswerKind, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._project(project_id) / ANSWERS_FOLDER / kind / f"{digest}{ARTIFACT_SUFFIX}"

    async def _load_record(self, project_id: str, run_id: str) -> PlanRunRecord | None:
        stored = await asyncio.to_thread(read_gzip_json, self._folder(project_id, run_id) / RECORD_FILE)
        return None if stored is None else PlanRunRecord.model_validate(stored)

    async def _store_record(self, record: PlanRunRecord) -> None:
        path = self._folder(record.project_id, record.run_id) / RECORD_FILE
        await asyncio.to_thread(write_store_file, path, encode_gzip_json(record.model_dump(mode="json")))

    async def _require(self, project_id: str, run_id: str) -> PlanRunRecord:
        record = await self._load_record(project_id, run_id)
        if record is None:
            raise ConfigurationError("run_not_found", run_id=run_id)
        return record

    async def create_run(self, record: PlanRunRecord) -> None:
        async with self._lock:
            if await self._load_record(record.project_id, record.run_id) is not None:
                raise ConfigurationError("run_exists", run_id=record.run_id)
            await self._store_record(record)

    async def get_run(self, project_id: str, run_id: str) -> PlanRunRecord | None:
        return await self._load_record(project_id, run_id)

    async def latest_run(
        self,
        project_id: str,
        *,
        finished_only: bool = False,
        modes: Collection[PlanMode] | None = None,
    ) -> PlanRunRecord | None:
        folder = self._project(project_id) / RUNS_FOLDER
        names = await asyncio.to_thread(_list_folders, folder)
        latest: PlanRunRecord | None = None
        for name in names:
            record = await self._load_record(project_id, name)
            if record is None or (finished_only and not record.finished):
                continue
            if modes is not None and record.mode not in modes:
                continue
            if latest is None or (record.started_at, record.run_id) > (latest.started_at, latest.run_id):
                latest = record
        return latest

    async def put_progress(self, project_id: str, run_id: str, step: PlanStepName, state: StepState) -> None:
        async with self._lock:
            record = await self._require(project_id, run_id)
            progress = {**record.progress, step: state}
            await self._store_record(record.model_copy(update={"progress": progress}))

    async def finish_run(self, result: Mapping[str, Any]) -> None:
        try:
            project_id = result["project_id"]
            run_id = result["run_id"]
            status = result["status"]
            failure = result.get("failure")
        except KeyError as error:
            raise ConfigurationError("invalid_plan_result", missing=error.args[0]) from None
        if not isinstance(project_id, str) or not isinstance(run_id, str):
            raise ConfigurationError("invalid_plan_result")
        if status not in ("succeeded", "partially_succeeded", "failed"):
            raise ConfigurationError("invalid_plan_result", field="status")
        if failure is not None and not isinstance(failure, str):
            raise ConfigurationError("invalid_plan_result", field="failure")

        async with self._lock:
            record = await self._require(project_id, run_id)
            folder = self._folder(project_id, run_id)
            await asyncio.to_thread(write_store_file, folder / RESULT_FILE, encode_gzip_json(dict(result)))
            finished = record.model_copy(update={"status": status, "failure": failure})
            await self._store_record(finished)

    async def get_result(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        stored = await asyncio.to_thread(read_gzip_json, self._folder(project_id, run_id) / RESULT_FILE)
        if stored is None:
            return None
        if not isinstance(stored, dict):
            raise InfrastructureUnavailableError("file_store_unreadable", error_type="result_not_object")
        return stored

    async def put_artifact(self, project_id: str, run_id: str, step: PlanStepName, payload: Mapping[str, Any]) -> None:
        path = self._folder(project_id, run_id) / f"{step}{ARTIFACT_SUFFIX}"
        await asyncio.to_thread(write_store_file, path, encode_gzip_json(payload))

    async def get_artifact(self, project_id: str, run_id: str, step: PlanStepName) -> dict[str, Any] | None:
        stored = await asyncio.to_thread(read_gzip_json, self._folder(project_id, run_id) / f"{step}{ARTIFACT_SUFFIX}")
        if stored is None:
            return None
        if not isinstance(stored, dict):
            raise InfrastructureUnavailableError("file_store_unreadable", error_type="artifact_not_object")
        return stored

    async def get_answer(
        self,
        project_id: str,
        kind: PlanAnswerKind,
        key: str,
        reuse_key: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        stored = await asyncio.to_thread(read_gzip_json, self._answer_path(project_id, kind, key))
        if not isinstance(stored, dict):
            return None
        if stored.get("key") != key or stored.get("reuse_key") != reuse_key:
            return None
        if run_id is not None and stored.get("run_id") != run_id:
            return None
        payload = stored.get("payload")
        return payload if isinstance(payload, dict) else None

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
        data = encode_gzip_json({"key": key, "reuse_key": reuse_key, "run_id": run_id, "payload": payload})
        await asyncio.to_thread(write_store_file, self._answer_path(project_id, kind, key), data)

    async def get_revisions(self, project_id: str) -> MeaningRevisions:
        stored = await asyncio.to_thread(read_gzip_json, self._project(project_id) / REVISIONS_FILE)
        if stored is None:
            return MeaningRevisions()
        return MeaningRevisions.model_validate(stored)

    async def put_revisions(self, project_id: str, revisions: MeaningRevisions) -> None:
        path = self._project(project_id) / REVISIONS_FILE
        await asyncio.to_thread(write_store_file, path, encode_gzip_json(revisions.model_dump(mode="json")))


def open_plan_store(root: str | Path) -> FilePlanStore:
    """The plan file store under ``root`` (see the module docstring for the layout)."""

    return FilePlanStore(Path(root))
