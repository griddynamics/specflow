"""Pick the Drive gateway from ``APP_ENV`` (NFR-PORT-0002).

``local`` -> fake. Anything else -> real, which reports ``DRIVE_IDENTITY_EMAIL`` as
its identity (unset or blank: ``identity()`` raises ``setup_error``; no fallback).
The fake inside Kubernetes (``KUBERNETES_SERVICE_HOST`` set) is refused: production
can only reach it by running ``APP_ENV=local`` in the cluster.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from drive.fake import FakeDriveGateway
from drive.gateway import DriveGateway
from drive.real import GoogleDriveGateway
from logger.logger import ERROR, get_logger, log_event
from settings import ConfigurationError

logger = get_logger("drive.select")


def build_drive_gateway(app_env: str, env: Mapping[str, str] = os.environ, *, role: str = "api") -> DriveGateway:
    """``role`` (``api`` or ``worker``) only matters to the fake (``fake-import-fails``)."""

    if app_env == "local":
        if env.get("KUBERNETES_SERVICE_HOST", "").strip():
            log_event(logger, ERROR, "drive.gateway.refused", reason="fake_in_kubernetes")
            raise ConfigurationError(("APP_ENV",))
        return FakeDriveGateway(app_env=app_env, role=role)
    return GoogleDriveGateway(identity_email=env.get("DRIVE_IDENTITY_EMAIL", ""))
