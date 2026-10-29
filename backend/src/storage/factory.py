"""Object store factory: the one place that builds a concrete store from settings.

The provider adapter and its SDK are imported inside ``build_object_store`` only, so a
process loads the SDK of the chosen provider and no other. Callers use
``factory.build_object_store`` (a module attribute), so a test replaces it with one patch.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from logger.logger import ERROR, get_logger, log_event
from settings import ConfigurationError, StorageSettings
from storage.base import ManagedObjectAdmin, ManagedObjectStore

logger = get_logger("storage.factory")


@dataclass(frozen=True)
class _Adapter:
    module: str  # the adapter module, imported lazily
    cls: str  # the store class in it
    extra: str  # the pyproject extra that installs the SDK
    sdk_roots: tuple[str, ...]  # top-level modules of the SDK


_ADAPTERS = {
    "gcs": _Adapter("storage.gcs", "GcsObjectStore", "gcp", ("google",)),
    "s3": _Adapter("storage.s3", "S3ObjectStore", "aws", ("boto3", "botocore")),
}


def _load(provider: str) -> Callable[[StorageSettings], Any]:
    adapter = _ADAPTERS[provider]
    try:
        module = importlib.import_module(adapter.module)
    except ImportError as error:
        # Only a missing SDK is a configuration problem; any other import failure is a bug.
        if (error.name or "").split(".")[0] not in adapter.sdk_roots:
            raise
        log_event(logger, ERROR, "storage.provider.sdk_missing", provider=provider, install_extra=adapter.extra)
        raise ConfigurationError(
            ("STORAGE_BACKEND",),
            f"The {provider} provider needs its SDK: install the `{adapter.extra}` extra "
            f"(`specflow[{adapter.extra}]`).",
        ) from error
    return cast(Callable[[StorageSettings], Any], getattr(module, adapter.cls))


def build_object_store(settings: StorageSettings, *, app_env: str) -> ManagedObjectStore:
    """Build the store for ``settings.backend``. ``app_env`` is kept for later providers."""

    del app_env  # no provider reads it yet
    return cast(ManagedObjectStore, _load(settings.backend)(settings))


def build_object_admin(settings: StorageSettings) -> ManagedObjectAdmin:
    """Build the bulk-cleanup view of the store (``ObjectAdmin``). Only cleanup tooling calls this."""

    return cast(ManagedObjectAdmin, _load(settings.backend)(settings))
