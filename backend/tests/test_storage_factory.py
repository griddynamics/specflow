"""Factory routing by provider, lazy SDK import and the missing-SDK error (SPL-CFG-04, SPL-STO-01)."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from logger_harness import events
from settings import ConfigurationError, StorageSettings
from storage import factory

SRC = Path(__file__).resolve().parents[1] / "src"


def settings(backend: str = "gcs") -> StorageSettings:
    return StorageSettings(STORAGE_BACKEND=backend, STORAGE_BUCKET="b", STORAGE_PREFIX="p/")


def test_gcs_routes_to_the_gcs_adapter(monkeypatch):
    built = []

    class Fake:
        def __init__(self, config):
            built.append(config)

    monkeypatch.setattr(importlib, "import_module", lambda _name: type("M", (), {"GcsObjectStore": Fake}))
    store = factory.build_object_store(settings(), app_env="dev")
    assert isinstance(store, Fake) and built[0].bucket == "b"


def test_s3_routes_to_the_s3_adapter(monkeypatch):
    names = []

    class Fake:
        def __init__(self, config):
            self.config = config

    def fake_import(name):
        names.append(name)
        return type("M", (), {"S3ObjectStore": Fake})

    monkeypatch.setattr(importlib, "import_module", fake_import)
    store = factory.build_object_store(settings("s3"), app_env="dev")
    admin = factory.build_object_admin(settings("s3"))
    assert names == ["storage.s3", "storage.s3"]
    assert isinstance(store, Fake) and isinstance(admin, Fake)


def test_a_missing_boto3_names_the_aws_extra(monkeypatch):
    def missing(name):
        raise ModuleNotFoundError("No module named 'boto3'", name="boto3")

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(ConfigurationError, match="`aws` extra"):
        factory.build_object_store(settings("s3"), app_env="dev")
    with pytest.raises(ConfigurationError, match="`aws` extra"):
        factory.build_object_admin(settings("s3"))


def test_a_missing_sdk_names_the_extra_and_logs(monkeypatch, caplog):
    def missing(name):
        raise ModuleNotFoundError("No module named 'google'", name="google")

    monkeypatch.setattr(importlib, "import_module", missing)
    caplog.set_level("ERROR", logger="storage")
    with pytest.raises(ConfigurationError) as caught:
        factory.build_object_store(settings(), app_env="dev")
    assert caught.value.variables == ("STORAGE_BACKEND",)
    assert "`gcp` extra" in str(caught.value)
    [record] = events(caplog, "storage.provider.sdk_missing")
    assert (record.provider, record.install_extra) == ("gcs", "gcp")


def test_another_import_failure_is_not_hidden(monkeypatch):
    def broken(name):
        raise ModuleNotFoundError("No module named 'other'", name="other")

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        factory.build_object_store(settings(), app_env="dev")


def test_the_configuration_error_rebuilds_from_its_reduce_tuple():
    error = ConfigurationError(("A",), "hint")
    kind, args = error.__reduce__()
    again = kind(*args)
    assert (again.variables, str(again)) == (error.variables, str(error))


def test_importing_the_factory_and_app_loads_no_storage_sdk():
    code = (
        f"import sys; sys.path.insert(0, {str(SRC)!r})\n"
        "import storage.factory, health, api.app, json\n"
        "print(json.dumps(sorted(m for m in ('google.cloud.storage', 'boto3', 'botocore') if m in sys.modules)))"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True).stdout
    assert json.loads(out.strip().splitlines()[-1]) == []
