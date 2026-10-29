"""Live S3 contract tests through ``S3ObjectStore`` (tier ``S3``; SPL-STO-02, SPL-STO-04).

Run with ``SPECFLOW_TEST_S3=1``, ``AWS_ENDPOINT_URL_S3``, ``AWS_ACCESS_KEY_ID`` and
``AWS_SECRET_ACCESS_KEY`` against an S3-compatible endpoint that already has the bucket
``STORAGE_BUCKET`` (default ``specflow-local``). Every object lives under
``pytest/<run-id>/`` and the teardown deletes it. The app never creates a bucket.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid

import pytest

from logger.logger import INFO, get_logger, log_event
from settings import StorageSettings
from specflow_live import requires_live
from storage.base import MiB, ObjectAlreadyExistsError, StorageUnavailableError
from storage.s3 import S3_PART_SIZE, S3ObjectStore

pytestmark = requires_live("S3")

logger = get_logger("tests.specflow_storage_s3_live")
PS = shutil.which("ps") or "ps"
RACE_ROUNDS = 6
STREAM_MIB = 200
MAX_RSS_GROWTH_MIB = 32


def _settings(prefix: str, bucket: str | None = None) -> StorageSettings:
    return StorageSettings(
        STORAGE_BACKEND="s3",
        STORAGE_BUCKET=bucket or os.environ.get("STORAGE_BUCKET", "specflow-local"),
        STORAGE_PREFIX=prefix,
        AWS_ENDPOINT_URL_S3=os.environ["AWS_ENDPOINT_URL_S3"],
        AWS_REGION=os.environ.get("AWS_REGION", "us-east-1"),
        STORAGE_S3_ADDRESSING="path",
    )


@pytest.fixture
async def store():
    prefix = f"pytest/{uuid.uuid4().hex[:12]}/"
    s3 = S3ObjectStore(_settings(prefix))
    yield s3
    await s3.delete_prefix(prefix)
    assert await s3.count_prefix(prefix) == 0
    await s3.close()


def _open_uploads(s3: S3ObjectStore, key: str) -> int:
    response = s3.client.list_multipart_uploads(Bucket=s3.bucket, Prefix=s3.full_key(key))
    return len(response.get("Uploads", []))


async def _write(s3: S3ObjectStore, key: str, data: bytes) -> None:
    writer = await s3.open_writer(key, content_type="application/octet-stream")
    await writer.write(data)
    await writer.close()


async def test_single_put_is_create_only(store):
    await _write(store, "a/put", b"one")

    with pytest.raises(ObjectAlreadyExistsError):
        await _write(store, "a/put", b"two")

    body = store.client.get_object(Bucket=store.bucket, Key=store.full_key("a/put"))["Body"].read()
    assert body == b"one"
    assert await store.exists("a/put")


async def test_multipart_close_is_create_only_and_leaves_no_open_upload(store):
    await _write(store, "a/mpu", b"first")
    big = os.urandom(S3_PART_SIZE + 1)

    with pytest.raises(ObjectAlreadyExistsError):
        await _write(store, "a/mpu", big)

    body = store.client.get_object(Bucket=store.bucket, Key=store.full_key("a/mpu"))["Body"].read()
    assert body == b"first"
    assert _open_uploads(store, "a/mpu") == 0

    await _write(store, "a/mpu-free", big)
    assert await store.exists("a/mpu-free")


async def test_two_concurrent_closes_on_one_key_create_exactly_one_object(store):
    for round_no in range(RACE_ROUNDS):
        key = f"race/{round_no}"
        writers = []
        for _ in range(2):
            writer = await store.open_writer(key, content_type="application/octet-stream")
            await writer.write(os.urandom(S3_PART_SIZE + 1))  # opens the multipart upload
            writers.append(writer)

        results = await asyncio.gather(*(w.close() for w in writers), return_exceptions=True)

        failed = [r for r in results if isinstance(r, ObjectAlreadyExistsError)]
        assert len(failed) == 1, results
        assert sum(1 for r in results if not isinstance(r, BaseException)) == 1, results
        assert await store.count_prefix(store.full_key(key)) == 1
        assert _open_uploads(store, key) == 0


async def test_abort_is_idempotent_and_leaves_nothing(store):
    writer = await store.open_writer("a/abort", content_type="application/octet-stream")
    await writer.write(os.urandom(S3_PART_SIZE + 1))
    assert _open_uploads(store, "a/abort") == 1

    await writer.abort()
    await writer.abort()

    assert _open_uploads(store, "a/abort") == 0
    assert not await store.exists("a/abort")


async def test_check_lists_the_prefix_and_delete_is_idempotent(store):
    await _write(store, "a/list", b"x")

    await store.check()
    await store.delete("a/list")
    await store.delete("a/list")

    assert not await store.exists("a/list")


def _rss_mib() -> float:
    out = subprocess.run([PS, "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=True)
    return int(out.stdout.strip()) / 1024


async def test_streaming_200_mib_grows_rss_by_less_than_32_mib(store):
    chunk = os.urandom(MiB)
    writer = await store.open_writer("big/stream", content_type="application/octet-stream")
    base = peak = _rss_mib()
    for _ in range(STREAM_MIB):
        await writer.write(chunk)
        peak = max(peak, _rss_mib())
    stored = await writer.close()
    peak = max(peak, _rss_mib())

    growth = peak - base
    log_event(
        logger,
        INFO,
        "storage.live.rss",
        mib=STREAM_MIB,
        growth_mib=round(growth, 1),
        base_mib=round(base),
        peak_mib=round(peak),
    )
    assert stored.size == STREAM_MIB * MiB
    assert growth < MAX_RSS_GROWTH_MIB


async def test_missing_bucket_gives_a_clear_error():
    s3 = S3ObjectStore(_settings("pytest/none/", bucket=f"specflow-missing-{uuid.uuid4().hex[:8]}"))
    try:
        with pytest.raises(StorageUnavailableError, match="does not exist"):
            await _write(s3, "a/x", b"x")
        with pytest.raises(StorageUnavailableError, match="does not exist"):
            await _write(s3, "a/y", os.urandom(S3_PART_SIZE + 1))
        with pytest.raises(StorageUnavailableError):
            await s3.check()
    finally:
        await s3.close()
