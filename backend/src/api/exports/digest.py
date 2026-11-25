"""The canonical form and digest of an item's content: what decides that an exported item changed."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def canonical(content: Mapping[str, Any]) -> str:
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(content).encode()).hexdigest()
