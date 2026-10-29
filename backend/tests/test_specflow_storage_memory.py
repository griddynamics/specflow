"""In-memory store: same contract as GCS, local only (AC-4.3, AC-4.4, AC-4.6)."""

from __future__ import annotations

import pytest

from storage.base import (
    InvalidObjectKeyError,
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
)
from storage.memory import InMemoryObjectStore


@pytest.mark.parametrize("env", ["dev", "prod", "", "LOCAL"])
def test_refused_outside_local(env):
    with pytest.raises(ObjectStoreError):
        InMemoryObjectStore(app_env=env)


def test_satisfies_the_protocol():
    assert isinstance(InMemoryObjectStore(app_env="local"), ObjectStore)


async def test_write_download_exists_delete(tmp_path):
    store = InMemoryObjectStore(app_env="local")
    w = await store.open_writer("a/b.pdf", content_type="application/pdf")
    await w.write(b"he")
    await w.write(b"llo")
    stored = await w.close()
    assert (stored.key, stored.size) == ("a/b.pdf", 5)
    assert await store.exists("a/b.pdf")
    assert store.content_types["a/b.pdf"] == "application/pdf"
    out = tmp_path / "o"
    assert await store.download_to_path("a/b.pdf", out) == 5
    assert out.read_bytes() == b"hello"
    await store.delete("a/b.pdf")
    await store.delete("a/b.pdf")  # idempotent
    assert not await store.exists("a/b.pdf")
    with pytest.raises(ObjectNotFoundError):
        await store.download_to_path("a/b.pdf", out)


async def test_create_only_and_abort_leave_nothing():
    store = InMemoryObjectStore(app_env="local")
    first = await store.open_writer("k", content_type="x/y")
    await first.write(b"1")
    await first.close()
    second = await store.open_writer("k", content_type="x/y")
    await second.write(b"2")
    with pytest.raises(ObjectAlreadyExistsError):
        await second.close()
    assert store.objects["k"] == b"1"
    third = await store.open_writer("other", content_type="x/y")
    await third.write(b"3")
    await third.abort()
    assert "other" not in store.objects


async def test_unsafe_keys_rejected_everywhere(tmp_path):
    store = InMemoryObjectStore(app_env="local")
    with pytest.raises(InvalidObjectKeyError):
        await store.open_writer("../x", content_type="a/b")
    with pytest.raises(InvalidObjectKeyError):
        await store.exists("/x")
    with pytest.raises(InvalidObjectKeyError):
        await store.delete("a//b")
    with pytest.raises(InvalidObjectKeyError):
        await store.download_to_path("..", tmp_path / "o")
