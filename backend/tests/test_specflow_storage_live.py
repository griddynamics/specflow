"""Live GCS tests (tier 2): ``SPECFLOW_TEST_GCS=1``, bucket and ADC from the env file.

Every object lives under ``dev/local/pytest/<run-id>/`` (a constructor override,
AC-4.7); teardown deletes it and the counts before and after are logged.
Cost hygiene (COLDLINE): exactly one upload of 100 MiB, everything else tiny.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import uuid

import pytest
from google.cloud import storage

from logger.logger import INFO, get_logger, log_event
from settings import StorageSettings
from specflow_live import requires_live
from storage.base import InvalidObjectKeyError, ObjectAlreadyExistsError, ObjectNotFoundError
from storage.gcs import GcsObjectStore
from storage.keys import document_original

pytestmark = requires_live("GCS")
logger = get_logger("tests.specflow_storage_live")
MiB = 1024 * 1024
PS = shutil.which("ps") or "ps"


def _count(client, bucket, prefix) -> int:
    return sum(1 for _ in client.list_blobs(bucket, prefix=prefix))


@pytest.fixture(scope="module")
def gcs(request):
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")

    def report(phase, prefix, objects):
        log_event(logger, INFO, "storage.live.counts", phase=phase, prefix=prefix, objects=objects)
        if reporter:
            reporter.write_line(f"\nGCS live objects {phase}: {objects} under {prefix}")

    run_prefix = f"dev/local/pytest/{uuid.uuid4().hex[:12]}/"
    cfg = StorageSettings(
        STORAGE_BUCKET=os.environ["STORAGE_BUCKET"],
        STORAGE_PREFIX=run_prefix,
        GOOGLE_CLOUD_PROJECT=os.environ["GOOGLE_CLOUD_PROJECT"],
    )
    client = storage.Client(project=cfg.google_cloud_project)
    before = _count(client, cfg.bucket, run_prefix)
    report("before", run_prefix, before)
    store = GcsObjectStore(cfg, client=client)
    yield store, client, cfg
    for blob in list(client.list_blobs(cfg.bucket, prefix=run_prefix)):
        blob.delete()
    after = _count(client, cfg.bucket, run_prefix)
    report("after", run_prefix, after)
    assert after == 0


async def _put(store, key, data: bytes):
    w = await store.open_writer(key, content_type="application/pdf")
    await w.write(data)
    return await w.close()


async def test_roundtrip_create_only_exists_download_delete(gcs, tmp_path):
    store, client, cfg = gcs
    key = document_original("p-live", "d-live")
    stored = await _put(store, key, b"%PDF-live")
    assert stored.size == 9
    blob = client.bucket(cfg.bucket).get_blob(cfg.prefix + key)
    assert blob is not None and blob.content_type == "application/pdf"
    assert await store.exists(key)
    with pytest.raises(ObjectAlreadyExistsError):
        await _put(store, key, b"second")
    out = tmp_path / "o.pdf"
    assert await store.download_to_path(key, out) == 9
    assert out.read_bytes() == b"%PDF-live"  # original untouched by the failed second write
    await store.delete(key)
    await store.delete(key)  # idempotent
    assert not await store.exists(key)
    with pytest.raises(ObjectNotFoundError):
        await store.download_to_path(key, tmp_path / "gone.pdf")


async def test_abort_leaves_no_object(gcs):
    store, _, _ = gcs
    w = await store.open_writer("abort/a.pdf", content_type="application/pdf")
    await w.write(b"x" * (5 * MiB))  # > one chunk: the resumable session exists
    await w.abort()
    assert not await store.exists("abort/a.pdf")


async def test_unsafe_key_is_rejected_before_any_request(gcs):
    store, _, _ = gcs
    with pytest.raises(InvalidObjectKeyError):
        await store.exists("../escape")


async def test_check_and_diagnose(gcs):
    store, _, _ = gcs
    await store.check()
    perms = await store.diagnose()
    assert perms["storage.objects.list"] is True


async def test_100_mib_stream_bounds_rss_and_integrity(gcs, tmp_path, request, record_property):
    """AC-4.2 (macOS): 100 MiB, RSS growth < 64 MiB, no temp file, sha matches."""

    store, _, _ = gcs
    key = "big/100mib.bin"
    sha = hashlib.sha256()
    block = os.urandom(MiB)

    def current_mib() -> float:
        # Current RSS of this process (KiB from `ps` on macOS and Linux). ru_maxrss is a
        # process-lifetime high-water mark: after earlier tests it never grows (S12 saw 0.0).
        out = subprocess.run(
            [PS, "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=True
        ).stdout
        return int(out.strip()) / 1024

    peak = [current_mib()]
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], current_mib())
            stop.wait(0.02)

    w = await store.open_writer(key, content_type="application/octet-stream")
    base = current_mib()
    peak[0] = base
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        for _ in range(100):
            await w.write(block)
            sha.update(block)
        stored = await w.close()
    finally:
        stop.set()
        sampler.join()
    peak[0] = max(peak[0], current_mib())
    growth = peak[0] - base
    log_event(logger, INFO, "storage.live.rss", growth_mib=round(growth, 1), size=stored.size)
    record_property("rss_growth_mib", round(growth, 1))
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter:
        reporter.write_line(f"\nAC-4.2 RSS growth for 100 MiB: {growth:.1f} MiB")
    assert stored.size == 100 * MiB
    assert growth < 64, f"RSS grew {growth:.1f} MiB for 100 MiB (limit 64)"
    out = tmp_path / "big.bin"
    await store.download_to_path(key, out)
    assert hashlib.sha256(out.read_bytes()).digest() == sha.digest()
    await store.delete(key)
