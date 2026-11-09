"""File implementation of the core ports, rooted at a directory the host passes."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import tempfile
import zlib
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

from graph.core.errors import InfrastructureUnavailableError
from graph.core.identity import SpecIdRow, validate_project_id


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same directory, then rename."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    except BaseException:
        with suppress(FileNotFoundError):
            Path(temporary).unlink()
        raise


class FileSpecIdRegistry:
    """``SpecIdRegistry`` on disk: one JSON file per project, ``<root>/<project_id>/spec-ids.json``."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._lock = asyncio.Lock()

    def _path(self, project_id: str) -> Path:
        return self._root / validate_project_id(project_id) / "spec-ids.json"

    def _load(self, path: Path) -> dict[str, Any]:
        try:
            loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"rows": {}, "builds": []}
        except (OSError, ValueError) as error:
            raise InfrastructureUnavailableError("registry_unreadable", error_type=type(error).__name__) from None
        return loaded

    async def taken(self, project_id: str) -> dict[str, SpecIdRow]:
        data = await asyncio.to_thread(self._load, self._path(project_id))
        return {spec_id: SpecIdRow(**row) for spec_id, row in data["rows"].items()}

    async def record(self, project_id: str, build_id: str, rows: Sequence[SpecIdRow]) -> None:
        path = self._path(project_id)
        async with self._lock:
            data = await asyncio.to_thread(self._load, path)
            for row in rows:
                data["rows"][row.spec_id] = asdict(row)
            if build_id not in data["builds"]:
                data["builds"].append(build_id)
            payload = json.dumps(data, sort_keys=True, indent=1).encode("utf-8")
            try:
                await asyncio.to_thread(write_atomic, path, payload)
            except OSError as error:
                raise InfrastructureUnavailableError("registry_unwritable", error_type=type(error).__name__) from None


def encode_gzip_json(payload: Any) -> bytes:
    """``payload`` as compact UTF-8 JSON, gzip-compressed with a fixed mtime (same input, same bytes)."""

    return gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)


def read_gzip_json(path: Path) -> Any | None:
    """The JSON value of a gzip file; None when the file does not exist."""

    try:
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, EOFError, ValueError, zlib.error) as error:
        raise InfrastructureUnavailableError("file_store_unreadable", error_type=type(error).__name__) from None


def write_store_file(path: Path, data: bytes) -> None:
    """``write_atomic`` for a file store: an I/O failure becomes ``InfrastructureUnavailableError``."""

    try:
        write_atomic(path, data)
    except OSError as error:
        raise InfrastructureUnavailableError("file_store_unwritable", error_type=type(error).__name__) from None
