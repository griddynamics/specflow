"""ObjectAdmin on the GCS adapter, build_object_admin, and the storage_cleanup example."""

from __future__ import annotations

import json

import pytest

import storage_cleanup as cleanup
from settings import StorageSettings
from storage import factory
from storage.base import ObjectAdmin
from storage.gcs import GcsObjectStore

PID = "11111111-2222-3333-4444-555555555555"
PREFIX = f"dev/local/x/projects/{PID}/"


class Blob:
    def __init__(self, name):
        self.name, self.deleted = name, False

    def delete(self):
        self.deleted = True


class Client:
    def __init__(self, names):
        self.blobs = [Blob(n) for n in names]
        self.prefixes = []

    def bucket(self, name):
        return name

    def list_blobs(self, _bucket, prefix):
        self.prefixes.append(prefix)
        return iter([b for b in self.blobs if b.name.startswith(prefix) and not b.deleted])


def store(names):
    client = Client(names)
    settings = StorageSettings(STORAGE_BUCKET="b", STORAGE_PREFIX="dev/local/x/")
    return GcsObjectStore(settings, client=client), client


async def test_gcs_count_and_delete_prefix_are_bounded_by_the_prefix():
    adapter, client = store([PREFIX + "a", PREFIX + "b", "dev/local/x/projects/other/c"])
    assert isinstance(adapter, ObjectAdmin)
    assert await adapter.count_prefix(PREFIX) == 2
    assert await adapter.delete_prefix(PREFIX) == 2
    assert await adapter.count_prefix(PREFIX) == 0
    assert [b.deleted for b in client.blobs] == [True, True, False]
    assert set(client.prefixes) == {PREFIX}


def test_build_object_admin_returns_the_provider_adapter(monkeypatch):
    class Fake:
        def __init__(self, config):
            self.config = config

    monkeypatch.setattr(factory.importlib, "import_module", lambda _n: type("M", (), {"GcsObjectStore": Fake}))
    admin = factory.build_object_admin(StorageSettings(STORAGE_BUCKET="b", STORAGE_PREFIX="p/"))
    assert isinstance(admin, Fake)


class FakeAdmin:
    def __init__(self, count=2):
        self.count, self.closed, self.calls = count, False, []

    async def count_prefix(self, prefix):
        self.calls.append(("count", prefix))
        return self.count

    async def delete_prefix(self, prefix):
        self.calls.append(("delete", prefix))
        self.count, deleted = 0, self.count
        return deleted

    async def close(self):
        self.closed = True


def env(monkeypatch, prefix="dev/local/x/"):
    for name in ("STORAGE_BACKEND", "AWS_ENDPOINT_URL_S3", "SPECFLOW_CONFIG_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STORAGE_BUCKET", "b")
    monkeypatch.setenv("STORAGE_PREFIX", prefix)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_a, **_k: False)


def test_cleanup_prints_the_same_json_line_through_the_factory(monkeypatch, capsys):
    env(monkeypatch)
    admin = FakeAdmin()
    monkeypatch.setattr(cleanup, "build_object_admin", lambda _settings: admin)
    assert cleanup.main(["--project-id", PID]) == 0
    assert json.loads(capsys.readouterr().out) == {"project_id": PID, "before": 2, "deleted": 2, "after": 0}
    assert admin.closed and ("delete", PREFIX) in admin.calls


def test_cleanup_count_only_deletes_nothing(monkeypatch, capsys):
    env(monkeypatch)
    admin = FakeAdmin()
    monkeypatch.setattr(cleanup, "build_object_admin", lambda _settings: admin)
    assert cleanup.main(["--project-id", PID, "--count-only"]) == 0
    assert json.loads(capsys.readouterr().out) == {"project_id": PID, "before": 2, "deleted": 0, "after": 2}
    assert all(kind == "count" for kind, _ in admin.calls)


@pytest.mark.parametrize("prefix", ["prod/", "dev/local/../x/", "dev/local/x"])
def test_cleanup_refuses_a_prefix_outside_dev_local(monkeypatch, capsys, prefix):
    env(monkeypatch, prefix)
    monkeypatch.setattr(cleanup, "build_object_admin", lambda _s: pytest.fail("store built before the guard"))
    assert cleanup.main(["--project-id", PID]) == 2
    assert "refused" in capsys.readouterr().err


def test_cleanup_refuses_a_non_canonical_project_id(monkeypatch):
    env(monkeypatch)
    assert cleanup.main(["--project-id", PID.upper().replace("-", "")]) == 2


def test_cleanup_without_the_bucket_exits_2(monkeypatch, capsys):
    env(monkeypatch)
    monkeypatch.delenv("STORAGE_BUCKET")
    assert cleanup.main(["--project-id", PID]) == 2
    assert "STORAGE_BUCKET" in capsys.readouterr().err
