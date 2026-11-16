"""The run artifact store of gap runs (docs/graph/gap/architecture.md "Run steps", api.md "Gap runs"; FR-GRUN-07).

Each step of a gap run writes its artifact here before the next step starts, and its progress (``pending``,
``running``, ``done``, ``failed``); an interrupted run resumes at its first unfinished step with the same run id. The
host keeps the files under ``GRAPH_STATE_DIR`` (``open_gap_run_store``):

- ``<root>/<project_id>/gap-runs/<run_id>/run.json.gz``: the run record (``GapRunRecord``);
- ``<root>/<project_id>/gap-runs/<run_id>/<step>.json.gz``: the artifact of each finished step;
- ``<root>/<project_id>/gap-runs/<run_id>/result.json.gz``: the ``GapRunResult`` of a finished run.

Files are gzip JSON written atomically (``graph.core.files``); I/O failures raise ``InfrastructureUnavailableError``.
``MemoryGapRunStore`` keeps the same semantics in memory (tests, and hosts without a state directory). Artifacts hold
seeds and reports with their quotes: they are run state, never logged (NFR-GSEC-02).
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, Field

from graph.core.errors import ConfigurationError, InfrastructureUnavailableError
from graph.core.files import encode_gzip_json, read_gzip_json, write_store_file
from graph.core.identity import validate_project_id
from graph.gap.models import GapModel, GapRunResult
from graph.gap.names import RunMode, RunStatus

RUNS_FOLDER: Final = "gap-runs"
RECORD_FILE: Final = "run.json.gz"
RESULT_FILE: Final = "result.json.gz"
ARTIFACT_SUFFIX: Final = ".json.gz"
StepState = Literal["pending", "running", "done", "failed"]
STEP_STATES: Final[tuple[StepState, ...]] = ("pending", "running", "done", "failed")


class GapRunRecord(GapModel):
    """One gap run as the run store keeps it: its mode, the build it follows, its status and the state of each step.
    ``site_ids`` are the sites a ``site`` run names (kept for its resume)."""

    project_id: str
    run_id: str
    mode: RunMode
    build_id: str | None = None
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    status: RunStatus = "running"
    progress: Mapping[str, StepState] = Field(default_factory=dict)
    site_ids: tuple[str, ...] = ()

    @property
    def finished(self) -> bool:
        return self.finished_at is not None


@runtime_checkable
class GapRunStore(Protocol):
    """Run records, step artifacts and results of gap runs, per project."""

    async def create_run(self, record: GapRunRecord) -> None: ...

    async def get_run(self, project_id: str, run_id: str) -> GapRunRecord | None: ...

    async def latest_run(self, project_id: str, *, finished_only: bool = False) -> GapRunRecord | None: ...

    async def put_progress(self, project_id: str, run_id: str, step: str, state: StepState) -> None: ...

    async def put_artifact(self, project_id: str, run_id: str, step: str, payload: Mapping[str, Any]) -> None: ...

    async def get_artifact(self, project_id: str, run_id: str, step: str) -> Mapping[str, Any] | None: ...

    async def finish_run(self, project_id: str, result: GapRunResult) -> None: ...

    async def get_result(self, project_id: str, run_id: str) -> GapRunResult | None: ...


def _check_state(state: str) -> None:
    if state not in STEP_STATES:
        raise ConfigurationError("invalid_step_state", state=state)


def _later(record: GapRunRecord, latest: GapRunRecord | None) -> bool:
    return latest is None or (record.started_at, record.run_id) > (latest.started_at, latest.run_id)


def _finished(record: GapRunRecord, result: GapRunResult) -> GapRunRecord:
    return record.model_copy(update={"finished_at": datetime.now(UTC), "status": result.status})


class MemoryGapRunStore:
    """``GapRunStore`` in memory, with the semantics of the file store."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], GapRunRecord] = {}
        self.artifacts: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.results: dict[tuple[str, str], GapRunResult] = {}

    async def create_run(self, record: GapRunRecord) -> None:
        key = (record.project_id, record.run_id)
        if key in self.records:
            raise ConfigurationError("run_exists", run_id=record.run_id)
        self.records[key] = record

    async def get_run(self, project_id: str, run_id: str) -> GapRunRecord | None:
        return self.records.get((project_id, run_id))

    async def latest_run(self, project_id: str, *, finished_only: bool = False) -> GapRunRecord | None:
        latest: GapRunRecord | None = None
        for (project, _), record in self.records.items():
            if project == project_id and (record.finished or not finished_only) and _later(record, latest):
                latest = record
        return latest

    async def put_progress(self, project_id: str, run_id: str, step: str, state: StepState) -> None:
        _check_state(state)
        record = self._require(project_id, run_id)
        self.records[(project_id, run_id)] = record.model_copy(update={"progress": {**record.progress, step: state}})

    async def put_artifact(self, project_id: str, run_id: str, step: str, payload: Mapping[str, Any]) -> None:
        self._require(project_id, run_id)
        self.artifacts[(project_id, run_id, step)] = copy.deepcopy(dict(payload))

    async def get_artifact(self, project_id: str, run_id: str, step: str) -> Mapping[str, Any] | None:
        found = self.artifacts.get((project_id, run_id, step))
        return None if found is None else copy.deepcopy(found)

    async def finish_run(self, project_id: str, result: GapRunResult) -> None:
        record = self._require(project_id, result.run_id)
        self.results[(project_id, result.run_id)] = result
        self.records[(project_id, result.run_id)] = _finished(record, result)

    async def get_result(self, project_id: str, run_id: str) -> GapRunResult | None:
        return self.results.get((project_id, run_id))

    def _require(self, project_id: str, run_id: str) -> GapRunRecord:
        record = self.records.get((project_id, run_id))
        if record is None:
            raise ConfigurationError("run_not_found", run_id=run_id)
        return record


