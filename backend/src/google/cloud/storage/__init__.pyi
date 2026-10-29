"""Type stub for the part of ``google-cloud-storage`` (3.x) this project calls.

project: the library ships no ``py.typed`` marker and no stub package exists on PyPI, so strict
mypy would skip it as ``import-untyped``. Only the API that ``storage.gcs`` and the GCS examples
use is declared here; extend it when new calls are added. Python ignores ``.pyi`` files at runtime.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

__version__: str

class Blob:
    name: str | None
    size: int | None
    storage_class: str | None
    def upload_from_string(
        self,
        data: bytes | str,
        content_type: str = ...,
        *,
        if_generation_match: int | None = ...,
        timeout: float = ...,
    ) -> None: ...
    def download_as_bytes(self, *, timeout: float = ...) -> bytes: ...
    def download_to_filename(self, filename: str, *, timeout: float = ...) -> None: ...
    def exists(self, *, timeout: float = ...) -> bool: ...
    def delete(self, *, timeout: float = ...) -> None: ...
    def reload(self, *, timeout: float = ...) -> None: ...
    def open(self, mode: str = ..., **kwargs: Any) -> Any: ...
    def generate_signed_url(
        self,
        expiration: datetime | timedelta | int | None = ...,
        *,
        version: str | None = ...,
        method: str = ...,
        service_account_email: str | None = ...,
        access_token: str | None = ...,
    ) -> str: ...

class Bucket:
    name: str
    def blob(self, blob_name: str) -> Blob: ...
    def test_iam_permissions(self, permissions: list[str], *, timeout: float = ...) -> list[str]: ...

class Batch: ...

class Client:
    def __init__(self, project: str | None = ..., credentials: Any = ...) -> None: ...
    def bucket(self, bucket_name: str) -> Bucket: ...
    def list_blobs(
        self,
        bucket_or_name: Bucket | str,
        max_results: int | None = ...,
        *,
        prefix: str | None = ...,
        timeout: float = ...,
    ) -> Iterator[Blob]: ...
    def close(self) -> None: ...

__all__ = ["Batch", "Blob", "Bucket", "Client", "__version__"]
