"""Blobbing/unblobbing: encode/decode values for the JSON ``data`` column and SQL params.

Datetimes are stored as fixed-width ISO-8601 UTC text so lexical order == chronological
order in both the columns and the blob.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any

_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _canonical_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _encode_for_storage(value: Any) -> Any:
    if isinstance(value, datetime):
        return _canonical_dt(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _encode_for_storage(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode_for_storage(v) for v in value]
    return value


def _decode_from_storage(value: Any) -> Any:
    if isinstance(value, str) and _ISO_DATETIME_RE.match(value):
        return datetime.fromisoformat(value).astimezone(UTC)
    if isinstance(value, dict):
        return {k: _decode_from_storage(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_from_storage(v) for v in value]
    return value


def _to_sql_param(value: Any) -> Any:
    if isinstance(value, datetime):
        return _canonical_dt(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bool):
        return int(value)
    return value


def _json_path(field: str) -> str:
    return "$." + field