def _list_folders(folder: Path) -> list[str]:
    try:
        return sorted(entry.name for entry in folder.iterdir() if entry.is_dir() and not entry.name.startswith("."))
    except FileNotFoundError:
        return []
    except OSError as error:
        raise InfrastructureUnavailableError("file_store_unreadable", error_type=type(error).__name__) from None


class FileGapRunStore:
    """``GapRunStore`` on disk (layout in the module docstring)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._lock = asyncio.Lock()

    def _folder(self, project_id: str, run_id: str) -> Path:
        if not run_id or run_id != Path(run_id).name or run_id.startswith("."):
            raise ConfigurationError("invalid_run_id", run_id=run_id)
        return self._root / validate_project_id(project_id) / RUNS_FOLDER / run_id

    def _artifact_path(self, project_id: str, run_id: str, step: str) -> Path:
        if not step or step != Path(step).name or step.startswith(".") or step in ("run", "result"):
            raise ConfigurationError("invalid_step", step=step)
        return self._folder(project_id, run_id) / f"{step}{ARTIFACT_SUFFIX}"

    async def _read(self, path: Path) -> dict[str, Any] | None:
        stored = await asyncio.to_thread(read_gzip_json, path)
        if stored is None:
            return None
        if not isinstance(stored, dict):
            raise InfrastructureUnavailableError("file_store_unreadable", error_type="not_object")
        return stored

    async def _write(self, path: Path, payload: Any) -> None:
        await asyncio.to_thread(write_store_file, path, encode_gzip_json(payload))

    async def _load(self, project_id: str, run_id: str) -> GapRunRecord | None:
        stored = await self._read(self._folder(project_id, run_id) / RECORD_FILE)
        return None if stored is None else GapRunRecord.model_validate(stored)

    async def _store(self, record: GapRunRecord) -> None:
        await self._write(self._folder(record.project_id, record.run_id) / RECORD_FILE, record.model_dump(mode="json"))

    async def _require(self, project_id: str, run_id: str) -> GapRunRecord:
        record = await self._load(project_id, run_id)
        if record is None:
            raise ConfigurationError("run_not_found", run_id=run_id)
        return record

    async def create_run(self, record: GapRunRecord) -> None:
        async with self._lock:
            if await self._load(record.project_id, record.run_id) is not None:
                raise ConfigurationError("run_exists", run_id=record.run_id)
            await self._store(record)

    async def get_run(self, project_id: str, run_id: str) -> GapRunRecord | None:
        return await self._load(project_id, run_id)

    async def latest_run(self, project_id: str, *, finished_only: bool = False) -> GapRunRecord | None:
        folder = self._root / validate_project_id(project_id) / RUNS_FOLDER
        latest: GapRunRecord | None = None
        for name in await asyncio.to_thread(_list_folders, folder):
            record = await self._load(project_id, name)
            if record is not None and (record.finished or not finished_only) and _later(record, latest):
                latest = record
        return latest

    async def put_progress(self, project_id: str, run_id: str, step: str, state: StepState) -> None:
        _check_state(state)
        async with self._lock:
            record = await self._require(project_id, run_id)
            await self._store(record.model_copy(update={"progress": {**record.progress, step: state}}))

    async def put_artifact(self, project_id: str, run_id: str, step: str, payload: Mapping[str, Any]) -> None:
        await self._require(project_id, run_id)
        await self._write(self._artifact_path(project_id, run_id, step), dict(payload))

    async def get_artifact(self, project_id: str, run_id: str, step: str) -> Mapping[str, Any] | None:
        return await self._read(self._artifact_path(project_id, run_id, step))

    async def finish_run(self, project_id: str, result: GapRunResult) -> None:
        async with self._lock:
            record = await self._require(project_id, result.run_id)
            folder = self._folder(project_id, result.run_id)
            await self._write(folder / RESULT_FILE, result.model_dump(mode="json"))
            await self._store(_finished(record, result))

    async def get_result(self, project_id: str, run_id: str) -> GapRunResult | None:
        stored = await self._read(self._folder(project_id, run_id) / RESULT_FILE)
        return None if stored is None else GapRunResult.model_validate(stored)


def open_gap_run_store(root: Path) -> GapRunStore:
    """The file run store under ``root`` (the host passes ``GRAPH_STATE_DIR``)."""

    return FileGapRunStore(root)
