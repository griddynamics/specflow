"""GCS object store (SPECS §7; verified behaviour in SP-B).

The sync client never runs on the event loop: every call is a thread hop.
Create-only writes use ``blob.open("wb", if_generation_match=0)``; the 412
arrives at ``close()`` as ``InvalidResponse`` and becomes ``ObjectAlreadyExistsError``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any

from google.api_core import exceptions as gexc
from google.cloud import storage
from google.cloud.storage.exceptions import InvalidResponse

from logger.logger import ERROR, INFO, get_logger, log_event
from settings import StorageSettings
from storage.base import (
    CHUNK_SIZE,
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStoreError,
    StorageUnavailableError,
    StoredObject,
)
from storage.keys import validate_key

logger = get_logger("storage.gcs")

CHECK_TIMEOUT_SECONDS = 5
CHECK_CACHE_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 60
LIST_PROBE_MAX_RESULTS = 1
DIAGNOSTIC_PERMISSIONS = (
    "storage.buckets.get",
    "storage.objects.list",
    "storage.objects.get",
    "storage.objects.create",
    "storage.objects.delete",
)


class GcsObjectWriter:
    """Async wrapper over the blocking ``BlobWriter`` (create-only resumable upload)."""

    def __init__(self, blob: Any, key: str, content_type: str) -> None:
        # open() makes no request; the session starts lazily at the first full chunk.
        self._w = blob.open(
            "wb",
            if_generation_match=0,
            chunk_size=CHUNK_SIZE,
            content_type=content_type,
        )
        self._key = key
        self._size = 0
        self._done = False

    @property
    def size(self) -> int:
        return self._size

    async def write(self, chunk: bytes) -> None:
        if self._done:
            raise ObjectStoreError("writer is closed")
        await asyncio.to_thread(self._w.write, chunk)
        self._size += len(chunk)

    async def close(self) -> StoredObject:
        if self._done:
            raise ObjectStoreError("writer is closed")
        self._done = True
        try:
            await asyncio.to_thread(self._w.close)
        except InvalidResponse as exc:
            if getattr(exc.response, "status_code", None) == HTTPStatus.PRECONDITION_FAILED:
                await asyncio.shield(asyncio.to_thread(self._w.terminate))
                raise ObjectAlreadyExistsError(self._key) from exc
            raise
        log_event(logger, INFO, "storage.object.written", key=self._key, size=self._size)
        return StoredObject(self._key, self._size)

    async def abort(self) -> None:
        """Cancel the upload session; no object remains. Safe to call repeatedly.

        The thread hop is shielded, so a cancelled caller (client disconnect)
        cannot leave the session open.
        """

        self._done = True
        await asyncio.shield(asyncio.to_thread(self._w.terminate))
        log_event(logger, INFO, "storage.object.aborted", key=self._key)


class GcsObjectStore:
    """``ObjectStore`` on one bucket; every key is stored under ``STORAGE_PREFIX``."""

    def __init__(
        self,
        settings: StorageSettings,
        *,
        client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._prefix = settings.prefix
        # One client per process: build the store once and share it.
        self._owns_client = client is None
        self._client = client or storage.Client(project=settings.google_cloud_project)
        self._closed = False
        self._bucket = self._client.bucket(settings.bucket)
        self._clock = clock
        self._check_at: float | None = None
        self._check_error: StorageUnavailableError | None = None

    async def close(self) -> None:
        """Release the HTTP session of the client this store created (idempotent).

        A client handed in by the caller stays the caller's to close.
        """

        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await asyncio.to_thread(self._client.close)

    def _blob(self, key: str) -> Any:
        return self._bucket.blob(self._prefix + validate_key(key))

    async def open_writer(self, key: str, *, content_type: str) -> GcsObjectWriter:
        key = validate_key(key)
        return GcsObjectWriter(self._blob(key), key, content_type)

    async def download_to_path(self, key: str, path: Path) -> int:
        blob = self._blob(key)
        try:
            await asyncio.to_thread(blob.download_to_filename, str(path), timeout=REQUEST_TIMEOUT_SECONDS)
        except gexc.NotFound:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            raise ObjectNotFoundError(key) from None
        except BaseException:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            raise
        return (await asyncio.to_thread(path.stat)).st_size

    async def exists(self, key: str) -> bool:
        blob = self._blob(key)
        return bool(await asyncio.to_thread(blob.exists, timeout=REQUEST_TIMEOUT_SECONDS))

    async def delete(self, key: str) -> None:
        """Delete; a missing object counts as success (idempotent)."""

        blob = self._blob(key)
        try:
            await asyncio.to_thread(blob.delete, timeout=REQUEST_TIMEOUT_SECONDS)
        except gexc.NotFound:
            return
        log_event(logger, INFO, "storage.object.deleted", key=key)

    async def check(self) -> None:
        """Readiness: list one object under the prefix (never reads an object), cached 10 s."""

        now = self._clock()
        if self._check_at is not None and now - self._check_at < CHECK_CACHE_SECONDS:
            if self._check_error is not None:
                raise self._check_error
            return
        try:
            await asyncio.to_thread(self._list_one)
        except Exception as exc:  # any listing failure means not ready
            error = StorageUnavailableError(f"storage listing failed: {type(exc).__name__}")
            log_event(logger, ERROR, "storage.readiness.failed", error_type=type(exc).__name__)
            self._check_at, self._check_error = self._clock(), error
            raise error from exc
        self._check_at, self._check_error = self._clock(), None

    def _list_one(self) -> None:
        blobs = self._client.list_blobs(
            self._bucket,
            prefix=self._prefix,
            max_results=LIST_PROBE_MAX_RESULTS,
            timeout=CHECK_TIMEOUT_SECONDS,
        )
        next(iter(blobs), None)

    async def count_prefix(self, prefix: str) -> int:
        """``ObjectAdmin``: count the objects under a full bucket prefix."""

        return await asyncio.to_thread(self._count, prefix)

    async def delete_prefix(self, prefix: str) -> int:
        """``ObjectAdmin``: delete every object under a full bucket prefix; returns the count."""

        return await asyncio.to_thread(self._delete_all, prefix)

    def _count(self, prefix: str) -> int:
        return sum(1 for _ in self._client.list_blobs(self._bucket, prefix=prefix))

    def _delete_all(self, prefix: str) -> int:
        deleted = 0
        for blob in list(self._client.list_blobs(self._bucket, prefix=prefix)):
            blob.delete()
            deleted += 1
        return deleted

    async def diagnose(self) -> dict[str, bool]:
        """Diagnostic only (never part of readiness): permissions the caller holds."""

        granted = await asyncio.to_thread(
            self._bucket.test_iam_permissions,
            list(DIAGNOSTIC_PERMISSIONS),
            timeout=CHECK_TIMEOUT_SECONDS,
        )
        return {p: p in set(granted) for p in DIAGNOSTIC_PERMISSIONS}
