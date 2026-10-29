"""Object storage contract (SPECS §7): errors, value types and the Protocol."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

MiB = 1024 * 1024
#: Resumable-upload chunk size. 8 MiB grew RSS by ~120 MiB per 200 MiB on macOS
#: (SP-B); 4 MiB stays flat. Must be a multiple of 256 KiB.
CHUNK_SIZE = 4 * MiB


class ObjectStoreError(Exception):
    """Base class for storage failures."""


class ObjectAlreadyExistsError(ObjectStoreError):
    """A create-only write hit an existing key."""


class ObjectNotFoundError(ObjectStoreError):
    """The key does not exist."""


class InvalidObjectKeyError(ObjectStoreError, ValueError):
    """The key is absolute, empty-segmented or contains traversal."""


class StorageUnavailableError(ObjectStoreError):
    """The readiness listing failed or timed out."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size: int


@runtime_checkable
class ObjectWriter(Protocol):
    async def write(self, chunk: bytes) -> None: ...

    async def close(self) -> StoredObject: ...

    async def abort(self) -> None: ...


@runtime_checkable
class ObjectStore(Protocol):
    async def open_writer(self, key: str, *, content_type: str) -> ObjectWriter: ...

    async def download_to_path(self, key: str, path: Path) -> int: ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> None: ...

    async def check(self) -> None: ...


@runtime_checkable
class ManagedObjectStore(ObjectStore, Protocol):
    """An ``ObjectStore`` that also releases its client on shutdown."""

    async def close(self) -> None: ...


@runtime_checkable
class ObjectAdmin(Protocol):
    """Bulk operations for cleanup tooling. Not part of ``ObjectStore``: the service never holds it.

    ``prefix`` is the full bucket prefix (``STORAGE_PREFIX`` is not added).
    """

    async def count_prefix(self, prefix: str) -> int: ...

    async def delete_prefix(self, prefix: str) -> int: ...


@runtime_checkable
class ManagedObjectAdmin(ObjectAdmin, Protocol):
    async def close(self) -> None: ...
