"""S3 object store (SPECS SPL-STO-02; verified against RustFS in plan step 01).

The sync boto3 client never runs on the event loop: every call is a thread hop.
Create-only writes use ``IfNoneMatch="*"`` on ``PutObject`` and on
``CompleteMultipartUpload``; a 412 becomes ``ObjectAlreadyExistsError``.
Credentials come from the standard AWS chain only.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from logger.logger import ERROR, INFO, get_logger, log_event
from settings import StorageSettings
from storage.base import (
    MiB,
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStoreError,
    StorageUnavailableError,
    StoredObject,
)
from storage.keys import validate_key

logger = get_logger("storage.s3")

#: Part size (the S3 minimum). Own constant: not the GCS ``CHUNK_SIZE``.
S3_PART_SIZE = 5 * MiB
MAX_PARTS = 10_000
CHECK_TIMEOUT_SECONDS = 5
CHECK_CACHE_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 60
DELETE_BATCH = 1000
NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def _status(error: ClientError) -> int:
    return int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _is_not_found(error: ClientError) -> bool:
    return _status(error) == HTTPStatus.NOT_FOUND or _code(error) in NOT_FOUND_CODES


def _is_conflict(error: ClientError) -> bool:
    return _status(error) == HTTPStatus.CONFLICT or _code(error) == "ConditionalRequestConflict"


def _is_precondition_failed(error: ClientError) -> bool:
    return _status(error) == HTTPStatus.PRECONDITION_FAILED or _code(error) == "PreconditionFailed"


def _is_missing_bucket(error: ClientError) -> bool:
    return _code(error) == "NoSuchBucket"


def _bucket_error(bucket: str) -> StorageUnavailableError:
    """The clear error for a bucket that does not exist (the app never creates one)."""

    return StorageUnavailableError(f"bucket {bucket!r} does not exist; create it first")


class S3ObjectWriter:
    """Buffers chunks into parts; one ``PutObject`` when small, else a multipart upload."""

    def __init__(self, store: S3ObjectStore, key: str, content_type: str, part_size: int) -> None:
        self._store = store
        self._key = key
        self._content_type = content_type
        self._part_size = part_size
        # A list of chunks joined per part: `bytearray +=` grows RSS ~15x more (step 01 spike).
        self._buffer: list[bytes] = []
        self._buffered = 0
        self._size = 0
        self._upload_id: str | None = None
        self._parts: list[dict[str, Any]] = []
        self._done = False
        self._aborted = False

    @property
    def size(self) -> int:
        return self._size

    async def write(self, chunk: bytes) -> None:
        if self._done:
            raise ObjectStoreError("writer is closed")
        self._buffer.append(bytes(chunk))
        self._buffered += len(chunk)
        self._size += len(chunk)
        if self._buffered <= self._part_size:
            return
        # Join once per call and slice by offset: a large chunk is not re-joined for every part.
        data = memoryview(b"".join(self._buffer))
        offset = 0
        while len(data) - offset > self._part_size:
            await self._upload_part(bytes(data[offset : offset + self._part_size]))
            offset += self._part_size
        rest = bytes(data[offset:])
        self._buffer, self._buffered = ([rest] if rest else []), len(rest)

    async def _upload_part(self, body: bytes) -> None:
        store = self._store
        if self._upload_id is None:
            try:
                response = await asyncio.to_thread(
                    store.client.create_multipart_upload,
                    Bucket=store.bucket,
                    Key=store.full_key(self._key),
                    ContentType=self._content_type,
                )
            except ClientError as exc:
                if _is_missing_bucket(exc):
                    raise _bucket_error(store.bucket) from exc
                raise
            self._upload_id = response["UploadId"]
        number = len(self._parts) + 1
        if number > MAX_PARTS:
            raise ObjectStoreError(f"object exceeds {MAX_PARTS} parts")
        response = await asyncio.to_thread(
            store.client.upload_part,
            Bucket=store.bucket,
            Key=store.full_key(self._key),
            UploadId=self._upload_id,
            PartNumber=number,
            Body=body,
        )
        self._parts.append({"ETag": response["ETag"], "PartNumber": number})

    async def close(self) -> StoredObject:
        if self._done:
            raise ObjectStoreError("writer is closed")
        self._done = True
        body = b"".join(self._buffer)
        self._buffer, self._buffered = [], 0
        try:
            if self._upload_id is None:
                await self._put(body)
            else:
                await self._complete(body)
        except BaseException:
            await self.abort()
            raise
        self._aborted = True  # completed: nothing is left to abort
        log_event(logger, INFO, "storage.object.written", key=self._key, size=self._size)
        return StoredObject(self._key, self._size)

    async def _put(self, body: bytes) -> None:
        store = self._store
        try:
            await asyncio.to_thread(
                store.client.put_object,
                Bucket=store.bucket,
                Key=store.full_key(self._key),
                Body=body,
                ContentType=self._content_type,
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if _is_precondition_failed(exc):
                raise ObjectAlreadyExistsError(self._key) from exc
            if _is_missing_bucket(exc):
                raise _bucket_error(store.bucket) from exc
            raise

    async def _complete(self, last: bytes) -> None:
        if last:
            await self._upload_part(last)
        store = self._store
        retried = False
        while True:
            try:
                await asyncio.to_thread(
                    store.client.complete_multipart_upload,
                    Bucket=store.bucket,
                    Key=store.full_key(self._key),
                    UploadId=self._upload_id,
                    MultipartUpload={"Parts": self._parts},
                    IfNoneMatch="*",
                )
            except ClientError as exc:
                if _is_conflict(exc) and not retried:
                    retried = True  # a concurrent complete on the same key: retry once
                    continue
                if _is_conflict(exc) or _is_precondition_failed(exc):
                    raise ObjectAlreadyExistsError(self._key) from exc
                raise
            else:
                return

    async def abort(self) -> None:
        """Cancel the upload; no object and no open upload remain. Safe to call repeatedly.

        The thread hop is shielded, so a cancelled caller cannot leave the upload open.
        A 404 ``NoSuchUpload`` (already gone) counts as success.
        """

        self._done = True
        self._buffer, self._buffered = [], 0
        if self._aborted or self._upload_id is None:
            return
        self._aborted = True
        store = self._store
        try:
            await asyncio.shield(
                asyncio.to_thread(
                    store.client.abort_multipart_upload,
                    Bucket=store.bucket,
                    Key=store.full_key(self._key),
                    UploadId=self._upload_id,
                )
            )
        except ClientError as exc:
            if not (_is_not_found(exc) or _code(exc) == "NoSuchUpload"):
                raise
        log_event(logger, INFO, "storage.object.aborted", key=self._key)


class S3ObjectStore:
    """``ObjectStore`` + ``ObjectAdmin`` on one bucket; keys live under ``STORAGE_PREFIX``."""

    def __init__(
        self,
        settings: StorageSettings,
        *,
        client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        part_size: int = S3_PART_SIZE,
    ) -> None:
        self._prefix = settings.prefix
        self.bucket = settings.bucket
        self._part_size = part_size
        self._owns_client = client is None
        self._closed = False
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            config=Config(
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                read_timeout=REQUEST_TIMEOUT_SECONDS,
                retries={"mode": "standard"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                s3={"addressing_style": settings.s3_addressing},
            ),
        )
        self._clock = clock
        self._check_at: float | None = None
        self._check_error: StorageUnavailableError | None = None

    async def close(self) -> None:
        """Release the client this store created (idempotent); a handed-in client stays the caller's."""

        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await asyncio.to_thread(self.client.close)

    def full_key(self, key: str) -> str:
        return self._prefix + validate_key(key)

    async def open_writer(self, key: str, *, content_type: str) -> S3ObjectWriter:
        key = validate_key(key)
        return S3ObjectWriter(self, key, content_type, self._part_size)

    async def download_to_path(self, key: str, path: Path) -> int:
        full = self.full_key(key)
        try:
            await asyncio.to_thread(self._download, full, path)
        except ClientError as exc:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            if _is_missing_bucket(exc):
                raise _bucket_error(self.bucket) from exc
            if _is_not_found(exc):
                raise ObjectNotFoundError(key) from None
            raise
        except BaseException:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            raise
        return (await asyncio.to_thread(path.stat)).st_size

    def _download(self, full_key: str, path: Path) -> None:
        response = self.client.get_object(Bucket=self.bucket, Key=full_key)
        body = response["Body"]
        try:
            with path.open("wb") as out:
                for chunk in body.iter_chunks(MiB):
                    out.write(chunk)
        finally:
            body.close()

    async def exists(self, key: str) -> bool:
        full = self.full_key(key)
        try:
            await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=full)
        except ClientError as exc:
            if _is_missing_bucket(exc):
                raise _bucket_error(self.bucket) from exc
            if _is_not_found(exc):
                return False
            raise
        return True

    async def delete(self, key: str) -> None:
        """Delete; a missing object counts as success (S3 is idempotent)."""

        full = self.full_key(key)
        try:
            await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=full)
        except ClientError as exc:
            if _is_missing_bucket(exc):
                raise _bucket_error(self.bucket) from exc
            if not _is_not_found(exc):
                raise
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
            await asyncio.to_thread(self.client.list_objects_v2, Bucket=self.bucket, Prefix=self._prefix, MaxKeys=1)
        except Exception as exc:  # any listing failure means not ready
            error = StorageUnavailableError(f"storage listing failed: {type(exc).__name__}")
            log_event(logger, ERROR, "storage.readiness.failed", error_type=type(exc).__name__)
            self._check_at, self._check_error = self._clock(), error
            raise error from exc
        self._check_at, self._check_error = self._clock(), None

    async def count_prefix(self, prefix: str) -> int:
        """``ObjectAdmin``: count the objects under a full bucket prefix."""

        return await asyncio.to_thread(lambda: len(self._keys(prefix)))

    async def delete_prefix(self, prefix: str) -> int:
        """``ObjectAdmin``: delete every object under a full bucket prefix; returns the count."""

        return await asyncio.to_thread(self._delete_all, prefix)

    def _keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            args: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                args["ContinuationToken"] = token
            try:
                page = self.client.list_objects_v2(**args)
            except ClientError as exc:
                if _is_missing_bucket(exc):
                    raise _bucket_error(self.bucket) from exc
                raise
            keys.extend(item["Key"] for item in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return keys
            token = page["NextContinuationToken"]

    def _delete_all(self, prefix: str) -> int:
        keys = self._keys(prefix)
        for start in range(0, len(keys), DELETE_BATCH):
            batch = keys[start : start + DELETE_BATCH]
            response = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
            if response.get("Errors"):
                raise ObjectStoreError(f"delete failed for {len(response['Errors'])} objects")
        return len(keys)
