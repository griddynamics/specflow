"""A2 selection and NFR-PORT-0002.AC4 refusal."""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest

from drive import real as real_module
from drive.fake import FakeDriveGateway
from drive.gateway import FAKE_DRIVE_EMAIL, DriveError
from drive.reasons import Reason
from drive.select import build_drive_gateway
from settings import ConfigurationError


def test_local_gives_the_fake():
    gateway = build_drive_gateway("local", {})
    assert isinstance(gateway, FakeDriveGateway)
    assert gateway.mode == "fake"


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_other_envs_give_the_real_one(env):
    gateway = build_drive_gateway(env, {"KUBERNETES_SERVICE_HOST": "10.0.0.1"})
    assert gateway.mode == "real"


def test_real_gateway_takes_the_identity_from_drive_identity_email(monkeypatch):
    monkeypatch.setattr(real_module.google.auth, "default", lambda *_args, **_kwargs: (object(), "project"))
    env = {"DRIVE_IDENTITY_EMAIL": " drive-reader@example.com "}
    assert build_drive_gateway("prod", env).identity() == "drive-reader@example.com"


@pytest.mark.parametrize("env", [{}, {"DRIVE_IDENTITY_EMAIL": ""}, {"DRIVE_IDENTITY_EMAIL": "  "}])
def test_real_gateway_without_identity_email_is_a_setup_error_and_logged(env, caplog):
    caplog.set_level(logging.INFO)
    gateway = build_drive_gateway("prod", env)  # no start-up failure
    with pytest.raises(DriveError) as raised:
        gateway.identity()
    assert raised.value.reason is Reason.SETUP_ERROR
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "drive.identity.missing"]
    assert record.levelno == logging.ERROR


def test_fake_gateway_ignores_the_identity_email():
    assert build_drive_gateway("local", {"DRIVE_IDENTITY_EMAIL": "x@example.com"}).identity() == FAKE_DRIVE_EMAIL


def test_fake_in_kubernetes_is_refused_and_logged(caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(ConfigurationError) as raised:
        build_drive_gateway("local", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"})
    assert raised.value.variables == ("APP_ENV",)
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "drive.gateway.refused"]
    assert record.levelno == logging.ERROR
    assert record.reason == "fake_in_kubernetes"


def test_the_role_reaches_the_fake():
    api = build_drive_gateway("local", {})
    worker = build_drive_gateway("local", {}, role="worker")
    api.list_all("fake-import-fails")  # api role: listing works (Test and Save pass)
    with pytest.raises(DriveError) as raised:
        worker.list_all("fake-import-fails")
    assert raised.value.reason is Reason.NO_ACCESS


def test_blank_kubernetes_host_does_not_refuse():
    assert build_drive_gateway("local", {"KUBERNETES_SERVICE_HOST": "  "}).mode == "fake"


def test_import_light_package():
    code = (
        "import sys, drive, drive.links, drive.categories;"
        "bad=[m for m in sys.modules if m.startswith(('googleapiclient','google.auth'))];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
