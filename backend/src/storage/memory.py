"""In-memory object store. Tests only; refused outside ``APP_ENV=local`` (AC-4.6)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from storage.base import (
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStoreError,
    ObjectWriter,
    StoredObject,
)
from storage.keys import validate_key


class InMemoryObjectStore:
    """Dict-backed store with the same create-only and abort semantics as GCS."""

    def __init__(self, *, app_env: str) -> None:
        if app_env != "local":
            raise ObjectStoreError("the in-memory object store is allowed only when APP_ENV=local")
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}

    async def open_writer(self, key: str, *, content_type: str) -> ObjectWriter:
        return _MemoryWriter(self, validate_key(key), content_type)

    async def download_to_path(self, key: str, path: Path) -> int:
        data = self._get(key)
        await asyncio.to_thread(path.write_bytes, data)
        return len(data)

    async def exists(self, key: str) -> bool:
        return validate_key(key) in self.objects

    async def delete(self, key: str) -> None:
        self.objects.pop(validate_key(key), None)

    async def check(self) -> None:
        return None

    async def close(self) -> None:
        """No client to release."""

    def _get(self, key: str) -> bytes:
        try:
            return self.objects[validate_key(key)]
        except KeyError:
            raise ObjectNotFoundError(key) from None


class _MemoryWriter:
    def __init__(self, store: InMemoryObjectStore, key: str, content_type: str) -> None:
        self._store, self._key, self._content_type = store, key, content_type
        self._buffer = bytearray()
        self._done = False

    async def write(self, chunk: bytes) -> None:
        if self._done:
            raise ObjectStoreError("writer is closed")
        self._buffer += chunk

    async def close(self) -> StoredObject:
        if self._done:
            raise ObjectStoreError("writer is closed")
        self._done = True
        if self._key in self._store.objects:
            raise ObjectAlreadyExistsError(self._key)
        self._store.objects[self._key] = bytes(self._buffer)
        self._store.content_types[self._key] = self._content_type
        return StoredObject(self._key, len(self._buffer))

    async def abort(self) -> None:
        self._done = True
        self._buffer.clear()
