"""File implementations of the build ports, rooted at a directory the host passes (api.md "Ports").

Layout under the root given to ``open_file_stores`` (tech-specs 5.1):

- ``<root>/<project_id>/spec-ids.json``: the core ``FileSpecIdRegistry``;
- ``<root>/<project_id>/builds/<build_id>/{build,result,<step>}.json.gz``: build records, results and
  step artifacts;
- ``<root>/<project_id>/corpus-index.json``: versions of the lineages ``FileCorpusSource`` has seen;
- ``<root>/_extraction/<kind>/<sha256(key)>.json.gz``: reusable extraction results. The
  ``ExtractionStore`` port carries no project id, so these are shared by the projects under the root;
  ``_`` cannot start a project id, so the folder never collides with a project.

Every file is written atomically through ``graph.core.files.write_atomic``; ``.json.gz`` files are
gzip JSON without a timestamp. This is the only build module that touches the disk (architecture.md
I5). I/O failures raise ``InfrastructureUnavailableError``, except in the extraction store, which is a
cache: an unreadable or corrupt entry is a miss (logged); ``get_many`` reads a batch in one pass and
``put_many`` writes one without an fsync per file (a file torn by a crash fails its gzip check and
reads as a miss). Kind ``vectors`` holds one embedding per distinct text (``graph.build.vectors``).
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import tempfile
import zlib
from collections.abc import Collection, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from graph.build.corpus import first_heading
from graph.build.models import BuildMode, BuildResult, StepName
from graph.build.ports import (
    BuildRecord,
    BuildStore,
    CorpusRevision,
    CorpusSource,
    ExtractionKind,
    ExtractionStore,
    JsonObject,
    StepState,
)
from graph.core.errors import ConfigurationError, CorpusError, InfrastructureUnavailableError
from graph.core.files import FileSpecIdRegistry, write_atomic
from graph.core.identity import SpecIdRegistry, validate_project_id
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("graph.build.files")

EXTRACTION_FOLDER: Final = "_extraction"
BUILDS_FOLDER: Final = "builds"
RECORD_FILE: Final = "build.json.gz"
RESULT_FILE: Final = "result.json.gz"
ARTIFACT_SUFFIX: Final = ".json.gz"
CORPUS_INDEX_FILE: Final = "corpus-index.json"
MARKDOWN_SUFFIX: Final = ".md"
FIRST_VERSION: Final = 1


def _encode(payload: Any) -> bytes:
    return gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)


def _read_gzip_json(path: Path) -> Any | None:
    """The JSON value of a gzip file; None when the file does not exist."""

    try:
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, EOFError, ValueError, zlib.error) as error:
        raise InfrastructureUnavailableError("file_store_unreadable", error_type=type(error).__name__) from None


def _write(path: Path, data: bytes) -> None:
    try:
        write_atomic(path, data)
    except OSError as error:
        raise InfrastructureUnavailableError("file_store_unwritable", error_type=type(error).__name__) from None


def _write_batch(items: list[tuple[Path, bytes]]) -> None:
    """Write each file through a temporary file and a rename, without fsync (cache entries only)."""

    try:
        for path, data in items:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                Path(temporary).replace(path)
            except BaseException:
                with suppress(FileNotFoundError):
                    Path(temporary).unlink()
                raise
    except OSError as error:
        raise InfrastructureUnavailableError("file_store_unwritable", error_type=type(error).__name__) from None


def _read_batch(paths: list[tuple[str, Path]]) -> tuple[dict[str, Any], int]:
    """The JSON value of each existing file by key, and the number of unreadable files (cache reads)."""

    found: dict[str, Any] = {}
    unreadable = 0
    for key, path in paths:
        try:
            value = _read_gzip_json(path)
        except InfrastructureUnavailableError:
            unreadable += 1
            continue
        if value is not None:
            found[key] = value
    return found, unreadable


class FileExtractionStore:
    """``ExtractionStore`` on disk: ``<root>/<kind>/<sha256(key)>.json.gz`` holding key, fingerprint, payload."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, kind: ExtractionKind, key: str) -> Path:
        return self._root / kind / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}{ARTIFACT_SUFFIX}"

    async def get(self, kind: ExtractionKind, key: str, fingerprint: str) -> JsonObject | None:
        """The stored payload; None when absent, stored with another fingerprint, or unreadable."""

        try:
            stored = await asyncio.to_thread(_read_gzip_json, self._path(kind, key))
        except InfrastructureUnavailableError as error:
            log_event(
                logger,
                WARNING,
                "graph.extraction_cache.unreadable",
                kind=kind,
                code=error.code,
                error_type=error.fields.get("error_type"),
            )
            return None
        if not isinstance(stored, dict) or stored.get("key") != key or stored.get("fingerprint") != fingerprint:
            return None
        payload = stored.get("payload")
        return payload if isinstance(payload, dict) else None

    async def get_many(self, kind: ExtractionKind, keys: Sequence[str], fingerprint: str) -> dict[str, JsonObject]:
        """``get`` of every key in one read pass: payloads by key; absent, mismatched or unreadable
        entries are left out (unreadable ones logged once, with their count)."""

        paths = [(key, self._path(kind, key)) for key in dict.fromkeys(keys)]
        stored, unreadable = await asyncio.to_thread(_read_batch, paths)
        if unreadable:
            log_event(logger, WARNING, "graph.extraction_cache.unreadable", kind=kind, entries=unreadable)
        result: dict[str, JsonObject] = {}
        for key, value in stored.items():
            if isinstance(value, dict) and value.get("key") == key and value.get("fingerprint") == fingerprint:
                payload = value.get("payload")
                if isinstance(payload, dict):
                    result[key] = payload
        return result

    async def put(self, kind: ExtractionKind, key: str, fingerprint: str, payload: JsonObject) -> None:
        data = _encode({"key": key, "fingerprint": fingerprint, "payload": payload})
        await asyncio.to_thread(_write, self._path(kind, key), data)

    async def put_many(self, kind: ExtractionKind, fingerprint: str, entries: Mapping[str, JsonObject]) -> None:
        """``put`` of every entry (payload by key) in one write pass, without an fsync per file."""

        items = [
            (self._path(kind, key), _encode({"key": key, "fingerprint": fingerprint, "payload": payload}))
            for key, payload in entries.items()
        ]
        await asyncio.to_thread(_write_batch, items)


