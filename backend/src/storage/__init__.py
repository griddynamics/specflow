"""Object storage: Protocol, GCS and S3 adapters, test-only memory adapter, key rules."""

from __future__ import annotations

from storage.base import (
    InvalidObjectKeyError,
    ManagedObjectAdmin,
    ManagedObjectStore,
    ObjectAdmin,
    ObjectAlreadyExistsError,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectWriter,
    StorageUnavailableError,
    StoredObject,
)

__all__ = [
    "InvalidObjectKeyError",
    "ManagedObjectAdmin",
    "ManagedObjectStore",
    "ObjectAdmin",
    "ObjectAlreadyExistsError",
    "ObjectNotFoundError",
    "ObjectStore",
    "ObjectStoreError",
    "ObjectWriter",
    "StorageUnavailableError",
    "StoredObject",
]
