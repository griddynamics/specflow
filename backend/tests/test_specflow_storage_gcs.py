"""GcsObjectStore against a fake client: writer wrapper, prefix, 412, abort, readiness."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core import exceptions as gexc
from google.cloud.storage.exceptions import InvalidResponse

import storage.gcs as gcs_module
from settings import StorageSettings
from storage.base import (
    CHUNK_SIZE,
    InvalidObjectKeyError,
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStoreError,
    StorageUnavailableError,
)
from storage.factory import build_object_store
from storage.gcs import GcsObjectStore
from storage.memory import InMemoryObjectStore


class FakeBlobWriter:
    def __init__(self, close_error=None, gate=None):
        self.writes, self.terminated, self.closed = [], 0, False
        self.write_threads, self.close_error, self.gate = set(), close_error, gate
        self.entered, self.done = threading.Event(), threading.Event()

    def write(self, data):
        self.write_threads.add(threading.get_ident())
        self.writes.append(bytes(data))

    def close(self):
        if self.close_error:
            raise self.close_error
        self.closed = True

    def terminate(self):
        self.entered.set()
        if self.gate:
            self.gate.wait(2)
        self.terminated += 1
        self.done.set()


class FakeBlob:
    def __init__(self, name, writer=None):
        self.name, self.open_kwargs = name, None
        self.writer = writer or FakeBlobWriter()
        self.deleted, self.delete_error = 0, None
        self.download_error, self.payload, self.exists_result = None, b"data", True

    def open(self, mode, **kwargs):
        self.open_mode, self.open_kwargs = mode, kwargs
        return self.writer

    def download_to_filename(self, filename, **_kwargs):
        if self.download_error:
            raise self.download_error
        with Path(filename).open("wb") as fh:
            fh.write(self.payload)

    def exists(self, **_kwargs):
        return self.exists_result

    def delete(self, **_kwargs):
        if self.delete_error:
            raise self.delete_error
        self.deleted += 1


class FakeClient:
    def __init__(self):
        self.blobs, self.list_calls, self.list_error = {}, [], None
        self.list_thread = None
        self.next_writer = None

    def bucket(self, name):
        self.bucket_name = name
        return self

    def blob(self, name):
        blob = FakeBlob(name, self.next_writer)
        self.next_writer = None
        self.blobs[name] = blob
        return blob

    def list_blobs(self, _bucket, **kwargs):
        self.list_thread = threading.get_ident()
        self.list_calls.append(kwargs)
        if self.list_error:
            raise self.list_error
        return iter([])

    def test_iam_permissions(self, perms, **_kwargs):
        return [p for p in perms if p != "storage.objects.delete"]


def settings(prefix="dev/local/pytest/x/"):
    return StorageSettings(STORAGE_BUCKET="bkt", STORAGE_PREFIX=prefix, GOOGLE_CLOUD_PROJECT="proj")


def make(prefix="dev/local/pytest/x/", clock=None):
    client = FakeClient()
    kwargs = {"clock": clock} if clock else {}
    return GcsObjectStore(settings(prefix), client=client, **kwargs), client


def invalid_response(status):
    return InvalidResponse(SimpleNamespace(status_code=status), "boom")


async def test_prefix_is_applied_and_writer_options_are_create_only_chunked():
    store, client = make()
    w = await store.open_writer("projects/p/a.pdf", content_type="application/pdf")
    blob = client.blobs["dev/local/pytest/x/projects/p/a.pdf"]
    assert blob.open_mode == "wb"
    assert blob.open_kwargs == {
        "if_generation_match": 0,
        "chunk_size": CHUNK_SIZE,
        "content_type": "application/pdf",
    }
    assert CHUNK_SIZE == 4 * 1024 * 1024 and CHUNK_SIZE % (256 * 1024) == 0
    assert "storage_class" not in blob.open_kwargs
    assert w.size == 0


async def test_chunks_pass_through_off_the_event_loop_and_size_is_counted():
    store, client = make()
    w = await store.open_writer("k", content_type="a/b")
    writer = client.blobs["dev/local/pytest/x/k"].writer
    chunks = [b"a" * 10, b"b" * 7, b"c"]
    for c in chunks:
        await w.write(c)
    stored = await w.close()
    assert writer.writes == chunks and writer.closed
    assert (stored.key, stored.size) == ("k", 18)
    assert threading.get_ident() not in writer.write_threads


async def test_hashing_in_the_caller_loop_sees_every_byte():
    store, client = make()
    w = await store.open_writer("k", content_type="a/b")
    sha, payload = hashlib.sha256(), [b"x" * 1000] * 5
    for c in payload:
        await w.write(c)
        sha.update(c)
    await w.close()
    writer = client.blobs["dev/local/pytest/x/k"].writer
    assert hashlib.sha256(b"".join(writer.writes)).digest() == sha.digest()


async def test_412_at_close_becomes_object_already_exists_after_terminate():
    store, client = make()
    client.next_writer = FakeBlobWriter(close_error=invalid_response(412))
    w = await store.open_writer("k", content_type="a/b")
    await w.write(b"x")
    with pytest.raises(ObjectAlreadyExistsError):
        await w.close()
    assert client.blobs["dev/local/pytest/x/k"].writer.terminated == 1


async def test_other_invalid_responses_are_not_swallowed():
    store, client = make()
    client.next_writer = FakeBlobWriter(close_error=invalid_response(500))
    w = await store.open_writer("k", content_type="a/b")
    with pytest.raises(InvalidResponse):
        await w.close()
    assert client.blobs["dev/local/pytest/x/k"].writer.terminated == 0


async def test_closed_writer_rejects_further_use():
    store, _ = make()
    w = await store.open_writer("k", content_type="a/b")
    await w.close()
    with pytest.raises(ObjectStoreError):
        await w.write(b"x")
    with pytest.raises(ObjectStoreError):
        await w.close()


async def test_abort_terminates_and_is_repeatable():
    store, client = make()
    w = await store.open_writer("k", content_type="a/b")
    await w.write(b"x")
    await w.abort()
    assert client.blobs["dev/local/pytest/x/k"].writer.terminated == 1
    with pytest.raises(ObjectStoreError):
        await w.write(b"y")


async def test_abort_completes_even_when_the_caller_is_cancelled():
    store, client = make()
    gate = threading.Event()
    client.next_writer = FakeBlobWriter(gate=gate)
    w = await store.open_writer("k", content_type="a/b")
    writer = client.blobs["dev/local/pytest/x/k"].writer
    task = asyncio.create_task(w.abort())
    assert await asyncio.to_thread(writer.entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.terminated == 0  # still blocked in the thread
    gate.set()
    assert await asyncio.to_thread(writer.done.wait, 2)
    assert writer.terminated == 1


async def test_delete_is_idempotent_and_other_errors_propagate():
    store, client = make()
    await store.delete("k")
    assert client.blobs["dev/local/pytest/x/k"].deleted == 1
    gone = FakeBlob("n")
    gone.delete_error = gexc.NotFound("nope")
    client.blob = lambda _name: gone
    await store.delete("k")  # second delete: NotFound is success
    gone.delete_error = gexc.Forbidden("no")
    with pytest.raises(gexc.Forbidden):
        await store.delete("k")


async def test_exists_and_download(tmp_path):
    store, client = make()
    assert await store.exists("k") is True
    out = tmp_path / "f.pdf"
    assert await store.download_to_path("k", out) == 4
    assert out.read_bytes() == b"data"
    client.blob = lambda _name: _missing()
    with pytest.raises(ObjectNotFoundError):
        await store.download_to_path("k", tmp_path / "g.pdf")
    assert not (tmp_path / "g.pdf").exists()


def _missing():
    blob = FakeBlob("m")
    blob.download_error = gexc.NotFound("nope")
    return blob


async def test_keys_are_validated_before_any_client_call(tmp_path):
    store, client = make()
    for call in (
        store.open_writer("../x", content_type="a/b"),
        store.exists("/x"),
        store.delete("a//b"),
        store.download_to_path("a/../b", tmp_path / "o"),
    ):
        with pytest.raises(InvalidObjectKeyError):
            await call
    assert client.blobs == {}


async def test_check_lists_one_object_under_the_prefix_in_a_thread_with_timeout():
    store, client = make()
    await store.check()
    assert client.list_calls == [{"prefix": "dev/local/pytest/x/", "max_results": 1, "timeout": 5}]
    assert client.list_thread != threading.get_ident()


async def test_check_is_cached_for_ten_seconds_including_failures():
    now = [100.0]
    store, client = make(clock=lambda: now[0])
    await store.check()
    await store.check()
    assert len(client.list_calls) == 1
    now[0] += 10.5
    client.list_error = gexc.DeadlineExceeded("slow")
    with pytest.raises(StorageUnavailableError):
        await store.check()
    with pytest.raises(StorageUnavailableError):
        await store.check()
    assert len(client.list_calls) == 2
    now[0] += 10.5
    client.list_error = None
    await store.check()
    assert len(client.list_calls) == 3


async def test_check_turns_a_non_google_failure_into_storage_unavailable_and_caches_it():
    store, client = make()
    client.list_error = ValueError("bad credentials")
    with pytest.raises(StorageUnavailableError) as first:
        await store.check()
    assert isinstance(first.value.__cause__, ValueError)
    with pytest.raises(StorageUnavailableError):
        await store.check()
    assert len(client.list_calls) == 1


async def test_diagnose_reports_per_permission_and_is_not_used_by_check():
    store, client = make()
    result = await store.diagnose()
    assert result["storage.objects.delete"] is False
    assert result["storage.objects.list"] is True
    await store.check()
    assert len(client.list_calls) == 1


async def test_the_factory_builds_the_gcs_store_and_memory_close_is_a_no_op(monkeypatch):
    monkeypatch.setattr(gcs_module.storage, "Client", lambda **_kw: FakeClient())
    built = build_object_store(settings("p/"), app_env="local")
    assert isinstance(built, GcsObjectStore)
    await InMemoryObjectStore(app_env="local").close()