class FileBuildStore:
    """``BuildStore`` on disk: ``<root>/<project_id>/builds/<build_id>/`` per build."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._lock = asyncio.Lock()

    def _builds(self, project_id: str) -> Path:
        return self._root / validate_project_id(project_id) / BUILDS_FOLDER

    def _folder(self, project_id: str, build_id: str) -> Path:
        if not build_id or build_id != Path(build_id).name or build_id.startswith("."):
            raise ConfigurationError("invalid_build_id")
        return self._builds(project_id) / build_id

    async def _load_record(self, project_id: str, build_id: str) -> BuildRecord | None:
        stored = await asyncio.to_thread(_read_gzip_json, self._folder(project_id, build_id) / RECORD_FILE)
        return None if stored is None else BuildRecord.model_validate(stored)

    async def _store_record(self, record: BuildRecord) -> None:
        path = self._folder(record.project_id, record.build_id) / RECORD_FILE
        await asyncio.to_thread(_write, path, _encode(record.model_dump(mode="json")))

    async def _require(self, project_id: str, build_id: str) -> BuildRecord:
        record = await self._load_record(project_id, build_id)
        if record is None:
            raise ConfigurationError("build_not_found", build_id=build_id)
        return record

    async def create_build(self, record: BuildRecord) -> None:
        async with self._lock:
            if await self._load_record(record.project_id, record.build_id) is not None:
                raise ConfigurationError("build_exists", build_id=record.build_id)
            await self._store_record(record)

    async def latest_build(
        self,
        project_id: str,
        *,
        finished_only: bool = False,
        modes: Collection[BuildMode] | None = None,
    ) -> BuildRecord | None:
        folder = self._builds(project_id)
        names = await asyncio.to_thread(_list_folders, folder)
        latest: BuildRecord | None = None
        for name in names:
            record = await self._load_record(project_id, name)
            if record is None or (finished_only and not record.finished):
                continue
            if modes is not None and record.mode not in modes:
                continue
            if latest is None or (record.started_at, record.build_id) > (latest.started_at, latest.build_id):
                latest = record
        return latest

    async def get_build(self, project_id: str, build_id: str) -> BuildRecord | None:
        return await self._load_record(project_id, build_id)

    async def put_progress(self, project_id: str, build_id: str, step: StepName, state: StepState) -> None:
        async with self._lock:
            record = await self._require(project_id, build_id)
            progress = {**record.progress, step: state}
            await self._store_record(record.model_copy(update={"progress": progress}))

    async def finish_build(self, result: BuildResult) -> None:
        async with self._lock:
            record = await self._require(result.project_id, result.build_id)
            folder = self._folder(result.project_id, result.build_id)
            await asyncio.to_thread(_write, folder / RESULT_FILE, _encode(result.model_dump(mode="json")))
            finished = record.model_copy(update={"status": result.status, "finished_at": result.finished_at})
            await self._store_record(finished)

    async def get_result(self, project_id: str, build_id: str) -> BuildResult | None:
        stored = await asyncio.to_thread(_read_gzip_json, self._folder(project_id, build_id) / RESULT_FILE)
        return None if stored is None else BuildResult.model_validate(stored)

    async def put_artifact(self, project_id: str, build_id: str, step: StepName, payload: JsonObject) -> None:
        path = self._folder(project_id, build_id) / f"{step}{ARTIFACT_SUFFIX}"
        await asyncio.to_thread(_write, path, _encode(payload))

    async def get_artifact(self, project_id: str, build_id: str, step: StepName) -> JsonObject | None:
        path = self._folder(project_id, build_id) / f"{step}{ARTIFACT_SUFFIX}"
        return await asyncio.to_thread(_read_gzip_json, path)


def _list_folders(folder: Path) -> list[str]:
    try:
        return sorted(entry.name for entry in folder.iterdir() if entry.is_dir() and not entry.name.startswith("."))
    except FileNotFoundError:
        return []
    except OSError as error:
        raise InfrastructureUnavailableError("file_store_unreadable", error_type=type(error).__name__) from None


class FileCorpusSource:
    """``CorpusSource`` over a folder: every ``.md`` file under it is one lineage.

    ``lineage_id`` and ``path`` are the file's path relative to the folder (POSIX); ``revision_id`` is
    the sha256 of its bytes; ``version`` starts at 1 and increments whenever the content hash
    changes, tracked in ``<index_root>/<project_id>/corpus-index.json``; ``title`` is the first
    heading, else the file name without its extension. Files and folders whose name starts with ``.``
    are ignored. Document order is the order of the relative paths. An unreadable file or one that
    is not UTF-8 raises ``CorpusError``. ``snapshot(..., dry_run=True)`` computes the same versions
    without writing the index (``estimate_build``).
    """

    def __init__(self, folder: Path, index_root: Path) -> None:
        self._folder = Path(folder)
        self._index_root = Path(index_root)
        self._lock = asyncio.Lock()

    async def snapshot(self, project_id: str, *, dry_run: bool = False) -> list[CorpusRevision]:
        index_path = self._index_root / validate_project_id(project_id) / CORPUS_INDEX_FILE
        async with self._lock:
            files = await asyncio.to_thread(self._read_files)
            index = await asyncio.to_thread(_read_index, index_path)
            changed = False
            revisions = []
            for lineage_id, data in files:
                revision_id = hashlib.sha256(data).hexdigest()
                entry = index.get(lineage_id)
                if entry is None or entry.get("revision_id") != revision_id:
                    version = FIRST_VERSION if entry is None else int(entry.get("version", 0)) + 1
                    index[lineage_id] = {"revision_id": revision_id, "version": version}
                    changed = True
                markdown = data.decode("utf-8")
                revisions.append(
                    CorpusRevision(
                        lineage_id=lineage_id,
                        revision_id=revision_id,
                        version=index[lineage_id]["version"],
                        path=lineage_id,
                        title=first_heading(markdown) or Path(lineage_id).stem,
                        markdown=markdown,
                    )
                )
            if changed and not dry_run:
                payload = json.dumps(index, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
                await asyncio.to_thread(_write, index_path, payload)
            return revisions

    def _read_files(self) -> list[tuple[str, bytes]]:
        if not self._folder.is_dir():
            raise CorpusError("corpus_folder_missing")
        found = []
        for path in self._folder.rglob(f"*{MARKDOWN_SUFFIX}"):
            relative = path.relative_to(self._folder)
            if any(part.startswith(".") for part in relative.parts) or not path.is_file():
                continue
            found.append(relative.as_posix())
        files = []
        for lineage_id in sorted(found):
            try:
                data = (self._folder / lineage_id).read_bytes()
                data.decode("utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise CorpusError("corpus_unreadable", lineage_id=lineage_id, error_type=type(error).__name__) from None
            files.append((lineage_id, data))
        return files


def _read_index(path: Path) -> dict[str, dict[str, Any]]:
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise InfrastructureUnavailableError("corpus_index_unreadable", error_type=type(error).__name__) from None
    if not isinstance(index, dict):
        raise InfrastructureUnavailableError("corpus_index_unreadable", error_type="not_an_object")
    return index


@dataclass(frozen=True, slots=True)
class FileStores:
    """The file ports under one root; ``corpus`` is None when no corpus folder was given."""

    registry: SpecIdRegistry
    extraction: ExtractionStore
    builds: BuildStore
    corpus: CorpusSource | None


def open_file_stores(root: Path, corpus_folder: Path | None = None) -> FileStores:
    """File implementations of every port under ``root`` (see the module docstring for the layout)."""

    root = Path(root)
    return FileStores(
        registry=FileSpecIdRegistry(root),
        extraction=FileExtractionStore(root / EXTRACTION_FOLDER),
        builds=FileBuildStore(root),
        corpus=None if corpus_folder is None else FileCorpusSource(corpus_folder, root),
    )
